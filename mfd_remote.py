#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "PySide6",
#     "python-vlc",
#     "zeroconf>=0.130",
# ]
# ///
"""
mfd_remote.py — recopie d'écran + télécommande tactile d'un MFD Raymarine.

Réunit les trois briques du projet :
  - VIDÉO  : flux RTSP du MFD (rtsp://IP:8554/RAYMARINEMFD) affiché via libVLC.
  - CONTRÔLE : les touchers (souris + pinch trackpad) sont convertis en trames
    RRCE et poussés sur TCP 50000 (cf. rrce_touch.py).
  - DÉCOUVERTE : sans IP, résout d'abord le MFD en mDNS (_rym_rrc._tcp /
    _rtsp._tcp) ; à défaut, écoute le multicast 224.0.0.1:5800 et récupère l'IP
    SOURCE de l'annonce du MFD (cf. raydb_client.py / dissectors/raymarine_5800.lua).

Mapping des coordonnées : LINÉAIRE PLEIN ÉCRAN.
    (0,0)         = coin supérieur gauche
    (65535,65535) = coin inférieur droit
Donc norm = fraction_écran * 65535 par axe (le rectangle vidéo réel est pris en
compte pour ignorer le letterbox / bandes noires).

Dépendances : PySide6, python-vlc (+ VLC.app).
Usage :
    ./mfd_remote.py                 # découverte auto du MFD
    ./mfd_remote.py 192.168.42.1    # IP imposée
    ./mfd_remote.py 192.168.42.1 --url rtsp://192.168.111.1:8554/RAYMARINEMFD
"""
import argparse
import socket
import struct
import sys
import threading
import time

import vlc
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

# Trames RRCE : on réutilise l'assembleur éprouvé de rrce_touch.py.
from rrce_touch import FULL, OP_DOWN, OP_MOVE, OP_UP, RRCE_PORT, build_touch

DISCOVERY_GROUP = "224.0.0.1"
DISCOVERY_PORT = 5800

# Services mDNS Raymarine interrogés avant le beacon 5800 : leur enregistrement
# porte déjà l'IP joignable du MFD (on ignore le port annoncé). mfd_remote pilote
# le RRCE (_rym_rrc) et affiche le RTSP (_rtsp), d'où ces deux types.
MDNS_SERVICES = ["_rym_rrc._tcp.local.", "_rtsp._tcp.local."]
MDNS_TIMEOUT = 5.0                # fenêtre de résolution mDNS avant repli sur 5800

OP_NAME = {OP_DOWN: "DOWN", OP_MOVE: "MOVE", OP_UP: "UP"}


def log(msg):
    """Trace sur stderr (visible dans le terminal qui lance l'app)."""
    print(msg, file=sys.stderr, flush=True)


def frac_to_norm(fx, fy):
    """Fraction d'écran (0..1) → coordonnée normalisée RRCE (0..65535).
    Mapping linéaire plein écran : 0→coin haut-gauche, 65535→coin bas-droite."""
    nx = max(0, min(FULL, round(fx * FULL)))
    ny = max(0, min(FULL, round(fy * FULL)))
    return nx, ny


# ============================================================ découverte =====
def parse_5800(payload):
    """Décode une annonce de découverte Raymarine (UDP 5800). Repris de
    raydb_client.py : en-tête 32 oct., MFD = descriptor à octets hauts non nuls."""
    if len(payload) < 32:
        return None
    mtype = struct.unpack_from("<I", payload, 0)[0]
    if mtype not in (1, 2):
        return None
    descriptor = struct.unpack_from("<I", payload, 12)[0]
    name = payload[20:32].split(b"\0")[0].decode("latin1", "replace")
    return {"name": name, "is_mfd": (descriptor & 0xFFFFFF00) != 0}


def _mdns_label(info, default):
    """Libellé lisible d'un service (modèle du TXT si présent, sinon `default`)."""
    props = getattr(info, "decoded_properties", None) or {}
    return props.get("raymarine-mfd-model") or default


def discover_via_mdns(timeout, is_stopped=None):
    """Cherche les services Raymarine (_rym_rrc._tcp, _rtsp._tcp) en mDNS et
    renvoie (ip, label) de la première instance résolue — l'IPv4 seule, **sans le
    port**. None si zeroconf est absent, si rien n'est annoncé dans le délai, ou
    si `is_stopped()` devient vrai."""
    try:
        from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
    except ImportError:
        return None

    found = []

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            found.append((service_type, name))

    zc = Zeroconf()
    seen = set()
    try:
        ServiceBrowser(zc, MDNS_SERVICES, handlers=[on_change])
        deadline = time.time() + timeout
        while time.time() < deadline and not (is_stopped and is_stopped()):
            for entry in found:
                if entry in seen:
                    continue
                seen.add(entry)
                info = zc.get_service_info(entry[0], entry[1], timeout=1500)
                if info is None:
                    continue
                for addr in info.parsed_addresses():
                    if ":" not in addr:            # IPv4 seulement
                        return addr, _mdns_label(info, entry[1].split(".", 1)[0])
            time.sleep(0.2)
        return None
    finally:
        zc.close()


class DiscoveryThread(QThread):
    """Découvre le MFD : d'abord en mDNS (_rym_rrc._tcp / _rtsp._tcp), puis à
    défaut en écoutant le multicast 5800. Émet l'IP du MFD (found)."""
    found = Signal(str, str)          # (ip, label)
    status = Signal(str)

    def run(self):
        # 1) mDNS d'abord : si un service _rym_rrc._tcp / _rtsp._tcp répond, on
        #    prend son IP (sans le port) et on NE lit PAS le multicast 5800.
        self.status.emit("découverte : résolution mDNS (_rym_rrc._tcp, _rtsp._tcp)…")
        hit = discover_via_mdns(MDNS_TIMEOUT, self.isInterruptionRequested)
        if hit is not None:
            ip, label = hit
            self.found.emit(ip, label)
            return
        if self.isInterruptionRequested():
            return

        # 2) Repli : le beacon multicast 5800 (IP source de l'annonce du MFD).
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            sock.bind(("", DISCOVERY_PORT))
            mreq = struct.pack("=4sl", socket.inet_aton(DISCOVERY_GROUP),
                               socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            self.status.emit(f"écoute 5800 impossible : {e}")
            return
        self.status.emit("découverte : écoute du multicast 224.0.0.1:5800…")
        sock.settimeout(1.0)
        fallback = None
        while not self.isInterruptionRequested():
            try:
                data, addr = sock.recvfrom(2048)
            except TimeoutError:
                continue
            info = parse_5800(data)
            if not info:
                continue
            src = addr[0]
            label = info["name"] or src
            if info["is_mfd"]:
                self.found.emit(src, label)
                sock.close()
                return
            if fallback is None:
                fallback = src
                self.status.emit(f"annonce {label} depuis {src}… (pas un MFD)")
        sock.close()


# ============================================================= lien RRCE =====
class RrceLink:
    """Connexion TCP persistante au MFD (port 50000). Un thread de fond
    (re)connecte ; send() pousse des octets sous verrou, ou les jette si
    déconnecté (le tactile est temps-réel : mieux vaut perdre un point que
    bloquer l'UI)."""
    def __init__(self, on_status=None):
        self.host = None
        self.sock = None
        self.lock = threading.Lock()
        self.on_status = on_status
        self.connected = False
        self.sent = 0                       # nb de trames effectivement envoyées
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def set_host(self, host):
        with self.lock:
            self.host = host
            self._drop_locked()

    def _drop_locked(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.connected = False

    def _loop(self):
        while not self._stop.is_set():
            with self.lock:
                host, connected = self.host, self.connected
            if host and not connected:
                try:
                    s = socket.create_connection((host, RRCE_PORT), timeout=4)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    with self.lock:
                        self.sock = s
                        self.connected = True
                    self._status(f"RRCE connecté à {host}:{RRCE_PORT}")
                    log(f"[lien] connecté à {host}:{RRCE_PORT}")
                except OSError as e:
                    self._status(f"RRCE : reconnexion… ({e})")
                    log(f"[lien] échec connexion {host}:{RRCE_PORT} : {e}")
                    time.sleep(1.5)
            else:
                time.sleep(0.3)

    def send(self, frame):
        with self.lock:
            if not self.sock:
                return
            try:
                self.sock.sendall(frame)
                self.sent += len(frame) // 15
            except OSError as e:
                self._drop_locked()
                self._status(f"RRCE : lien perdu ({e})")
                log(f"[lien] perdu : {e}")

    def _status(self, msg):
        if self.on_status:
            self.on_status(msg)

    def stop(self):
        self._stop.set()
        with self.lock:
            self._drop_locked()


# =========================================================== overlay tactile =
class TouchOverlay(QWidget):
    """Widget translucide au-dessus de la vidéo : capte souris + pinch trackpad
    et convertit en trames RRCE (mapping linéaire plein écran)."""
    _PINCH_D0 = 0.12                     # demi-écartement initial des 2 doigts

    def __init__(self, win):
        super().__init__(win.video_frame)
        self.win = win
        self.link = win.link
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setFocusPolicy(Qt.StrongFocus)
        self._down = False
        self._pinch = False
        self._pinch_scale = 1.0
        self._pinch_center = QPointF()
        self._last_pos = QPointF()
        self._move_n = 0                     # throttle du log des MOVE

    # ---- géométrie : rectangle vidéo réel dans le widget (letterbox) --------
    def video_rect(self):
        """Rectangle où la vidéo est réellement dessinée (aspect conservé)."""
        W, H = self.width(), self.height()
        vw, vh = self.win.video_size()
        if not vw or not vh or not W or not H:
            return QRectF(0, 0, W, H)
        scale = min(W / vw, H / vh)
        dw, dh = vw * scale, vh * scale
        return QRectF((W - dw) / 2, (H - dh) / 2, dw, dh)

    def pos_to_frac(self, pos):
        """Point widget → fraction (0..1) dans le rectangle vidéo, ou None si
        hors de la vidéo (bandes noires)."""
        r = self.video_rect()
        if not r.contains(pos):
            return None
        fx = (pos.x() - r.left()) / r.width()
        fy = (pos.y() - r.top()) / r.height()
        return fx, fy

    def send_touch(self, op, finger, fx, fy):
        nx, ny = frac_to_norm(fx, fy)
        self.link.send(build_touch(op, finger, nx, ny))
        # log DOWN/UP toujours, MOVE 1 sur 8 (évite le spam)
        if op != OP_MOVE:
            log(f"  → {OP_NAME.get(op, op):4} f{finger} ({nx:5d},{ny:5d})  "
                f"lien={'OK' if self.link.connected else 'HORS-LIGNE'} envois={self.link.sent}")
        else:
            self._move_n += 1
            if self._move_n % 8 == 0:
                log(f"  → MOVE f{finger} ({nx:5d},{ny:5d})  envois={self.link.sent}")

    # ---- souris = doigt 0 ---------------------------------------------------
    def mousePressEvent(self, e):
        frac = self.pos_to_frac(e.position())
        log(f"[souris] PRESS @ {e.position().x():.0f},{e.position().y():.0f} "
            f"→ frac={frac}")
        if frac is None:
            return
        self._down = True
        self._last_pos = e.position()
        self.send_touch(OP_DOWN, 0, *frac)

    def mouseMoveEvent(self, e):
        self.win.update_cursor_readout(e.position())
        if not self._down:
            return
        frac = self.pos_to_frac(e.position())
        if frac is None:
            return
        self.send_touch(OP_MOVE, 0, *frac)

    def mouseReleaseEvent(self, e):
        log(f"[souris] RELEASE (down={self._down})")
        if not self._down:
            return
        self._down = False
        frac = self.pos_to_frac(e.position()) or self.pos_to_frac(self._last_pos)
        if frac:
            self.send_touch(OP_UP, 0, *frac)

    # ---- touche de test : tap au centre, sans passer par la souris ----------
    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_T, Qt.Key_Space):
            log("[test] tap au centre (0.5,0.5) demandé au clavier")
            self.send_touch(OP_DOWN, 0, 0.5, 0.5)
            self.send_touch(OP_UP, 0, 0.5, 0.5)
            self.win.set_status(
                f"TEST tap centre — connecté={self.link.connected}, "
                f"{self.link.sent} trames envoyées au total")
        else:
            super().keyPressEvent(e)

    # ---- pinch trackpad natif (macOS) = doigts 0 et 1 -----------------------
    def event(self, e):
        if e.type() == e.Type.NativeGesture:
            gt = e.gestureType()
            if gt == Qt.NativeGestureType.BeginNativeGesture:
                self._begin_pinch(e.position())
                return True
            if gt == Qt.NativeGestureType.ZoomNativeGesture:
                self._update_pinch(e.value())
                return True
            if gt == Qt.NativeGestureType.EndNativeGesture:
                self._end_pinch()
                return True
        return super().event(e)

    def _pinch_positions(self):
        """Positions (frac) des 2 doigts autour du centre, écartement courant."""
        d = max(0.03, min(0.47, self._PINCH_D0 * self._pinch_scale))
        cx, cy = self._pinch_center.x(), self._pinch_center.y()
        h = d / (2 ** 0.5)                       # composantes diagonales
        f0 = (max(0.0, min(1.0, cx - h)), max(0.0, min(1.0, cy - h)))
        f1 = (max(0.0, min(1.0, cx + h)), max(0.0, min(1.0, cy + h)))
        return f0, f1

    def _pinch_frame(self, op):
        f0, f1 = self._pinch_positions()
        n0 = frac_to_norm(*f0)
        n1 = frac_to_norm(*f1)
        # deux records dans le même envoi TCP (batch multi-doigts)
        return build_touch(op, 0, *n0) + build_touch(op, 1, *n1)

    def _begin_pinch(self, pos):
        frac = self.pos_to_frac(pos)
        if frac is None:
            return
        self._pinch = True
        self._pinch_scale = 1.0
        self._pinch_center = QPointF(*frac)
        self.link.send(self._pinch_frame(OP_DOWN))

    def _update_pinch(self, delta):
        if not self._pinch:
            return
        self._pinch_scale *= (1.0 + float(delta))
        self._pinch_scale = max(0.2, min(4.0, self._pinch_scale))
        self.link.send(self._pinch_frame(OP_MOVE))

    def _end_pinch(self):
        if not self._pinch:
            return
        self._pinch = False
        self.link.send(self._pinch_frame(OP_UP))


# =============================================================== fenêtre =====
class MainWindow(QMainWindow):
    _status_signal = Signal(str)

    def __init__(self, host, url_tpl):
        super().__init__()
        self.setWindowTitle("Raymarine — recopie & télécommande")
        self.resize(1280, 800)
        self.url_tpl = url_tpl
        self.host = None

        self.link = RrceLink(on_status=self._status_signal.emit)
        self._status_signal.connect(self.set_status)

        # VLC
        self.vlc = vlc.Instance("--no-xlib", "--quiet", "--no-audio")
        self.player = self.vlc.media_player_new()

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.video_frame = QFrame()
        self.video_frame.setStyleSheet("background:#000;")
        lay.addWidget(self.video_frame, 1)
        self.status = QLabel("démarrage…")
        self.status.setStyleSheet(
            "background:#111;color:#ddd;padding:4px 8px;font-family:Menlo;font-size:12px;")
        lay.addWidget(self.status)
        self.setCentralWidget(central)

        self.overlay = TouchOverlay(self)
        self.overlay.setGeometry(self.video_frame.rect())
        self.overlay.raise_()

        self._bind_vlc_output()

        if host:
            self.connect_to(host)
        else:
            self.disco = DiscoveryThread()
            self.disco.found.connect(self._on_discovered)
            self.disco.status.connect(self.set_status)
            self.disco.start()

    # ---- VLC surface --------------------------------------------------------
    def _bind_vlc_output(self):
        wid = int(self.video_frame.winId())
        if sys.platform == "darwin":
            self.player.set_nsobject(wid)
        elif sys.platform.startswith("win"):
            self.player.set_hwnd(wid)
        else:
            self.player.set_xwindow(wid)

    def video_size(self):
        try:
            w, h = self.player.video_get_size(0)
            return int(w), int(h)
        except Exception:
            return 0, 0

    def connect_to(self, host):
        self.host = host
        url = self.url_tpl.format(ip=host)
        self.link.set_host(host)
        media = self.vlc.media_new(url)
        media.add_option(":network-caching=200")   # latence basse
        media.add_option(":rtsp-tcp")               # RTSP sur TCP (fiable)
        media.add_option(":no-audio")
        self.player.set_media(media)
        self.player.play()
        self.set_status(f"MFD {host} — vidéo {url}")

    def _on_discovered(self, ip, label):
        self.set_status(f"MFD découvert : {ip} ({label})")
        self.connect_to(ip)

    # ---- UI -----------------------------------------------------------------
    def set_status(self, msg):
        self.status.setText(msg)

    def update_cursor_readout(self, pos):
        frac = self.overlay.pos_to_frac(pos)
        link = ("● connecté" if self.link.connected else "○ HORS-LIGNE")
        if frac:
            nx, ny = frac_to_norm(*frac)
            self.status.setText(
                f"{link} · {self.link.sent} envois | MFD {self.host} — "
                f"curseur ({nx},{ny}) [{frac[0]*100:.0f}%,{frac[1]*100:.0f}%] "
                f"| T=tap test")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.overlay.setGeometry(self.video_frame.rect())

    def closeEvent(self, e):
        try:
            if hasattr(self, "disco"):
                self.disco.requestInterruption()
                self.disco.wait(1500)
        except Exception:
            pass
        self.player.stop()
        self.link.stop()
        super().closeEvent(e)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="?", help="IP du MFD ; si omis, découverte auto 5800")
    ap.add_argument("--url", default="rtsp://{ip}:8554/RAYMARINEMFD",
                    help="gabarit d'URL RTSP ({ip} = IP du MFD)")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    win = MainWindow(args.host, args.url)
    win.show()
    win.overlay.setFocus()                  # capte les touches (T=tap test) d'emblée
    log("[app] démarrée. Clique/glisse sur la vidéo pour piloter ; "
        "T ou Espace = tap de test au centre. Les touchers émis sont tracés ici.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
