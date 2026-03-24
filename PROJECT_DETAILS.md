# P2P File Conversion Network — Project Details
**CN Mini Project | Team 18 | PES2UG24AM047 · PES2UG24AM032 · PES2UG24AM037**

---

## What We're Building

A Peer-to-Peer file conversion network where every node is equal — any peer can
request a conversion OR fulfill one for another peer. No central server, no coordinator,
no load balancer. Peers discover each other automatically on the same LAN/hotspot
using mDNS (no manual IP entry). All transfers happen over raw TCP sockets secured
with SSL/TLS.

Think: BitTorrent model but for file conversion instead of file sharing.

---

## Architecture

**Type:** Pure P2P Mesh
**Discovery:** mDNS via `zeroconf` library (automatic on same WiFi/hotspot)
**Transport:** Raw TCP sockets (Python `socket` + `ssl` libraries)
**Security:** SSL/TLS with self-signed certs (auto-generated on first run)

### Every peer node runs:
- TCP server thread      → accepts inbound conversion jobs from other peers
- mDNS thread            → announces self, discovers other peers
- Metrics thread         → tracks CPU, bandwidth, latency (psutil)
- Flask thread           → serves the web UI on localhost:8080

### Task offloading (the P2P benefit):
When Peer A receives a conversion request:
  1. Check own CPU load
  2. If busy → find lowest-load peer among known peers → send file there
  3. If free → convert locally → return result
This means adding more peers to the network improves performance (measurable!).

### No port forwarding needed:
- Development/testing: run multiple instances on same laptop (different ports)
- Demo: all laptops on same mobile hotspot — direct LAN connections work fine

---

## File Conversions Supported

### Documents (via docx2pdf — uses MS Word if installed, LibreOffice as fallback)
- DOCX → PDF
- PPTX → PDF
- ODT  → PDF
- RTF  → PDF

### PDF Operations (via pypdf — pure Python)
- Combine multiple PDFs into one

### Images (via Pillow — pure Python)
- PNG, JPEG, BMP, GIF, TIFF, WEBP → any of the others

### Audio (via FFmpeg binary)
- MP3, WAV, FLAC, OGG, M4A, AAC → any of the others

### Video (via FFmpeg binary)
- MP4, AVI, MKV, MOV, WebM → any of the others
- MP4/MKV/AVI → MP3 (extract audio)

---

## UI Design (Single Page, Flask)

Layout inspired by Google Translate — two dropdowns side by side:

```
┌─────────────────────────────────────────────────────────┐
│  ⬡ P2P Convert              ● ● ●  3 peers connected    │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   ┌─────────────────┐   →   ┌─────────────────┐        │
│   │  Auto Detect  ▾ │       │  PDF          ▾ │        │
│   └─────────────────┘       └─────────────────┘        │
│                                                         │
│         ┌───────────────────────────────┐               │
│         │     Drop file(s) here         │               │
│         │     or click to browse        │               │
│         └───────────────────────────────┘               │
│                                                         │
│                      [ Convert ]                        │
│                                                         │
│  ── Recent Jobs ──────────────────────────────────────  │
│  report.docx → PDF     1.2s  ✓  [↓ Download]           │
│  slides.pptx → PDF     2.4s  ✓  [↓ Download]           │
│  video.mp4   → MP3      ...  ⟳                         │
│                                                         │
│  ── Network ──────────────────────────────────────────  │
│  ● This PC  ● Ashrit-Laptop  ● Peer-3                   │
│                                                         │
│  [ Network Stats tab ]                                  │
│  Latency 1.2s · Throughput 3/min · CPU 24% · BW 1.2MB/s│
└─────────────────────────────────────────────────────────┘
```

### Smart dropdown behavior:
- Drop a file → left auto-detects format
- Right dropdown filters to only valid output formats for that input
- Drop multiple PDFs → right shows "Combine PDFs" only
- Multiple non-PDF files → queued as separate jobs

### Two tabs:
1. **Convert** — main conversion UI (above)
2. **Network Stats** — live metrics table (the performance evaluation parameters)

---

## Performance Metrics (shown in Network Stats tab)

| Parameter         | Description                                 | How Measured                        |
|-------------------|---------------------------------------------|-------------------------------------|
| Conversion Latency| Time from file send to converted file back  | Timestamped at socket send/receive  |
| Throughput        | Files converted per minute                  | Counter over time window            |
| CPU Utilization   | CPU usage on converting peer                | psutil per peer                     |
| Bandwidth Usage   | Network bytes per conversion                | Bytes sent/received at socket level |
| Peer Scalability  | Latency/throughput vs peer count            | Benchmark 2, 3, 4 peers             |
| Fault Tolerance   | Recovery time on peer failure               | Kill peer, measure retry time       |
| File Size vs Time | Conversion time vs file size                | 1MB, 10MB, 100MB tests              |
| TLS Overhead      | SSL/TLS vs plain TCP transfer time          | USE_TLS flag comparison             |

---

## Tech Stack

| Component       | Technology                                      |
|-----------------|-------------------------------------------------|
| Socket layer    | Python `socket` + `ssl` + `threading`           |
| Peer discovery  | `zeroconf` (mDNS)                               |
| Documents→PDF   | `docx2pdf` (Word/LibreOffice auto-detect)       |
| PDF combining   | `pypdf`                                         |
| Images          | `Pillow`                                        |
| Audio/Video     | FFmpeg binary (bundled in /bin/)                |
| Web UI          | Flask + plain HTML/CSS/JS (no heavy framework)  |
| Metrics         | `psutil`                                        |
| Security        | SSL/TLS self-signed certs (auto-generated)      |

---

## Project File Structure (planned)

```
Mini_Project/
├── peer.py                  # entry point — starts everything
├── core/
│   ├── converter.py         # all conversion logic (FFmpeg, docx2pdf, Pillow, pypdf)
│   ├── server.py            # TCP server — accepts inbound jobs
│   ├── client.py            # TCP client — sends jobs to other peers
│   ├── discovery.py         # mDNS announce + peer list management
│   ├── protocol.py          # message format (JSON over TCP)
│   └── metrics.py           # latency, throughput, CPU, bandwidth tracking
├── ui/
│   ├── dashboard.py         # Flask app
│   ├── templates/
│   │   └── index.html       # single page UI
│   └── static/
│       ├── style.css
│       └── app.js           # auto-refresh peer list, job status polling
├── certs/                   # auto-generated SSL certs (gitignored)
├── bin/
│   └── ffmpeg.exe           # bundled FFmpeg binary (Windows)
├── temp/                    # temp files during conversion (auto-cleaned)
├── requirements.txt
├── setup.bat                # Windows: pip install + first run
├── setup.sh                 # Linux/Mac: pip install + first run
└── README.md
```

---

## SSL/TLS Plan

- On first run, `peer.py` auto-generates a self-signed cert+key in `/certs/`
- All TCP connections wrapped with `ssl.wrap_socket()`
- `USE_TLS = True` flag at top of `peer.py` — flip to False for plain TCP testing
  (useful for measuring TLS overhead — one of the evaluation parameters)
- NOTE: In production/real use, TLS should always be ON — files sent to other
  peers are a privacy risk without encryption

---

## Demo Setup (no port forwarding)

1. All laptops connect to one mobile hotspot
2. Each runs `python peer.py` (or double-clicks start.bat)
3. Peers discover each other automatically via mDNS within seconds
4. Browser opens automatically at http://localhost:8080
5. Upload a file on one laptop, watch it get converted (possibly on another peer)

For solo testing on one laptop:
- Run 3 terminals: `python peer.py --port 9001`, `--port 9002`, `--port 9003`
- All discover each other on localhost

---

## Build Order

1. `core/converter.py`    — test all conversions work standalone
2. `core/protocol.py`     — define message format
3. `core/server.py`       — TCP server, accept jobs, call converter
4. `core/client.py`       — TCP client, send jobs, receive results
5. `core/discovery.py`    — mDNS, peer list
6. `core/metrics.py`      — stats tracking
7. `peer.py`              — wire everything together
8. `ui/dashboard.py`      — Flask app
9. `ui/index.html`        — web UI
10. Testing               — multi-instance on localhost
11. Packaging             — requirements.txt, setup.bat, bundle ffmpeg

---

## Requirements (requirements.txt)

```
flask
zeroconf
psutil
pillow
pypdf
docx2pdf
```

FFmpeg: bundled as binary in /bin/ — not a pip package.
LibreOffice: optional fallback for docx2pdf, not required if MS Word is present.

---

## Rubric Coverage

| Criterion                     | How covered                                              |
|-------------------------------|----------------------------------------------------------|
| Problem definition            | P2P synopsis doc                                         |
| Core socket implementation    | raw socket/ssl, bind/listen/accept explicit in server.py |
| Feature impl (Deliverable 1)  | full conversion + multi-peer + SSL                       |
| Performance evaluation        | metrics in Network Stats tab, benchmarks                 |
| Optimization & fixes          | TLS flag, retry on peer failure, edge case handling      |
| Final demo + GitHub           | README, setup scripts, live demo on hotspot              |
