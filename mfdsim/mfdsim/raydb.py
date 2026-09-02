"""
raydb.py — serveur RayDB (TCP 23333) : le bus publish/subscribe du MFD.

Implémente le côté serveur de « 2. protocole-raydb-23333.md » : le client
s'annonce (HELLO op7), souscrit à des sous-arbres (SUBSCRIBE op3), et le MFD
pousse l'état courant puis les changements (UPDATE op4). Le keepalive op5 du
client est ré-écho en op6 avec le même compteur.

Encodage : little-endian partout. Une trame vaut

    [u32 len][u32 msg_type=1][u8 op][u32 path_len][u32 pad][path][bloc valeur]

où `len` couvre tout ce qui suit le champ `len` lui-même. Plusieurs trames
peuvent tenir dans un segment TCP et une trame peut être scindée : le serveur
bufferise et ne décode qu'une fois `4 + len` octets disponibles, exactement
comme l'exige la spec côté client.
"""
from __future__ import annotations

import logging
import socket
import socketserver
import struct
import threading
import time

from . import config
from .sim import T_DOUBLE, T_FLOAT, T_STRING, Simulation

log = logging.getLogger("raydb")

MSG_TYPE = 1
OP_SUBSCRIBE = 3
OP_UPDATE = 4
OP_KEEPALIVE = 5
OP_KEEPALIVE_ACK = 6
OP_HELLO = 7

T_BOOL = 0x00
T_U32 = 0x07
T_NAMED = 0x0E

HEADER_LEN = 17          # len..pad inclus
MAX_FRAME = 1 << 20      # garde-fou : aucune trame légitime n'approche 1 Mo


# ------------------------------------------------------------- encodage ------
def _frame(op: int, path: str, value_block: bytes) -> bytes:
    """Assemble une trame RayDB complète."""
    path_b = path.encode("latin1")
    body = (struct.pack("<I", MSG_TYPE)
            + bytes([op])
            + struct.pack("<I", len(path_b))
            + struct.pack("<I", 0)              # pad / flags
            + path_b
            + value_block)
    return struct.pack("<I", len(body)) + body


def _typed_payload(vtype: int, value: object) -> bytes:
    """Sérialise une valeur selon son type RayDB, sans le padding de fin.

    Le payload suit immédiatement son type : seules les chaînes portent une
    longueur, et c'est le [u64 len] du type 0x0b lui-même.
    """
    if vtype == T_FLOAT:
        return struct.pack("<f", float(value))       # type: ignore[arg-type]
    if vtype == T_DOUBLE:
        return struct.pack("<d", float(value))       # type: ignore[arg-type]
    if vtype == T_STRING:
        raw = str(value).encode("latin1")
        return struct.pack("<Q", len(raw)) + raw
    if vtype == T_U32:
        return struct.pack("<I", int(value))         # type: ignore[arg-type]
    if vtype == T_BOOL:
        return bytes([1 if value else 0])
    raise ValueError(f"type RayDB non sérialisable : {vtype:#x}")


def build_update(path: str, vtype: int, value: object) -> bytes:
    """UPDATE (op4) : [3 réservés = 00 00 00][u32 type][payload][4 oct. pad]."""
    block = b"\x00\x00\x00" + struct.pack("<I", vtype) + _typed_payload(vtype, value)
    return _frame(OP_UPDATE, path, block + b"\x00\x00\x00\x00")


def build_named_update(path: str, vtype: int, value: object) -> bytes:
    """UPDATE de type 0x0e (valeur nommée), utilisé par les chemins `diag/…`.

    Le conteneur prévoit N champs mais le MFD n'en publie qu'un (count=1), et la
    clé réencodée double toujours le dernier segment du chemin — on reproduit
    cette redondance, sur laquelle des décodeurs peuvent s'appuyer.
    """
    key = path.rsplit("/", 1)[-1].encode("latin1")
    block = (b"\x00\x00\x00" + struct.pack("<I", T_NAMED)
             + struct.pack("<Q", 1)                  # count
             + struct.pack("<Q", len(key)) + key
             + struct.pack("<I", vtype)
             + _typed_payload(vtype, value))
    return _frame(OP_UPDATE, path, block + b"\x00\x00\x00\x00")


def build_keepalive_ack(counter: int) -> bytes:
    """ACK op6 : trame de 32 octets, path_len=0, compteur ré-écho en fin."""
    block = (b"\x00\x01\x01" + struct.pack("<I", T_U32)
             + struct.pack("<I", 0) + struct.pack("<I", counter))
    return _frame(OP_KEEPALIVE_ACK, "", block)


# ------------------------------------------------------------- décodage ------
def parse_frames(buf: bytearray) -> list[tuple[int, str, bytes]]:
    """Extrait du buffer toutes les trames complètes, et les en retire.

    Le buffer est modifié en place : ce qui reste est le début d'une trame
    incomplète, à compléter par le prochain segment TCP.
    """
    out: list[tuple[int, str, bytes]] = []
    while len(buf) >= 4:
        (length,) = struct.unpack_from("<I", buf, 0)
        if length > MAX_FRAME:
            raise ValueError(f"longueur de trame aberrante : {length}")
        if len(buf) < 4 + length:
            break
        frame = bytes(buf[4:4 + length])
        del buf[:4 + length]
        if len(frame) < HEADER_LEN - 4:
            continue                                  # trame tronquée : ignorée
        op = frame[4]
        (path_len,) = struct.unpack_from("<I", frame, 5)
        path = frame[13:13 + path_len].decode("latin1", "replace")
        out.append((op, path, frame[13 + path_len:]))
    return out


# ------------------------------------------------- chemins de diagnostic -----
def diag_paths(ip: str) -> dict[str, tuple[int, object]]:
    """Sous-arbre `diag/mfd/<modèle> <série>/…` : réseau, cartographie, versions.

    Ces chemins sont statiques sur un MFD réel (ils ne changent qu'au reboot ou
    au changement de carte) ; ils sont donc servis en « retained » seulement.
    """
    base = f"diag/mfd/{config.DEVICE_ID}"
    mac = "00:1e:c0:" + ":".join(
        f"{int(config.SERIAL[i:i + 2] or 0) % 256:02x}" for i in (0, 2, 4))
    return {
        f"{base}/network/ip_address": (T_STRING, ip),
        f"{base}/network/mac_address": (T_STRING, mac),
        f"{base}/network/sub_net_mask": (T_STRING, "255.255.255.0"),
        f"{base}/network/link_status": (T_STRING, "up"),
        f"{base}/nmea2000_info/can_address": (T_U32, 22),
        f"{base}/cartography_info/cmap_base_map_version": (T_STRING, "1.24-00033"),
        f"{base}/cartography_info/lighthouse_base_map_version": (T_STRING, "3.11"),
        f"{base}/software_info/application_version": (T_STRING, config.FIRMWARE),
        f"{base}/software_info/platform_version": (T_STRING, config.FIRMWARE),
        f"{base}/hardware_info/product": (T_STRING, config.PRODUCT),
        f"{base}/hardware_info/serial_number": (T_STRING, config.SERIAL),
    }


def settings_paths() -> dict[str, tuple[int, object]]:
    """Sous-arbre `Settings/…` : réglages du MFD, servis en « retained ».

    Le MFD réel expose des milliers de chemins `Settings/…` ; on ne modélise
    ici que le nom du bateau (`Settings/Data/-/7/13/-/-/-/-`), publié comme une
    valeur nue de type chaîne, à la manière des chemins `data/…`.
    """
    return {
        "Settings/Data/-/7/13/-/-/-/-": (T_STRING, config.BOAT_NAME),
    }


def _matches(sub: str, path: str) -> bool:
    """Un abonnement couvre-t-il ce chemin ?

    Comparaison segment par segment (séparateur `/`). Un `#` **final** couvre le
    sous-arbre : `foo/#` matche `foo` lui-même et tout `foo/…`. Un `#` **intérieur**
    matche exactement un segment : `diag/mfd/#/network/mac_address` matche le
    chemin réel `diag/mfd/E70363 1234567/network/mac_address` sans connaître la
    série. `#` seul couvre tout. Sans `#`, l'abonnement est exact.
    """
    if sub in ("#", "/#"):
        return True

    sub_seg = sub.split("/")
    path_seg = path.split("/")

    if sub_seg[-1] == "#":
        # Sous-arbre : le préfixe (hors `#` final) doit matcher, `#` intérieur
        # inclus, et le chemin peut avoir des segments en plus (ou zéro).
        head = sub_seg[:-1]
        if len(path_seg) < len(head):
            return False
        return all(s == "#" or s == p for s, p in zip(head, path_seg))

    # Pas de `#` final : même nombre de segments, `#` intérieur = un segment.
    if len(sub_seg) != len(path_seg):
        return False
    return all(s == "#" or s == p for s, p in zip(sub_seg, path_seg))


# --------------------------------------------------------------- serveur -----
class _Handler(socketserver.BaseRequestHandler):
    """Une connexion client : HELLO/SUBSCRIBE en entrée, UPDATE en sortie."""

    def setup(self) -> None:
        self.subs: list[str] = []
        self.alive = True
        self.lock = threading.Lock()
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _send(self, data: bytes) -> bool:
        """Envoi protégé : une déconnexion client ne doit pas tuer le pousseur."""
        with self.lock:
            try:
                self.request.sendall(data)
                return True
            except OSError:
                self.alive = False
                return False

    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        log.info("connexion de %s", peer)
        server: RayDBServer = self.server            # type: ignore[assignment]

        pusher = threading.Thread(target=self._push_loop, args=(server,),
                                  daemon=True)
        pusher.start()

        buf = bytearray()
        try:
            while self.alive:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                try:
                    frames = parse_frames(buf)
                except ValueError as e:
                    log.warning("%s : %s — connexion fermée", peer, e)
                    break
                for op, path, block in frames:
                    self._dispatch(server, peer, op, path, block)
        except OSError as e:
            log.info("%s : %s", peer, e)
        finally:
            self.alive = False
            log.info("déconnexion de %s", peer)

    def _dispatch(self, server: RayDBServer, peer: str,
                  op: int, path: str, block: bytes) -> None:
        if op == OP_HELLO:
            log.info("%s HELLO « %s »", peer, path)
        elif op == OP_SUBSCRIBE:
            log.info("%s SUBSCRIBE « %s »", peer, path)
            with self.lock:
                self.subs.append(path)
            self._send_retained(server, path)
        elif op == OP_KEEPALIVE:
            counter = struct.unpack_from("<I", block, 11)[0] if len(block) >= 15 else 0
            self._send(build_keepalive_ack(counter))
        else:
            log.debug("%s opcode inattendu %d (« %s »)", peer, op, path)

    def _send_retained(self, server: RayDBServer, sub: str) -> None:
        """Envoie l'état courant des chemins couverts par un nouvel abonnement.

        C'est le comportement « retained » du MFD : on reçoit tout de suite la
        photo, puis seulement les deltas.
        """
        matched = 0
        for path, (vtype, value) in server.sim.snapshot().items():
            if _matches(sub, path):
                self._send(build_update(path, vtype, value))
                matched += 1
        for path, (vtype, value) in server.settings.items():
            if _matches(sub, path):
                self._send(build_update(path, vtype, value))
                matched += 1
        for path, (vtype, value) in server.diag.items():
            if _matches(sub, path):
                self._send(build_named_update(path, vtype, value))
                matched += 1
        if matched == 0:
            # Souscription qu'aucun chemin connu ne couvre : on la trace pour
            # pouvoir modéliser le chemin manquant par la suite.
            log.warning("SUBSCRIBE non satisfait, chemin absent du modèle : « %s »", sub)

    def _push_loop(self, server: RayDBServer) -> None:
        """Pousse les changements de la simulation aux chemins abonnés."""
        period = 1.0 / config.SIM_HZ
        while self.alive:
            time.sleep(period)
            with self.lock:
                subs = list(self.subs)
            if not subs:
                continue
            for path, (vtype, value) in server.changes().items():
                if any(_matches(s, path) for s in subs):
                    if not self._send(build_update(path, vtype, value)):
                        return


class RayDBServer(socketserver.ThreadingTCPServer):
    """Serveur RayDB multi-clients.

    La simulation est partagée : elle avance une seule fois par tick, et chaque
    connexion lit le même lot de changements. Deux clients voient donc des
    valeurs identiques, comme sur un vrai MFD.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int, sim: Simulation, ip: str) -> None:
        super().__init__((host, port), _Handler)
        self.sim = sim
        self.settings = settings_paths()
        self.diag = diag_paths(ip)
        self._lock = threading.Lock()
        self._changes: dict[str, tuple[int, object]] = {}
        self._stamp = 0.0

    def changes(self) -> dict[str, tuple[int, object]]:
        """Lot de changements courant, recalculé au plus une fois par tick."""
        with self._lock:
            now = time.monotonic()
            if now - self._stamp >= 1.0 / config.SIM_HZ:
                self._changes = self.sim.step()
                self._stamp = now
            return self._changes


def serve(sim: Simulation, ip: str) -> RayDBServer:
    """Démarre le serveur RayDB dans un thread et le renvoie."""
    srv = RayDBServer("0.0.0.0", config.RAYDB_PORT, sim, ip)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("RayDB à l'écoute sur 0.0.0.0:%d", config.RAYDB_PORT)
    return srv
