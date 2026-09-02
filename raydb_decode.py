#!/usr/bin/env python3
"""
raydb_decode.py — décodeur du protocole Raymarine "RayDB" (TCP 23333).

Le protocole est un bus de souscription clé/valeur. Chaque message TCP est une
trame binaire little-endian :

    [u32 len]           longueur du reste de la trame (octets qui suivent)
    [u32 msg_type]      toujours 1 observé
    [u8  op]            opcode : 3=SUBSCRIBE, 4=UPDATE(valeur), 5/6=ACK, 7=HELLO
    [u32 path_len]      longueur de la chaîne "chemin"
    [u32 pad]           réservé / flags (0 observé)
    [path_len octets]   chemin ASCII  ("data/sog", "diag/mfd/.../version", ...)
    [reste]             bloc valeur (voir decode_value)

Le bloc valeur est [3 octets réservés][u32 type][valeur][4 octets 00], donc la
valeur commence à l'offset 7. Le type est un **u32**, et l'énumération se lit
comme une suite d'entiers de largeur croissante suivie des composites :

    0 bool(1)   1 i8(1)   2 i16(2)   3 i32(4)   4 i64(8)
    7 u32(4)    9 f32(4)  10 f64(8)  11 chaîne [u64 len][octets]

    13 = liste  : [u64 n] puis n × ([u32 type][valeur])
    14 = table  : [u64 n] puis n × ([u64 klen][clé][u32 type][valeur])

Une valeur suit toujours immédiatement son type — seules les chaînes portent
une longueur —, et listes comme tables s'imbriquent. Les types 5, 6, 8 et 12
n'apparaissent dans aucune capture ; la suite laisse deviner u8, u16 et u64,
mais rien ne le vérifie.

La table (14) porte les `diag/…`, où elle n'a qu'une entrée dont le nom
redouble le dernier segment du chemin. Ce n'est qu'un cas particulier :.

Les 4 octets de queue sont nuls sur les UPDATE ; sur les KEEPALIVE (op 5/6) ils
portent un compteur qui s'incrémente.

Tous les entiers sont little-endian. Unités observées : angles en radians,
vitesses en m/s, profondeurs en mètres ; data/position est une chaîne
"latitude,longitude".

Le script lance tshark lui-même pour extraire les charges utiles TCP, en
neutralisant le dissecteur Lua raymarine_raydb qui consommerait sinon la
charge utile (voir payloads_from_tshark), puis recolle les segments d'un même
flux (voir reassemble).

Usage :
    python3 raydb_decode.py cap.pcapng                  # décodage direct
    python3 raydb_decode.py cap.pcapng --port 23333     # forcer le port
    tshark ... -T fields -e tcp.srcport -e data.data | python3 raydb_decode.py

Sur stdin, un champ « -e frame.time_epoch » intercalé entre les deux autres est
accepté et rend l'instant de capture ; sans lui, l'instant est simplement None.
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

OPS: dict[int, str] = {3: "SUBSCRIBE", 4: "UPDATE", 5: "ACK", 6: "ACK", 7: "HELLO"}

# Pile de dissection autorisée pour tshark : liste blanche, donc aucun plugin
# Lua ni dissecteur heuristique ne peut s'intercaler. « data » livre la charge
# utile TCP, vlan/ipv6 évitent un silence total sur une capture taguée ou v6.
ONLY_PROTOCOLS = "eth,vlan,ip,ipv6,tcp,data"

# (port source, instant de capture en epoch — None si non fourni —, charge
# utile TCP en hexadécimal)
Payload = tuple[int, float | None, str]

# Segment TCP en entrée du recollage : (flux, port source, instant, octets).
Segment = tuple[str | None, int, float | None, bytes]


def u32(b: bytes, o: int) -> int:  return struct.unpack_from("<I", b, o)[0]
def u64(b: bytes, o: int) -> int:  return struct.unpack_from("<Q", b, o)[0]


# --------------------------------------------------------------------------- #
#  1. Extraction des charges utiles TCP via tshark
# --------------------------------------------------------------------------- #
def payloads_from_tshark(pcap: Path, port: int, tshark: str) -> Iterator[Payload]:
    """Génère (port_source, instant, hex) pour chaque segment TCP porteur de données.

    L'hexadécimal rendu ne contient que des **trames entières** : les segments
    d'un même flux sont recollés au passage (voir reassemble).

    L'instant est celui de la capture (frame.time_epoch), ce qui permet de
    rejouer une capture en la datant comme elle l'a été et non à l'horloge du
    rejeu (cf. `raydb_client.py --replay`). Une trame à cheval sur plusieurs
    segments porte l'instant de celui qui l'achève.

    --only-protocols borne la pile de dissection : sans cela le dissecteur Lua
    dissectors/raymarine_raydb.lua, s'il est installé, consomme la charge utile du port
    23333 et « data.data » ressort vide.

    On lit « data.data » et non « tcp.payload » parce que tshark y remet les
    segments dans l'ordre et écarte les retransmissions. Ce n'est en revanche
    **pas** un PDU réassemblé : le dissecteur « data » ne demande jamais de
    désegmentation, si bien que data.data vaut exactement la charge utile d'un
    segment. D'où le recollage, sans lequel une trame à cheval
    était perdue et la suivante lue à contretemps — 20 567 trames rendues sur
    66 177 pour cette capture, dont 230 aberrantes.

    `-l` vide la sortie de tshark après chaque paquet. Sans lui, la sortie d'un
    tuyau est tamponnée par blocs, une sélection
    rendant 8 ko de lignes n'arrivait qu'à la sortie de tshark, alors que les
    paquets concernés étaient lus 0,3 s plus tôt — un rejeu d'une capture où le
    trafic RayDB est clairsemé restait muet jusqu'à la fin. Le surcoût est dans
    le bruit (1,05 s contre 1,01 s sur cette capture de 204 Mo).
    """
    cmd = [
        tshark, "-r", str(pcap), "-n", "-l",
        "--only-protocols", ONLY_PROTOCOLS,
        "-Y", f"tcp.port=={port} && tcp.len>0",
        # Ordre fixe, lu positionnellement par _parse_tshark_line. Le format
        # documenté en tête de fichier (sans tcp.stream) reste celui de stdin,
        # que parse_field_line comprend seul.
        "-T", "fields", "-e", "tcp.stream", "-e", "tcp.srcport",
        "-e", "frame.time_epoch", "-e", "data.data",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    assert proc.stdout is not None and proc.stderr is not None
    try:
        segments = (s for s in map(_parse_tshark_line, proc.stdout) if s)
        yield from reassemble(segments)
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read()
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"tshark a échoué (code {rc}) : {stderr.strip()}")


def _parse_tshark_line(line: str) -> Segment | None:
    """Décode une ligne des quatre champs demandés par payloads_from_tshark."""
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) != 4 or not parts[3]:
        return None
    try:
        srcport = int(parts[1])
        data = bytes.fromhex(parts[3])
    except ValueError:
        return None
    try:
        when: float | None = float(parts[2])
    except ValueError:
        when = None
    return parts[0], srcport, when, data


def parse_field_line(line: str) -> Payload | None:
    """Décode une ligne « -T fields » : port source [<TAB> instant] <TAB> hex."""
    # rstrip du seul saut de ligne, surtout pas des tabulations : un segment
    # sans data.data (fragment d'un PDU réassemblé plus loin) sort avec un
    # dernier champ vide, et `strip()` effaçait le séparateur qui le signale —
    # l'instant de capture passait alors pour la charge utile.
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) < 2 or not parts[-1]:
        return None
    try:
        srcport = int(parts[0])
    except ValueError:
        return None
    when: float | None = None
    if len(parts) >= 3:                      # champ intercalé : frame.time_epoch
        try:
            when = float(parts[1])
        except ValueError:
            pass
    return srcport, when, parts[-1]


def reassemble(segments: Iterable[Segment]) -> Iterator[Payload]:
    """Recolle les segments d'un même flux et ne rend que des trames entières.

    Un segment TCP ne commence pas sur une frontière de trame : les trames
    RayDB s'y enchaînent sans alignement, et les grosses (`Settings/Data/…`,
    plusieurs kilo-octets) en couvrent plusieurs. Le reliquat d'une trame à
    cheval est donc gardé jusqu'au segment suivant du **même** flux et du même
    sens — deux sens qui partagent un tampon se mélangeraient.

    L'instant rendu est celui du segment qui achève les trames rendues.
    """
    buffers: dict[tuple[str | None, int], bytes] = {}
    for stream, srcport, when, data in segments:
        key = (stream, srcport)
        buf = buffers.get(key, b"") + data
        o = 0
        while o + 4 <= len(buf):
            end = o + 4 + u32(buf, o)
            if end > len(buf):               # trame incomplète : on attend
                break
            o = end
        buffers[key] = buf[o:]
        if o:
            yield srcport, when, buf[:o].hex()


# --------------------------------------------------------------------------- #
#  2. Décodage des trames RayDB
# --------------------------------------------------------------------------- #
# Types scalaires du bloc valeur, et leur format struct.
SCALAR_FMT: dict[int, str] = {
    0: "?", 1: "b", 2: "h", 3: "i", 4: "q",
    5: "B", 6: "H", 7: "I", 8: "Q", 9: "f", 10: "d",
}
TYPE_F32 = 9                                 # flottant 32 bits, à traiter à part
TYPE_STR = 11                                # [u64 len][octets]
TYPE_LIST = 13                               # [u64 n] puis n valeurs typées
TYPE_NAMED = 14                              # table [u64 n] puis n × [clé, valeur]
TYPE_NAMES: dict[int, str] = {
    0: "bool", 1: "i8", 2: "i16", 3: "i32", 4: "i64", 5: "u8", 6: "u16",
    7: "u32", 8: "u64", 9: "f32", 10: "f64", 11: "str", 13: "list", 14: "map",
}

# valeur décodée : scalaire, chaîne, liste ou table
Value = bool | int | float | str | list["Value"] | dict[str, "Value"]


def read_value(b: bytes, o: int, vtype: int) -> tuple[Value, int] | None:
    """Lit la valeur de type `vtype` à l'offset `o`. Retourne (valeur, offset_fin).

    Les listes et les tables s'imbriquent, d'où la récursion : une table de
    `sf/…` porte des entrées de type 0, 3, 9 ou 11, une liste de
    `Settings/Data/…` des entiers de largeurs mêlées.
    """
    fmt = SCALAR_FMT.get(vtype)
    if fmt is not None:
        return struct.unpack_from("<" + fmt, b, o)[0], o + struct.calcsize(fmt)

    if vtype == TYPE_STR:
        n = u64(b, o)
        raw = b[o + 8:o + 8 + n]
        if len(raw) < n:
            return None
        return raw.split(b"\0")[0].decode("latin1", "replace"), o + 8 + n

    if vtype in (TYPE_LIST, TYPE_NAMED):
        n = u64(b, o)
        if n > len(b):            # compteur aberrant : bloc tronqué ou mal cadré
            return None
        o += 8
        items: list[Value] = []
        entries: dict[str, Value] = {}
        for _ in range(n):
            key = ""
            if vtype == TYPE_NAMED:
                klen = u64(b, o)
                o += 8
                key = b[o:o + klen].split(b"\0")[0].decode("latin1", "replace")
                o += klen
            inner = read_value(b, o + 4, u32(b, o))
            if inner is None:
                return None
            if vtype == TYPE_NAMED:
                entries[key] = inner[0]
            else:
                items.append(inner[0])
            o = inner[1]
        return (entries if vtype == TYPE_NAMED else items), o

    return None


def format_value(v: Value) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, list):
        return "[" + ", ".join(format_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {format_value(x)}" for k, x in v.items()) + "}"
    return str(v)


def decode_typed(b: bytes) -> tuple[str | None, Value, int] | None:
    """Décode le bloc valeur d'un UPDATE en (nom, valeur **native**, type), ou
    None si le bloc n'est pas reconnu. Le nom vaut None hors table à une seule
    entrée ; le type est celui de la valeur, l'interne pour une telle table.

    C'est la forme utile aux consommateurs qui calculent (passerelle web, NMEA) ;
    decode_value n'en est que le rendu texte. Le type accompagne la valeur parce
    qu'un flottant ne se restitue pas sans lui : Python n'a que le double, où un
    float32 reçu s'étale en chiffres qu'il n'a jamais portés.

    La table à une entrée unique — la forme des `diag/…` — garde son rendu
    historique (nom, valeur, type interne), qui laisse l'appelant comparer ce
    nom au dernier segment du chemin. Au-delà d'une entrée, la table est rendue
    telle quelle : la réduire à sa première entrée perdait les autres en
    silence.
    """
    if len(b) < 8 or b[0:3] != b"\0\0\0":
        return None
    vtype = u32(b, 3)
    try:
        if vtype == TYPE_NAMED and u64(b, 7) == 1:
            klen = u64(b, 15)
            name = b[23:23 + klen].split(b"\0")[0].decode("latin1", "replace")
            o = 23 + klen
            inner = read_value(b, o + 4, u32(b, o))
            return None if inner is None else (name, inner[0], u32(b, o))
        value = read_value(b, 7, vtype)
        return None if value is None else (None, value[0], vtype)
    except (struct.error, IndexError):
        return None


def decode_value(b: bytes, leaf: str = "") -> str | None:
    """Décode le bloc valeur d'un UPDATE, ou None si le bloc n'est pas reconnu.

    Les tables à une entrée (type 14) réencodent le nom du champ, qui est le
    dernier segment du chemin sur la plupart des captures : on ne le préfixe
    que s'il en diffère, `leaf` étant ce dernier segment.
    """
    typed = decode_typed(b)
    if typed is None:
        return None
    name, value, _vtype = typed
    val = format_value(value)
    return val if name in (None, leaf) else f"{name} = {val}"


def decode_frame(b: bytes) -> str | None:
    if len(b) < 17:
        return None
    op = b[8]
    path_len = u32(b, 9)
    o = 17
    path = b[o:o + path_len].split(b"\0")[0].decode("latin1")
    o += path_len
    rest = b[o:]
    val = decode_value(rest, path.rsplit("/", 1)[-1]) if rest and op == 4 else None
    label = OPS.get(op, f"op{op}")
    if val is None:
        return f"{label:9} {path}"
    return f"{label:9} {path:<46} → {val}"


def frames_from_hex(hexstr: str) -> Iterator[bytes]:
    """Un paquet TCP peut contenir plusieurs trames RayDB concaténées."""
    b = bytes.fromhex(hexstr)
    o = 0
    while o + 4 <= len(b):
        length = u32(b, o)
        frame = b[o:o + 4 + length]
        if len(frame) < 4 + length:
            break
        yield frame
        o += 4 + length


# --------------------------------------------------------------------------- #
#  3. Programme principal
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Décodeur Raymarine RayDB (TCP 23333)")
    ap.add_argument("pcap", type=Path, nargs="?",
                    help="capture .pcap/.pcapng ; à défaut, lit la sortie "
                         "« -T fields -e tcp.srcport -e data.data » sur stdin")
    ap.add_argument("--port", type=int, default=23333, help="port TCP RayDB (défaut 23333)")
    ap.add_argument("--tshark", default="tshark", help="chemin de l'exécutable tshark")
    args = ap.parse_args()

    if args.pcap:
        payloads: Iterator[Payload] = payloads_from_tshark(args.pcap, args.port, args.tshark)
    else:
        # Sur stdin le flux n'est pas dit : on recolle par port source, ce qui
        # suffit tant que la capture ne porte qu'une connexion RayDB.
        stdin_payloads = (p for p in map(parse_field_line, sys.stdin) if p)
        payloads = reassemble(
            (None, sp, when, bytes.fromhex(hx)) for sp, when, hx in stdin_payloads
        )

    try:
        for srcport, _when, hexstr in payloads:
            arrow = "S→C" if srcport == args.port else "C→S"
            for frame in frames_from_hex(hexstr):
                out = decode_frame(frame)
                if out:
                    print(f"{arrow} {out}")
    except BrokenPipeError:
        # sortie tronquée par un « | head » : fin normale, mais il faut couper
        # stdout pour que Python ne rejoue pas l'erreur en vidant les tampons.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (RuntimeError, OSError) as exc:
        sys.exit(f"{ap.prog}: {exc}")


if __name__ == "__main__":
    main()
