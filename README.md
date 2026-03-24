# P2P File Conversion Network

A distributed peer-to-peer file conversion system built with raw Python sockets and TLS encryption. Peers on the same LAN discover each other automatically and offload conversion jobs to peers with spare capacity.

## Versions

| Folder | UI | Description |
|---|---|---|
| `v1Basic/` | Tkinter desktop | Lightweight desktop app — one window per peer |
| `UI/` | Flask web browser | Full-featured web dashboard with metrics, job history, GPU toggle |

## Features

- **Auto-discovery** — mDNS + UDP beacon scanner; no manual IP configuration
- **Job offloading** — if you're busy, conversion is sent to a free peer automatically
- **TLS encryption** — all job transfers encrypted; self-signed certs auto-generated on first run
- **Multi-format** — images, video/audio (FFmpeg), documents (PDF, DOCX, PPTX)
- **GPU acceleration** — NVENC/AMF/QSV support for video conversions
- **Fault tolerance** — if a peer goes down mid-job, falls back through remaining peers then local

## Quick Start

### v1Basic (Tkinter)
```bash
cd v1Basic
pip install -r requirements.txt
python peer.py --name Alice
```
Run the same command on other machines on your LAN with different `--name` values.

### UI (Flask Web)
```bash
cd UI
pip install -r requirements.txt
python peer.py --name Alice --ui-port 8080
```
Opens a browser tab at `http://localhost:8080` automatically.

## Requirements

- Python 3.10+
- FFmpeg in PATH (for audio/video conversion)
- LibreOffice in PATH (for document conversion, optional)

## Architecture

```
┌─────────────────────────────────────────┐
│              Each Peer Node             │
│                                         │
│  Discovery  ←→  mDNS + UDP Beacon       │
│  Server     ←→  TCP (TLS wrapped)       │
│  Client     ←→  TCP (TLS wrapped)       │
│  Converter  ←→  FFmpeg / LibreOffice /  │
│                 Pillow / python-pptx    │
└─────────────────────────────────────────┘
         ↕ TCP/TLS          ↕ TCP/TLS
┌──────────────┐    ┌──────────────┐
│   Peer B     │    │   Peer C     │
└──────────────┘    └──────────────┘
```

## Protocol

Control messages use **length-prefixed JSON** (4-byte big-endian uint32 + JSON body).
File transfers use **length-prefixed binary** (8-byte uint64 + raw bytes).
All communication is over **TCP**, wrapped in **TLS** using self-signed certificates.

Message flow:
```
Client → HELLO       → Server
Client ← HELLO       ← Server
Client → JOB_REQUEST → Server
Client ← JOB_OFFER   ← Server   (or JOB_REJECT if too busy)
Client → [file bytes] → Server
Client ← JOB_DONE   ← Server
Client ← [file bytes] ← Server
Client → BYE         → Server
```

## Team

CN Mini Project — Team 18
