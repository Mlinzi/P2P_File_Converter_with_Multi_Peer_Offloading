# Part 3 — Application & Job Orchestration
**Files to explain:** `peer.py`, `core/converter.py`

---

## What This Does (One Line)
The entry point that ties everything together — runs the UI, decides whether to offload a conversion job to a peer or do it locally, and handles the actual file format conversion using whatever tools are installed.

---

## peer.py — The Entry Point

### Startup Sequence
```
1. Parse args (--port, --name)
2. Generate TLS certs if missing (_ensure_certs)
3. Start TCP server (PeerServer) with TLS
4. Start peer discovery (Discovery) — mDNS + UDP scan
5. Launch Tkinter UI
6. Begin polling loop (updates UI from background threads)
```

### Why Daemon Threads Everywhere

Tkinter is **not thread-safe** — you cannot call `.insert()`, `.config()` etc. from a background thread directly. The solution used here:

```python
# Background thread puts messages into a queue
LOG_Q.put(f"Got result from {peer.peer_name}: ...")

# Main thread (poll loop, runs every 400ms) reads from queue and updates UI
def _poll(self):
    while not LOG_Q.empty():
        self._log(LOG_Q.get_nowait())     # safe: main thread
    while not PEERS_Q.empty():
        PEERS_Q.get_nowait()
        self._refresh_peers()             # safe: main thread
    self.root.after(400, self._poll)      # reschedule itself
```

Two queues:
- `LOG_Q` — text messages to append to the log box
- `PEERS_Q` — signals that the peer list changed (refresh listbox)

`self.root.after(0, _finish)` is also used to safely push UI updates from conversion threads back to the main thread.

---

## The Offload Decision (_convert_worker)

This is the core distributed-computing logic. Runs in a daemon thread per file:

```
Has peers? ──Yes──► Try peer[0]
                         │
                    Success? ──Yes──► return result ✓
                         │
                         No (failed/rejected)
                         │
                         ▼
                    Try peer[1]
                         │
                    Success? ──Yes──► return result ✓
                         │
                         No  (keep trying remaining peers...)
                         │
                         ▼
                    All peers exhausted
                         │
                         ▼
                    Convert locally
                         │
                    Success? ──Yes──► return result ✓
                         │
                         No
                         │
                         ▼
                    Log error
```

Key part of the code:
```python
peers = DISCOVERY.get_peers() if DISCOVERY else []
for peer in peers:
    try:
        result = submit_job(
            peer_host=peer.host, peer_port=peer.port,
            ...
            use_gpu=get_gpu_accel(),                           # sender's GPU preference
            ssl_context=_make_client_ssl_ctx() if peer.tls else None,  # TLS if peer needs it
        )
        break   # success — stop trying more peers
    except Exception as e:
        LOG_Q.put(f"{peer.peer_name} failed (...), trying next peer...")
        result = None

if result is None:
    result = convert(path, out_fmt, save_dir)   # local fallback (all peers exhausted)
```

**Why offload?** Distributes CPU load across the group. If your laptop is already busy rendering something, a peer with spare capacity can take your conversion job.

**Why TLS check on `peer.tls`?** Each peer independently advertises whether it requires TLS (via discovery). If the remote peer has TLS on, we must use `ssl_context`; otherwise the TLS handshake will fail.

---

## GPU Preference Propagation

The GPU checkbox only calls:
```python
set_gpu_accel(self._gpu_var.get())   # sets global USE_GPU in converter.py
```

When sending to a peer:
```python
use_gpu=get_gpu_accel()   # reads current USE_GPU → sent in JOB_REQUEST message
```

The remote peer's server reads `use_gpu` from the message and passes it to `convert()`. This means:
- **Only the sender's checkbox matters** for remote jobs
- If the remote peer has no GPU → `GPU_ENCODERS` is empty → silently uses CPU
- The remote peer's own checkbox is irrelevant for jobs sent to it

---

## TLS Toggle

The TLS checkbox in the UI:
```python
self._tls_cb = Checkbutton(top, text='TLS', variable=self._tls_var,
                            command=self._toggle_tls)
```

Toggling restarts the TCP server in a **background thread** (so UI doesn't freeze):
```python
def _toggle_tls(self):
    threading.Thread(target=self._apply_tls, args=(new_tls,), daemon=True).start()

def _apply_tls(self, new_tls: bool):
    SERVER.stop()
    time.sleep(0.3)                          # let socket fully close
    ssl_ctx = _make_server_ssl_ctx() if new_tls else None
    SERVER = PeerServer(..., ssl_context=ssl_ctx)
    SERVER.start()
    DISCOVERY.set_tls(new_tls)              # re-advertise new TLS state via mDNS
```

`DISCOVERY.set_tls()` unregisters and re-registers the mDNS service so all peers immediately see the updated TLS flag.

---

## converter.py — The Conversion Engine

### Tool Discovery (at import time)
```python
FFMPEG  = _find_ffmpeg()    # checks bin/ffmpeg.exe, then PATH, then imageio-ffmpeg
SOFFICE = _find_soffice()   # checks common Windows install paths, then PATH
HAS_DOCX2PDF = _has_docx2pdf()   # pip package
HAS_COMTYPES = _has_comtypes()   # Windows COM automation
GPU_ENCODERS = _detect_gpu_encoders()  # queries FFmpeg for h264_nvenc/amf/qsv
```
All tool checks happen **once** at startup. If a tool isn't found, its conversions simply won't appear in the format dropdown.

### Format Groups
```python
WORD_FORMATS         = {'docx', 'doc', 'odt', 'rtf'}
PRESENTATION_FORMATS = {'pptx', 'ppt', 'odp'}
SPREADSHEET_FORMATS  = {'xlsx', 'xls', 'ods', 'csv'}
IMAGE_FORMATS        = {'png', 'jpeg', 'bmp', 'gif', 'tiff', 'webp'}
AUDIO_FORMATS        = {'mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac'}
VIDEO_FORMATS        = {'mp4', 'avi', 'mkv', 'mov', 'webm'}
```

### Conversion Routing
`convert()` reads the input extension and routes to the right backend:

```
Input format
    │
    ├── Document (docx/pptx/xlsx/etc.) ──► _convert_document()
    │                                           │
    │                                    ┌──────┴──────────┐
    │                                    │  → PDF?          │  → Other format?
    │                                    │                  │
    │                              Word: docx2pdf      LibreOffice (soffice)
    │                              pptx: LibreOffice
    │                                    → PowerPoint COM
    │                                    → python-pptx + reportlab
    │
    ├── Image (png/jpg/gif/etc.) ────────► _convert_image()  [Pillow]
    │
    └── Audio / Video ──────────────────► _convert_ffmpeg()  [FFmpeg]
```

### pptx → PDF Fallback Chain
Most people don't have LibreOffice. Three fallbacks before giving up:
```
1. LibreOffice (soffice) — best quality
2. PowerPoint COM (comtypes) — requires MS PowerPoint installed
3. python-pptx + reportlab — pure Python, basic text rendering, no tools needed
4. Raise RuntimeError with install instructions
```

### GPU Acceleration
The `use_gpu` parameter flows all the way from the checkbox to FFmpeg:
```python
def _convert_ffmpeg(input_path, output_path, use_gpu: bool = False):
    cmd = [FFMPEG, '-i', str(input_path)]

    if use_gpu and GPU_ENCODERS and out_fmt in VIDEO_FORMATS:
        cmd += ['-c:v', GPU_ENCODERS[0]]   # e.g. h264_nvenc

    cmd += ['-y', str(output_path)]

    result = subprocess.run(cmd, ...)
    if result.returncode != 0 and use_gpu:
        # GPU failed — retry with CPU (automatic fallback)
        result = subprocess.run([FFMPEG, '-i', input_path, '-y', output_path], ...)
```

Auto-retry with CPU if GPU encode fails (driver issues, unsupported codec, etc.).

### RGBA → JPEG handling
```python
if img.mode == 'RGBA' and output_format == 'jpeg':
    img = img.convert('RGB')    # JPEG doesn't support transparency
```
JPEG has no alpha channel. PNG with transparency would produce a corrupted JPEG without this.

---

## CONVERSION_MAP — What the Dropdown Shows

Built at import time. Each format maps to a list of achievable output formats:
```python
CONVERSION_MAP = {
    'mp4': ['avi', 'mkv', 'mov', 'webm', 'mp3'],   # mp3 = audio extraction
    'docx': ['doc', 'odt', 'pdf', 'rtf'],
    'png': ['bmp', 'gif', 'jpeg', 'tiff', 'webp'],
    ...
}
```

`get_available_outputs(fmt)` filters this list based on which tools are actually installed:
- Audio/video: only shows if FFmpeg found
- Documents → PDF: only shows if LibreOffice OR docx2pdf OR comtypes found (or always for pptx since python-pptx is a fallback)
- Images: always shows (Pillow is always installed)

---

## Key CN Concepts to Mention

| Concept | Where it appears |
|---|---|
| Distributed processing | Offloading jobs to peers with spare capacity |
| Client-server model | Each peer is both client (submits jobs) and server (accepts jobs) |
| End-to-end principle | Conversion logic lives at endpoints, not in the "network" |
| Load-aware routing | Checks CPU/RAM before sending to a peer |
| Fault tolerance | Falls back to local if peer fails or rejects |
| Thread safety | `LOG_Q`/`PEERS_Q` queues, `root.after()` for UI updates |
| GPU acceleration | Hardware encoder preference propagated via JOB_REQUEST |

---

## Likely Teacher Questions

**Q: Why offload to a peer instead of always converting locally?**
A: Distributed load sharing. If peer A has 4 cores idle and peer B has 1 core at 80%, offloading from B to A is faster. Also demonstrates the P2P networking concept — every node is both client and server.

**Q: What if the peer rejects the job?**
A: `submit_job()` raises `JobRejected`. The `except` block in `_convert_worker` catches it, logs the reason, and tries the next peer in the list. Only after all peers are exhausted does it fall back to local conversion. The user gets their file either way.

**Q: How does the format dropdown know what's possible?**
A: `get_available_outputs()` checks which tools are installed at runtime and only shows achievable conversions. If you don't have LibreOffice, docx→pdf won't appear at all rather than appearing and failing.

**Q: Why are conversions done in threads?**
A: Converting a large video file can take minutes. If done on the main Tkinter thread, the UI would freeze completely — no spinner, no log updates, window appears unresponsive. Threading keeps the UI live while conversion runs in background.

**Q: Explain the end-to-end principle here.**
A: The network (TCP layer) just moves bytes from one machine to another. All intelligence — what format to convert to, which tool to use, whether to use GPU — lives entirely at the endpoints (sender and receiver). The network itself is completely unaware of what's being transferred.
