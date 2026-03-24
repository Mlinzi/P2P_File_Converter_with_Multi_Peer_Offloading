#!/usr/bin/env python3
"""
peer.py — entry point for the P2P File Conversion Network
Usage:  python peer.py [--port PORT] [--name NAME] [--ui-port PORT]
"""

import argparse
import collections
import logging
import socket
import ssl
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file as flask_send_file

import concurrent.futures

from core.client import submit_job, probe_peer
from core.converter import combine_pdfs, convert, detect_format, get_available_outputs, GPU_ENCODERS, set_gpu_accel, get_gpu_accel
from core.discovery import Discovery
from core.metrics import Metrics
from core.server import PeerServer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s  %(name)-10s  %(levelname)s  %(message)s')
log = logging.getLogger('peer')

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument('--port',    type=int, default=0,                  help='TCP server port (0=auto)')
parser.add_argument('--name',    type=str, default=socket.gethostname(), help='Peer display name')
parser.add_argument('--ui-port', type=int, default=8080,               help='Web UI port')
parser.add_argument('--tls',     action='store_true',                  help='Enable TLS on startup (requires certs/)')
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

PEER_ID   = str(uuid.uuid4())[:8]
PEER_NAME = args.name
TEMP_DIR  = Path('temp')
RESULTS   = TEMP_DIR / 'results'
METRICS   = Metrics()
JOBS: dict = {}
JOBS_LOCK  = threading.Lock()

DISCOVERY: Discovery = None
SERVER: PeerServer   = None
PREFER_PEERS = False   # when True, always offload to peers if available (good for demos)
USE_TLS      = True    # on by default; toggled at runtime via /api/toggle-tls
TCP_PORT     = None    # set in main(); needed by toggle to restart server on same port

# TLS cert paths  (run  python ../certs/gen_cert.py  once to create these)
CERT_FILE = Path(__file__).parent.parent / 'certs' / 'peer.crt'
KEY_FILE  = Path(__file__).parent.parent / 'certs' / 'peer.key'

# Activity log — last 50 human-readable events shown in the UI
ACTIVITY_LOG: collections.deque = collections.deque(maxlen=50)
_log_lock = threading.Lock()

def _log(msg: str):
    """Append a timestamped entry to the activity log and print to console."""
    ts = time.strftime('%H:%M:%S')
    entry = {'time': ts, 'msg': msg}
    with _log_lock:
        ACTIVITY_LOG.appendleft(entry)
    log.info(msg)


def _make_server_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    return ctx


def _make_client_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE   # self-signed; we only need encryption
    return ctx


def _ensure_certs() -> bool:
    """Auto-generate a self-signed cert/key pair if not present. Returns True on success."""
    CERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return True
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"p2pconvert")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        KEY_FILE.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
        CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        log.info("TLS certs auto-generated")
        return True
    except Exception as e:
        log.warning(f"Could not generate TLS certs ({e}) — running without TLS")
        return False

# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def _new_job(filename, input_fmt, output_format):
    job_id = str(uuid.uuid4())[:8]
    with JOBS_LOCK:
        JOBS[job_id] = {
            'job_id'         : job_id,
            'filename'       : filename,
            'input_fmt'      : input_fmt,
            'output_format'  : output_format,
            'status'         : 'queued',
            'peer'           : 'local',
            'output_filename': None,
            'result_path'    : None,
            'latency_ms'     : None,
            'error'          : None,
            'started_at'     : time.time(),
        }
    return job_id


def _run_job(job_id: str, input_path: Path):
    """Convert a file — locally or offload to a peer. Runs in a daemon thread."""
    with JOBS_LOCK:
        JOBS[job_id]['status'] = 'converting'
        output_format = JOBS[job_id]['output_format']
        started_at    = JOBS[job_id]['started_at']
        filename      = JOBS[job_id]['filename']

    try:
        result = None

        # Offload if busy OR if "prefer peers" mode is on
        if SERVER and (SERVER.active_jobs > 0 or PREFER_PEERS):
            peers = DISCOVERY.get_peers() if DISCOVERY else []
            if not peers:
                _log("No peers available — converting locally")
            else:
                # Probe all peers concurrently to get current CPU load
                def _probe_one(p):
                    return p, probe_peer(
                        p.host, p.port, PEER_ID, PEER_NAME,
                        ssl_context=_make_client_ssl_ctx() if p.tls else None,
                    )

                with concurrent.futures.ThreadPoolExecutor(max_workers=len(peers)) as ex:
                    futs = {ex.submit(_probe_one, p): p for p in peers}
                    reachable = []
                    for fut in concurrent.futures.as_completed(futs, timeout=7):
                        try:
                            peer, info = fut.result()
                            if info.get('reachable'):
                                reachable.append((peer, info))
                        except Exception:
                            pass

                # Sort: fewest active jobs first, then lowest CPU load
                reachable.sort(key=lambda x: (x[1].get('active_jobs', 0), x[1].get('cpu_load', 1.0)))
                if reachable:
                    _log(f"Peer ranking: " + ", ".join(
                        f"{p.peer_name} (cpu={i.get('cpu_load', 0):.0%} jobs={i.get('active_jobs', 0)})"
                        for p, i in reachable
                    ))

                for peer, _ in reachable:
                    tls_tag = " [TLS]" if peer.tls else ""
                    _log(f"Attempting {peer.peer_name} ({peer.host}:{peer.port}){tls_tag}...")
                    with JOBS_LOCK:
                        JOBS[job_id]['peer'] = peer.peer_name
                    try:
                        result = submit_job(
                            peer_host=peer.host, peer_port=peer.port,
                            input_path=str(input_path), output_format=output_format,
                            output_dir=str(RESULTS),
                            this_peer_id=PEER_ID, this_peer_name=PEER_NAME,
                            ssl_context=_make_client_ssl_ctx() if peer.tls else None,
                            use_gpu=get_gpu_accel(),
                        )
                        size_mb = round(result.stat().st_size / 1_048_576, 2)
                        _log(f"Received result from {peer.peer_name} — {result.name} ({size_mb} MB)")
                        break
                    except Exception as e:
                        _log(f"{peer.peer_name} failed ({type(e).__name__}: {e}), trying next...")
                        result = None

        if result is None:
            with JOBS_LOCK:
                JOBS[job_id]['peer'] = 'local'
            _log(f"Converting {filename} → {output_format.upper()} locally")
            result = convert(str(input_path), output_format, str(RESULTS))

        latency_ms = (time.time() - started_at) * 1000
        METRICS.record_job_done(latency_ms, result.stat().st_size)
        _log(f"Done: {filename} → {output_format.upper()} in {latency_ms/1000:.2f}s")

        with JOBS_LOCK:
            JOBS[job_id].update({
                'status'         : 'done',
                'result_path'    : str(result),
                'output_filename': result.name,
                'latency_ms'     : round(latency_ms, 1),
            })

    except Exception as e:
        _log(f"Failed: {filename} — {e}")
        METRICS.record_job_failed()
        with JOBS_LOCK:
            JOBS[job_id].update({'status': 'error', 'error': str(e)})
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except Exception:
            pass


def _run_combine(job_id: str, paths: list):
    """Combine PDFs. Runs in a daemon thread."""
    _log(f"Combining {len(paths)} PDFs locally")
    try:
        RESULTS.mkdir(parents=True, exist_ok=True)
        out = combine_pdfs(paths, str(RESULTS / f"{job_id}_combined.pdf"))
        latency_ms = (time.time() - JOBS[job_id]['started_at']) * 1000
        METRICS.record_job_done(latency_ms, out.stat().st_size)
        _log(f"Done: combined {len(paths)} PDFs → {out.name} in {latency_ms/1000:.2f}s")
        with JOBS_LOCK:
            JOBS[job_id].update({
                'status'         : 'done',
                'result_path'    : str(out),
                'output_filename': out.name,
                'latency_ms'     : round(latency_ms, 1),
            })
    except Exception as e:
        _log(f"Failed: PDF combine — {e}")
        with JOBS_LOCK:
            JOBS[job_id].update({'status': 'error', 'error': str(e)})
    finally:
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder='ui/templates', static_folder='ui/static')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024   # 500 MB


@app.route('/')
def index():
    return render_template('index.html', peer_name=PEER_NAME)


@app.route('/api/prefer-peers', methods=['POST'])
def api_prefer_peers():
    global PREFER_PEERS
    PREFER_PEERS = request.json.get('enabled', False)
    log.info(f"Prefer-peers mode: {'ON' if PREFER_PEERS else 'OFF'}")
    return jsonify({'prefer_peers': PREFER_PEERS})


@app.route('/api/toggle-tls', methods=['POST'])
def api_toggle_tls():
    global USE_TLS, SERVER
    enabled = request.json.get('enabled', not USE_TLS)

    if enabled and not _ensure_certs():
        return jsonify({'error': 'Could not generate TLS certs (is cryptography installed?)'}), 400

    USE_TLS = enabled

    # Restart TCP server with updated TLS setting
    if SERVER and TCP_PORT:
        SERVER.stop()
        time.sleep(0.3)
        ssl_ctx = _make_server_ssl_ctx() if USE_TLS else None
        SERVER = PeerServer(
            host='', port=TCP_PORT,
            peer_id=PEER_ID, peer_name=PEER_NAME,
            temp_dir=str(TEMP_DIR), metrics=METRICS,
            ssl_context=ssl_ctx,
        )
        SERVER.start()

    # Update mDNS advertisement so peers know our TLS state
    if DISCOVERY:
        DISCOVERY.set_tls(USE_TLS)

    _log(f"TLS {'enabled' if USE_TLS else 'disabled'}")
    return jsonify({'tls': USE_TLS})


@app.route('/api/toggle-gpu', methods=['POST'])
def api_toggle_gpu():
    if not GPU_ENCODERS:
        return jsonify({'error': 'No GPU encoders detected (NVENC/AMF/QSV not available)'}), 400
    enabled = request.json.get('enabled', False)
    set_gpu_accel(enabled)
    enc = GPU_ENCODERS[0] if enabled else 'cpu'
    _log(f"GPU accel {'enabled' if enabled else 'disabled'} ({enc})")
    return jsonify({'gpu': enabled, 'encoder': GPU_ENCODERS[0] if GPU_ENCODERS else None})


@app.route('/api/status')
def api_status():
    from core.converter import USE_GPU
    peers = [{'peer_name': p.peer_name, 'host': p.host, 'port': p.port}
             for p in (DISCOVERY.get_peers() if DISCOVERY else [])]
    return jsonify({
        'peer_id'     : PEER_ID,
        'peer_name'   : PEER_NAME,
        'peers'       : peers,
        'metrics'     : METRICS.snapshot(),
        'prefer_peers': PREFER_PEERS,
        'tls'         : USE_TLS,
        'gpu'         : USE_GPU,
        'gpu_encoder' : GPU_ENCODERS[0] if GPU_ENCODERS else None,
    })


@app.route('/api/formats')
def api_formats():
    ext = request.args.get('ext', '').lstrip('.').lower()
    if ext == 'jpg':
        ext = 'jpeg'
    return jsonify({'ext': ext, 'outputs': get_available_outputs(ext)})


@app.route('/api/convert', methods=['POST'])
def api_convert():
    files         = request.files.getlist('file')
    output_format = request.form.get('output_format', '').lower().strip()

    if not files or not files[0].filename:
        return jsonify({'error': 'No file uploaded'}), 400
    if not output_format:
        return jsonify({'error': 'No output format specified'}), 400

    TEMP_DIR.mkdir(exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    # --- Combine PDFs ---
    if output_format == 'combine_pdf':
        if len(files) < 2:
            return jsonify({'error': 'Select at least 2 PDFs to combine'}), 400
        job_id = str(uuid.uuid4())[:8]
        paths  = []
        for f in files:
            p = TEMP_DIR / f"{job_id}_{f.filename}"
            f.save(str(p))
            paths.append(p)
        with JOBS_LOCK:
            JOBS[job_id] = {
                'job_id': job_id, 'filename': f"{len(files)} PDFs",
                'input_fmt': 'pdf', 'output_format': 'pdf (combined)',
                'status': 'converting', 'peer': 'local',
                'output_filename': None, 'result_path': None,
                'latency_ms': None, 'error': None, 'started_at': time.time(),
            }
        threading.Thread(target=_run_combine, args=(job_id, paths), daemon=True).start()
        return jsonify({'job_id': job_id})

    # --- Single file conversion ---
    f         = files[0]
    input_fmt = detect_format(f.filename)
    job_id    = _new_job(f.filename, input_fmt, output_format)
    save_path = TEMP_DIR / f"{job_id}_{f.filename}"
    f.save(str(save_path))
    threading.Thread(target=_run_job, args=(job_id, save_path), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/logs')
def api_logs():
    with _log_lock:
        entries = list(ACTIVITY_LOG)
    return jsonify({'logs': entries})


@app.route('/api/jobs')
def api_jobs():
    with JOBS_LOCK:
        jobs = [
            {k: v for k, v in j.items() if k != 'result_path'}
            for j in reversed(list(JOBS.values()))
        ]
    return jsonify({'jobs': jobs})


@app.route('/api/download/<job_id>')
def api_download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'Not ready'}), 404
    path = job.get('result_path')
    if not path or not Path(path).exists():
        return jsonify({'error': 'File missing'}), 404
    return flask_send_file(path, as_attachment=True, download_name=job['output_filename'])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global DISCOVERY, SERVER, TCP_PORT, USE_TLS

    TEMP_DIR.mkdir(exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    # Auto-assign TCP port if needed
    tcp_port = args.port
    if tcp_port == 0:
        with socket.socket() as s:
            s.bind(('', 0))
            tcp_port = s.getsockname()[1]
    TCP_PORT = tcp_port

    # TLS setup — on by default; auto-generate certs if missing
    ssl_ctx = None
    if USE_TLS:
        USE_TLS = _ensure_certs()
    if USE_TLS:
        ssl_ctx = _make_server_ssl_ctx()
        log.info(f"TLS enabled — cert: {CERT_FILE}")
    else:
        log.warning("TLS disabled (cert generation failed)")

    SERVER = PeerServer(
        host='', port=TCP_PORT,
        peer_id=PEER_ID, peer_name=PEER_NAME,
        temp_dir=str(TEMP_DIR), metrics=METRICS,
        ssl_context=ssl_ctx,
    )
    SERVER.start()

    DISCOVERY = Discovery(
        peer_id=PEER_ID, peer_name=PEER_NAME, port=TCP_PORT,
        tls=USE_TLS,
        on_peer_added   =lambda p:   _log(f"Peer joined: {p.peer_name} ({p.host}:{p.port})"),
        on_peer_removed =lambda pid: _log(f"Peer left: {pid}"),
    )
    DISCOVERY.start()

    ui_port = args.ui_port
    log.info(f"'{PEER_NAME}'  id={PEER_ID}  tcp={tcp_port}  ui=http://localhost:{ui_port}")

    threading.Timer(1.5, lambda: webbrowser.open(f'http://localhost:{ui_port}')).start()
    app.run(host='0.0.0.0', port=ui_port, debug=False, use_reloader=False, threaded=True)


if __name__ == '__main__':
    main()
