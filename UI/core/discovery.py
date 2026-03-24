"""
discovery.py — mDNS peer discovery using zeroconf

Each peer:
  - Registers itself as a service on the local network
  - Listens for other peers joining/leaving
  - Maintains a live dict of known peers: { peer_id: PeerInfo }

Service type: _p2pconvert._tcp.local.
"""

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, ServiceStateChange

UDP_BEACON_PORT = 55780   # fixed port for subnet scan / cross-subnet discovery

log = logging.getLogger('discovery')

SERVICE_TYPE = '_p2pconvert._tcp.local.'


@dataclass
class PeerInfo:
    peer_id  : str
    peer_name: str
    host     : str
    port     : int
    tls      : bool = False   # True if that peer's server requires TLS

    def __repr__(self):
        tls_tag = ' [TLS]' if self.tls else ''
        return f"PeerInfo({self.peer_name!r} @ {self.host}:{self.port}{tls_tag})"


class Discovery:
    def __init__(self, peer_id: str, peer_name: str, port: int,
                 tls: bool = False,
                 on_peer_added: Callable[[PeerInfo], None] = None,
                 on_peer_removed: Callable[[str], None] = None):
        """
        peer_id         : unique ID for this peer (used as service name)
        peer_name       : human-readable name
        port            : TCP port this peer's server listens on
        tls             : whether this peer's server requires TLS
        on_peer_added   : callback(PeerInfo) when a new peer appears
        on_peer_removed : callback(peer_id: str) when a peer leaves
        """
        self.peer_id          = peer_id
        self.peer_name        = peer_name
        self.port             = port
        self.tls              = tls
        self.on_peer_added    = on_peer_added
        self.on_peer_removed  = on_peer_removed

        self._peers: dict[str, PeerInfo] = {}
        self._lock      = threading.Lock()
        self._zc        = None
        self._info      = None
        self._browser   = None
        self._udp_sock  = None
        self._last_seen: dict[str, float] = {}   # peer_id → last UDP DISCOVER timestamp

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Register this peer on the network and start listening for others."""
        self._zc = Zeroconf()

        # Build service info for this peer
        local_ip = _get_local_ip()
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
            server=f"{self.peer_id}.local.",
        )

        self._zc.register_service(self._info)
        self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, handlers=[self._on_service_change])

        log.info(f"[discovery] registered as '{self.peer_name}' ({self.peer_id}) "
                 f"on {local_ip}:{self.port}")

        self._start_udp_beacon()
        threading.Thread(target=self._heartbeat_monitor, daemon=True,
                         name='peer-heartbeat').start()

    def stop(self):
        """Unregister from the network and stop listening."""
        if self._udp_sock:
            try:
                self._udp_sock.close()
            except Exception:
                pass
            self._udp_sock = None
        if self._zc:
            if self._info:
                try:
                    self._zc.unregister_service(self._info)
                except Exception:
                    pass
            self._zc.close()
            self._zc = None
        log.info("[discovery] stopped")

    def set_tls(self, tls: bool):
        """Update TLS flag in mDNS advertisement (unregister → update → re-register)."""
        self.tls = tls
        if self._zc and self._info:
            try:
                self._zc.unregister_service(self._info)
                self._info.properties[b'tls'] = b'1' if tls else b'0'
                self._zc.register_service(self._info)
                log.info(f"[discovery] TLS advertisement updated: {'on' if tls else 'off'}")
            except Exception as e:
                log.warning(f"[discovery] failed to update TLS advertisement: {e}")

    # ------------------------------------------------------------------
    # Peer list access
    # ------------------------------------------------------------------

    def get_peers(self) -> list[PeerInfo]:
        """Return list of all currently known peers (excludes self)."""
        with self._lock:
            return list(self._peers.values())

    def get_peer(self, peer_id: str) -> PeerInfo | None:
        with self._lock:
            return self._peers.get(peer_id)

    def peer_count(self) -> int:
        with self._lock:
            return len(self._peers)

    # ------------------------------------------------------------------
    # UDP beacon — cross-subnet discovery
    # ------------------------------------------------------------------

    def _udp_probe(self) -> bytes:
        return json.dumps({
            'type'    : 'DISCOVER',
            'peer_id' : self.peer_id,
            'peer_name': self.peer_name,
            'tcp_port': self.port,
            'tls'     : self.tls,
        }).encode()

    def _start_udp_beacon(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(('', UDP_BEACON_PORT))
            self._udp_sock = s
            threading.Thread(target=self._udp_listen, daemon=True, name='udp-listen').start()
            threading.Thread(target=self._udp_scan,   daemon=True, name='udp-scan'  ).start()
            log.info(f"[discovery] UDP beacon on port {UDP_BEACON_PORT}")
        except Exception as e:
            log.warning(f"[discovery] UDP beacon unavailable ({e}), using mDNS only")
            self._udp_sock = None

    def _udp_add_peer(self, msg: dict, host: str):
        peer_id = msg.get('peer_id', '')
        if not peer_id or peer_id == self.peer_id:
            return
        port = msg.get('tcp_port', 0)
        if not port:
            return
        peer = PeerInfo(
            peer_id=peer_id,
            peer_name=msg.get('peer_name', host),
            host=host, port=port,
            tls=msg.get('tls', False),
        )
        with self._lock:
            if peer_id in self._peers:
                return
            self._peers[peer_id] = peer
        log.info(f"[discovery] UDP peer found: {peer}")
        if self.on_peer_added:
            threading.Thread(target=self.on_peer_added, args=(peer,), daemon=True).start()

    def _udp_listen(self):
        while self._udp_sock:
            try:
                data, addr = self._udp_sock.recvfrom(1024)
                msg = json.loads(data.decode())
                if msg.get('type') != 'DISCOVER':
                    continue
                host    = addr[0]
                peer_id = msg.get('peer_id', '')
                # Refresh heartbeat for any known peer (even if already in _peers)
                if peer_id and peer_id != self.peer_id:
                    with self._lock:
                        self._last_seen[peer_id] = time.time()
                self._udp_add_peer(msg, host)
                # Reply so the other peer learns about us too
                try:
                    self._udp_sock.sendto(self._udp_probe(), (host, UDP_BEACON_PORT))
                except Exception:
                    pass
            except Exception:
                if not self._udp_sock or self._udp_sock.fileno() == -1:
                    break

    def _udp_scan(self):
        if not self._udp_sock:
            return
        local_ip = _get_local_ip()
        parts    = local_ip.split('.')

        def send(ip):
            try:
                self._udp_sock.sendto(self._udp_probe(), (ip, UDP_BEACON_PORT))
            except Exception:
                pass

        # Determine scan width based on private IP range
        a = int(parts[0]); b = int(parts[1])
        if a == 10:
            third_range = range(256)              # 10.x.0-255.y  — scan whole /16
        elif a == 172 and 16 <= b <= 31:
            third_range = range(16, 32)           # 172.16-31.x.y — scan /12
        elif a == 192 and b == 168:
            third_range = range(256)              # 192.168.0-255.y — scan /16
        else:
            third_range = [int(parts[2])]         # public IP: local /24 only

        p01 = f"{parts[0]}.{parts[1]}"
        for third in third_range:
            send(f"{p01}.{third}.255")            # subnet broadcast first
            for i in range(1, 255):
                ip = f"{p01}.{third}.{i}"
                if ip != local_ip:
                    send(ip)

        log.info(f"[discovery] UDP scan complete ({len(list(third_range))} subnets)")

        # Keepalive every 15 s: broadcast on local subnet + unicast to known peers.
        # Unicast is more reliable than broadcast on WiFi (APs often throttle broadcasts).
        prefix = '.'.join(parts[:3])
        while self._udp_sock and self._udp_sock.fileno() != -1:
            time.sleep(15)
            send(f"{prefix}.255")          # subnet broadcast
            with self._lock:
                known_hosts = [p.host for p in self._peers.values()]
            for host in known_hosts:       # unicast to each known peer
                send(host)

    # ------------------------------------------------------------------
    # Heartbeat monitor — evict peers that stop sending UDP keepalives
    # ------------------------------------------------------------------

    def _heartbeat_monitor(self):
        """
        Runs every 20 s. Removes any peer whose last UDP DISCOVER was
        more than 120 s ago (= 4× the 30 s keepalive period).
        This catches abrupt disconnects / crashes that don't send a
        graceful mDNS goodbye.
        Only applies to peers discovered/maintained via UDP (_last_seen dict).
        mDNS-only peers (same subnet) are handled by ServiceStateChange.Removed.
        """
        TIMEOUT = 120   # seconds
        while self._zc is not None or self._udp_sock is not None:
            time.sleep(20)
            now = time.time()
            with self._lock:
                stale = [pid for pid, t in self._last_seen.items()
                         if now - t > TIMEOUT and pid in self._peers]
            for pid in stale:
                with self._lock:
                    removed = self._peers.pop(pid, None)
                    self._last_seen.pop(pid, None)
                if removed:
                    log.info(f"[discovery] peer timed out (no heartbeat): {removed}")
                    if self.on_peer_removed:
                        threading.Thread(target=self.on_peer_removed,
                                         args=(pid,), daemon=True).start()

    # ------------------------------------------------------------------
    # mDNS event handler
    # ------------------------------------------------------------------

    def _on_service_change(self, zeroconf: Zeroconf, service_type: str,
                            name: str, state_change: ServiceStateChange):
        if state_change is ServiceStateChange.Added:
            self._add_peer(zeroconf, service_type, name)
        elif state_change is ServiceStateChange.Removed:
            self._remove_peer(name)

    def _add_peer(self, zeroconf: Zeroconf, service_type: str, name: str):
        info = zeroconf.get_service_info(service_type, name)
        if not info:
            return

        props   = info.properties or {}
        peer_id = (props.get(b'peer_id') or b'').decode(errors='replace')

        # Ignore self
        if peer_id == self.peer_id:
            return

        peer_name = (props.get(b'peer_name') or b'').decode(errors='replace') or peer_id
        host      = socket.inet_ntoa(info.addresses[0]) if info.addresses else None
        port      = info.port
        tls       = (props.get(b'tls') or b'0').decode() == '1'

        if not host or not port:
            log.warning(f"[discovery] incomplete info for {name}, skipping")
            return

        peer = PeerInfo(peer_id=peer_id, peer_name=peer_name, host=host, port=port, tls=tls)

        with self._lock:
            self._peers[peer_id] = peer

        log.info(f"[discovery] peer joined: {peer}")

        if self.on_peer_added:
            threading.Thread(target=self.on_peer_added, args=(peer,), daemon=True).start()

    def _remove_peer(self, name: str):
        # name looks like  "<peer_id>._p2pconvert._tcp.local."
        peer_id = name.replace(f'.{SERVICE_TYPE}', '').rstrip('.')

        # mDNS Removed fires spuriously on WiFi (multicast loss, TTL expiry).
        # If we've seen this peer via UDP recently, they're still alive — ignore it.
        with self._lock:
            last_udp = self._last_seen.get(peer_id, 0)
            if time.time() - last_udp < 45:   # within 1.5× beacon interval
                log.info(f"[discovery] ignoring spurious mDNS Removed for {peer_id}"
                         f" — seen via UDP {time.time()-last_udp:.0f}s ago")
                return
            removed = self._peers.pop(peer_id, None)

        if removed:
            log.info(f"[discovery] peer left: {removed}")
            if self.on_peer_removed:
                threading.Thread(target=self.on_peer_removed, args=(peer_id,), daemon=True).start()


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _get_local_ip() -> str:
    """Best-effort: get this machine's LAN IP (not 127.0.0.1)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


# ---------------------------------------------------------------------------
# Quick standalone test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import time

    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s  %(name)-12s  %(levelname)s  %(message)s')

    joined  = []
    left    = []

    d1 = Discovery('peer-AAA', 'Alice',   9001,
                   on_peer_added=lambda p: joined.append(p.peer_name),
                   on_peer_removed=lambda pid: left.append(pid))

    d2 = Discovery('peer-BBB', 'Bob',     9002,
                   on_peer_added=lambda p: joined.append(p.peer_name),
                   on_peer_removed=lambda pid: left.append(pid))

    d1.start()
    d2.start()

    print("[test] waiting for peers to discover each other...")
    time.sleep(3)

    print(f"[test] d1 sees: {d1.get_peers()}")
    print(f"[test] d2 sees: {d2.get_peers()}")

    assert d1.peer_count() >= 1, "d1 should see d2"
    assert d2.peer_count() >= 1, "d2 should see d1"
    assert 'Bob'   in joined
    assert 'Alice' in joined

    d2.stop()
    time.sleep(2)

    print(f"[test] after d2 stops, d1 sees: {d1.get_peers()}")
    assert d1.peer_count() == 0, "d1 should see d2 as gone"

    d1.stop()
    print("[test] discovery.py self-test passed.")
