# Part 2 — Transport & Protocol
**Files to explain:** `core/protocol.py`, `core/server.py`, `core/client.py`

---

## What This Does (One Line)
Defines exactly how two peers talk to each other over TCP — the message format, the handshake sequence, file transfer, TLS encryption, and how the server decides to accept or reject a job.

---

## Wire Format (protocol.py)

Two types of data travel over the socket:

### Control Messages (JSON)
```
┌──────────────────┬─────────────────────────────────┐
│  4 bytes (uint32)│  N bytes (UTF-8 JSON)            │
│  body length     │  { "type": "HELLO", ... }        │
└──────────────────┴─────────────────────────────────┘
```
- 4-byte big-endian header tells the receiver how many bytes to read next
- Prevents TCP stream fragmentation issues ("how do I know where one message ends?")
- Max message size: 10 MB (guard against memory exhaustion)

```python
# Sending
body   = json.dumps({'type': msg_type, **payload}).encode('utf-8')
header = struct.pack('>I', len(body))   # '>I' = big-endian uint32
sock.sendall(header + body)

# Receiving
raw_len = _recv_exact(sock, 4)
msg_len = struct.unpack('>I', raw_len)[0]
body    = _recv_exact(sock, msg_len)
return json.loads(body.decode('utf-8'))
```

### File Transfer (Binary)
```
┌──────────────────┬─────────────────────────────────┐
│  8 bytes (uint64)│  N bytes (raw file data)         │
│  file size       │  streamed in 64 KB chunks        │
└──────────────────┴─────────────────────────────────┘
```
- 8-byte header = up to 18 exabytes (no practical file size limit)
- Sent/received in **64 KB chunks** (`CHUNK_SIZE = 64 * 1024`) — avoids loading the whole file into RAM

### `_recv_exact` — The Critical Helper
```python
def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Peer disconnected mid-transfer")
        buf.extend(chunk)
    return bytes(buf)
```
TCP is a **stream protocol** — `sock.recv(1000)` might return 200 bytes or 1000 bytes. This loop keeps reading until exactly `n` bytes are collected. This is essential for correct framing.

---

## Message Types

| Message | Direction | Key Fields |
|---|---|---|
| `HELLO` | Both → Both | peer_id, peer_name, port, version |
| `JOB_REQUEST` | Client → Server | job_id, input_format, output_format, filename, file_size, use_gpu |
| `JOB_OFFER` | Server → Client | job_id, cpu_load (0.0–1.0), queue_len |
| `JOB_REJECT` | Server → Client | job_id, reason |
| `JOB_DONE` | Server → Client | job_id, output_filename, file_size |
| `JOB_ERROR` | Server → Client | job_id, reason |
| `BYE` | Either | peer_id |

---

## Protocol State Machine (Full Conversation)

```
CLIENT                                    SERVER
  │                                          │
  │──── HELLO (peer_id, version) ──────────►│
  │◄─── HELLO (peer_id, version) ───────────│
  │                                          │
  │──── JOB_REQUEST (format, filename) ────►│  checks CPU/RAM/job count
  │◄─── JOB_OFFER (cpu_load) ───────────────│  or JOB_REJECT (reason)
  │                                          │
  │──── [8-byte size][raw file bytes] ──────►│
  │                                          │  converts file...
  │◄─── JOB_DONE (output_filename) ─────────│  or JOB_ERROR (reason)
  │◄─── [8-byte size][raw file bytes] ───────│
  │                                          │
  │──── BYE ───────────────────────────────►│
  │                                    [close]
```

---

## Server — How It Works (server.py)

### Accept Loop
```python
self._sock.listen(20)   # queue up to 20 pending connections
```
Runs in a daemon thread. For each new connection, spawns another daemon thread:
```python
t = threading.Thread(target=self._handle_peer, args=(conn, addr), daemon=True)
t.start()
```
Multiple peers can be served **simultaneously** — each gets its own thread.

### Job Rejection Logic
Before accepting a job, checks three things:
```python
# 1. Too many concurrent jobs
if self._job_count >= MAX_CONCURRENT_JOBS:   # = 3
    send_msg(conn, Msg.JOB_REJECT, reason='Too busy')

# 2. CPU overloaded
cpu_load = psutil.cpu_percent(interval=0.1)
if cpu_load >= CPU_REJECT_THRESHOLD:         # = 90%
    send_msg(conn, Msg.JOB_REJECT, reason=f'CPU too high ({cpu_load:.0f}%)')

# 3. RAM overloaded
ram_load = psutil.virtual_memory().percent
if ram_load >= RAM_REJECT_THRESHOLD:         # = 90%
    send_msg(conn, Msg.JOB_REJECT, reason=f'RAM too high ({ram_load:.0f}%)')
```
Also checks if it has the tools to do the conversion:
```python
if out_fmt not in get_available_outputs(input_fmt):
    send_msg(conn, Msg.JOB_REJECT, reason='Missing tool for ...')
```

### Path Traversal Security Fix
```python
# VULNERABLE (old):
filename = msg.get('filename', f'{job_id}.bin')

# SAFE (current):
filename = Path(msg.get('filename', f'{job_id}.bin')).name
```
`Path.name` strips any directory components. Without this, a malicious peer could send `filename = "../../Windows/evil.exe"` and the server would write the received file outside the temp directory.

### Temp File Cleanup
Always runs, even if conversion fails:
```python
finally:
    for p in [input_path, output_path]:
        if p:
            Path(p).unlink(missing_ok=True)
```

---

## TLS — Encryption Layer

TLS wraps the TCP socket **after** the TCP connection is established, **before** any application data is sent.

### Server side (server.py)
```python
if self.ssl_context:
    conn = self.ssl_context.wrap_socket(conn, server_side=True)
```

### Client side (peer.py)
```python
if ssl_context:
    sock = ssl_context.wrap_socket(sock, server_hostname=host)
```

### Cert setup (peer.py)
Auto-generated on first run using the `cryptography` library:
- RSA 2048-bit key
- Self-signed X.509 certificate, valid 10 years
- Saved to `../certs/peer.crt` and `../certs/peer.key`

The client uses:
```python
ctx.check_hostname = False
ctx.verify_mode    = ssl.CERT_NONE
```
This means: **encryption yes, authentication no**. The data is encrypted in transit, but we don't verify the server's identity (expected — self-signed cert). For a LAN demo this is the right trade-off.

### What TLS protects
- The file bytes being transferred (main threat on a shared WiFi)
- All control messages (HELLO, JOB_REQUEST, etc.)

### What TLS does NOT protect
- UDP discovery packets (always plaintext — just metadata: peer name, IP, port)

---

## TCP Keepalive

Detects silently dead connections (e.g., laptop closed lid mid-transfer):
```python
conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  30)  # start probing after 30s idle
conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)  # probe every 10s
conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,   3)   # give up after 3 misses
```
Total dead-connection detection time: 30 + (10 × 3) = **60 seconds**.

This is different from the discovery heartbeat — TCP keepalive is at the **transport layer** (OS-level probes), while the discovery heartbeat is at the **application layer** (UDP DISCOVER packets).

---

## Client — How It Works (client.py)

`submit_job()` handles the full conversation:
1. `_connect()` — TCP connect with 10s timeout, enable keepalive, optionally wrap TLS
2. Send/recv `HELLO`
3. Send `JOB_REQUEST` with `use_gpu` flag (sender's GPU preference travels with the job)
4. Check reply: `JOB_OFFER` (proceed) or `JOB_REJECT` (raise `JobRejected`)
5. `send_file()` — stream the file in 64 KB chunks
6. Wait for `JOB_DONE` or `JOB_ERROR`
7. `recv_file()` — receive converted file
8. Send `BYE`, close socket

Timeouts:
- Connect: `CONNECT_TIMEOUT = 10s`
- Receive result: `RECV_TIMEOUT = 120s` (large video files take time to convert)

---

## Key CN Concepts to Mention

| Concept | Where it appears |
|---|---|
| Length-prefixed framing | 4-byte header before every JSON message |
| TCP stream vs message boundary | Why `_recv_exact` is necessary |
| TLS (Transport Layer Security) | `ssl.SSLContext.wrap_socket()` |
| Self-signed certificate | `cryptography` lib, RSA 2048 |
| TCP keepalive | `SO_KEEPALIVE`, `TCP_KEEPIDLE/INTVL/CNT` |
| Concurrency | Thread-per-connection server model |
| Load balancing / admission control | CPU/RAM/job-count checks before accepting |
| State machine | HELLO → REQUEST → OFFER → transfer → DONE |

---

## Likely Teacher Questions

**Q: Why length-prefix instead of delimiter-based framing?**
A: Delimiter-based (e.g., newline) breaks when the message content contains the delimiter. Length-prefix is unambiguous regardless of content.

**Q: Why use threads instead of async/select?**
A: Simpler code. Conversions are CPU-bound (FFmpeg, LibreOffice), so async I/O wouldn't help much anyway. Thread-per-connection is standard for this scale (max 3 concurrent jobs).

**Q: What happens if the peer crashes mid-transfer?**
A: `_recv_exact` raises `ConnectionError("Peer disconnected mid-transfer")` when `sock.recv()` returns empty bytes. The server's `finally` block cleans up temp files. The client's caller catches the exception and falls back to local conversion.

**Q: Why does the client ignore TLS cert errors?**
A: We use self-signed certs. There's no CA to verify against. On a LAN, the main threat is passive sniffing, which TLS with `CERT_NONE` fully protects against. MITM is theoretically possible but requires active effort not typical of a college network.

**Q: What does `use_gpu` do in JOB_REQUEST?**
A: The sender's GPU preference travels with the job. The server reads it and passes it to `convert()`. If the receiver has a GPU encoder (NVENC/AMF/QSV) and `use_gpu=True`, it uses hardware encoding. If the receiver has no GPU, it silently falls back to CPU — the sender's preference is just a request, not a requirement.
