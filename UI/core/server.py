"""
server.py — TCP server that accepts incoming conversion jobs from other peers

Flow per connection:
  recv HELLO  →  send HELLO
  recv JOB_REQUEST  →  send JOB_OFFER or JOB_REJECT
  if accepted:
      recv file  →  convert  →  send JOB_DONE  →  send file
      (on failure: send JOB_ERROR)
  recv BYE  →  close
"""

import ssl
import socket
import threading
import logging
import time
from pathlib import Path

from core.protocol import send_msg, recv_msg, send_file, recv_file, Msg, PROTOCOL_VERSION
from core.converter import convert, get_available_outputs

log = logging.getLogger('server')

MAX_CONCURRENT_JOBS  = 3   # refuse new jobs beyond this
CPU_REJECT_THRESHOLD = 90  # refuse if CPU% above this (80% is normal multitasking)
RAM_REJECT_THRESHOLD = 90  # refuse if RAM% above this


class PeerServer:
    def __init__(self, host, port, peer_id, peer_name, temp_dir,
                 metrics=None, ssl_context=None):
        """
        host        : bind address ('' or '0.0.0.0' for all interfaces)
        port        : TCP port to listen on
        peer_id     : unique string ID for this peer
        peer_name   : human-readable name shown in UI
        temp_dir    : directory for in-flight files (auto-cleaned after each job)
        metrics     : core.metrics.Metrics instance (optional)
        ssl_context : ssl.SSLContext (server-side). None = plain TCP.
                      # USE_TLS: set ssl_context=None to disable TLS for benchmarking
        """
        self.host        = host
        self.port        = port
        self.peer_id     = peer_id
        self.peer_name   = peer_name
        self.temp_dir    = Path(temp_dir)
        self.metrics     = metrics
        self.ssl_context = ssl_context

        self._sock       = None
        self._thread     = None
        self._running    = False
        self._job_count  = 0
        self._lock       = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(20)
        self._running = True

        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name='server-accept')
        self._thread.start()
        log.info(f"[server] listening on {self.host or '0.0.0.0'}:{self.port}  "
                 f"TLS={'on' if self.ssl_context else 'off'}")

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass
        log.info("[server] stopped")

    @property
    def active_jobs(self):
        return self._job_count

    # ------------------------------------------------------------------
    # Accept loop
    # ------------------------------------------------------------------

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break   # socket was closed via stop()

            # Keepalive: probe after 30s idle, every 10s, give up after 3 misses (~60s total)
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, 'TCP_KEEPIDLE'):
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  30)
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,   3)
                elif hasattr(socket, 'SIO_KEEPALIVE_VALS'):
                    import struct
                    conn.ioctl(socket.SIO_KEEPALIVE_VALS, struct.pack('LLL', 1, 30_000, 10_000))
            except OSError:
                pass

            # Wrap with TLS if enabled
            if self.ssl_context:
                try:
                    conn = self.ssl_context.wrap_socket(conn, server_side=True)
                except ssl.SSLError as e:
                    log.warning(f"[server] TLS handshake failed from {addr}: {e}")
                    conn.close()
                    continue

            t = threading.Thread(
                target=self._handle_peer,
                args=(conn, addr),
                daemon=True,
                name=f'peer-{addr[0]}:{addr[1]}'
            )
            t.start()

    # ------------------------------------------------------------------
    # Per-connection handler
    # ------------------------------------------------------------------

    def _handle_peer(self, conn, addr):
        log.debug(f"[server] connection from {addr}")
        try:
            # --- Handshake ---
            msg = recv_msg(conn)
            if msg.get('type') != Msg.HELLO:
                log.warning(f"[server] expected HELLO from {addr}, got {msg.get('type')}")
                conn.close()
                return

            remote_name = msg.get('peer_name', str(addr))
            log.info(f"[server] HELLO from '{remote_name}' at {addr}")

            try:
                import psutil
                _cpu = psutil.cpu_percent(interval=0.1)
            except Exception:
                _cpu = 0.0

            send_msg(conn, Msg.HELLO,
                     peer_id=self.peer_id,
                     peer_name=self.peer_name,
                     port=self.port,
                     version=PROTOCOL_VERSION,
                     cpu_load=round(_cpu / 100.0, 2),
                     active_jobs=self._job_count)

            # --- Message loop ---
            while True:
                msg = recv_msg(conn)
                mtype = msg.get('type')

                if mtype == Msg.JOB_REQUEST:
                    self._handle_job(conn, msg)

                elif mtype == Msg.BYE:
                    log.debug(f"[server] BYE from '{remote_name}'")
                    break

                else:
                    log.warning(f"[server] unexpected msg type '{mtype}' from {addr}")

        except ConnectionError:
            log.debug(f"[server] {addr} disconnected")
        except Exception as e:
            log.error(f"[server] error handling {addr}: {e}", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Job handler
    # ------------------------------------------------------------------

    def _handle_job(self, conn, msg):
        job_id = msg.get('job_id', 'unknown')

        # --- Reject if overloaded (job count OR cpu) ---
        try:
            import psutil
            cpu_load = psutil.cpu_percent(interval=0.1)
            ram_load = psutil.virtual_memory().percent
        except Exception:
            cpu_load = 0.0
            ram_load = 0.0

        if self._job_count >= MAX_CONCURRENT_JOBS:
            send_msg(conn, Msg.JOB_REJECT, job_id=job_id, reason='Too busy')
            log.info(f"[server] rejected job {job_id} (at job capacity)")
            return

        if cpu_load >= CPU_REJECT_THRESHOLD:
            send_msg(conn, Msg.JOB_REJECT, job_id=job_id,
                     reason=f'CPU too high ({cpu_load:.0f}%)')
            log.info(f"[server] rejected job {job_id} (CPU {cpu_load:.0f}%)")
            return

        if ram_load >= RAM_REJECT_THRESHOLD:
            send_msg(conn, Msg.JOB_REJECT, job_id=job_id,
                     reason=f'RAM too high ({ram_load:.0f}%)')
            log.info(f"[server] rejected job {job_id} (RAM {ram_load:.0f}%)")
            return

        # Check we actually have the tools for this conversion
        input_fmt  = msg.get('input_format', '')
        out_fmt    = msg.get('output_format', '')
        if out_fmt not in get_available_outputs(input_fmt):
            send_msg(conn, Msg.JOB_REJECT, job_id=job_id,
                     reason=f'Missing tool for {input_fmt.upper()} → {out_fmt.upper()}')
            log.info(f"[server] rejected job {job_id} (no tool for {input_fmt}→{out_fmt})")
            return

        send_msg(conn, Msg.JOB_OFFER,
                 job_id=job_id,
                 peer_id=self.peer_id,
                 cpu_load=round(cpu_load / 100.0, 2),
                 queue_len=self._job_count)

        # --- Receive file ---
        # Strip any path components from filename to prevent path traversal
        filename      = Path(msg.get('filename', f'{job_id}.bin')).name
        output_format = msg.get('output_format', '')
        input_path    = self.temp_dir / f"{job_id}_in_{filename}"
        output_path   = None

        with self._lock:
            self._job_count += 1

        t_start = time.perf_counter()

        try:
            log.info(f"[server] job {job_id}: receiving '{filename}'")
            bytes_recv = recv_file(conn, str(input_path))

            if self.metrics:
                self.metrics.add_bytes_recv(bytes_recv)

            # --- Convert ---
            use_gpu = msg.get('use_gpu', False)
            log.info(f"[server] job {job_id}: converting → {output_format}"
                     f"{'  [GPU requested]' if use_gpu else ''}")
            output_path = convert(str(input_path), output_format, str(self.temp_dir),
                                  use_gpu=use_gpu)

            latency_ms  = (time.perf_counter() - t_start) * 1000
            output_size = output_path.stat().st_size

            # --- Send result ---
            send_msg(conn, Msg.JOB_DONE,
                     job_id=job_id,
                     output_filename=output_path.name,
                     file_size=output_size)

            bytes_sent = send_file(conn, str(output_path))

            if self.metrics:
                self.metrics.record_job_done(latency_ms, bytes_sent)

            log.info(f"[server] job {job_id} done in {latency_ms:.0f} ms  "
                     f"({bytes_recv:,} → {output_size:,} bytes)")

        except Exception as e:
            log.error(f"[server] job {job_id} failed: {e}", exc_info=True)
            try:
                send_msg(conn, Msg.JOB_ERROR, job_id=job_id, reason=str(e))
            except Exception:
                pass

        finally:
            with self._lock:
                self._job_count -= 1
            # Clean up temp files
            for p in [input_path, output_path]:
                if p:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Quick standalone test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import tempfile
    import sys

    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s  %(name)-8s  %(levelname)s  %(message)s')

    with tempfile.TemporaryDirectory() as tmp:
        server = PeerServer(
            host='127.0.0.1', port=19998,
            peer_id='test-server', peer_name='TestServer',
            temp_dir=tmp
        )
        server.start()

        # Simple client to test handshake + job rejection (no real file)
        import time as _time
        _time.sleep(0.1)

        c = socket.socket()
        c.connect(('127.0.0.1', 19998))

        send_msg(c, Msg.HELLO, peer_id='test-client', peer_name='TestClient',
                 port=0, version=PROTOCOL_VERSION)
        reply = recv_msg(c)
        print(f"[test] HELLO reply: {reply}")
        assert reply['type'] == Msg.HELLO

        send_msg(c, Msg.BYE, peer_id='test-client')
        c.close()

        server.stop()
        print("[test] server.py self-test passed.")
