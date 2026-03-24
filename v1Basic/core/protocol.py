"""
protocol.py — message framing and file transfer over raw TCP sockets

Wire format for control messages (JSON):
    [4 bytes big-endian uint32 = body length][UTF-8 JSON body]

Wire format for file transfer:
    [8 bytes big-endian uint64 = file size][raw bytes]

Message types and their required fields:

  HELLO         peer_id, peer_name, port, version
  JOB_REQUEST   job_id, input_format, output_format, filename, file_size
  JOB_OFFER     job_id, peer_id, cpu_load (0.0–1.0), queue_len
  JOB_REJECT    job_id, reason
  FILE_DATA     job_id, filename, file_size       (file bytes follow immediately)
  JOB_DONE      job_id, output_filename, file_size (result bytes follow immediately)
  JOB_ERROR     job_id, reason
  METRICS       peer_id, cpu_load, jobs_done, avg_latency_ms, bytes_sent, bytes_recv
  BYE           peer_id
"""

import json
import struct
import os

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_SIZE      = 4               # uint32 for JSON message length
FILE_HEADER_SIZE = 8               # uint64 for file byte length
MAX_MSG_SIZE     = 10 * 1024 * 1024  # 10 MB max for a single control message
CHUNK_SIZE       = 64 * 1024       # 64 KB read/write chunks for file transfer

PROTOCOL_VERSION = '1.0'


# ---------------------------------------------------------------------------
# Message type constants
# ---------------------------------------------------------------------------

class Msg:
    HELLO       = 'HELLO'
    JOB_REQUEST = 'JOB_REQUEST'
    JOB_OFFER   = 'JOB_OFFER'
    JOB_REJECT  = 'JOB_REJECT'
    JOB_DONE    = 'JOB_DONE'
    JOB_ERROR   = 'JOB_ERROR'
    BYE         = 'BYE'


# ---------------------------------------------------------------------------
# Send / receive control messages
# ---------------------------------------------------------------------------

def send_msg(sock, msg_type: str, **payload) -> None:
    """
    Serialise and send a control message.
    Usage: send_msg(sock, Msg.HELLO, peer_id='abc', peer_name='Laptop', port=9001)
    """
    body = json.dumps({'type': msg_type, **payload}).encode('utf-8')
    header = struct.pack('>I', len(body))
    sock.sendall(header + body)


def recv_msg(sock) -> dict:
    """
    Receive and deserialise a control message.
    Raises ConnectionError if the peer disconnects.
    Raises ValueError if the message is malformed or too large.
    """
    raw_len = _recv_exact(sock, HEADER_SIZE)
    msg_len = struct.unpack('>I', raw_len)[0]

    if msg_len == 0:
        raise ValueError("Received empty message")
    if msg_len > MAX_MSG_SIZE:
        raise ValueError(f"Message too large: {msg_len:,} bytes (max {MAX_MSG_SIZE:,})")

    body = _recv_exact(sock, msg_len)
    return json.loads(body.decode('utf-8'))


# ---------------------------------------------------------------------------
# File transfer (binary, length-prefixed)
# ---------------------------------------------------------------------------

def send_file(sock, file_path: str, progress_cb=None) -> int:
    """
    Send file bytes over socket.
    progress_cb(bytes_sent, total) called each chunk if provided.
    Returns total bytes sent.
    """
    total = os.path.getsize(file_path)
    sock.sendall(struct.pack('>Q', total))   # 8-byte size header

    sent = 0
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            sock.sendall(chunk)
            sent += len(chunk)
            if progress_cb:
                progress_cb(sent, total)

    return sent


def recv_file(sock, output_path: str, progress_cb=None) -> int:
    """
    Receive file bytes and write to output_path.
    progress_cb(bytes_received, total) called each chunk if provided.
    Returns total bytes received.
    """
    raw_size = _recv_exact(sock, FILE_HEADER_SIZE)
    total    = struct.unpack('>Q', raw_size)[0]

    received = 0
    with open(output_path, 'wb') as f:
        while received < total:
            to_read = min(CHUNK_SIZE, total - received)
            chunk   = _recv_exact(sock, to_read)
            f.write(chunk)
            received += len(chunk)
            if progress_cb:
                progress_cb(received, total)

    return received


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _recv_exact(sock, n: int) -> bytes:
    """Read exactly n bytes from socket. Raises ConnectionError on disconnect."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Peer disconnected mid-transfer")
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Quick standalone test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import socket
    import threading
    import tempfile
    import time

    PORT = 19999

    def server():
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', PORT))
        s.listen(1)
        conn, _ = s.accept()
        msg = recv_msg(conn)
        print(f"[server] received: {msg}")
        send_msg(conn, Msg.JOB_OFFER, job_id=msg['job_id'], peer_id='server', cpu_load=0.2, queue_len=0)

        # receive file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as tmp:
            tmp_path = tmp.name
        recv_file(conn, tmp_path)
        size = os.path.getsize(tmp_path)
        print(f"[server] received file: {size} bytes")
        os.unlink(tmp_path)
        conn.close()
        s.close()

    def client():
        time.sleep(0.1)
        c = socket.socket()
        c.connect(('127.0.0.1', PORT))
        send_msg(c, Msg.JOB_REQUEST,
                 job_id='test-001',
                 input_format='docx',
                 output_format='pdf',
                 filename='report.docx',
                 file_size=1024)
        reply = recv_msg(c)
        print(f"[client] received: {reply}")

        # send a dummy file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as tmp:
            tmp.write(b'x' * 4096)
            tmp_path = tmp.name
        send_file(c, tmp_path, progress_cb=lambda s, t: print(f"[client] sent {s}/{t}"))
        os.unlink(tmp_path)
        c.close()

    t = threading.Thread(target=server, daemon=True)
    t.start()
    client()
    t.join(timeout=3)
    print("\nprotocol.py self-test passed.")
