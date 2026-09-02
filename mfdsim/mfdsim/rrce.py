"""
rrce.py — canal d'entrée RRCE (TCP 50000), côté MFD.

Le MFD écoute, le client (RayControl, ou `rrce_touch.py`) pousse ses événements
d'entrée : touchers de l'écran et appuis des boutons de façade. Le trafic est
**strictement unidirectionnel** : vérifié en capture, 0 segment de données côté
MFD. Le serveur ne renvoie donc **rien** — pas de bannière, pas d'accusé de
réception. Il se contente de décoder et de journaliser, ce qui en fait un banc
d'essai pour `rrce_touch.py`.

Trois formats de record partagent un en-tête de 9 octets dont l'octet [6] porte
le type et l'octet [7] la longueur de la charge utile :

    tactile (15 o) : "ECRR" | 01 0a 03 06 00 | op | finger | X u16 LE | Y u16 LE
    bouton  (11 o) : "ECRR" | 01 00 01 02 00 | code | état
    molette (13 o) : "ECRR" | 01 00 02 04 00 | delta i16 LE | cumul i16 LE

L'octet [5] de l'en-tête (`0a` ci-dessus pour le tactile, `00` pour les deux
autres) n'appartient pas à l'identifiant du type : c'est la version du protocole
annoncée en mDNS (`raymarine-mfd-rrc-version`), qui varie d'un client à l'autre.
Le démultiplexage se fait donc sur le seul octet [6] — 1 bouton, 2 molette,
3 tactile.

X et Y sont normalisés sur 0..65535, indépendamment de la résolution réelle.
`état` vaut 1 (enfoncé) ou 2 (relâché) ; RayControl réémet « enfoncé » à ~120 Hz
tant que le bouton reste tenu. Pour la molette, `cumul` est la somme des `delta`
depuis le début de la salve ; le record d'ouverture porte `cumul = 0`.

Un record dont le type n'est pas connu est **journalisé en hexa**, jamais
ignoré silencieusement : c'est ce qui a révélé successivement les boutons puis
la molette.

Aucun handshake applicatif : les records arrivent dès la connexion établie, et
plusieurs peuvent être concaténés dans un même segment TCP. Comme un segment
peut aussi couper un record en deux, le décodage resynchronise sur la magie
`ECRR` au lieu de supposer un alignement.
"""
from __future__ import annotations

import logging
import socketserver
import struct
import threading
from typing import NamedTuple

from . import config

log = logging.getLogger("rrce")

MAGIC = b"ECRR"
HDR_LEN = 9                      # magie + 01 <version> <type> <longueur> 00
# Type de record : octet [6] de l'en-tête. L'octet [5] qui le précède est une
# version, pas une partie de l'identifiant — voir le docstring du module.
TYPE_KEY, TYPE_WHEEL, TYPE_TOUCH = 1, 2, 3
RECORD_LEN = 15                  # record tactile complet
KEY_RECORD_LEN = 11              # record bouton complet
WHEEL_RECORD_LEN = 13            # record molette complet

OPS = {1: "DOWN", 2: "MOVE", 3: "UP", 4: "CANCEL"}
STATES = {1: "enfoncé", 2: "relâché"}
# Codes de touches virtuelles Windows réutilisés par RayControl.
KEYS = {
    0x0d: "OK", 0x1b: "BACK", 0x21: "ZOOM-", 0x22: "ZOOM+",
    0x25: "LEFT", 0x26: "UP", 0x27: "RIGHT", 0x28: "DOWN",
    0x76: "HOME", 0x77: "WPT", 0x78: "MENU", 0x7a: "SWITCH",
}


class Touch(NamedTuple):
    """Événement tactile : op, doigt, X/Y normalisés sur 0..65535."""
    op: int
    finger: int
    x: int
    y: int


class Key(NamedTuple):
    """Appui bouton : code de touche, état (1=enfoncé, 2=relâché)."""
    code: int
    state: int


class Wheel(NamedTuple):
    """Cran de molette : incrément signé, et cumul depuis le début de la salve
    (un record d'ouverture porte `total == 0` et ne s'applique pas)."""
    delta: int
    total: int


class Unknown(NamedTuple):
    """Record de type non identifié, gardé brut pour être journalisé : les
    3 octets [4..6] de l'en-tête, dont le dernier est le type inconnu."""
    source: bytes
    data: bytes


Event = Touch | Key | Wheel | Unknown


def decode_records(buf: bytearray) -> list[Event]:
    """Extrait les records complets du buffer, en resynchronisant sur `ECRR`.

    Renvoie une liste de `Touch` / `Key` et laisse dans le buffer le fragment de
    tête d'un record incomplet.
    """
    out: list[Event] = []
    while True:
        start = buf.find(MAGIC)
        if start < 0:
            # Pas de magie : on ne garde que de quoi reconstituer un « ECRR »
            # coupé entre deux segments.
            del buf[:max(0, len(buf) - (len(MAGIC) - 1))]
            return out
        if start:
            log.debug("resynchronisation : %d octet(s) écarté(s)", start)
            del buf[:start]
        if len(buf) < HDR_LEN:
            return out
        n = buf[7]                           # longueur de la charge utile
        if len(buf) < HDR_LEN + n:
            return out
        src = bytes(buf[4:7])
        rtype = buf[6]                       # type de record
        if rtype == TYPE_TOUCH and n >= 6:
            x, y = struct.unpack_from("<HH", buf, 11)
            out.append(Touch(buf[9], buf[10], x, y))
        elif rtype == TYPE_KEY and n >= 2:
            out.append(Key(buf[9], buf[10]))
        elif rtype == TYPE_WHEEL and n >= 4:
            out.append(Wheel(*struct.unpack_from("<hh", buf, 9)))
        else:
            out.append(Unknown(src, bytes(buf[9:9 + n])))
        del buf[:HDR_LEN + n]


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        log.info("télécommande connectée : %s", peer)
        buf = bytearray()
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                for ev in decode_records(buf):
                    if isinstance(ev, Unknown):
                        # Type non identifié : on le montre au lieu de le taire,
                        # c'est le seul moyen de repérer un organe d'entrée
                        # encore inconnu.
                        log.warning("%s ????   en-tête %s  charge utile %s", peer,
                                    ev.source.hex(), ev.data.hex() or "(vide)")
                        continue
                    if isinstance(ev, Wheel):
                        log.info("%s WHEEL  delta=%+5d  cumul=%+6d%s", peer,
                                 ev.delta, ev.total,
                                 "  (début de salve)" if ev.total == 0 else "")
                        continue
                    if isinstance(ev, Key):
                        log.info("%s KEY    %-6s 0x%02x  %s", peer,
                                 KEYS.get(ev.code, "INCONNU"), ev.code,
                                 STATES.get(ev.state, f"état{ev.state}"))
                        continue
                    log.info("%s %-6s doigt=%d  X=%5d (%5.1f%%)  Y=%5d (%5.1f%%)",
                             peer, OPS.get(ev.op, f"op{ev.op}"), ev.finger,
                             ev.x, ev.x / 655.35, ev.y, ev.y / 655.35)
        except OSError as e:
            log.info("%s : %s", peer, e)
        finally:
            log.info("télécommande déconnectée : %s", peer)


class RRCEServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve() -> RRCEServer:
    """Démarre l'écoute RRCE dans un thread et la renvoie."""
    srv = RRCEServer(("0.0.0.0", config.RRCE_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("RRCE à l'écoute sur 0.0.0.0:%d", config.RRCE_PORT)
    return srv
