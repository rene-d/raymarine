#!/usr/bin/env python3
"""
rrce_touch.py — injecte des entrées (touchers, boutons, molette) dans un MFD
Raymarine via le canal de télécommande "RRCE" (TCP 50000).

L'app RayControl affiche l'écran du MFD (vidéo RTSP 8554) et renvoie les entrées
de l'utilisateur sur ce port. Cet outil reproduit ces trames pour piloter le MFD
à distance (tap, glissé, boutons, molette) ou rejouer une capture.

Trois sortes de records, en-tête commun de 9 octets dont l'octet [6] porte le
type et l'octet [7] la longueur de la charge utile :
    tactile : "ECRR" + 01 0a 03 06 00 + [op u8][finger u8][X u16 LE][Y u16 LE]
              op : 1=DOWN 2=MOVE 3=UP 4=CANCEL ; X/Y normalisés 0..65535.
    clavier : "ECRR" + 01 00 01 02 00 + [code u8][état u8]
              état : 1=enfoncé 2=relâché ; codes = boutons de la façade.
    molette : "ECRR" + 01 00 02 04 00 + [delta i16 LE][cumul i16 LE]
              cumul = somme des delta depuis le début de la salve ; le record
              d'ouverture porte cumul=0 et ne s'applique pas.

Exemples :
    ./rrce_touch.py 192.168.42.1 tap 32000 33000
    ./rrce_touch.py 192.168.42.1 tap --frac 0.5 0.5           # centre de l'écran
    ./rrce_touch.py 192.168.42.1 swipe 60000 33000 5000 33000 # balayage droite→gauche
    ./rrce_touch.py 192.168.42.1 swipe --frac 0.5 0.8 0.5 0.2 --duration 0.6
    ./rrce_touch.py 192.168.42.1 key home                     # bouton Home
    ./rrce_touch.py 192.168.42.1 key zoom+ --hold 0.5 --repeat
    ./rrce_touch.py 192.168.42.1 wheel 5                      # 5 crans de molette
    ./rrce_touch.py 192.168.42.1 wheel -3 --step 30
    ./rrce_touch.py 192.168.42.1 raw 2 0 12345 54321
    ./rrce_touch.py 192.168.42.1 --replay pcap/rm1.pcapng     # rejoue les touchers capturés
    ./rrce_touch.py --dry-run tap --frac 0.5 0.5              # affiche les octets, n'envoie rien
    ./rrce_touch.py --list-keys                               # noms de boutons acceptés

Coordonnées : entières 0..65535 par défaut, ou fractions 0..1 avec --frac, ou
pixels avec --screen LARGEURxHAUTEUR (converties en normalisées).
"""
from __future__ import annotations

import argparse
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from typing import NamedTuple

RRCE_PORT = 50000
MAGIC = b"ECRR"
HDR_LEN = 9                                 # magie + 5 octets d'en-tête
# En-tête : 01 <version RRC> <type de record> <longueur charge utile> 00.
# L'octet [4] vaut toujours 01, [5] porte la version du protocole annoncée en
# mDNS (clé TXT `raymarine-mfd-rrc-version`) et [6] le TYPE de record.
# Les trois constructeurs de trames n'y diffèrent que par [6] et la longueur,
# l'octet [5] étant une variable statique commune. On démultiplexe donc sur le
# seul octet [6] ; les en-têtes complets ci-dessous restent ceux qu'on émet, à
# l'identique des captures.
HEADER = bytes.fromhex("010a030600")        # tactile (E70363 & E70481) — 6 octets
HEADER_KEY = bytes.fromhex("010001" "02" "00")   # clavier/boutons — 2 octets
HEADER_WHEEL = bytes.fromhex("010002" "04" "00")  # molette — 4 octets
TYPE_KEY, TYPE_WHEEL, TYPE_TOUCH = 1, 2, 3  # valeurs de l'octet [6]
OP_DOWN, OP_MOVE, OP_UP, OP_CANCEL = 1, 2, 3, 4
KEY_PRESS, KEY_RELEASE = 1, 2
FULL = 65535
RECORD_LEN = HDR_LEN + 6                    # 15 : record tactile complet
KEY_RECORD_LEN = HDR_LEN + 2                # 11 : record bouton complet
WHEEL_RECORD_LEN = HDR_LEN + 4              # 13 : record molette complet
# Delta constant du record qui ouvre une salve de molette (cumul remis à 0).
WHEEL_OPEN = -24
WHEEL_STEP = 24                             # plus petit incrément observé (1 cran)

# Codes des boutons de façade (ce sont les codes de touches virtuelles Windows,
# réutilisés tels quels par RayControl : F7/F9/F8/F11, Échap, Page↑/↓, Entrée).
KEYS: dict[str, int] = {
    "ok":     0x0d,     # Entrée — valide
    "back":   0x1b,     # Échap — retour
    "zoom-":  0x21,     # Page↑ — dézoom
    "zoom+":  0x22,     # Page↓ — zoom
    "left":   0x25,
    "up":     0x26,
    "right":  0x27,
    "down":   0x28,
    "home":   0x76,     # F7
    "wpt":    0x77,     # F8 — waypoint
    "menu":   0x78,     # F9
    "switch": 0x7a,     # F11 — switch (bascule de panneau)
}
KEY_NAME: dict[int, str] = {c: n for n, c in KEYS.items()}


class Touch(NamedTuple):
    """Événement tactile : op (DOWN/MOVE/UP/CANCEL), doigt, X/Y sur 0..65535."""
    op: int
    finger: int
    x: int
    y: int


class Key(NamedTuple):
    """Appui bouton : code de touche, état (1=enfoncé, 2=relâché)."""
    code: int
    state: int


class Wheel(NamedTuple):
    """Cran de molette : incrément signé et cumul depuis le début du geste.
    Un record d'ouverture (`total == 0`, `delta == WHEEL_OPEN`) précède chaque
    salve et ne doit pas être appliqué."""
    delta: int
    total: int

    @property
    def opening(self) -> bool:
        return self.total == 0


class Unknown(NamedTuple):
    """Record d'un type non identifié : conservé tel quel (les 3 octets [4..6]
    de l'en-tête et la charge utile brute) pour être affiché et rejoué sans
    perte. `source[2]` est le type qui n'a pas été reconnu."""
    source: bytes
    data: bytes


Event = Touch | Key | Wheel | Unknown
# Rétro-compatibilité : (op, finger, x, y) reste dépaquetable depuis un Touch.
Record = Touch
# (valeur_x, valeur_y) -> (X, Y) normalisés
Converter = Callable[[float, float], tuple[float, float]]


def build_touch(op: int, finger: int, x: float, y: float) -> bytes:
    """Assemble une trame tactile RRCE de 15 octets."""
    xi = max(0, min(FULL, round(x)))
    yi = max(0, min(FULL, round(y)))
    return MAGIC + HEADER + struct.pack("<BBHH", op, finger, xi, yi)


def build_key(code: int, state: int) -> bytes:
    """Assemble une trame bouton RRCE de 11 octets."""
    return MAGIC + HEADER_KEY + struct.pack("<BB", code, state)


def build_wheel(delta: int, total: int) -> bytes:
    """Assemble une trame molette RRCE de 13 octets (incrément + cumul)."""
    return MAGIC + HEADER_WHEEL + struct.pack("<hh", delta, total)


def build_raw(source: bytes, data: bytes) -> bytes:
    """Assemble un record quelconque : source de 3 octets + charge utile."""
    return MAGIC + source + bytes([len(data), 0]) + data


def parse_records(buf: bytes) -> tuple[list[Event], bytes]:
    """Découpe un flux d'octets en records RRCE, la longueur étant lue dans
    l'en-tête (octet [7]) : 15 octets pour le tactile, 11 pour un bouton.
    Retourne (liste d'événements, reste).

    Le type est lu dans le seul octet [6] : l'octet [5] est une version, qui
    varie d'un client à l'autre (`0a` pour le tactile de `rrce.pcap`, `00` pour
    ses boutons, dans le même flux TCP). Se caler sur [5..6] ferait passer pour
    inconnu un record parfaitement décodable émis par un MFD d'une autre
    version."""
    recs: list[Event] = []
    o = 0
    while len(buf) - o >= HDR_LEN:
        if buf[o:o + 4] != MAGIC:
            break
        n = buf[o + 7]                       # longueur de la charge utile
        if len(buf) - o < HDR_LEN + n:
            break                            # record incomplet : on attend la suite
        src, data = buf[o + 4:o + 7], buf[o + HDR_LEN:o + HDR_LEN + n]
        rtype = buf[o + 6]                   # type de record
        if rtype == TYPE_TOUCH and n >= 6:
            recs.append(Touch(*struct.unpack_from("<BBHH", data)))
        elif rtype == TYPE_KEY and n >= 2:
            recs.append(Key(data[0], data[1]))
        elif rtype == TYPE_WHEEL and n >= 4:
            recs.append(Wheel(*struct.unpack_from("<hh", data)))
        else:
            # Type inconnu : on le remonte tel quel plutôt que de le taire —
            # c'est ainsi qu'on a trouvé les boutons puis la molette.
            recs.append(Unknown(bytes(src), bytes(data)))
        o += HDR_LEN + n
    return recs, buf[o:]


def extract_records(buf: bytes) -> tuple[list[Event], bytes]:
    """Comme parse_records, mais se resynchronise sur la magie si le flux est
    désaligné (segment TCP coupé au milieu d'un record, octet parasite).
    Retourne (liste d'événements, reste)."""
    recs, rest = parse_records(buf)
    while len(rest) >= HDR_LEN and rest[:4] != MAGIC:
        idx = rest.find(MAGIC, 1)
        if idx < 0:
            rest = rest[-3:]                 # garde une éventuelle magie coupée
            break
        rest = rest[idx:]
        more, rest = parse_records(rest)
        recs.extend(more)
    return recs, rest


def format_event(ev: Event) -> str:
    """Rendu court d'un événement, commun aux affichages de l'outillage."""
    if isinstance(ev, Key):
        st = {KEY_PRESS: "enfoncé", KEY_RELEASE: "relâché"}.get(ev.state, f"état{ev.state}")
        return f"KEY    0x{ev.code:02x} {KEY_NAME.get(ev.code, '?'):6} {st}"
    if isinstance(ev, Wheel):
        tag = " (ouverture)" if ev.opening else ""
        return f"WHEEL  delta={ev.delta:+5d} cumul={ev.total:+6d}{tag}"
    if isinstance(ev, Unknown):
        return f"?????  source {ev.source.hex()} : {ev.data.hex()}"
    name = {1: "DOWN", 2: "MOVE", 3: "UP", 4: "CANCEL"}.get(ev.op, f"op{ev.op}")
    return f"{name:6} f{ev.finger} ({ev.x:5d},{ev.y:5d})"


def find_tshark() -> str:
    """Localise tshark (PATH, puis le bundle Wireshark macOS)."""
    for p in ("tshark", "/Applications/Wireshark.app/Contents/MacOS/tshark"):
        try:
            # Sonde d'existence : seul compte que le binaire se lance, d'où le
            # code de retour ignoré — c'est l'OSError qui dit « absent ».
            subprocess.run([p, "--version"], capture_output=True, check=False)
            return p
        except OSError:
            continue
    sys.exit("tshark introuvable (installer Wireshark).")


# ------------------------------------------------------------ transport -----
class Sender:
    """Envoie les trames au MFD, ou les affiche en mode --dry-run."""
    def __init__(self, host: str | None, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.sock: socket.socket | None = None
        if not dry_run:
            assert host is not None          # garanti par main()
            self.sock = socket.create_connection((host, RRCE_PORT), timeout=5)

    def send(self, frame: bytes) -> None:
        evs, _ = parse_records(frame)
        desc = "  ".join(format_event(e) for e in evs) or "?"
        line = f"{desc}  {frame.hex()}"
        if self.sock is None:
            print(line)
        else:
            self.sock.sendall(frame)
            print(line, file=sys.stderr)

    def close(self) -> None:
        if self.sock:
            self.sock.close()


# ------------------------------------------------- conversion coordonnées ---
def make_converter(args: argparse.Namespace) -> Converter:
    """Retourne une fonction (vx, vy) -> (X, Y) normalisés selon le mode."""
    if getattr(args, "frac", False):
        return lambda vx, vy: (float(vx) * FULL, float(vy) * FULL)
    if getattr(args, "screen", None):
        w, h = (int(n) for n in args.screen.lower().split("x"))
        return lambda vx, vy: (float(vx) / w * FULL, float(vy) / h * FULL)
    return lambda vx, vy: (float(vx), float(vy))


# ------------------------------------------------------------- gestes -------
def do_tap(s: Sender, conv: Converter, args: argparse.Namespace) -> None:
    x, y = conv(args.x, args.y)
    s.send(build_touch(OP_DOWN, 0, x, y))
    time.sleep(args.hold)
    s.send(build_touch(OP_UP, 0, x, y))


def do_swipe(s: Sender, conv: Converter, args: argparse.Namespace) -> None:
    x1, y1 = conv(args.x1, args.y1)
    x2, y2 = conv(args.x2, args.y2)
    n = max(1, args.steps)
    dt = args.duration / n
    s.send(build_touch(OP_DOWN, 0, x1, y1))
    for i in range(1, n + 1):
        t = i / n
        s.send(build_touch(OP_MOVE, 0, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        time.sleep(dt)
    s.send(build_touch(OP_UP, 0, x2, y2))


def do_raw(s: Sender, conv: Converter, args: argparse.Namespace) -> None:
    s.send(build_touch(args.op, args.finger, args.x, args.y))


# Cadence de répétition de RayControl tant qu'un bouton reste enfoncé (~120 Hz).
KEY_REPEAT = 1 / 120


def resolve_key(name: str) -> int:
    """Nom de bouton (home, zoom+, …) ou code numérique (0x76, 118) → code."""
    code = KEYS.get(name.lower())
    if code is not None:
        return code
    try:
        return int(name, 0) & 0xFF
    except ValueError:
        raise SystemExit(f"bouton inconnu : {name} (voir --list-keys)") from None


def do_key(s: Sender, conv: Converter, args: argparse.Namespace) -> None:
    """Appui bouton : enfoncé (répété comme RayControl si --repeat) puis relâché."""
    code = resolve_key(args.name)
    s.send(build_key(code, KEY_PRESS))
    if args.repeat:
        end = time.monotonic() + args.hold
        while time.monotonic() < end:
            time.sleep(KEY_REPEAT)
            s.send(build_key(code, KEY_PRESS))
    else:
        time.sleep(args.hold)
    s.send(build_key(code, KEY_RELEASE))


def do_wheel(s: Sender, conv: Converter, args: argparse.Namespace) -> None:
    """Salve de molette : record d'ouverture, puis N crans signés, le cumul
    étant recalculé à chaque record comme le fait le client."""
    s.send(build_wheel(WHEEL_OPEN, 0))
    step = args.step if args.count >= 0 else -args.step
    total = 0
    for _ in range(abs(args.count)):
        total += step
        s.send(build_wheel(step, total))
        time.sleep(args.rate_wheel)


def payloads_from_pcap(pcap: str, tshark: str) -> Iterator[bytes]:
    """Génère la charge utile de chaque segment TCP client→MFD d'une capture."""
    cmd = [tshark, "-r", pcap, "-n",
           "-Y", f"tcp.dstport=={RRCE_PORT} && tcp.len>0",
           "-T", "fields", "-e", "tcp.payload"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    assert proc.stdout is not None and proc.stderr is not None
    try:
        for line in proc.stdout:
            hexstr = line.strip().replace(":", "")
            if not hexstr:
                continue
            try:
                yield bytes.fromhex(hexstr)
            except ValueError:
                continue
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read()
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"tshark a échoué (code {rc}) : {stderr.strip()}")


def do_replay(s: Sender, args: argparse.Namespace) -> None:
    """Rejoue les événements d'une capture (records RRCE dst 50000) : touchers
    et appuis de boutons, dans l'ordre du flux."""
    tshark = find_tshark()
    buf = b""
    n = 0
    for payload in payloads_from_pcap(args.replay, tshark):
        recs, buf = extract_records(buf + payload)
        for ev in recs:
            if isinstance(ev, Key):
                s.send(build_key(*ev))
            elif isinstance(ev, Wheel):
                s.send(build_wheel(*ev))
            elif isinstance(ev, Unknown):
                s.send(build_raw(*ev))       # rejoué tel quel, même non décodé
            else:
                s.send(build_touch(*ev))
            n += 1
            if args.rate:
                time.sleep(args.rate)
    if not n:
        print(f"aucun record RRCE dans {args.replay}", file=sys.stderr)


# ----------------------------------------------------------------- main -----
def _extract_host(argv: list[str]) -> tuple[str | None, list[str]]:
    """Sort l'IP hôte (1er positionnel avant toute sous-commande) de argv, pour
    éviter que argparse ne prenne "tap"/"swipe" pour l'hôte. Retourne (host, rest)."""
    subcmds = {"tap", "swipe", "raw", "key", "wheel"}
    valued = {"--replay", "--rate"}          # options prenant une valeur, avant la sous-cmd
    host: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in subcmds:
            rest.extend(argv[i:])            # sous-commande + ses args : intacts
            break
        if a.startswith("-"):
            rest.append(a)
            if a in valued and i + 1 < len(argv):
                i += 1
                rest.append(argv[i])
        elif host is None:
            host = a                         # 1er token nu = hôte
        else:
            rest.append(a)
        i += 1
    return host, rest


def _add_coord_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--frac", action="store_true", help="coordonnées en fractions 0..1")
    p.add_argument("--screen", metavar="LxH", help="coordonnées en pixels (ex. 1280x720)")


def main() -> None:
    host, rest = _extract_host(sys.argv[1:])
    ap = argparse.ArgumentParser(
        prog="rrce_touch.py",
        description="Injecteur d'entrées RRCE (TCP 50000). Usage : "
                    "rrce_touch.py <IP_MFD> <tap|swipe|key|wheel|raw> …  (ou --replay).")
    ap.add_argument("--dry-run", action="store_true", help="afficher les octets sans envoyer")
    ap.add_argument("--replay", metavar="PCAP", help="rejouer les entrées d'une capture")
    ap.add_argument("--rate", type=float, default=0.0, help="pause (s) entre records rejoués")
    ap.add_argument("--list-keys", action="store_true", help="lister les boutons et quitter")

    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("tap", help="toucher bref (down+up)")
    _add_coord_opts(p)
    p.add_argument("x", type=float); p.add_argument("y", type=float)
    p.add_argument("--hold", type=float, default=0.05, help="durée d'appui (s)")

    p = sub.add_parser("swipe", help="glissé (down+moves+up)")
    _add_coord_opts(p)
    for a in ("x1", "y1", "x2", "y2"):
        p.add_argument(a, type=float)
    p.add_argument("--steps", type=int, default=16, help="nb de points intermédiaires")
    p.add_argument("--duration", type=float, default=0.4, help="durée totale (s)")

    p = sub.add_parser("key", help="appui bouton (home, menu, back, ok, wpt, switch, "
                                   "zoom+, zoom-, flèches, ou code 0x76)")
    p.add_argument("name", help="nom de bouton ou code numérique")
    p.add_argument("--hold", type=float, default=0.05, help="durée d'appui (s)")
    p.add_argument("--repeat", action="store_true",
                   help="répéter l'état « enfoncé » à 120 Hz, comme RayControl")

    p = sub.add_parser("wheel", help="molette : nombre de crans, signé")
    p.add_argument("count", type=int, help="crans (négatif = sens inverse)")
    p.add_argument("--step", type=int, default=WHEEL_STEP,
                   help=f"amplitude d'un cran (défaut {WHEEL_STEP})")
    p.add_argument("--rate-wheel", type=float, default=0.05, dest="rate_wheel",
                   help="pause (s) entre deux crans")

    p = sub.add_parser("raw", help="événement brut : op finger x y (0..65535)")
    p.add_argument("op", type=int); p.add_argument("finger", type=int)
    p.add_argument("x", type=float); p.add_argument("y", type=float)

    args = ap.parse_args(rest)
    args.host = host
    if args.list_keys:
        for name, code in sorted(KEYS.items(), key=lambda kv: kv[1]):
            print(f"  0x{code:02x}  {name}")
        return
    if not args.replay and not args.cmd:
        ap.error("préciser une commande (tap/swipe/raw) ou --replay")
    if args.replay and args.cmd:
        ap.error(f"--replay et la commande {args.cmd} sont exclusifs")
    if not args.dry_run and not host:
        ap.error("préciser l'IP du MFD en 1er argument (ou utiliser --dry-run)")

    try:
        s = Sender(args.host, args.dry_run)
    except OSError as exc:
        sys.exit(f"{ap.prog}: connexion à {args.host}:{RRCE_PORT} impossible : {exc}")
    try:
        if args.replay:
            do_replay(s, args)
        else:
            conv = make_converter(args)
            {"tap": do_tap, "swipe": do_swipe, "key": do_key,
             "wheel": do_wheel, "raw": do_raw}[args.cmd](s, conv, args)
    except (RuntimeError, OSError) as exc:
        sys.exit(f"{ap.prog}: {exc}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
