#!/usr/bin/env python3
"""
udp5800_decode.py — Décodeur du protocole de découverte Raymarine (multicast UDP 5800).

Lit un fichier .pcap ou .pcapng (sans dépendance externe : parseur natif intégré),
extrait les datagrammes UDP à destination du port 5800 et décode :

  - les annonces d'équipement (type 1 / 2) : handle, IP, nom ;
  - les trames de télémétrie/état (type 0) : rtype, flags, longueur et payload,
    dont l'adresse RayNet et l'identifiant, en little- ou big-endian selon flags2.

Voir protocole-udp5800.md pour la spécification du protocole.

Usage :
    python3 udp5800_decode.py capture.pcapng                 # résumé + inventaire
    python3 udp5800_decode.py capture.pcapng --frames        # + détail trame par trame
    python3 udp5800_decode.py capture.pcapng --port 5800     # forcer le port (défaut 5800)
    python3 udp5800_decode.py capture.pcapng --json out.jsonl # export JSON Lines
"""

from __future__ import annotations

import argparse
import json
import struct
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

# (timestamp_float, linktype, raw_bytes)
Packet = tuple[float, int, bytes]
# (src, dst, sport, dport, payload)
UdpDatagram = tuple[str, str, int, int, bytes]
# trame 5800 décodée
Record = dict[str, Any]


# --------------------------------------------------------------------------- #
#  1. Lecture native pcap / pcapng  ->  itérateur de paquets bruts (link layer)
# --------------------------------------------------------------------------- #
def iter_packets(path: Path) -> Iterator[Packet]:
    """Génère (timestamp_float, linktype, raw_bytes) pour chaque paquet du fichier."""
    data = path.read_bytes()
    if len(data) < 4:
        raise ValueError("fichier trop court")
    magic = data[:4]
    if magic == b"\x0a\x0d\x0d\x0a":
        yield from _iter_pcapng(data)
    elif magic in (
        b"\xa1\xb2\xc3\xd4",
        b"\xd4\xc3\xb2\xa1",
        b"\xa1\xb2\x3c\x4d",
        b"\x4d\x3c\xb2\xa1",
    ):
        yield from _iter_pcap(data, magic)
    else:
        raise ValueError(f"format inconnu (magic={magic.hex()})")


def _iter_pcap(data: bytes, magic: bytes) -> Iterator[Packet]:
    le = magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
    nano = magic in (b"\xa1\xb2\x3c\x4d", b"\x4d\x3c\xb2\xa1")
    e = "<" if le else ">"
    linktype = struct.unpack_from(e + "I", data, 20)[0]
    off = 24
    n = len(data)
    while off + 16 <= n:
        ts_sec, ts_frac, caplen, _origlen = struct.unpack_from(e + "IIII", data, off)
        off += 16
        pkt = data[off : off + caplen]
        off += caplen
        ts = ts_sec + ts_frac / (1e9 if nano else 1e6)
        yield ts, linktype, pkt


def _iter_pcapng(data: bytes) -> Iterator[Packet]:
    off, n = 0, len(data)
    e = "<"  # corrigé dès la lecture du SHB
    linktypes: dict[int, int] = {}  # interface_id -> linktype
    tsresol: dict[int, int] = {}  # interface_id -> unités de temps par seconde
    if_index = 0
    while off + 8 <= n:
        btype = struct.unpack_from(e + "I", data, off)[0]
        # Section Header Block : (re)détermine l'endianness
        if btype == 0x0A0D0D0A:
            bom = data[off + 8 : off + 12]
            e = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            if_index = 0
        blen = struct.unpack_from(e + "I", data, off + 4)[0]
        if blen < 12 or off + blen > n:
            break
        body = data[off : off + blen]

        if btype == 0x00000001:  # Interface Description Block
            lt = struct.unpack_from(e + "H", body, 8)[0]
            resol = 1_000_000  # défaut : microsecondes
            # options : parcourir pour if_tsresol (code 9)
            opt = 16
            while opt + 4 <= blen - 4:
                ocode, olen = struct.unpack_from(e + "HH", body, opt)
                if ocode == 0:
                    break
                if ocode == 9 and olen >= 1:
                    r = body[opt + 4]
                    resol = (1 << (r & 0x7F)) if (r & 0x80) else (10 ** (r & 0x7F))
                opt += 4 + ((olen + 3) & ~3)
            linktypes[if_index] = lt
            tsresol[if_index] = resol
            if_index += 1

        elif btype == 0x00000006:  # Enhanced Packet Block
            ifid, th, tl, caplen = struct.unpack_from(e + "IIII", body, 8)
            pkt = body[28 : 28 + caplen]
            ticks = (th << 32) | tl
            ts = ticks / tsresol.get(ifid, 1_000_000)
            yield ts, linktypes.get(ifid, 1), pkt

        elif btype == 0x00000003:  # Simple Packet Block
            caplen = struct.unpack_from(e + "I", body, 8)[0]
            yield 0.0, linktypes.get(0, 1), body[12 : 12 + caplen]

        off += blen
    return


# --------------------------------------------------------------------------- #
#  2. Extraction UDP (Ethernet / VLAN / IPv4 / IPv6)  ->  (src, dst, sport, dport, payload)
# --------------------------------------------------------------------------- #
def extract_udp(linktype: int, pkt: bytes) -> UdpDatagram | None:
    if linktype != 1:  # on ne gère que Ethernet ici
        return None
    if len(pkt) < 14:
        return None
    eth_type = struct.unpack_from(">H", pkt, 12)[0]
    off = 14
    while eth_type == 0x8100 and off + 4 <= len(pkt):  # VLAN 802.1Q
        eth_type = struct.unpack_from(">H", pkt, off + 2)[0]
        off += 4

    if eth_type == 0x0800:  # IPv4
        if off + 20 > len(pkt):
            return None
        ihl = (pkt[off] & 0x0F) * 4
        proto = pkt[off + 9]
        src = ".".join(map(str, pkt[off + 12 : off + 16]))
        dst = ".".join(map(str, pkt[off + 16 : off + 20]))
        l4 = off + ihl
    elif eth_type == 0x86DD:  # IPv6 (en-tête fixe, sans extensions)
        if off + 40 > len(pkt):
            return None
        proto = pkt[off + 6]
        src = pkt[off + 8 : off + 24].hex(":")
        dst = pkt[off + 24 : off + 40].hex(":")
        l4 = off + 40
    else:
        return None

    if proto != 17:  # UDP
        return None
    if l4 + 8 > len(pkt):
        return None
    sport, dport, ulen = struct.unpack_from(">HHH", pkt, l4)
    payload = pkt[l4 + 8 : l4 + max(ulen, 8)]
    return src, dst, sport, dport, payload


# --------------------------------------------------------------------------- #
#  3. Décodage du protocole Raymarine UDP 5800
# --------------------------------------------------------------------------- #
def _ip_le(b: bytes) -> str:  # 4 octets little-endian -> a.b.c.d
    return ".".join(str(x) for x in b[::-1])


# Types d'enregistrement du message 0 (champ @8), groupés par forme de payload.
RT_IP_ECHO = {0x07, 0x0F, 0x13}  # 8 o  : [IP RayNet][écho de flags2]
RT_LONG = {0x10}  # 20 o : identifiant en val[12:16], reste inconnu
RT_IDENT_IP = {0x08, 0x09, 0x1E, 0x23, 0x28, 0x29, 0x2A}
#                                   # 16 o : [ident][index][IP RayNet][écho de flags2]


def domain(word4: bytes) -> str | None:
    """Identifie le domaine d'adressage d'un mot de 4 octets, ou None.

    Un mot dont les deux octets hauts ne désignent ni 198.18 ni 226.192 n'est
    pas une adresse : le rendre quand même sous un préfixe « B: » le faisait
    passer pour un pair du domaine B et polluait l'inventaire — le Quantum_W3
    de pcap/boat-c_axiom9.pcap y inscrivait 417 fois un « e8000100 » qui n'est pas
    une adresse. L'octet brut reste lisible dans le champ `ident`.
    """
    if len(word4) == 4 and word4[2:] == b"\x12\xc6":
        return f"A:198.18.{word4[1]}.{word4[0]}"
    if len(word4) == 4 and word4[2:] == b"\xc0\xe2":
        return f"B:226.192.{word4[1]}.{word4[0]}"
    return None


def parse_5800(payload: bytes) -> Record | None:
    """Décode un payload UDP 5800. Retourne un dict, ou None si non reconnu."""
    if len(payload) < 8:
        return None
    t = struct.unpack_from("<I", payload, 0)[0]

    if t in (1, 2) and len(payload) >= 32:  # annonce d'équipement
        # Les types 1 et 2 sont le MÊME enregistrement, avec une queue de longueur
        # variable — ce ne sont pas deux formats distincts :
        #   @0 type  @4 u1  @8 handle  @12 descriptor  @16 ip(LE)  @20 name(ASCIIZ)
        #   @52 u16 trail_len = nb d'octets à partir de @54  (2 => type 1, 16 => type 2)
        #   @54 flag  @55 = 0   puis, en type 2 seulement, 14 octets d'extension @56..69.
        #   - u1 (@4)   : 🔴 NON RÉSOLU. Dépend du device ET du type de message (nœuds :
        #                 0x11 en type 1, 0x54/0x60/0x87/0x8b en type 2 ; radars : 0x4c/0x4d
        #                 dans les deux). Ce n'est ni une longueur ni un marqueur de classe
        #                 stable : les valeurs de type 2 varient d'un MFD à l'autre au sein
        #                 d'une même capture (0x87 et 0x8b sur pcap/boat-c_axiom7.pcap).
        #   - handle (@8)      : identifiant UNIQUE du device (opaque, stable inter-MFD).
        #   - descriptor (@12) : type/modèle, PARTAGÉ par devices identiques
        #        = [@12 code sous-type][@13=0x00][@14..15 mot de classe : 0b84 nœud / 0000 radar].
        rec: Record = {
            "type": t,
            "handle": payload[8:12].hex(),
            "u1": struct.unpack_from("<I", payload, 4)[0],
            "descriptor": struct.unpack_from("<I", payload, 12)[0],
            "dev_subtype": payload[12],  # octet bas du descriptor
            "class_word": struct.unpack_from("<H", payload, 14)[0],
            "ip": _ip_le(payload[16:20]),
            # nom ASCIIZ : couper au 1er 0x00. Les octets suivants sont un buffer
            # réutilisé non nettoyé (ex. 'r' résiduel de "QuantumRadar" derrière "Quantum_W3").
            "name": payload[20:52].split(b"\x00")[0].decode("latin1", "replace"),
        }
        if len(payload) >= 56:
            trail_len = struct.unpack_from("<H", payload, 52)[0]
            rec["trail_len"] = trail_len
            rec["trail_len_ok"] = trail_len == len(payload) - 54
            rec["flag"] = payload[54]  # @54, valeurs observées 0 ou 1
        if len(payload) >= 70:  # extension du type 2 (@56..69)
            ext = payload[56:70]
            rec["ext"] = ext.hex()
            rec["ext_id"] = struct.unpack_from("<I", ext, 0)[0]  # 🔴 inexpliqué
            rec["ext_variant"] = ext[6]  # @62 : 0x08 nœud / 0x02 radar
            rec["ext_marker"] = struct.unpack_from("<H", ext, 12)[0]
            # Variante radar : ext[8:12] porte l'adresse du bus interne RayNet
            # (198.18.0.0/21) — utile quand l'IP annoncée en @16 n'en est pas une
            # (le QuantumRadar annonce l'IP de son propre point d'accès).
            o = _ip_le(ext[8:12]).split(".")
            if ext[6] == 0x02 and o[0] == "198" and o[1] == "18" and int(o[2]) < 8:
                rec["raynet_ip"] = ".".join(o)
        return rec

    if t == 0 and len(payload) >= 20:  # télémétrie / état
        # En-tête fixe de 20 octets (handle en @4, pas en @8 comme les annonces) :
        #   @0 = 0   @4 handle   @8 rtype   @12 flags1   @16 flags2   @18 len
        #   - rtype (@8)  : type d'enregistrement. Constant par record (ce n'est PAS
        #                   un compteur de séquence).
        #   - flags1 (@12): se lit comme deux u16 (a, b). b = 30 pour les records
        #                   instruments, 100 pour les records radar, 0 pour 0x2a.
        #   - flags2 (@16): le bit 0x0800 indique l'ordre des octets de la payload
        #                   (mis => little-endian). 🟡 Un seul type big-endian (0x2a)
        #                   a été observé et son flags2 vaut 0x0000 : impossible
        #                   d'isoler le bit exact — tous ses bits sont à zéro.
        #   - len (@18)   : longueur de la payload (vérifié sur 100 % des trames).
        rtype = struct.unpack_from("<I", payload, 8)[0]
        flags2 = struct.unpack_from("<H", payload, 16)[0]
        ln = struct.unpack_from("<H", payload, 18)[0]
        val = payload[20 : 20 + ln]
        little = bool(flags2 & 0x0800)
        rec = {
            "type": 0,
            "handle": payload[4:8].hex(),
            "rtype": rtype,
            "flags1": payload[12:16].hex(),
            "flags1_a": struct.unpack_from("<H", payload, 12)[0],
            "flags1_b": struct.unpack_from("<H", payload, 14)[0],
            "flags2": flags2,
            "little_endian": little,
            "len": ln,
            "len_ok": len(val) == ln,
            "value": val.hex(),
        }
        end = "<" if little else ">"

        def _u32(o: int) -> int:
            return struct.unpack_from(end + "I", val, o)[0]

        def _ip(o: int) -> str:
            w = val[o : o + 4]
            return _ip_le(w) if little else ".".join(str(x) for x in w)

        if rtype in RT_IP_ECHO and len(val) >= 8:
            rec["raynet_ip"] = _ip(0)
            rec["echo"] = _u32(4)
        elif rtype in RT_LONG and len(val) >= 20:
            rec["ident"] = val[12:16].hex()
        elif rtype in RT_IDENT_IP and len(val) >= 16:
            rec["ident"] = val[0:4].hex()
            if little and (d := domain(val[0:4])):
                rec["ident_domain"] = d
            rec["index"] = _u32(4)
            rec["raynet_ip"] = _ip(8)
            rec["echo"] = _u32(12)
        # Le dernier u32 réécho le champ flags2 sur les records instruments
        # (vérifié 26/26), jamais sur les records radar 0x28/0x29 ni sur 0x2a.
        if "echo" in rec:
            rec["echo_ok"] = rec["echo"] == flags2
        return rec

    # Ni annonce ni télémétrie : le port 5800 porte aussi des datagrammes qui
    # ne relèvent pas du protocole de découverte. pcap/boat-c_axiom7.pcap en donne
    # deux, émis vers 224.0.0.1:5800 — un paquet magique Wake-on-LAN (ff×6 puis
    # 00:11:c7:xx:xx:xx seize fois, OUI Raymarine) et une sonde
    # « ABCDEFGHIJKLMNOP ». On les rend tels quels, sans « handle » : les lire à
    # l'offset 4 comme les trames de type 0 fabriquait un identifiant qui
    # n'existe pas.
    return {"type": t, "raw": payload.hex()}


# --------------------------------------------------------------------------- #
#  4. Programme principal
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Décodeur Raymarine UDP 5800 (.pcap/.pcapng)"
    )
    ap.add_argument("pcap", type=Path, help="fichier de capture .pcap ou .pcapng")
    ap.add_argument(
        "--port", type=int, default=5800, help="port UDP à décoder (défaut 5800)"
    )
    ap.add_argument(
        "--frames", action="store_true", help="afficher chaque trame décodée"
    )
    ap.add_argument(
        "--json", type=Path, metavar="FICHIER", help="exporter les trames en JSON Lines"
    )
    args = ap.parse_args()

    # handle -> {names, ips, t1, t2}
    devices: dict[str, dict[str, Any]] = {}
    rt_counts: Counter[tuple[str, int, int]] = Counter()  # (handle, rtype, len) -> n
    telem_by_handle: Counter[str] = Counter()
    peer_map: defaultdict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"A": set(), "B": set()}
    )
    n_total = n_5800 = n_bad = n_other = 0
    t0: float | None = None
    jout: TextIO | None = args.json.open("w") if args.json else None

    for ts, linktype, pkt in iter_packets(args.pcap):
        n_total += 1
        u = extract_udp(linktype, pkt)
        if not u:
            continue
        _src, _dst, sport, dport, payload = u
        if dport != args.port and sport != args.port:
            continue
        rec = parse_5800(payload)
        if rec is None:
            continue
        n_5800 += 1
        if t0 is None:
            t0 = ts
        rel = ts - t0

        if rec["type"] in (1, 2):
            d = devices.setdefault(
                rec["handle"],
                {"names": set(), "ips": set(), "raynet": set(), "t1": 0, "t2": 0},
            )
            if rec["name"]:
                d["names"].add(rec["name"])
            d["ips"].add(rec["ip"])
            if rec.get("raynet_ip"):
                d["raynet"].add(rec["raynet_ip"])
            d[f"t{rec['type']}"] += 1
        elif rec["type"] == 0:
            telem_by_handle[rec["handle"]] += 1
            rt_counts[(rec["handle"], rec["rtype"], rec["len"])] += 1
            if not rec.get("len_ok", True):
                n_bad += 1
            if rec.get("raynet_ip"):
                peer_map[rec["handle"]]["A"].add(rec["raynet_ip"])
            dom_b = rec.get("ident_domain", "")
            if dom_b.startswith("B:"):
                peer_map[rec["handle"]]["B"].add(dom_b[2:])
        else:
            n_other += 1

        if args.frames:
            extra = ""
            if "raw" in rec:
                pass
            elif rec["type"] == 0:
                extra = (
                    f"rtype={rec['rtype']:#04x} len={rec['len']:<2d} "
                    f"flags2={rec['flags2']:#06x} "
                    f"{'LE' if rec['little_endian'] else 'BE'}"
                )
                if "raynet_ip" in rec:
                    extra += f" raynet={rec['raynet_ip']}"
                if "ident" in rec:
                    extra += f" ident={rec['ident']}"
                if rec.get("echo_ok") is False:
                    extra += " echo!=flags2"
            elif rec["type"] in (1, 2):
                extra = f"ip={rec['ip']} name={rec['name']!r}"
                if rec.get("raynet_ip"):
                    extra += f" raynet={rec['raynet_ip']}"
            if "raw" in rec:
                print(f"[{rel:9.3f}] non reconnue type={rec['type']:#010x} "
                      f"len={len(payload)} {rec['raw'][:64]}")
            else:
                print(f"[{rel:9.3f}] type={rec['type']} handle={rec['handle']} {extra}")

        if jout:
            rec["_t"] = round(rel, 6)
            jout.write(json.dumps(rec) + "\n")

    if jout:
        jout.close()

    # ---- Rapport de synthèse ----
    print("\n=== Résumé ===")
    print(f"Paquets lus         : {n_total}")
    print(
        f"Trames UDP {args.port:<5d}    : {n_5800} "
        f"(dont {n_bad} à longueur incohérente)"
    )
    if n_other:
        print(f"  dont non reconnues : {n_other} (ni annonce ni télémétrie)")

    print("\n=== Équipements découverts ===")
    if not devices:
        print("  (aucune annonce type 1/2)")
    for h, d in sorted(devices.items()):
        names = ", ".join(sorted(d["names"])) or "(sans nom)"
        ips = ", ".join(sorted(d["ips"]))
        pm = peer_map.get(h, {})
        dom = ""
        if pm:
            a = ", ".join(sorted(pm["A"])) or "-"
            b = ", ".join(sorted(pm["B"])) or "-"
            dom = f"  [A:{a} | B:{b}]"
        ray = ", ".join(sorted(d["raynet"]))
        ray = f"  raynet={ray}" if ray else ""
        print(
            f"  handle={h}  {names:<14}  ip={ips:<16}  "
            f"annonces T1={d['t1']} T2={d['t2']}  "
            f"télémétrie={telem_by_handle.get(h, 0)}{ray}{dom}"
        )

    print("\n=== Records type 0 (handle, rtype, len) ===")
    for (h, rt, ln), c in sorted(rt_counts.items()):
        print(f"  handle={h}  rtype={rt:#04x}  len={ln:<2d}  n={c}")


if __name__ == "__main__":
    main()
