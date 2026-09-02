"""
disco5800.py — beacon de découverte propriétaire, multicast UDP 224.0.0.1:5800.

Reproduit le push unidirectionnel décrit dans « 1. protocole-udp5800.md » : le
MFD relaie sur le Wi-Fi les équipements du backbone interne SeaTalkHS, à raison
d'un beacon toutes les ~0,72 s par équipement. Aucune réponse n'est attendue.

Trois types de message, discriminés par un u32 de tête :

    type 1 (56 o)  annonce d'équipement, compacte
    type 2 (70 o)  annonce d'équipement, étendue (+ bloc de 18 o)
    type 0         télémétrie/état, encodée en TLV

Le piège que doit reproduire le simulateur : l'IP contenue **dans** l'annonce
est une adresse interne `198.18.x.x`, non joignable depuis le Wi-Fi. Un client
correct se connecte à l'**IP source du datagramme**, pas à celle qu'il lit
dedans (c'est ce que fait `raydb_client.py`).
"""
from __future__ import annotations

import logging
import socket
import struct
import threading

from . import config

log = logging.getLogger("disco5800")

TYPE_ANNOUNCE = 1
TYPE_ANNOUNCE_EXT = 2
TYPE_TELEMETRY = 0

ANNOUNCE_LEN = 56
ANNOUNCE_EXT_LEN = 70
NAME_FIELD_LEN = 32       # zone de nom @20..51, ASCIIZ

# u1 (@4) : rôle NON RÉSOLU. Ce n'est pas une longueur (0x4c = 76 dépasse les
# 56 o du payload) ni un marqueur de classe stable : chez les nœuds il change
# entre le type 1 (0x11) et le type 2 (0x54 ou 0x60), alors qu'il reste identique
# chez les radars. On rejoue donc les valeurs relevées, par équipement et par
# type de message. Cf. « 1. protocole-udp5800.md » §4.
U1_NODE_T1 = 0x11         # nœud instrument, annonce compacte
U1_RADAR = 0x4C           # radar Quantum, antenne (identique en type 1 et 2)
U1_RADAR_W3 = 0x4D        # radar Quantum, canal Wi-Fi (idem)

# Formes de payload du message 0, indexées par record type (@8).
RT_IP_ECHO = frozenset({0x07, 0x0F, 0x13})          # 8 o  : [IP][écho]
RT_IDENT_IP = frozenset({0x08, 0x09, 0x1E, 0x23, 0x28, 0x29, 0x2A})  # 16 o

# Enregistrements de type 0 rejoués par un nœud instrument, tels que relevés en
# capture : (rtype, flags1_a, flags1_b, flags2, index).
#
# `rtype` n'est PAS une clé unique : 0x08 apparaît avec plusieurs (flags2, index).
# C'est le couple (rtype, flags2) qui identifie l'enregistrement. `flags1_a` est
# lié au rtype : 0x01 pour 07/0f/13/10/23, 0x04 pour 09/1e, 0x13 pour 08.
NODE_RECORDS = (
    (0x07, 0x01, 30, 0x0807, 0),
    (0x0F, 0x01, 30, 0x0805, 0),
    (0x13, 0x01, 30, 0x0806, 0),
    (0x23, 0x01, 30, 0x0802, 0),
    (0x08, 0x13, 30, 0x0808, 2),
    (0x09, 0x04, 30, 0x0809, 3),
    (0x1E, 0x04, 30, 0x080A, 5),
    (0x08, 0x04, 30, 0x080B, 6),
)

# 19030ad5 n'expose qu'un sous-ensemble (plus un record 0x2a big-endian, non simulé).
NODE_RECORDS_MIN = (
    (0x23, 0x01, 30, 0x0802, 0),
    (0x08, 0x13, 30, 0x0803, 1),
)

# Variante du bloc d'extension du type 2 (octet @62) : dit comment lire @64.
EXT_VARIANT_NODE = 0x08   # @64 ne porte pas d'adresse
EXT_VARIANT_RADAR = 0x02  # @64 = adresse RayNet (198.18.0.0/21)

# Le descriptor (@12) est le type/modèle : il est PARTAGÉ par des équipements
# identiques (ce n'est ni un compteur, ni un identifiant unique — c'est le
# handle @8 qui est unique). Ses octets hauts forment le « mot de classe », dont
# les clients se servent comme heuristique : 0x840b => nœud/MFD, 0x0000 =>
# radar/capteur (valeur <= 0xff). Cf. parse_5800() dans raydb_client.py.
DESC_NODE = 0x840B0067
DESC_RADAR = 0xA2
DESC_RADAR_W3 = 0xCD


class Device:
    """Un équipement annoncé sur le bus, avec ses deux domaines d'adressage."""

    def __init__(self, handle: int, name: str, ip: str, descriptor: int,
                 u1_t1: int, u1_t2: int, ident: bytes = b"\x81\xe4\xc0\xe2",
                 flag: int = 0, ext_id: int = 0, ext_marker: int = 0x8118,
                 ext_ip: str | None = None,
                 records: tuple = NODE_RECORDS) -> None:
        self.handle = handle              # @8  : identifiant UNIQUE de l'équipement
        self.name = name
        self.ip = ip
        self.descriptor = descriptor      # @12 : type/modèle (partagé)
        self.u1_t1 = u1_t1                # @4 en annonce compacte (type 1)
        self.u1_t2 = u1_t2                # @4 en annonce étendue  (type 2)
        self.ident = ident                # identifiant porté par la payload type 0
        self.records = records            # enregistrements de type 0 à émettre
        self.flag = flag                  # octet @54 (queue type 1 / bloc étendu)
        self.ext_id = ext_id              # bloc étendu : id stable par session
        self.ext_marker = ext_marker      # bloc étendu : marqueur de fin (u16)
        # IP portée par le bloc étendu d'un radar : c'est celle de l'unité
        # PHYSIQUE (backbone), que les deux annonces du même radar partagent —
        # pas forcément l'IP annoncée en @16.
        self.ext_ip = ext_ip or ip
        self.seq = 0

    @property
    def is_node(self) -> bool:
        return self.u1_t1 == U1_NODE_T1


def _ip_le(ip: str) -> bytes:
    """IP encodée en little-endian, comme dans les trames (192.168.0.146 → 92 00 a8 c0)."""
    return bytes(reversed(socket.inet_aton(ip)))


def _ext_block(dev: Device) -> bytes:
    """Extension du type 2 : 14 octets placés en @56, indexés ici 0..13.

        nœud  : [00000000][00 00 08 00][03 00 ext_id 00][marqueur]
        radar : [ext_id u32][08 00 02 00][IP RayNet LE ][marqueur]

    L'octet d'index 6 (@62) est le sélecteur de variante : il indique si les
    octets 8..11 doivent se lire comme une adresse IPv4. Chez le radar ils
    portent son adresse sur le bus interne RayNet (198.18.0.0/21), ce qui est
    l'apport du type 2 — le radar annonce en @16 l'IP de son propre point
    d'accès, hors RayNet. Chez le nœud, l'IP annoncée est déjà une adresse
    RayNet et ce champ ne porte pas d'adresse.
    """
    if dev.is_node:
        return (b"\x00\x00\x00\x00"
                + bytes([0x00, 0x00, EXT_VARIANT_NODE, 0x00])
                + bytes([0x03, 0x00, dev.ext_id & 0xFF, 0x00])
                + struct.pack("<H", dev.ext_marker))
    return (struct.pack("<I", dev.ext_id)
            + bytes([0x08, 0x00, EXT_VARIANT_RADAR, 0x00])
            + _ip_le(dev.ext_ip)
            + struct.pack("<H", dev.ext_marker))


def build_announce(dev: Device, extended: bool = False) -> bytes:
    """Trame d'annonce : type 1 (56 o) et type 2 (70 o).

    Les deux types sont le MÊME enregistrement : un en-tête commun de 52 octets,
    puis une queue préfixée par sa longueur (u16 en @52, comptée à partir de
    @54). La queue vaut 2 octets en type 1 et 16 en type 2, d'où les deux
    tailles ; le type 2 ajoute donc 14 octets d'extension.

    Le nom est un ASCIIZ dans une zone de 32 o (@20..51). Sur un vrai MFD cette
    zone n'est écrasée que jusqu'au NUL — le buffer est réutilisé, si bien qu'on
    y lit des résidus d'annonces précédentes. Le simulateur la remplit de zéros :
    c'est conforme pour tout lecteur qui coupe au premier NUL, comme il se doit.
    """
    mtype = TYPE_ANNOUNCE_EXT if extended else TYPE_ANNOUNCE
    head = (struct.pack("<I", mtype)
            + struct.pack("<I", dev.u1_t2 if extended else dev.u1_t1)
            # handle : suite de 4 octets opaque, émise telle qu'elle se lit en
            # capture (0xB83942D2 -> b8 39 42 d2), donc en big-endian.
            + struct.pack(">I", dev.handle)
            + struct.pack("<I", dev.descriptor)
            + _ip_le(dev.ip)
            + dev.name.encode("latin1")[:NAME_FIELD_LEN - 1]
                      .ljust(NAME_FIELD_LEN, b"\x00"))
    trail = bytes([dev.flag, 0x00]) + (_ext_block(dev) if extended else b"")
    return head + struct.pack("<H", len(trail)) + trail


def build_telemetry(dev: Device, rtype: int, f1a: int, f1b: int,
                    flags2: int, index: int) -> bytes:
    """Trame de type 0 (télémétrie / état).

        [u32 0][4 handle][u32 rtype][u16 a][u16 b][u16 flags2][u16 len][payload]

    L'en-tête fait 20 octets. La forme de la payload dépend du `rtype` :

        RT_IP_ECHO  (8 o)  : [IP RayNet][écho de flags2]
        RT_IDENT_IP (16 o) : [ident][index][IP RayNet][écho de flags2]

    Le dernier u32 réécho `flags2`, ce qui rend l'enregistrement auto-référencé —
    vrai pour tous les records instruments. Les radars, eux, y placent une donnée
    variable dont seuls les 16 bits de poids faible sont stables (et valent le
    marqueur porté par leur annonce de type 2) : on reproduit ce comportement.

    Le bit 0x0800 de `flags2` annonce une payload little-endian ; le simulateur
    n'émet que du little-endian (seul le record 0x2a, non simulé, est big-endian).
    """
    if dev.is_node:
        tail = struct.pack("<I", flags2)
    else:
        dev.seq = (dev.seq + 1) & 0xFFFF
        tail = struct.pack("<HH", dev.ext_marker, dev.seq)

    # L'adresse portée par la payload est toujours celle du bus RayNet, qui n'est
    # pas l'adresse annoncée en @16 pour un radar (celle-ci désigne son propre AP).
    ip = _ip_le(dev.ext_ip)
    if rtype in RT_IP_ECHO:
        payload = ip + tail
    else:
        # Chez les nœuds, l'octet bas de l'identifiant suit l'index : 0x81 + index
        # (index 0 -> ...81, 1 -> ...82, 3 -> ...84, 5 -> ...86). Un radar porte
        # au contraire un identifiant opaque fixe, et son 2e mot n'est pas un
        # index — on garde ici le gabarit des nœuds, suffisant pour un client.
        ident = (bytes([dev.ident[0] + index]) + dev.ident[1:]
                 if dev.is_node else dev.ident)
        payload = (ident
                   + bytes([index, 0x0A, 0x00, 0x00])
                   + ip
                   + tail)
    return (struct.pack("<I", TYPE_TELEMETRY)
            + struct.pack(">I", dev.handle)      # même ordre que dans l'annonce
            + struct.pack("<I", rtype)
            + struct.pack("<HH", f1a, f1b)       # flags1 : b=30 instrument, 100 radar
            + struct.pack("<HH", flags2, len(payload))
            + payload)


def _devices(ip: str) -> list[Device]:
    """Le MFD lui-même, plus les équipements qu'il relaie depuis le backbone.

    Le MFD s'annonce avec l'IP interne du backbone, jamais avec son IP Wi-Fi :
    c'est bien l'IP source du datagramme qui porte l'adresse joignable.
    """
    # La zone de nom fait 32 o : le modèle complet y tient largement. (Les nœuds
    # des captures étaient anonymes — mettre un nom aide à repérer le simulateur.)
    # flag / ext_id / ext_marker reprennent les valeurs relevées en capture.
    return [
        Device(0xB83942D2, config.MODEL, config.BACKBONE_IP, DESC_NODE,
               U1_NODE_T1, 0x54, b"\x81\xe4\xc0\xe2",
               flag=1, ext_id=0x21, ext_marker=0x8118),
        # Le radar est UN seul équipement physique exposé par deux annonces : la
        # première porte l'IP de son point d'accès, la seconde celle du backbone.
        # Les deux extensions portent donc la même adresse RayNet.
        Device(0x981E82CB, "QuantumRadar", "192.168.0.146", DESC_RADAR,
               U1_RADAR, U1_RADAR, b"\x01\x92\x01\xe8",
               flag=0, ext_id=0x1D4, ext_marker=0x0A0F, ext_ip="198.18.5.44",
               records=((0x28, 3, 100, 0x0806, 0x0E),)),
        Device(0x981EC2CB, "Quantum_W3", "198.18.5.44", DESC_RADAR_W3,
               U1_RADAR_W3, U1_RADAR_W3, b"\x00\x01\x00\xe8",
               flag=0, ext_id=0x3B, ext_marker=0x1446,
               records=((0x29, 3, 100, 0x0806, 0x0E),)),
        Device(0x19030AD5, "", "198.18.2.105", DESC_NODE,
               U1_NODE_T1, 0x60, b"\x81\xa7\xc0\xe2",
               flag=0, ext_id=0x21, ext_marker=0x8118,
               records=NODE_RECORDS_MIN),
    ]


def _open_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # TTL 1 : le bus reste sur le lien local, comme sur le vrai réseau du bord.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    # Boucle locale activée : un client sur le même hôte (ou le même conteneur)
    # doit pouvoir recevoir les annonces.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    return sock


def _loop(ip: str, stop: threading.Event) -> None:
    sock = _open_socket()
    devices = _devices(ip)
    dst = (config.DISCOVERY_GROUP, config.DISCOVERY_PORT)
    tick = 0
    try:
        while not stop.is_set():
            for dev in devices:
                try:
                    # Alternance annonce compacte / étendue, comme en capture
                    # (les deux types sont émis en nombres comparables).
                    sock.sendto(build_announce(dev, extended=bool(tick & 1)), dst)
                    rec = dev.records[tick % len(dev.records)]
                    sock.sendto(build_telemetry(dev, *rec), dst)
                except OSError as e:
                    log.debug("émission 5800 impossible : %s", e)
            tick += 1
            stop.wait(config.BEACON_PERIOD)
    finally:
        sock.close()


def serve(ip: str) -> threading.Event:
    """Démarre le beacon en tâche de fond ; renvoie son drapeau d'arrêt."""
    stop = threading.Event()
    threading.Thread(target=_loop, args=(ip, stop), daemon=True).start()
    log.info("beacon 5800 vers %s:%d toutes les %.2f s",
             config.DISCOVERY_GROUP, config.DISCOVERY_PORT, config.BEACON_PERIOD)
    return stop
