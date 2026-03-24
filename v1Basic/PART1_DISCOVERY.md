# Part 1 — Peer Discovery
**File to explain:** `core/discovery.py`

---

## What This Does (One Line)
Every peer automatically finds every other peer on the network — even across different subnets — without any central server or manual IP entry.

---

## The Problem

Standard mDNS (multicast DNS) only works on the **same subnet**. In college WiFi, different access points put laptops on different subnets (e.g., `10.1.19.x` vs `10.1.6.x`). A multicast packet sent from one subnet never reaches the other — the router drops it because its TTL is 1 (link-local only).

So we need two mechanisms:
1. **mDNS** — for same-subnet discovery (fast, zero config)
2. **UDP Beacon Scanner** — for cross-subnet discovery (scans the whole private IP range)

---

## Mechanism 1 — mDNS (Zeroconf)

```python
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, ServiceStateChange

SERVICE_TYPE = '_p2pconvert._tcp.local.'
```

Each peer **registers itself** as a named network service:
```python
self._info = ServiceInfo(
    type_=SERVICE_TYPE,
    name=f"{self.peer_id}.{SERVICE_TYPE}",
    addresses=[socket.inet_aton(local_ip)],
    port=self.port,
    properties={
        b'peer_id'  : self.peer_id.encode(),
        b'peer_name': self.peer_name.encode(),
        b'tls'      : b'1' if self.tls else b'0',
    },
)
self._zc.register_service(self._info)
```

A `ServiceBrowser` listens for other peers joining or leaving:
```python
self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, handlers=[self._on_service_change])
```

When a peer joins → `ServiceStateChange.Added` → `_add_peer()` extracts IP, port, TLS flag and stores in `_peers` dict.
When a peer leaves gracefully → `ServiceStateChange.Removed` → `_remove_peer()` deletes from dict and fires callback.

**Limitation:** mDNS uses multicast address `224.0.0.251`, port `5353`. Multicast packets have TTL=1, so they never cross a router. Only works within the same broadcast domain.

---

## Mechanism 2 — UDP Beacon Scanner (Cross-Subnet Fix)

Fixed port: `UDP_BEACON_PORT = 55780`

### The Probe Packet
Every peer sends a JSON `DISCOVER` packet:
```python
def _udp_probe(self) -> bytes:
    return json.dumps({
        'type'    : 'DISCOVER',
        'peer_id' : self.peer_id,
        'peer_name': self.peer_name,
        'tcp_port': self.port,
        'tls'     : self.tls,
    }).encode()
```

### Startup Scan (`_udp_scan`)
On startup, scans every IP in the private range:
```python
if a == 10:
    third_range = range(256)        # scans 10.x.0–255.y  (whole /16)
elif a == 172 and 16 <= b <= 31:
    third_range = range(16, 32)     # scans 172.16–31.x.y (/12)
elif a == 192 and b == 168:
    third_range = range(256)        # scans 192.168.0–255.y (/16)
else:
    third_range = [int(parts[2])]   # public IP: local /24 only
```
Sends a subnet broadcast first (`.255`), then unicasts to every individual IP.

### Keepalive (Re-announce)
After the initial scan, re-announces to the local /24 every 30 seconds so peers that join later can find us:
```python
while self._udp_sock and self._udp_sock.fileno() != -1:
    time.sleep(30)
    send(f"{prefix}.255")
    for i in range(1, 255):
        send(f"{prefix}.{i}")
```

### Listener (`_udp_listen`)
Receives DISCOVER packets from other peers:
1. Stamps `_last_seen[peer_id] = time.time()` (used for heartbeat)
2. Calls `_udp_add_peer()` to add to `_peers` dict if new
3. **Replies with its own probe** so the remote peer learns about us too (bidirectional)

```python
self._udp_sock.sendto(self._udp_probe(), (host, UDP_BEACON_PORT))
```

---

## Mechanism 3 — Heartbeat Timeout (Crash Detection)

**Problem:** If a peer crashes (process killed), it never sends a mDNS "goodbye" packet. Other peers would keep showing it as connected forever.

**Solution:** Track the last time a UDP DISCOVER was received from each peer. If silence for >120s, evict them.

```python
self._last_seen: dict[str, float] = {}   # peer_id → timestamp
```

Updated in `_udp_listen` every time a DISCOVER arrives:
```python
if peer_id and peer_id != self.peer_id:
    with self._lock:
        self._last_seen[peer_id] = time.time()
```

Background monitor runs every 20 seconds:
```python
def _heartbeat_monitor(self):
    TIMEOUT = 120   # 4× the 30s keepalive period
    while self._zc is not None or self._udp_sock is not None:
        time.sleep(20)
        now = time.time()
        with self._lock:
            stale = [pid for pid, t in self._last_seen.items()
                     if now - t > TIMEOUT and pid in self._peers]
        for pid in stale:
            # remove from _peers, fire on_peer_removed callback
```

**Why 120s?** The keepalive sends every 30s. Waiting 4 periods (120s) allows for up to 3 missed packets due to packet loss before declaring a peer dead.

**Only applies to UDP-discovered peers.** mDNS-discovered peers (same subnet) are handled by `ServiceStateChange.Removed` which is faster and more reliable on the same subnet.

---

## Data Structures

```python
_peers: dict[str, PeerInfo]   # peer_id → PeerInfo (the live peer list)
_lock: threading.Lock          # protects _peers and _last_seen from race conditions
_last_seen: dict[str, float]  # peer_id → last UDP heartbeat time
```

```python
@dataclass
class PeerInfo:
    peer_id  : str
    peer_name: str
    host     : str    # IP address
    port     : int    # TCP port for job submission
    tls      : bool   # whether that peer requires TLS
```

**Thread safety:** All reads/writes to `_peers` and `_last_seen` are wrapped in `with self._lock`. Callbacks (`on_peer_added`, `on_peer_removed`) are fired in **new daemon threads** so they never block the discovery loop.

---

## Public API Used by the Rest of the App

```python
discovery.start()                    # register + begin scanning
discovery.stop()                     # unregister + cleanup
discovery.get_peers() → list[PeerInfo]  # current live peer list
discovery.get_peer(peer_id) → PeerInfo | None
discovery.set_tls(True/False)        # re-advertise after TLS toggle
```

---

## Key CN Concepts to Mention

| Concept | Where it appears |
|---|---|
| Multicast DNS | mDNS on 224.0.0.251:5353, TTL=1 |
| Link-local addressing | Why mDNS can't cross subnets |
| UDP unicast scanning | The cross-subnet fix |
| UDP broadcast | `send(f"{prefix}.255")` — subnet broadcast |
| Service discovery | Zeroconf / Bonjour protocol |
| Heartbeat / keepalive | `_last_seen` + 120s timeout |
| Race condition prevention | `threading.Lock` on shared dicts |
| Daemon threads | Callbacks don't block discovery |

---

## Likely Teacher Questions

**Q: Why not just use mDNS?**
A: mDNS is multicast with TTL=1. It's link-local only — packets never cross a router. In college WiFi, different access points are on different subnets, so peers on `10.1.19.x` and `10.1.6.x` can't see each other via mDNS.

**Q: Isn't scanning the whole /16 slow?**
A: The UDP scan is fire-and-forget (no waiting for replies). Sending ~65000 tiny UDP packets takes ~2–3 seconds at startup. It's a one-time cost; after that only the /24 keepalive runs every 30s.

**Q: What if a peer joins after the initial scan?**
A: Two ways it gets found: (1) It scans us when it starts up. (2) Our 30s local keepalive reaches it. Max discovery delay = 30s.

**Q: How do you prevent duplicate peers?**
A: In `_udp_add_peer`, we check `if peer_id in self._peers: return` before adding. The lock ensures this check-then-add is atomic.
