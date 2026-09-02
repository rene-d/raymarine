#!/usr/bin/env python3
"""
rrce_sniff.py — capture LIVE et décodage des trames RRCE (TCP 50000).

Sniffe le trafic du canal de télécommande Raymarine et affiche en temps réel les
événements décodés : touchers (DOWN/MOVE/UP, doigt, X/Y normalisés), appuis de
boutons (Home, Menu, Back, OK, WPT, switch, zoom, flèches) et crans de molette.
Pratique pour observer ce que l'app Raymarine (ou notre mfd_remote.py /
rrce_touch.py) envoie au MFD.

Trois formats de record, le type étant porté par l'octet [6] de l'en-tête et la
longueur par l'octet [7] (l'octet [5], `0a` ou `00`, n'est qu'une version) :
    tactile (15 o) : "ECRR" + 01 0a 03 06 00 + [op u8][finger u8][X u16][Y u16]
        op : 1=DOWN 2=MOVE 3=UP 4=CANCEL ; X/Y normalisés 0..65535, LE.
    bouton  (11 o) : "ECRR" + 01 00 01 02 00 + [code u8][état u8]
        état : 1=enfoncé (répété à ~120 Hz tant que tenu) 2=relâché.
    molette (13 o) : "ECRR" + 01 00 02 04 00 + [delta i16][cumul i16]
        cumul = somme des delta depuis le début de la salve.
Plusieurs records par paquet TCP quand plusieurs doigts (batch multi-touch).

Capture via tshark (déjà l'outil du projet). Le MFD ne renvoie aucun payload
applicatif : on ne filtre donc que le sens client→MFD (tcp.len>0). Le décodage
réutilise extract_records() de rrce_touch.py ; un buffer par flux TCP + une
resynchronisation sur la magie "ECRR" rendent le tout robuste aux segments TCP
coupés.

On lit tcp.payload et non data.data : le champ est produit par le dissecteur TCP
lui-même, donc insensible au plugin Lua dissectors/raymarine_rrce.lua, et le découpage en
segments est repris par notre propre bufferisation de flux.

Permissions macOS : la capture live nécessite l'accès à /dev/bpf* (groupe
access_bpf via ChmodBPF de Wireshark, sinon lancer avec sudo).

Exemples :
    ./rrce_sniff.py                      # interface Wi-Fi par défaut (en0)
    ./rrce_sniff.py -i en0 --host 192.168.42.1
    ./rrce_sniff.py --list               # liste les interfaces et quitte
    ./rrce_sniff.py -r pcap/axiom.pcapng # relecture d'une capture (hors-ligne)
    ./rrce_sniff.py -r rrce.pcap --keys  # seulement les boutons et la molette
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from types import FrameType

from rrce_touch import (
    KEY_NAME,
    KEY_PRESS,
    RRCE_PORT,
    Key,
    Unknown,
    Wheel,
    extract_records,
    find_tshark,
)
from rrce_touch import Event as Input  # touch ou bouton décodé

OP_NAME: dict[int, str] = {1: "DOWN", 2: "MOVE", 3: "UP", 4: "CANCEL"}

# Couleurs ANSI par type d'événement (désactivables via --no-color).
COLOR: dict[int, str] = {1: "\033[32m", 2: "\033[90m", 3: "\033[31m", 4: "\033[33m"}
KEY_COLOR = "\033[36m"
WHEEL_COLOR = "\033[35m"
RESET = "\033[0m"
DIM = "\033[2m"

# (timestamp, ip source, port source, ip dest, n° de flux TCP, charge utile)
Event = tuple[float, str, int, str, str, bytes]


class Decoder:
    """Maintient un buffer par flux TCP et imprime les records décodés."""
    def __init__(self, color: bool = True, keys_only: bool = False) -> None:
        self.buffers: dict[tuple[str, int], bytes] = {}   # (stream, srcport) -> octets
        self.color = color and sys.stdout.isatty()
        self.keys_only = keys_only
        self.counts: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}
        self.gestures = 0                    # nb de DOWN doigt 0 (gestes démarrés)
        self.presses = 0                     # nb d'appuis bouton (hors répétitions)
        self.key_records = 0
        self.wheel_records = 0
        self.wheel_bursts = 0                # nb de salves de molette
        self.unknown = 0                     # records de type non identifié
        self.held: set[int] = set()          # codes actuellement enfoncés

    @property
    def total(self) -> int:
        return (sum(self.counts.values()) + self.key_records
                + self.wheel_records + self.unknown)

    def feed(self, ts: float, src: str, sport: int, dst: str,
             stream: str, payload: bytes) -> None:
        key = (stream, sport)
        buf = self.buffers.get(key, b"") + payload
        recs, buf = extract_records(buf)
        self.buffers[key] = buf
        for ev in recs:
            self._print(ts, src, sport, dst, ev)

    def _prefix(self, ts: float, src: str, sport: int, dst: str) -> str:
        d = DIM if self.color else ""
        r = RESET if self.color else ""
        clk = time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int((ts%1)*1000):03d}"
        return f"{d}{clk}{r}  {src}:{sport}{d}→{dst}{r}  "

    def _print(self, ts: float, src: str, sport: int, dst: str, ev: Input) -> None:
        r = RESET if self.color else ""
        d = DIM if self.color else ""
        if isinstance(ev, Unknown):
            # Jamais silencieux : un type inconnu est précisément ce qu'on
            # cherche quand un nouvel organe d'entrée apparaît.
            self.unknown += 1
            c = "\033[33m" if self.color else ""
            print(f"{self._prefix(ts, src, sport, dst)}"
                  f"{c}?????{r}  en-tête {ev.source.hex()}  {ev.data.hex()}")
            return
        if isinstance(ev, Wheel):
            self.wheel_records += 1
            c = WHEEL_COLOR if self.color else ""
            if ev.opening:
                self.wheel_bursts += 1
                print(f"{self._prefix(ts, src, sport, dst)}"
                      f"{c}WHEEL{r}  {d}début de salve (cumul remis à 0){r}")
                return
            print(f"{self._prefix(ts, src, sport, dst)}"
                  f"{c}WHEEL{r}  delta={ev.delta:+4d}  {d}cumul={ev.total:+6d}{r}")
            return
        if isinstance(ev, Key):
            self.key_records += 1
            held = ev.code in self.held
            if ev.state == KEY_PRESS:
                # Raymarine répète « enfoncé » à ~120 Hz : un seul appui compté.
                self.held.add(ev.code)
                if not held:
                    self.presses += 1
            else:
                self.held.discard(ev.code)
            c = KEY_COLOR if self.color else ""
            st = "enfoncé" if ev.state == KEY_PRESS else \
                 "relâché" if ev.state == 2 else f"état{ev.state}"
            rep = f" {d}(répétition){r}" if ev.state == KEY_PRESS and held else ""
            name = KEY_NAME.get(ev.code, "?")
            print(f"{self._prefix(ts, src, sport, dst)}"
                  f"{c}KEY{r}    {name:6} {d}0x{ev.code:02x}{r}  {st}{rep}")
            return
        op, finger, x, y = ev
        self.counts[op] = self.counts.get(op, 0) + 1
        if op == 1 and finger == 0:
            self.gestures += 1
        if self.keys_only:                   # compté, mais non affiché
            return
        name = OP_NAME.get(op, f"op{op}")
        c = COLOR.get(op, "") if self.color else ""
        px, py = x / 655.35, y / 655.35      # en %
        print(f"{self._prefix(ts, src, sport, dst)}"
              f"{c}{name:6}{r} f{finger}  ({x:5d},{y:5d})  "
              f"{d}{px:5.1f}% {py:5.1f}%{r}")

    def summary(self) -> None:
        print(f"\n{self.counts.get(1,0)} DOWN / {self.counts.get(2,0)} MOVE / "
              f"{self.counts.get(3,0)} UP / {self.counts.get(4,0)} CANCEL — "
              f"{self.gestures} geste(s) ; {self.presses} appui(s) bouton "
              f"({self.key_records} record(s) clavier, répétitions comprises) ; "
              f"{self.wheel_bursts} salve(s) de molette "
              f"({self.wheel_records} record(s))"
              + (f" ; {self.unknown} record(s) de type INCONNU" if self.unknown else "")
              + ".",
              file=sys.stderr)


FIELDS = ["frame.time_epoch", "ip.src", "tcp.srcport", "ip.dst",
          "tcp.stream", "tcp.payload"]


def build_cmd(tshark: str, args: argparse.Namespace) -> list[str]:
    disp = f"tcp.port=={args.port} && tcp.len>0"
    if args.host:
        disp += f" && ip.addr=={args.host}"
    cmd = [tshark, "-l", "-n", "-Y", disp, "-T", "fields"]
    for f in FIELDS:
        cmd += ["-e", f]
    cmd += ["-E", "separator=\t", "-E", "occurrence=f"]
    if args.read:
        cmd += ["-r", args.read]
    else:
        cmd += ["-i", args.iface]
    return cmd


def parse_line(line: str) -> Event | None:
    """Découpe une ligne tshark (champs séparés par TAB) → tuple exploitable."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 6:
        return None
    ts, src, sport, dst, stream, payload = parts[:6]
    hexstr = payload.replace(":", "").strip()
    if not hexstr:
        return None
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    return (float(ts or 0), src, int(sport or 0), dst, stream, data)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iface", default="en0",
                    help="interface de capture (défaut : en0 / Wi-Fi)")
    ap.add_argument("-r", "--read", metavar="PCAP",
                    help="relire une capture au lieu de sniffer en live")
    ap.add_argument("--host", help="ne montrer que le trafic avec cette IP")
    ap.add_argument("--port", type=int, default=RRCE_PORT, help="port RRCE (défaut 50000)")
    ap.add_argument("--no-color", action="store_true", help="désactiver la couleur")
    ap.add_argument("--keys", action="store_true",
                    help="n'afficher que les appuis de boutons (masque le tactile)")
    ap.add_argument("--list", action="store_true", help="lister les interfaces et quitter")
    args = ap.parse_args()

    tshark = find_tshark()
    if args.list:
        # Le code de retour de tshark devient le nôtre : pas de levée ici.
        sys.exit(subprocess.run([tshark, "-D"], check=False).returncode)

    dec = Decoder(color=not args.no_color, keys_only=args.keys)
    cmd = build_cmd(tshark, args)
    where = f"capture {args.read}" if args.read else f"interface {args.iface}"
    print(f"# RRCE sniff — {where}, port {args.port}"
          + (f", host {args.host}" if args.host else "")
          + "  (Ctrl-C pour arrêter)", file=sys.stderr)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    assert proc.stdout is not None and proc.stderr is not None

    # stderr drainé en continu : sur une capture live longue, un tube plein
    # bloquerait tshark si on n'y lisait qu'à la fin.
    errlines: list[str] = []
    errpump = threading.Thread(target=lambda: errlines.extend(proc.stderr or []),
                               daemon=True)
    errpump.start()

    interrupted = False

    def on_sigint(_sig: int, _frm: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = True
        proc.terminate()
    signal.signal(signal.SIGINT, on_sigint)

    try:
        for line in proc.stdout:
            rec = parse_line(line)
            if rec:
                dec.feed(*rec)
    finally:
        proc.stdout.close()
        rc = proc.wait()
        errpump.join(timeout=1)
        err = "".join(errlines).strip()
        dec.summary()

    # Ctrl-C est une fin normale ; sinon on ne signale l'échec que si tshark a
    # manifestement échoué (code non nul et aucun record décodé).
    if not interrupted and rc != 0 and not dec.total:
        hint = ""
        if "permission" in err.lower() or "bpf" in err.lower():
            hint = ("\nAstuce : capture live sans droits → lancer avec sudo, "
                    "ou installer ChmodBPF (Wireshark).")
        sys.exit(f"\ntshark : {err}{hint}")


if __name__ == "__main__":
    main()
