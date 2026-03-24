#!/usr/bin/env python3
"""P2P File Converter — Basic Tkinter UI"""

import argparse, logging, queue, socket, ssl, threading, time, uuid
from pathlib import Path
from tkinter import *
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

from core.server    import PeerServer
from core.client    import submit_job
from core.discovery import Discovery
from core.converter import convert, detect_format, get_available_outputs, GPU_ENCODERS, set_gpu_accel, get_gpu_accel

# ── args & globals ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=0)
parser.add_argument('--name', type=str, default=socket.gethostname())
args = parser.parse_args()

PEER_ID   = str(uuid.uuid4())[:8]
PEER_NAME = args.name
TEMP_DIR  = Path(__file__).parent / 'temp'
DISCOVERY = SERVER = None
LOG_Q     = queue.Queue()
PEERS_Q   = queue.Queue()
FRAMES    = list('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
USE_TLS   = True    # on by default
_TCP_PORT = None    # set after server starts; needed by TLS toggle to restart on same port

# TLS cert paths — shared with v1_UI, auto-generated on first run
CERT_DIR  = Path(__file__).parent.parent / 'certs'
CERT_FILE = CERT_DIR / 'peer.crt'
KEY_FILE  = CERT_DIR / 'peer.key'

class _QH(logging.Handler):
    def emit(self, r):
        if r.levelno >= logging.INFO:
            LOG_Q.put(f"[{r.name}] {r.getMessage()}")

_qh = _QH()
for _n in ('server', 'client', 'discovery', 'converter'):
    _l = logging.getLogger(_n); _l.addHandler(_qh); _l.setLevel(logging.INFO)


# ── TLS helpers ──────────────────────────────────────────────────────────────

def _ensure_certs() -> bool:
    """Auto-generate a self-signed cert/key pair if not present. Returns True on success."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
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
        LOG_Q.put("TLS certs auto-generated")
        return True
    except Exception as e:
        LOG_Q.put(f"WARNING: Could not generate TLS certs ({e}) — running without TLS")
        return False


def _make_server_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    return ctx


def _make_client_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE   # self-signed; we only need encryption
    return ctx


# ── App ──────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"P2P File Converter — {PEER_NAME}")
        root.resizable(False, False)
        self._spinner_job    = None
        self._spinner_active = False
        self._files_list     = []
        self._result_path    = None
        self._pending        = 0
        self._done           = 0
        self._build_ui()
        self._start_backend()
        self._poll()

    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}

        # node info + TLS/GPU toggles
        top = Frame(self.root, bd=1, relief=SUNKEN)
        top.pack(fill=X, **pad)
        Label(top, text=f"Node: {PEER_NAME}   ID: {PEER_ID}", font=('Courier', 9)).pack(side=LEFT, padx=4, pady=2)

        # TLS toggle (right side of top bar)
        self._tls_var = BooleanVar(value=USE_TLS)
        self._tls_cb  = Checkbutton(top, text='TLS', variable=self._tls_var,
                                    font=('Arial', 9), fg='#2a7a2a',
                                    command=self._toggle_tls)
        self._tls_cb.pack(side=RIGHT, padx=6)

        # peers list
        pf = LabelFrame(self.root, text='Connected Peers', font=('Arial', 9, 'bold'))
        pf.pack(fill=X, **pad)
        self.peers_lb = Listbox(pf, height=3, font=('Courier', 9))
        self.peers_lb.pack(fill=X, padx=4, pady=4)

        # file picker
        ff = LabelFrame(self.root, text='Convert Files', font=('Arial', 9, 'bold'))
        ff.pack(fill=X, **pad)

        lr = Frame(ff); lr.pack(fill=X, padx=4, pady=4)
        self.files_lb = Listbox(lr, height=4, font=('Courier', 8), selectmode=EXTENDED, activestyle='none')
        sb = Scrollbar(lr, orient=VERTICAL, command=self.files_lb.yview)
        self.files_lb.config(yscrollcommand=sb.set)
        self.files_lb.pack(side=LEFT, fill=X, expand=True); sb.pack(side=RIGHT, fill=Y)

        br = Frame(ff); br.pack(fill=X, padx=4, pady=2)
        Button(br, text='Add Files',       font=('Arial', 9), command=self._browse         ).pack(side=LEFT, padx=2)
        Button(br, text='Remove Selected', font=('Arial', 9), command=self._remove_selected).pack(side=LEFT, padx=2)
        Button(br, text='Clear All',       font=('Arial', 9), command=self._clear_files    ).pack(side=LEFT, padx=2)

        row2 = Frame(ff); row2.pack(fill=X, padx=4, pady=2)
        Label(row2, text='From:', font=('Arial', 9)).pack(side=LEFT)
        self.from_var = StringVar(value='—')
        Label(row2, textvariable=self.from_var, font=('Arial', 9, 'bold'), fg='blue', width=10).pack(side=LEFT, padx=4)
        Label(row2, text='  →  To:', font=('Arial', 9)).pack(side=LEFT)
        self.to_var  = StringVar()
        self.to_menu = OptionMenu(row2, self.to_var, '')
        self.to_menu.config(font=('Arial', 9), width=8); self.to_menu.pack(side=LEFT, padx=4)

        btn_row2 = Frame(ff); btn_row2.pack(pady=6)
        self.convert_btn = Button(btn_row2, text='Convert All', font=('Arial', 10, 'bold'),
                                  bg='#4a90d9', fg='white', state=DISABLED, command=self._do_convert)
        self.convert_btn.pack(side=LEFT, padx=(0, 8))

        if GPU_ENCODERS:
            self._gpu_var = BooleanVar(value=False)
            gpu_label = GPU_ENCODERS[0].replace('h264_', '').upper()
            Checkbutton(btn_row2, text=f'GPU ({gpu_label})', variable=self._gpu_var,
                        font=('Arial', 9), command=lambda: set_gpu_accel(self._gpu_var.get())
                        ).pack(side=LEFT)

        # result row
        rf = Frame(ff); rf.pack(fill=X, padx=4, pady=(0, 6))
        self.result_var = StringVar()
        Label(rf, textvariable=self.result_var, font=('Courier', 8), fg='#2a7a2a',
              wraplength=380, justify=LEFT).pack(side=LEFT, fill=X, expand=True)
        self._open_btn = Button(rf, text='Open folder', font=('Arial', 8),
                                command=self._open_result_folder, state=DISABLED)
        self._open_btn.pack(side=RIGHT, padx=4)

        # log
        lf = LabelFrame(self.root, text='Log', font=('Arial', 9, 'bold'))
        lf.pack(fill=BOTH, expand=True, **pad)
        self.log_box = ScrolledText(lf, height=10, font=('Courier', 9),
                                    state=DISABLED, bg='#1e1e1e', fg='#d4d4d4')
        self.log_box.pack(fill=BOTH, expand=True, padx=4, pady=4)
        self._refresh_peers()

    def _start_backend(self):
        global DISCOVERY, SERVER, USE_TLS, _TCP_PORT
        TEMP_DIR.mkdir(exist_ok=True)
        if args.port == 0:
            with socket.socket() as s:
                s.bind(('', 0)); port = s.getsockname()[1]
        else:
            port = args.port
        _TCP_PORT = port

        # Auto-generate certs if needed; fall back to no-TLS on failure
        if USE_TLS:
            USE_TLS = _ensure_certs()
            self._tls_var.set(USE_TLS)

        ssl_ctx = _make_server_ssl_ctx() if USE_TLS else None
        SERVER  = PeerServer(host='', port=port, peer_id=PEER_ID, peer_name=PEER_NAME,
                             temp_dir=str(TEMP_DIR), ssl_context=ssl_ctx)
        SERVER.start()
        self._log(f"Server on port {port}  {'[TLS on]' if USE_TLS else '[TLS off]'}")

        DISCOVERY = Discovery(
            peer_id=PEER_ID, peer_name=PEER_NAME, port=port,
            tls=USE_TLS,
            on_peer_added   =lambda p: (LOG_Q.put(f"Peer joined: {p.peer_name} ({p.host}:{p.port})"
                                                  + (" [TLS]" if p.tls else "")), PEERS_Q.put(1)),
            on_peer_removed =lambda _: (LOG_Q.put("A peer left"), PEERS_Q.put(1)),
        )
        DISCOVERY.start()
        self._log(f"Announcing as '{PEER_NAME}'...")

    def _toggle_tls(self):
        """Restart the TCP server with TLS on/off and re-announce via mDNS."""
        new_tls = self._tls_var.get()
        if new_tls and not _ensure_certs():
            # Cert generation failed — revert checkbox
            self._tls_var.set(False)
            return
        # Run the restart in a background thread so the UI doesn't freeze
        threading.Thread(target=self._apply_tls, args=(new_tls,), daemon=True).start()

    def _apply_tls(self, new_tls: bool):
        global USE_TLS, SERVER
        USE_TLS = new_tls
        if SERVER and _TCP_PORT:
            SERVER.stop()
            time.sleep(0.3)
            ssl_ctx = _make_server_ssl_ctx() if USE_TLS else None
            SERVER  = PeerServer(host='', port=_TCP_PORT, peer_id=PEER_ID, peer_name=PEER_NAME,
                                 temp_dir=str(TEMP_DIR), ssl_context=ssl_ctx)
            SERVER.start()
        if DISCOVERY:
            DISCOVERY.set_tls(USE_TLS)
        LOG_Q.put(f"TLS {'enabled' if USE_TLS else 'disabled'}")

    def _poll(self):
        while not LOG_Q.empty():   self._log(LOG_Q.get_nowait())
        while not PEERS_Q.empty(): PEERS_Q.get_nowait(); self._refresh_peers()
        self.root.after(400, self._poll)

    def _log(self, msg):
        self.log_box.config(state=NORMAL)
        self.log_box.insert(END, f"[{time.strftime('%H:%M:%S')}]  {msg}\n")
        self.log_box.see(END); self.log_box.config(state=DISABLED)

    def _refresh_peers(self):
        peers = DISCOVERY.get_peers() if DISCOVERY else []
        self.peers_lb.delete(0, END)
        tls_tag = ' [TLS]' if USE_TLS else ''
        self.peers_lb.insert(END, f"● {PEER_NAME}  (this node){tls_tag}")
        for p in peers:
            tag = ' [TLS]' if p.tls else ''
            self.peers_lb.insert(END, f"● {p.peer_name}  ({p.host}:{p.port}){tag}")

    # spinner
    def _start_spinner(self, label):
        self._spinner_label  = label
        self._spinner_start  = time.perf_counter()
        self._spinner_frame  = 0
        self._spinner_active = True
        self.log_box.config(state=NORMAL)
        self.log_box.insert(END, '\n')
        self.log_box.config(state=DISABLED)
        self._spinner_idx = self.log_box.index('end-2l')
        self._tick_spinner()

    def _tick_spinner(self):
        if not self._spinner_active: return
        elapsed = time.perf_counter() - self._spinner_start
        frame   = FRAMES[self._spinner_frame % len(FRAMES)]
        self._spinner_frame += 1
        self.log_box.config(state=NORMAL)
        self.log_box.delete(self._spinner_idx, f"{self._spinner_idx} lineend")
        self.log_box.insert(self._spinner_idx, f"{frame}  {self._spinner_label}  ({elapsed:.1f}s)")
        self.log_box.see(END); self.log_box.config(state=DISABLED)
        self._spinner_job = self.root.after(120, self._tick_spinner)

    def _stop_spinner(self):
        self._spinner_active = False
        if self._spinner_job: self.root.after_cancel(self._spinner_job); self._spinner_job = None
        self.log_box.config(state=NORMAL)
        self.log_box.delete(self._spinner_idx, f"{self._spinner_idx} lineend+1c")
        self.log_box.config(state=DISABLED)

    # file list management
    def _browse(self):
        for p in filedialog.askopenfilenames(title='Select files'):
            if p not in self._files_list:
                self._files_list.append(p)
                self.files_lb.insert(END, Path(p).name)
        self._refresh_format_menu()

    def _remove_selected(self):
        for i in reversed(self.files_lb.curselection()):
            self.files_lb.delete(i); del self._files_list[i]
        self._refresh_format_menu()

    def _clear_files(self):
        self._files_list.clear(); self.files_lb.delete(0, END)
        self._refresh_format_menu()

    def _refresh_format_menu(self):
        if not self._files_list:
            self.from_var.set('—'); self.to_var.set('')
            self.to_menu['menu'].delete(0, END); self.convert_btn.config(state=DISABLED)
            return
        ext     = detect_format(self._files_list[0])
        outputs = get_available_outputs(ext)
        self.from_var.set(ext.upper())
        menu = self.to_menu['menu']; menu.delete(0, END)
        for o in outputs:
            menu.add_command(label=o.upper(), command=lambda v=o: (self.to_var.set(v), self.convert_btn.config(state=NORMAL)))
        if outputs: self.to_var.set(outputs[0]); self.convert_btn.config(state=NORMAL)
        else:       self.convert_btn.config(state=DISABLED); messagebox.showwarning('Unsupported', f'No conversions for .{ext}')

    def _open_result_folder(self):
        import subprocess
        if self._result_path:
            try:    subprocess.Popen(['explorer', '/select,', self._result_path])
            except: pass

    # conversion
    def _do_convert(self):
        out_fmt = self.to_var.get().lower()
        if not self._files_list or not out_fmt: return
        self.result_var.set(''); self._open_btn.config(state=DISABLED)
        self._result_path = None; self._pending = len(self._files_list); self._done = 0
        self.convert_btn.config(state=DISABLED, text='Converting...')
        n = len(self._files_list)
        self._start_spinner(f"Converting {n} file{'s' if n > 1 else ''} → {out_fmt.upper()}")
        for p in list(self._files_list):
            threading.Thread(target=self._convert_worker, args=(p, out_fmt), daemon=True).start()

    def _convert_worker(self, path, out_fmt):
        t0       = time.perf_counter()
        fname    = Path(path).name
        save_dir = str(Path(path).parent)
        result   = None

        peers = DISCOVERY.get_peers() if DISCOVERY else []
        for peer in peers:
            tls_tag = ' [TLS]' if peer.tls else ''
            LOG_Q.put(f"Attempting {peer.peer_name} ({peer.host}:{peer.port}){tls_tag}...")
            try:
                result = submit_job(
                    peer_host=peer.host, peer_port=peer.port,
                    input_path=path, output_format=out_fmt,
                    output_dir=save_dir, this_peer_id=PEER_ID, this_peer_name=PEER_NAME,
                    use_gpu=get_gpu_accel(),
                    ssl_context=_make_client_ssl_ctx() if peer.tls else None,
                )
                LOG_Q.put(f"Got result from {peer.peer_name}: {result.name} ({result.stat().st_size/1e6:.2f} MB) in {time.perf_counter()-t0:.2f}s")
                break  # success — stop trying more peers
            except Exception as e:
                LOG_Q.put(f"{peer.peer_name} failed ({type(e).__name__}: {e}), trying next peer...")
                result = None

        if result is None:
            LOG_Q.put(f"All peers failed or unavailable — converting '{fname}' → {out_fmt.upper()} locally...")
            try:
                result = convert(path, out_fmt, save_dir)
                LOG_Q.put(f"Done in {time.perf_counter()-t0:.2f}s → {result.name}")
            except Exception as e:
                LOG_Q.put(f"ERROR: {fname}: {e}")
                result = None

        saved = str(result) if result else None
        def _finish():
            self._pending -= 1
            if saved:
                self._done += 1; self._result_path = saved
                if self._pending > 0:
                    LOG_Q.put(f"✔ {Path(saved).name}  ({self._pending} remaining)")
            if self._pending <= 0:
                self._stop_spinner()
                self.convert_btn.config(state=NORMAL, text='Convert All')
                if self._done:
                    n = self._done
                    self.result_var.set(f"✔ {n} file{'s' if n>1 else ''} saved in {Path(self._result_path).parent}")
                    self._open_btn.config(state=NORMAL)
        self.root.after(0, _finish)

    def on_close(self):
        if DISCOVERY: DISCOVERY.stop()
        if SERVER:    SERVER.stop()
        self.root.destroy()


if __name__ == '__main__':
    root = Tk()
    app  = App(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()
