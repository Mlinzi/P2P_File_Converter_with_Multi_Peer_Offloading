"""
client.py — connects to a remote peer and submits a conversion job

Flow:
  connect  →  send HELLO  →  recv HELLO
  send JOB_REQUEST  →  recv JOB_OFFER or JOB_REJECT
  if offered:
      send file  →  recv JOB_DONE or JOB_ERROR  →  recv file
  send BYE  →  close

Usage:
    result = submit_job(
        peer_host='192.168.1.5',
        peer_port=9001,
        input_path='report.docx',
        output_format='pdf',
        output_dir='temp/',
        this_peer_id='abc',
        this_peer_name='MyLaptop',
    )
    # result is a Path to the downloaded converted file
"""

import ssl
import socket
import uuid
import logging
import time
import os
from pathlib import Path

from core.protocol import (
    send_msg, recv_msg, send_file, recv_file,
    Msg, PROTOCOL_VERSION
)
from core.converter import detect_format

log = logging.getLogger('client')

CONNECT_TIMEOUT = 10   # seconds to wait for TCP connection
RECV_TIMEOUT    = 120  # seconds to wait for conversion result (large files need time)


class JobRejected(Exception):
    """Raised when remote peer refuses the job (too busy)."""


class JobFailed(Exception):
    """Raised when remote peer returns JOB_ERROR."""


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------

def submit_job(peer_host: str, peer_port: int,
               input_path: str, output_format: str, output_dir: str,
               this_peer_id: str, this_peer_name: str,
               ssl_context=None,        # ssl.SSLContext (client-side). None = plain TCP
               progress_cb=None,        # progress_cb(bytes_sent, total) during upload
               use_gpu: bool = False    # request GPU acceleration on the remote peer
               ) -> Path:
    """
    Send a conversion job to a remote peer.
    Returns Path to the converted output file saved in output_dir.

    Raises:
        JobRejected  — peer too busy
        JobFailed    — peer conversion error
        ConnectionError — network failure
    """
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    job_id     = str(uuid.uuid4())[:8]
    input_fmt  = detect_format(str(input_path))
    file_size  = os.path.getsize(input_path)

    log.info(f"[client] job {job_id}: {input_path.name} → {output_format}  "
             f"peer={peer_host}:{peer_port}")

    t_start = time.perf_counter()

    sock = _connect(peer_host, peer_port, ssl_context)
    try:
        sock.settimeout(RECV_TIMEOUT)

        # --- Handshake ---
        send_msg(sock, Msg.HELLO,
                 peer_id=this_peer_id,
                 peer_name=this_peer_name,
                 port=0,
                 version=PROTOCOL_VERSION)

        hello = recv_msg(sock)
        if hello.get('type') != Msg.HELLO:
            raise ConnectionError(f"Expected HELLO, got {hello.get('type')}")

        remote_name = hello.get('peer_name', f'{peer_host}:{peer_port}')
        log.info(f"[client] job {job_id}: connected to '{remote_name}'")

        # --- Job request ---
        send_msg(sock, Msg.JOB_REQUEST,
                 job_id=job_id,
                 input_format=input_fmt,
                 output_format=output_format,
                 filename=input_path.name,
                 file_size=file_size,
                 use_gpu=use_gpu)

        reply = recv_msg(sock)

        if reply.get('type') == Msg.JOB_REJECT:
            raise JobRejected(reply.get('reason', 'Peer busy'))

        if reply.get('type') != Msg.JOB_OFFER:
            raise ConnectionError(f"Expected JOB_OFFER, got {reply.get('type')}")

        cpu_load = reply.get('cpu_load', 0)
        log.info(f"[client] job {job_id}: accepted by '{remote_name}' (cpu={cpu_load:.0%}), uploading...")

        # --- Send file ---
        send_file(sock, str(input_path), progress_cb=progress_cb)

        log.info(f"[client] job {job_id}: upload done ({file_size:,} B), waiting for conversion...")

        # --- Wait for result ---
        result_msg = recv_msg(sock)

        if result_msg.get('type') == Msg.JOB_ERROR:
            raise JobFailed(result_msg.get('reason', 'Unknown error on remote peer'))

        if result_msg.get('type') != Msg.JOB_DONE:
            raise ConnectionError(f"Expected JOB_DONE, got {result_msg.get('type')}")

        # --- Receive converted file ---
        output_filename = result_msg.get('output_filename', f'{job_id}_result')
        output_size     = result_msg.get('file_size', 0)
        output_path     = output_dir / output_filename

        log.info(f"[client] job {job_id}: receiving result '{output_filename}' ({output_size:,} B)...")
        recv_file(sock, str(output_path))

        elapsed_ms = (time.perf_counter() - t_start) * 1000
        log.info(f"[client] job {job_id}: done in {elapsed_ms:.0f} ms — saved to {output_path.name}")

        # --- Bye ---
        try:
            send_msg(sock, Msg.BYE, peer_id=this_peer_id)
        except Exception:
            pass

        return output_path

    finally:
        try:
            sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Probe a peer — get its load without submitting a job
# ---------------------------------------------------------------------------

def probe_peer(peer_host: str, peer_port: int,
               this_peer_id: str, this_peer_name: str,
               ssl_context=None) -> dict:
    """
    Connect, exchange HELLOs, send BYE.
    Returns dict with peer_id, peer_name, and reachable=True.
    Returns {'reachable': False} on any failure.
    """
    try:
        sock = _connect(peer_host, peer_port, ssl_context, timeout=5)
        sock.settimeout(5)
        try:
            send_msg(sock, Msg.HELLO,
                     peer_id=this_peer_id,
                     peer_name=this_peer_name,
                     port=0,
                     version=PROTOCOL_VERSION)
            hello = recv_msg(sock)
            send_msg(sock, Msg.BYE, peer_id=this_peer_id)
            return {
                'reachable'   : True,
                'peer_id'     : hello.get('peer_id'),
                'peer_name'   : hello.get('peer_name'),
                'cpu_load'    : hello.get('cpu_load', 1.0),    # 0.0–1.0; default 1.0 = deprioritise if unknown
                'active_jobs' : hello.get('active_jobs', 0),
            }
        finally:
            sock.close()
    except Exception as e:
        log.debug(f"[client] probe {peer_host}:{peer_port} failed: {e}")
        return {'reachable': False}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(host: str, port: int, ssl_context=None, timeout=CONNECT_TIMEOUT) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((host, port))
    _enable_keepalive(sock)

    if ssl_context:
        sock = ssl_context.wrap_socket(sock, server_hostname=host)

    return sock


def _enable_keepalive(sock: socket.socket):
    """
    Enable TCP keepalive: probe after 30s idle, every 10s, give up after
    3 missed probes → total ~60s to declare a truly dead peer.

    30s idle before probing tolerates brief WiFi blips without killing a
    live job. Large jobs are never false-killed because the peer's OS ACKs
    keepalive probes at the kernel level regardless of app load.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    try:
        if hasattr(socket, 'TCP_KEEPIDLE'):      # Linux + Python 3.10+ Windows
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,   3)
        elif hasattr(socket, 'SIO_KEEPALIVE_VALS'):  # older Windows
            import struct
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, struct.pack('LLL', 1, 30_000, 10_000))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Quick standalone test (requires server.py running)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import threading
    import tempfile
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))

    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s  %(name)-8s  %(levelname)s  %(message)s')

    from core.server import PeerServer

    with tempfile.TemporaryDirectory() as tmp:
        srv = PeerServer('127.0.0.1', 19997, 'srv', 'ServerPeer', tmp)
        srv.start()
        time.sleep(0.1)

        info = probe_peer('127.0.0.1', 19997, 'cli', 'ClientPeer')
        print(f"[test] probe: {info}")
        assert info['reachable']
        assert info['peer_name'] == 'ServerPeer'

        srv.stop()
        print("[test] client.py self-test passed.")
