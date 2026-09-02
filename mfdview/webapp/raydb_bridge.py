#!/usr/bin/env python3
"""
raydb_bridge.py — passerelle RayDB → navigateur (HTTP + Server-Sent Events).

Un navigateur ne peut ni ouvrir une socket TCP brute (donc pas de RayDB 23333)
ni faire du mDNS/multicast : la page ne peut pas parler au MFD directement. Ce
programme fait les deux à sa place et expose les valeurs de navigation en HTTP :

    GET /                 l'application (static/, une page autonome)
    GET /api/state        instantané JSON de toutes les valeurs connues
    GET /api/stream       flux SSE : « snapshot » puis « delta » et « status »

Usage :
    ./raydb_bridge.py                     # MFD sur 127.0.0.1 (simulateur mfdsim)
    ./raydb_bridge.py 192.168.42.1        # MFD réel, IP imposée
    ./raydb_bridge.py auto                # découverte mDNS puis beacon 5800
    ./raydb_bridge.py --http-port 8080 --bind 0.0.0.0

Mise au point sans MFD, dans un autre terminal :
    python3 -m mfdsim --no-mdns --no-5800 --no-rtsp --no-ssh --no-8182

Pourquoi SSE et pas WebSocket : le flux est unidirectionnel (le MFD pousse, la
page affiche), SSE tient en bibliothèque standard — pas de framing à écrire, pas
de dépendance — et `EventSource` se reconnecte tout seul, ce qui compte sur un
téléphone qui met la page en veille dès que l'écran s'éteint.

Unités : celles de RayDB, telles quelles (angles en **radians**, vitesses en
**m/s**, profondeurs en **mètres**). La conversion nœuds/degrés est faite à
l'affichage, pour garder un seul jeu d'unités sur le fil.
"""
import argparse
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import raydb_client as rdb
import raydb_decode

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Chemins RayDB retenus → clé JSON. Tout le reste de data/# est ignoré : la page
# n'affiche que la navigation, et un delta court traverse mieux un lien Wi-Fi
# médiocre. `data/position` est une chaîne "lat,lon", éclatée en deux clés.
NAV_PATHS = {
    "data/sog": "sog",                        # m/s
    "data/cog": "cog",                        # rad, référencé au nord
    "data/heading/true": "hdg",               # rad
    "data/heading/magnetic": "hdgMag",        # rad
    "data/bearing/variation": "variation",    # rad
    "data/wind/speed/true": "tws",            # m/s
    "data/wind/direction/true": "twa",        # rad, **relatif à l'étrave**
    "data/wind/speed/apparent": "aws",        # m/s
    "data/wind/direction/apparent": "awa",    # rad, **relatif à l'étrave**
    "data/position": "position",              # "lat,lon"
    "data/position/accuracy": "posAcc",       # m
    "data/depth": "depth",                    # m, sous la sonde
    # Hors de data/# : demande son propre abonnement (voir SUBSCRIPTIONS).
    "Settings/Data/-/7/13/-/-/-/-": "boat",   # nom du bateau, chaîne
}

# Chemin des données du bateau
DATA_PATH = "data/#"

# Chemin du nom du bateau, souscrit en plus de l'arbre de navigation. Valeur
# « retained » : le MFD la pousse une fois à l'abonnement, puis plus jamais.
BOAT_NAME_PATH = "Settings/Data/-/7/13/-/-/-/-"

BROADCAST_PERIOD = 0.2      # cadence de diffusion : 5 Hz suffit à l'œil
SSE_PING_PERIOD = 15.0      # commentaire de maintien en vie du flux


# ------------------------------------------------------------------ hub -----
class Hub:
    """État courant + diffusion aux navigateurs connectés.

    Les updates RayDB arrivent par rafales (plusieurs dizaines par seconde) : on
    les accumule dans `pending` et on ne diffuse qu'à BROADCAST_PERIOD, ce qui
    coalesce les valeurs et évite d'inonder le téléphone.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.values = {}                 # clé → valeur (dernier état connu)
        self.pending = {}                # clé → valeur, à diffuser au prochain tour
        self.status = {"text": "démarrage…", "target": None, "connected": False}
        self.clients = []                # queue.Queue par navigateur connecté

    def publish(self, key, value):
        with self.lock:
            if self.values.get(key) == value:
                return                   # valeur inchangée : rien à diffuser
            self.values[key] = value
            self.pending[key] = value

    def set_status(self, text, target=None, connected=False):
        with self.lock:
            self.status = {"text": text, "target": target, "connected": connected}
            status = dict(self.status)
        self._send_all("status", status)

    def subscribe(self):
        q = queue.Queue(maxsize=64)
        with self.lock:
            self.clients.append(q)
            q.put_nowait(("snapshot", dict(self.values)))
            q.put_nowait(("status", dict(self.status)))
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def _send_all(self, event, payload):
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait((event, payload))
            except queue.Full:
                # Client trop lent (téléphone en veille) : on le lâche, son
                # EventSource se reconnectera et repartira d'un snapshot.
                self.unsubscribe(q)

    def broadcast_loop(self, stop):
        while not stop.is_set():
            time.sleep(BROADCAST_PERIOD)
            with self.lock:
                delta, self.pending = self.pending, {}
            if delta:
                self._send_all("delta", delta)


# --------------------------------------------------------------- RayDB ------
def decode_update(fr):
    """(chemin, valeur native) d'une trame RayDB, ou None si ce n'est pas un
    UPDATE exploitable."""
    if len(fr) < 17 or fr[8] != 4:                       # 4 = UPDATE
        return None
    plen = struct.unpack_from("<I", fr, 9)[0]
    path = fr[17:17 + plen].split(b"\0")[0].decode("latin1", "replace")
    typed = raydb_decode.decode_typed(fr[17 + plen:])
    return None if typed is None else (path, typed[1])


def apply_update(hub, path, value):
    """Range une valeur RayDB dans le hub sous sa clé JSON, si elle nous intéresse."""
    key = NAV_PATHS.get(path)
    if key is None:
        return
    if key == "position":
        lat, _, lon = str(value).partition(",")
        try:
            hub.publish("lat", float(lat))
            hub.publish("lon", float(lon))
        except ValueError:
            pass
        return
    hub.publish(key, value)


def reader_thread(target, hub, stop, discover_timeout=15):
    """Découverte éventuelle → connexion RayDB → lecture, avec reconnexion.

    Calque de rdb.reader_thread, mais les valeurs sont gardées **typées**
    (decode_typed) au lieu d'être formatées en texte : la page a besoin de
    nombres pour faire tourner une aiguille.
    """
    while not stop.is_set():
        ip = target
        if ip == "auto":
            hub.set_status("découverte du MFD (mDNS puis multicast 5800)…")
            ip = rdb.discover_mfd(rdb.State(), stop, discover_timeout)
            if ip is None:
                hub.set_status("aucun MFD trouvé — nouvel essai…")
                continue
        try:
            hub.set_status(f"connexion à {ip}:{rdb.RAYDB_PORT}…", ip)
            s = socket.create_connection((ip, rdb.RAYDB_PORT), timeout=5)
            s.settimeout(1.0)
            s.sendall(rdb.build_hello())
            for sub in (DATA_PATH, BOAT_NAME_PATH):
                s.sendall(rdb.build_subscribe(sub))
            hub.set_status(f"connecté à {ip}:{rdb.RAYDB_PORT}", ip, True)
            buf = b""
            while not stop.is_set():
                try:
                    data = s.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    break
                buf += data
                frames, buf = rdb.parse_frames(buf)
                for fr in frames:
                    update = decode_update(fr)
                    if update:
                        apply_update(hub, *update)
            s.close()
            hub.set_status(f"{ip} a fermé la connexion — reconnexion…", ip)
        except OSError as e:
            hub.set_status(f"{ip} : {e} — reconnexion…", ip)
        for _ in range(20):                       # 2 s, interruptibles
            if stop.is_set():
                return
            time.sleep(0.1)


# ---------------------------------------------------------------- HTTP ------
class Handler(SimpleHTTPRequestHandler):
    """Fichiers de static/ + les deux points d'entrée JSON."""

    protocol_version = "HTTP/1.1"
    hub = None
    verbose = False

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/stream":
            return self.serve_stream()
        if route == "/api/state":
            with self.hub.lock:
                body = {"status": dict(self.hub.status), "values": dict(self.hub.values)}
            return self.serve_json(body)
        return super().do_GET()

    def serve_json(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def serve_stream(self):
        """Flux SSE : snapshot à la connexion, puis deltas et statuts.

        La réponse n'a pas de longueur connue : on force la fermeture en fin de
        flux (`close_connection`) plutôt que de laisser HTTP/1.1 attendre une
        requête suivante sur une connexion qu'on a monopolisée.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")     # pas de tampon si reverse proxy
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        q = self.hub.subscribe()
        try:
            while True:
                try:
                    event, payload = q.get(timeout=SSE_PING_PERIOD)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")     # garde le flux ouvert
                else:
                    self.wfile.write(
                        f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                        # onglet fermé : normal
        finally:
            self.hub.unsubscribe(q)

    def end_headers(self):
        # La page est développée en direct : pas de cache navigateur, sinon un
        # rechargement sert l'ancien app.js. Le service worker, lui, garde une
        # copie pour le hors-ligne (voir static/sw.js).
        if self.path.startswith("/api/") is False:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        if self.verbose:
            super().log_message(fmt, *args)


def lan_ip():
    """IP de l'interface qui sort du Mac, pour afficher une URL utile au téléphone."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))          # TEST-NET-1 : aucun paquet n'est émis
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="Passerelle RayDB → navigateur (SSE).")
    ap.add_argument("mfd", nargs="?", default="127.0.0.1",
                    help="IP du MFD, ou « auto » pour la découverte (défaut : 127.0.0.1)")
    ap.add_argument("--http-port", type=int, default=8080, help="port HTTP (défaut 8080)")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="adresse d'écoute HTTP (défaut 0.0.0.0 : joignable du téléphone)")
    ap.add_argument("-v", "--verbose", action="store_true", help="journal des requêtes HTTP")
    args = ap.parse_args()

    hub = Hub()
    stop = threading.Event()
    threading.Thread(target=hub.broadcast_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=reader_thread, args=(args.mfd, hub, stop),
                     daemon=True).start()

    handler = partial(Handler, directory=STATIC_DIR)
    Handler.hub, Handler.verbose = hub, args.verbose
    httpd = ThreadingHTTPServer((args.bind, args.http_port), handler)
    httpd.daemon_threads = True
    print(f"MFD      : {args.mfd}")
    print(f"page     : http://127.0.0.1:{args.http_port}/")
    if args.bind != "127.0.0.1":
        print(f"téléphone: http://{lan_ip()}:{args.http_port}/   (même Wi-Fi)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
