#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["zeroconf>=0.130"]
# ///
"""
raydb_client.py — client RayDB : TUI, texte, JSON, NMEA 0183.

Se connecte à un MFD Raymarine (TCP 23333), s'annonce (HELLO), souscrit à
"data/#", puis **rend** ce que le MFD pousse (position, cap, vent, profondeur,
vitesses, tensions…). Une seule source — le fil d'événements —, quatre
rendus : la TUI curses, le texte au fil de l'eau (--dump), le JSON par ligne
(--json) et les phrases NMEA 0183 (--nmea, ex-`raydb_to_nmea.py`).

Usage :
    ./raydb_client.py                         # découverte auto du MFD (mcast 5800)
    ./raydb_client.py 192.168.42.1            # IP imposée (pas de découverte)
    ./raydb_client.py --replay rm1.pcapng     # rejouer une capture (test hors-ligne)
    ./raydb_client.py --replay rm1.pcapng --dump   # décoder sans TUI (stdout)
    ./raydb_client.py --path data/sog --path 'Settings/#'   # plusieurs abonnements
    ./raydb_client.py --json | jq 'select(.path == "data/sog")'   # un JSON/ligne
    ./raydb_client.py --replay rm1.pcapng --realtime   # à la cadence d'origine
    ./raydb_client.py --json --log toto.json   # même chose à l'écran et au fichier
    ./raydb_client.py --nmea                  # phrases NMEA 0183 sur stdout
    ./raydb_client.py --udp                   # ... et en broadcast UDP :10110
    ./raydb_client.py --udp-to 192.168.1.42   # ... vers un hôte précis

Découverte : sans IP, le client rejoint le groupe multicast 224.0.0.1:5800 et
attend une annonce Raymarine (protocole de découverte, cf. dissectors/raymarine_5800.lua).
Il se connecte ensuite à l'IP SOURCE du datagramme (adresse WiFi du MFD, où
écoute RayDB) — et NON à l'IP interne 198.18.x.x contenue dans l'annonce.

Touches dans la TUI :  ↑/↓ PgUp/PgDn Début/Fin pour défiler,  q pour quitter.

Protocole : voir dissectors/raymarine_raydb.lua / raydb_decode.py. Les trames HELLO et
SUBSCRIBE sont construites (build_hello / build_subscribe) à l'octet près comme
le vrai client (mêmes octets sur E70363 et E70481), ce qui garantit que le MFD
accepte l'abonnement.
"""
import argparse
import json
import math
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime

RAYDB_PORT = 23333

# Abonnement par défaut : tout l'arbre de navigation. `--path`, répétable,
# le remplace — le MFD accepte plusieurs SUBSCRIBE sur une même connexion.
DEFAULT_PATHS = ["data/#"]

# Note de fin de rejeu : c'est elle qui arrête `run_dump`, faute de quoi il
# attendrait indéfiniment une suite que le thread de rejeu n'enverra plus.
END_OF_REPLAY = "fin de capture"

# Horodatage d'une note (`State.note`) : par défaut l'horloge, `when=None` pour
# une note que rien ne date — l'ouverture d'un rejeu, avant la première trame.
NOW = object()

# Largeur de l'horodatage « 23:33:24.444 », pour aligner les notes sur les
# updates dans le fil `--dump`.
CLOCK_WIDTH = 12

# Attente maximale entre deux paquets en `--realtime`. Une capture porte les
# trous de la vie réelle.
REALTIME_MAX_GAP = 10.0

# Découverte : les MFD Raymarine annoncent en multicast sur 224.0.0.1:5800.
# On ne se fie PAS à l'IP contenue dans l'annonce (adresse interne 198.18.x.x
# non joignable) mais à l'IP SOURCE du datagramme = l'adresse WiFi/LAN du MFD,
# là où écoute réellement RayDB (23333).
DISCOVERY_GROUP = "224.0.0.1"
DISCOVERY_PORT = 5800

# La découverte mDNS a besoin de `zeroconf`. Le shebang PEP 723 le fournit via
# `uv run` ; lancé avec un python nu qui ne l'a pas, on le signale et on se
# rabat sur le beacon 5800 (qui n'atteint pas toujours un MFD simulé local).
try:
    import zeroconf as _zeroconf  # noqa: F401
    _HAS_ZEROCONF = True
except ImportError:
    _HAS_ZEROCONF = False

MSG_TYPE = 1                       # champ msg_type, toujours 1
OP_SUBSCRIBE = 3
OP_HELLO = 7

OPS = {0: "op0", 3: "SUB", 4: "UPDATE", 5: "ACK", 6: "ACK", 7: "HELLO"}

# Nom sous lequel le client s'annonce — celui du vrai client Raymarine.
CLIENT_NAME = "RayDBRemoteClient"

# Requêtes montantes, telles qu'on les annonce dans le fil : (nom de
# l'événement, nom du champ JSON). Le champ « chemin » de la trame porte le nom
# du client pour HELLO et le chemin souscrit pour SUBSCRIBE — même octet, deux
# sens, d'où deux noms de champ.
REQUESTS = {OP_HELLO: ("hello", "name"), OP_SUBSCRIBE: ("subscribe", "path")}


# ---------------------------------------------------- construction requêtes --
def _frame(op, path, trailer):
    """Assemble une trame RayDB :
        [u32 len][u32 msg_type][u8 op][u32 path_len][u32 pad][path][trailer]
    où `len` couvre tout ce qui suit le champ len lui-même."""
    path_b = path.encode("latin1")
    body = (struct.pack("<I", MSG_TYPE)
            + bytes([op])
            + struct.pack("<I", len(path_b))
            + struct.pack("<I", 0)                 # pad / flags
            + path_b
            + trailer)
    return struct.pack("<I", len(body)) + body


def _request_trailer(reserved):
    """Bloc valeur "vide" des requêtes : [3 réservés][u32 type=0x07][u64 0].
    Les 3 octets réservés sont opaques ; repris tels quels des captures (ils
    diffèrent selon l'opcode) pour rester acceptés par le MFD."""
    return reserved + struct.pack("<I", 0x07) + struct.pack("<Q", 0)


def build_hello(client_name=CLIENT_NAME):
    """Trame HELLO : le client s'annonce (identique aux captures E70363/E70481)."""
    return _frame(OP_HELLO, client_name, _request_trailer(b"\x00\x01\x01"))


def build_subscribe(path):
    """Trame SUBSCRIBE pour un chemin (ex. "data/#")."""
    return _frame(OP_SUBSCRIBE, path, _request_trailer(b"\x01\x00\x00"))


# --------------------------------------------------------------- décodage ---
def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


# --------------------------------------------- découverte UDP 5800 (mcast) ---
def parse_5800(payload):
    """Décode une trame de découverte Raymarine (UDP 5800).
    Retourne un dict pour une annonce (type 1/2), sinon None.
      type=1 et type=2 sont le MÊME enregistrement, avec une queue de longueur
      variable (@52 = u16 donnant le nombre d'octets à partir de @54 : 2 pour le
      type 1, 16 pour le type 2, d'où 56 et 70 octets) :
      [u32 type][u32 u1][4 handle][u32 descriptor][4 ip LE][nom ASCIIZ @20]
    u1 (@4)       : rôle NON RÉSOLU — varie selon le device ET le type de message.
    handle (@8)   : identifiant unique du device (opaque, stable inter-MFD).
    descriptor (@12) : type/modèle, PARTAGÉ par devices identiques ; ses octets
      hauts forment le « mot de classe » (0x840b nœud/MFD, 0x0000 radar/capteur).
    Le nom se coupe au premier NUL : au-delà, buffer réutilisé non nettoyé.
    Heuristique MFD : mot de classe non nul (0x840b0067) ; radars <= 0xff (0xa2, 0xcd).
    Cf. « docs/1. protocole-udp5800.md » §4."""
    if len(payload) < 32:
        return None
    mtype = _u32(payload, 0)
    if mtype not in (1, 2):
        return None
    descriptor = _u32(payload, 12)
    ip = ".".join(str(payload[16 + 3 - i]) for i in range(4))   # little-endian
    name = payload[20:52].split(b"\0")[0].decode("latin1", "replace")
    return {
        "type": mtype,
        "descriptor": descriptor,
        "announced_ip": ip,
        "name": name,
        "is_mfd": (descriptor & 0xFFFFFF00) != 0,
    }


def _open_discovery_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", DISCOVERY_PORT))
    mreq = struct.pack("=4sl", socket.inet_aton(DISCOVERY_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return sock


# Services mDNS Raymarine interrogés avant le beacon 5800 : leur enregistrement
# porte déjà l'IP joignable du MFD (on ignore le port annoncé).
MDNS_SERVICES = ["_raydb._tcp.local."]


def _discover_via_mdns(timeout, stop=None):
    """Cherche les services Raymarine (_raydb._tcp, _rym_rrc._tcp) en mDNS et
    renvoie l'adresse IPv4 de la première instance résolue, sans le port. None si
    zeroconf est absent, si rien n'est annoncé dans le délai, ou si `stop` tombe."""
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
        while time.time() < deadline and not (stop is not None and stop.is_set()):
            for entry in found:
                if entry in seen:
                    continue
                seen.add(entry)
                info = zc.get_service_info(entry[0], entry[1], timeout=1500)
                if info is None:
                    continue
                for addr in info.parsed_addresses():
                    if ":" not in addr:            # IPv4 seulement
                        return addr
            time.sleep(0.2)
        return None
    finally:
        zc.close()


def discover_mfd(state, stop, timeout):
    """Découvre l'IP du MFD. D'ABORD via mDNS (_raydb._tcp / _rym_rrc._tcp) : si
    un service répond, on prend son IP et on **ne lit pas** le multicast 5800.
    Sinon, repli sur le beacon 224.0.0.1:5800 (IP SOURCE du datagramme qui annonce
    un MFD ; sur ce réseau tout provient de l'adresse WiFi du MFD). None si rien."""
    if not _HAS_ZEROCONF:
        state.note(
            "zeroconf absent — découverte mDNS ignorée, repli sur le beacon 5800. "
            "Lancez « ./raydb_client.py » (via uv) ou passez l'IP en argument.",
            event="warning")
    ip = _discover_via_mdns(timeout, stop)
    if ip is not None:
        state.note(f"MFD découvert (mDNS) : {ip}",
                   event="discovered", ip=ip, via="mdns")
        return ip
    try:
        sock = _open_discovery_socket()
    except OSError as e:
        state.note(f"écoute 5800 impossible : {e}",
                   event="error", message=str(e))
        return None
    sock.settimeout(1.0)
    deadline = time.time() + timeout
    fallback = None
    try:
        while not stop.is_set() and time.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except TimeoutError:
                continue
            info = parse_5800(data)
            if not info:
                continue
            src = addr[0]
            label = info["name"] or info["announced_ip"]
            if info["is_mfd"]:
                state.note(f"MFD découvert : {src} ({label})",
                           event="discovered", ip=src, name=label, via="5800")
                return src
            fallback = fallback or src           # annonce non-MFD : on garde
            state.note(f"annonce {label} depuis {src}…",
                       event="announce", ip=src, name=label)
        return fallback
    finally:
        sock.close()


def decode_value(vb, leaf=""):
    """Décode le bloc valeur d'un UPDATE. Retourne une chaîne affichable, ou
    None si non interprété.

    Délègue à raydb_decode, seule implémentation tenue à jour : la copie qui
    vivait ici supposait un champ [u64 vlen] devant les valeurs internes des
    enregistrements nommés, qui n'existe pas, et jetait donc silencieusement
    tous les doubles de diag/… (mêmes erreurs de largeur sur les types 0x00 et
    0x07, lus sur 4 et 8 octets au lieu de 1 et 4).
    """
    import raydb_decode

    return raydb_decode.decode_value(vb, leaf)


def shortest_f32(value):
    """Plus courte écriture décimale qui redonne le float32 reçu.

    Le MFD envoie des flottants sur 32 bits ; Python n'a que le double, où la
    conversion étale des chiffres que la valeur n'a jamais portés —
    0.03009999915957451 pour un 0,0301 émis. Ce ne sont pas des décimales
    perdues mais inventées : le float32 le plus proche de 0,0301 *est* ce
    nombre-là, et 0,0301 est la façon la plus courte de le désigner.

    On cherche donc la première écriture, de 1 à 9 chiffres significatifs, qui
    retombe bit pour bit sur la valeur reçue. Neuf suffisent toujours pour un
    float32, la boucle aboutit donc toujours ; c'est ce que font `repr` pour un
    double, Wireshark et `--dump` pour ces valeurs.
    """
    for digits in range(1, 10):
        short = float(f"{value:.{digits}g}")
        if struct.unpack("<f", struct.pack("<f", short))[0] == value:
            return short
    return value                              # inatteignable pour un float32


def decode_native(vb, leaf=""):
    """Valeur **native** du même bloc — bool, entier, flottant ou chaîne.

    C'est ce que --json sort : un nombre y sort en nombre, celui que le MFD a
    envoyé (cf. `shortest_f32`), là où le rendu texte s'arrête à six chiffres
    significatifs.

    Un enregistrement nommé (type 0x0000000e, les `diag/…`) réencode le nom du
    champ. Il double le dernier segment du chemin, auquel cas il n'apprend rien et on
    ne rend que la valeur. S'il en diffère, il porte une information que le
    chemin n'a pas, et la valeur devient `{"name": …, "value": …}` plutôt que de
    la perdre en silence — le mode texte, lui, écrit « nom = valeur ».
    """
    import raydb_decode

    typed = raydb_decode.decode_typed(vb)
    if typed is None:
        return None
    name, value, vtype = typed
    if isinstance(value, float) and not math.isfinite(value):
        # Le MFD publie NaN pour « pas de valeur » — data/cog/stable en donne
        # peu. JSON n'a ni NaN ni infini : `null` le dit, là
        # où le NaN littéral de json.dumps sortirait du format (JSON.parse le
        # refuse). L'affichage texte, lui, garde son « nan ».
        value = None
    elif vtype == raydb_decode.TYPE_F32:
        value = shortest_f32(value)
    return value if name in (None, leaf) else {"name": name, "value": value}


def _fmt(x):
    """Formate un flottant sans zéros parasites."""
    return f"{x:.6g}"


def nap(seconds, stop):
    """Attend `seconds`, par tranches, pour rester réactif à l'arrêt.

    Un `sleep` d'un bloc retarderait un Ctrl-C d'autant, et `--realtime` attend
    jusqu'à dix secondes d'affilée.
    """
    end = time.monotonic() + seconds
    while not stop.is_set():
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.2, left))


def _clock(ts):
    """Horodate un événement à la milliseconde (« 14:32:07.418 »).

    La seconde ne suffit pas : le MFD pousse plusieurs UPDATE par seconde et par
    chemin, et c'est justement l'écart entre deux qui renseigne sur la cadence
    réelle d'un capteur. `time.strftime` s'arrête à la seconde, d'où le reste
    calculé à part.
    """
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"


def parse_frames(buf):
    """Découpe un flux d'octets en trames RayDB complètes.
    Retourne (liste_de_trames, reste_non_consommé)."""
    frames, o, n = [], 0, len(buf)
    while n - o >= 4:
        flen = _u32(buf, o)
        if flen < 13:                       # longueur invalide : on arrête
            break
        total = 4 + flen
        if n - o < total:                   # trame incomplète
            break
        frames.append(buf[o:o + total])
        o += total
    return frames, buf[o:]


def decode_frame(fr):
    """(op, path, value_str|None, value|None) à partir d'une trame complète.

    La valeur ressort deux fois : telle qu'on l'affiche, et telle qu'elle est
    (cf. `decode_native`). Les deux formes viennent du même bloc, décodé deux
    fois plutôt que transporté à moitié converti. Toutes deux reçoivent le
    dernier segment du chemin, seul juge du nom d'un enregistrement nommé.
    """
    op = fr[8]
    plen = _u32(fr, 9)
    path = fr[17:17 + plen].split(b"\0")[0].decode("latin1", "replace")
    if op != 4:
        return op, path, None, None
    vb, leaf = fr[17 + plen:], path.rsplit("/", 1)[-1]
    return op, path, decode_value(vb, leaf), decode_native(vb, leaf)


# ------------------------------------------------------------------ état ----
# Chemins masqués à l'affichage (bruit de config, sans intérêt navigation).
HIDDEN_PREFIXES = ("data/group/sharing/", "data/ledMode/", "diag/")
HIDDEN_PREFIXES = ()


def json_line(ts, document):
    """Un document du fil en JSON, tel que le sortent --json et --log.

    L'horodatage est en ISO-8601 local, donc porteur du jour — que le mode texte
    ne donne qu'en note, au changement. Il vaut `null` pour la seule note que
    rien ne date, l'ouverture d'un rejeu.
    """
    stamp = None if ts is None else datetime.fromtimestamp(ts).astimezone(
        ).isoformat(timespec="milliseconds")
    return json.dumps({"timestamp": stamp, **document}, ensure_ascii=False)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {}          # path -> [value_str, count, last_ts]
        self.status = "connexion…"
        self.updates = 0
        self.stream = None        # queue.Queue en mode --dump (affichage live)
        self.log = None           # fichier ouvert par --log, au format --json

    def _log(self, ts, document):
        """Écrit une ligne du journal, s'il y en a un.

        Le journal est alimenté à la **production** et non à l'affichage : il
        contient donc exactement la même chose quel que soit le mode, TUI
        comprise, là où brancher un second consommateur sur la file lui aurait
        volé un élément sur deux.
        """
        if self.log is not None:
            self.log.write(json_line(ts, document) + "\n")

    def apply(self, path, val, when=None, native=None):
        """Enregistre une valeur. `when` date l'événement s'il ne vient pas de
        l'horloge — en --replay, l'instant de capture. `native` est la même
        valeur non formatée, que seul --json utilise.

        Seul le fil `--dump` en tient compte : la TUI, elle, affiche un âge, et
        rejouer une capture de l'été dernier y donnerait des âges en mois. D'où
        deux dates distinctes, l'une pour dire *quand c'est arrivé*, l'autre
        pour dire *depuis quand on l'a*.
        """
        if path.startswith(HIDDEN_PREFIXES):
            return
        now = time.time()
        with self.lock:
            self.updates += 1
            e = self.values.get(path)
            if e:
                e[0], e[1], e[2] = val, e[1] + 1, now
            else:
                self.values[path] = [val, 1, now]
        stamp = now if when is None else when
        if self.stream is not None:                 # streaming (hors verrou)
            self.stream.put((stamp, path, val, native))
        self._log(stamp, {"path": path, "value": native})

    def note(self, text, header=True, when=NOW, event=None, **fields):
        """Message de suivi : découverte, connexion, HELLO, abonnement, fin.

        Il passe par la **même** file que les updates, d'où son affichage à sa
        place dans le fil : lire `status` à côté de la file, comme avant, faisait
        sortir « connecté à … » après le premier update — celui-là même que la
        connexion venait de rendre possible.

        `header=False` pour un message qui ne vaut que dans le fil (HELLO,
        abonnements) : l'en-tête de la TUI garde alors l'état de la connexion,
        qui lui est plus utile qu'un chemin souscrit une fois pour toutes.

        `when` date la note comme `apply` date un update — instant de capture en
        --replay ; `None` la laisse sans date, faute de quoi une note du rejeu
        porterait l'heure qu'il est au milieu d'événements d'il y a un mois.

        `event` et ses `fields` sont la même note sous forme structurée, pour
        --json : le texte est écrit pour l'œil, une phrase française dont rien
        ne se relit par programme.
        """
        if header:
            with self.lock:
                self.status = text
        stamp = time.time() if when is NOW else when
        payload = None if event is None else {"event": event, **fields}
        if self.stream is not None:
            self.stream.put((stamp, None, text, payload))
        self._log(stamp, payload or {"event": "note", "message": text})


# --------------------------------------------------------------- réseau -----
def request_note(state, op, value, when=NOW):
    """Note d'une requête montante — HELLO ou SUBSCRIBE, émise ici comme relue
    d'une capture (d'où `when`). Le libellé et le nom du champ JSON viennent de
    `REQUESTS`, seul endroit où se décide comment une requête se raconte.
    """
    event, field = REQUESTS[op]
    state.note(f"{event} {value}", header=False, when=when, event=event,
               **{field: value})


def reader_thread(ip, state, stop, paths, discover_timeout=15):
    """Boucle : (découverte MFD si besoin) → connexion RayDB → lecture.
    Reconnexion et re-découverte automatiques.

    `paths` liste les abonnements demandés : un SUBSCRIBE par chemin, envoyé à
    chaque (re)connexion. Le MFD répond à chacun par son état « retained », puis
    ne pousse plus que les changements — deux abonnements qui se recouvrent
    valent donc un doublon à l'ouverture, sans conséquence sur l'affichage
    (`State.apply` indexe par chemin).
    """
    while not stop.is_set():
        target = ip
        if target is None:                       # pas d'IP fournie → découvrir
            state.note(f"découverte MFD (mDNS puis mcast "
                       f"{DISCOVERY_GROUP}:{DISCOVERY_PORT})…",
                       event="discovering")
            target = discover_mfd(state, stop, discover_timeout)
            if target is None:
                continue                         # rien vu : on relance l'écoute
        try:
            state.note(f"connexion à {target}:{RAYDB_PORT}…",
                       event="connecting", ip=target, port=RAYDB_PORT)
            s = socket.create_connection((target, RAYDB_PORT), timeout=5)
            s.settimeout(1.0)
            # La connexion tient : on l'annonce avant les abonnements, pour que
            # le fil se lise dans l'ordre où les choses se sont passées.
            state.note(f"connecté à {target}:{RAYDB_PORT}",
                       event="connected", ip=target, port=RAYDB_PORT)
            s.sendall(build_hello())
            request_note(state, OP_HELLO, CLIENT_NAME)
            for sub in paths:
                s.sendall(build_subscribe(sub))
                request_note(state, OP_SUBSCRIBE, sub)
            buf = b""
            while not stop.is_set():
                try:
                    data = s.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    break
                buf += data
                frames, buf = parse_frames(buf)
                for fr in frames:
                    op, path, val, native = decode_frame(fr)
                    if op == 4 and val is not None:
                        state.apply(path, val, native=native)
            s.close()
        except OSError as e:
            state.note(f"{target}: {e} — reconnexion…",
                       event="error", ip=target, message=str(e))
            for _ in range(20):
                if stop.is_set():
                    return
                time.sleep(0.1)


def replay_thread(pcap, state, stop, realtime=False):
    """Rejoue une capture pour tester sans MFD : les UPDATE du MFD, et les HELLO
    et SUBSCRIBE du client qui les a provoqués — c'est ce qui permet de lire une
    capture comme une session, en voyant à quoi le client s'était abonné et à
    quel moment il s'est reconnecté.

    Les événements sont datés de leur **instant de capture** (frame.time_epoch),
    pas de l'heure du rejeu : c'est la seule date qui ait un sens ici.

    `realtime` rejoue la cadence d'origine, en attendant entre deux paquets ce
    que la capture dit qu'on a attendu — plafonné à REALTIME_MAX_GAP. Sans lui,
    tout défile aussi vite que tshark et le décodage le permettent.

    L'extraction passe par raydb_decode.payloads_from_tshark, qui lit data.data
    et non tcp.payload : ce dernier est brut, si bien qu'une retransmission ou
    un segment hors séquence désynchronisait le flux définitivement — rm2 ne
    rendait que 10 UPDATE sur plusieurs milliers, avec 400 ko bloqués dans le
    tampon. data.data, lui, est remis dans l'ordre et dédoublonné par tshark,
    mais **pas** réassemblé : c'est payloads_from_tshark qui recolle les
    segments et ne rend que des trames entières (cf. sa docstring).
    """
    from pathlib import Path

    import raydb_decode

    # Rien ne date encore cette note : tshark n'a pas rendu sa première ligne,
    # ce qui peut prendre quelques secondes sur une grosse capture.
    name = os.path.basename(pcap)
    state.note(f"replay {name}", when=None, event="replay", file=name)
    day, last = None, None
    for srcport, when, hexstr in raydb_decode.payloads_from_tshark(
            Path(pcap), RAYDB_PORT, _find_tshark()):
        if stop.is_set():
            return
        from_mfd = srcport == RAYDB_PORT   # seul le MFD pousse des UPDATE
        if realtime and last is not None and when is not None:
            nap(min(when - last, REALTIME_MAX_GAP), stop)
        if when is not None:
            last = when
            # Les lignes ne portent que l'heure : le jour se dit à part, à
            # l'ouverture puis à chaque changement.
            stamp = time.strftime("%d/%m/%Y", time.localtime(when))
            if stamp != day:
                day = stamp
                state.note(f"capture du {day}", when=when, event="day",
                           date=time.strftime("%Y-%m-%d", time.localtime(when)))
        # payloads_from_tshark a déjà recollé les segments : chaque hexstr ne
        # porte que des trames entières, qu'on redécoupe ici.
        for fr in raydb_decode.frames_from_hex(hexstr):
            if len(fr) < 17:               # en-tête incomplet : trame ignorée
                continue
            op, path, val, native = decode_frame(fr)
            if not from_mfd:
                # Trames montantes : on ne retient que les requêtes qui disent
                # ce que le client a demandé (les KEEPALIVE n'apprennent rien).
                if op in REQUESTS:
                    request_note(state, op, path, when)
                continue
            if op == 4 and val is not None:
                state.apply(path, val, when, native)
    state.note(f"{name} : {END_OF_REPLAY}", when=last, event="end", file=name)


def _find_tshark():
    for p in ("tshark", "/Applications/Wireshark.app/Contents/MacOS/tshark"):
        try:
            # Sonde d'existence : seul compte que le binaire se lance, d'où le
            # code de retour ignoré — c'est l'OSError qui dit « absent ».
            subprocess.run([p, "--version"], capture_output=True, check=False)
            return p
        except OSError:
            continue
    sys.exit("tshark introuvable (nécessaire pour --replay)")


# ------------------------------------------------------------------ TUI -----
def run_tui(state, stop):
    import curses

    def put(stdscr, y, x, text, w, attr=0):
        # addnstr sur la dernière cellule fait déborder le curseur (ERR) :
        # on borne à w-1 et on ignore les erreurs de bord.
        try:
            stdscr.addnstr(y, x, text, max(0, w - 1), attr)
        except curses.error:
            pass

    def _draw(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        try:
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
        except curses.error:
            pass
        top = 0
        while not stop.is_set():
            h, w = stdscr.getmaxyx()
            body = max(1, h - 3)
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                break
            elif ch in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif ch in (curses.KEY_UP, ord("k")):
                top -= 1
            elif ch == curses.KEY_NPAGE:
                top += body
            elif ch == curses.KEY_PPAGE:
                top -= body
            elif ch == curses.KEY_HOME:
                top = 0
            elif ch == curses.KEY_END:
                top = 10 ** 9

            with state.lock:
                items = sorted(state.values.items())
                status, updates = state.status, state.updates
            now = time.time()
            top = max(0, min(top, max(0, len(items) - body)))

            stdscr.erase()
            head = f" RayDB  {status}   {len(items)} chemins   {updates} updates "
            put(stdscr, 0, 0, head.ljust(w), w, curses.A_REVERSE)
            pathw = max(20, min(w - 24, 52))
            put(stdscr, 1, 0, f" {'CHEMIN':<{pathw}} {'VALEUR':<18} AGE", w,
                curses.color_pair(1) | curses.A_BOLD)
            for i, (path, (val, cnt, ts)) in enumerate(items[top:top + body]):
                age = now - ts
                attr = curses.color_pair(3) if age < 1.0 else 0
                line = f" {path:<{pathw}.{pathw}} {val:<18.18} {age:4.1f}s"
                put(stdscr, 2 + i, 0, line.ljust(w), w, attr)
            foot = " up/down PgUp/PgDn defiler   q quitter "
            put(stdscr, h - 1, 0, foot.ljust(w), w, curses.A_REVERSE)
            stdscr.refresh()
            time.sleep(0.15)

    try:
        curses.wrapper(_draw)
    finally:
        stop.set()


def print_note(ts, text, _payload=None):
    """Écrit une note du fil sur stderr, telle que la lit un humain.

    Le « # » vient **après** l'horodatage, et non en tête de ligne : les dates
    restent ainsi dans une seule colonne, la seule façon de suivre à l'œil la
    cadence d'un flux où s'intercalent des reconnexions. Signature de rappel de
    `run_stream`, pour se passer en argument tel quel.
    """
    stamp = _clock(ts) if ts is not None else " " * CLOCK_WIDTH
    print(f"{stamp}  # {text}", file=sys.stderr, flush=True)


def run_stream(state, stop, render, note):
    """Déroule le fil : les updates à `render`, les messages de suivi à `note`.

    Les deux sortent de la même file, donc dans l'ordre où ils se sont produits.
    Chaque mode décide ensuite de la forme des uns et des autres.

    En --replay, se termine sur la note de fin de capture, qui est le dernier
    élément de la file ; en live, tourne jusqu'à Ctrl-C.
    """
    while not stop.is_set():
        try:
            ts, path, val, extra = state.stream.get(timeout=0.3)
        except queue.Empty:
            continue
        if path is None:                            # message de suivi
            note(ts, val, extra)
            if val.endswith(END_OF_REPLAY):
                break
            continue
        render(ts, path, val, extra)


def run_dump(state, stop):
    """Affiche chaque update au fil de l'eau (capture live comme --replay).

    Deux espaces au moins entre le chemin et la valeur, y compris quand le
    chemin déborde de sa colonne : les `diag/mfd/…` dépassent la cinquantaine
    de caractères, et une valeur collée à un chemin ne se relit pas.

    Les notes vont sur stderr (cf. `print_note`).
    """
    def update(ts, path, val, _extra):
        print(f"{_clock(ts)}  {path:<48}  {val}", flush=True)

    run_stream(state, stop, update, print_note)


def run_json(state, stop):
    """Comme --dump, mais un document JSON par ligne sur stdout (JSON Lines).

    Updates et événements de session sortent dans le même flux, donc dans
    l'ordre : un consommateur voit à quoi le client s'est abonné avant de voir
    ce qui en découle. Un update porte `path` et `value`, un événement porte
    `event` et ce qui le décrit — `path` pour un abonnement, `name` pour un
    HELLO ou un MFD découvert.

    La valeur est la valeur native : un nombre sort en nombre, à pleine
    précision, là où l'affichage texte s'arrête à six chiffres significatifs.
    Elle prend la forme `{"name": …, "value": …}` dans le seul cas où le nom
    d'un enregistrement nommé n'est pas celui du dernier segment du chemin (cf.
    `decode_native`) — jamais vu sur les captures, mais rien ne l'interdit.

    C'est ligne pour ligne ce qu'écrit --log : les documents sont les mêmes, et
    `json_line` les met en forme des deux côtés.
    """
    def update(ts, path, _val, native):
        # `native` seul : un bloc indécodable n'arrive pas jusqu'ici (l'appelant
        # écarte les updates sans valeur), et `None` veut donc bien dire la
        # valeur nulle — le NaN que le MFD publie pour « pas de valeur ».
        print(json_line(ts, {"path": path, "value": native}), flush=True)

    def note(ts, text, payload):
        # Une note sans forme structurée ne devrait pas exister ; si elle
        # survient, mieux vaut la livrer en clair que la perdre.
        print(json_line(ts, payload or {"event": "note", "message": text}),
              flush=True)

    run_stream(state, stop, update, note)


# ------------------------------------------------------- NMEA 0183 (--nmea) -
#
# Ce qui suit vient de `raydb_to_nmea.py`, qui n'était qu'un quatrième rendu du
# même fil : il importait ce fichier pour tout le reste. La table des
# correspondances et la comparaison avec la passerelle Yacht Devices sont dans
# « docs/2. protocole-raydb-23333.md » §NMEA.
#
# Unités RayDB supposées (cohérentes avec NMEA 2000 / SeaTalkNG) : angles en
# **radians**, vitesses en **m/s**, profondeurs et altitudes en **mètres**. Si
# le MFD envoie en réalité des nœuds, --knots.

MS_TO_KN = 1.943844          # m/s → nœuds
MS_TO_KMH = 3.6              # m/s → km/h
M_TO_FT = 3.28084            # m → pieds
M_TO_FATH = 0.546807         # m → brasses

NMEA_UDP_PORT = 10110        # port conventionnel NMEA 0183 sur UDP
NMEA_UDP_BROADCAST = "127.0.0.1"    # destination par défaut de --udp


def nmea(talker, body):
    """Assemble '$<talker><body>*CS' avec la somme de contrôle NMEA."""
    payload = talker + body
    cs = 0
    for c in payload:
        cs ^= ord(c)
    return f"${payload}*{cs:02X}"


def deg(rad):
    """Radians → degrés signés."""
    return math.degrees(rad)


def deg360(rad):
    """Radians → degrés dans [0, 360)."""
    return math.degrees(rad) % 360.0


def angle_lr(rad):
    """Angle/étrave en radians → (0..180, 'L'|'R') pour VWR / VWT."""
    d = deg360(rad)
    return (360.0 - d, "L") if d > 180.0 else (d, "R")


def latlon_nmea(s):
    """'50.3646,-4.13203' → ('5021.8760','N','00407.9218','W') ou None."""
    try:
        lat, lon = (float(x) for x in s.split(","))
    except (ValueError, AttributeError):
        return None
    if math.isnan(lat) or math.isnan(lon):
        return None
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    lat, lon = abs(lat), abs(lon)
    return (f"{int(lat):02d}{(lat % 1) * 60:07.4f}", ns,
            f"{int(lon):03d}{(lon % 1) * 60:07.4f}", ew)


def hms(ts):
    """Horodatage → 'hhmmss.ss' UTC."""
    t = time.gmtime(ts)
    return f"{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}.{int(ts % 1 * 100):02d}"


def dmy(ts):
    """Horodatage → 'ddmmyy' UTC."""
    t = time.gmtime(ts)
    return f"{t.tm_mday:02d}{t.tm_mon:02d}{t.tm_year % 100:02d}"


class Bridge:
    """Transforme les événements RayDB (chemin, valeur) en phrases NMEA 0183.

    Chaque phrase a **un** chemin déclencheur, pour éviter les doublons ; les
    autres chemins ne font qu'alimenter un cache lu à l'émission :

        déclencheur                phrases   chemins en cache
        ---------------------------------------------------------------------
        data/position              RMC       sog, cog, bearing/variation
                                   GGA       position/altitude
                                   GLL       —
        data/position/accuracy     GST       —
        data/sog                   VTG       cog, bearing/variation
        data/heading/true          HDT       —
        data/heading/magnetic      HDM, HDG  bearing/variation
        data/wind/speed/apparent   MWV (R),  wind/direction/apparent
                                   VWR
        data/wind/speed/true       MWV (T),  wind/direction/true,
                                   VWT, MWD  heading/true, variation
        data/depth                 DPT, DBT  depth/offset
                                   DBS       depth/offset
        data/stw                   VHW       heading/{true,magnetic}
        data/rot                   ROT       —
        data/rudder                RSA       —
        data/roll                  XDR       pitch
        data/tide/drift            VDR       tide/set

    Référentiels vérifiés sur les captures : `data/wind/direction/{true,
    apparent}` sont des angles **relatifs à l'étrave**, pas des directions
    référencées au nord (l'écart entre les deux vaut ~0° alors que le cap vaut
    249°), d'où l'ajout du cap pour MWD ; `data/rot` est en rad/s, converti en
    degrés/minute pour ROT.
    """

    def __init__(self, knots=False, talker_nav="GP", talker_inst="II"):
        self.knots = knots               # True : vitesses RayDB déjà en nœuds
        self.gp = talker_nav             # phrases de positionnement
        self.ii = talker_inst            # phrases instruments
        self.cache = {}                  # path → float (dernière valeur)

    # -- accès cache / conversions --------------------------------------
    def _get(self, path):
        v = self.cache.get(path)
        return None if v is None or math.isnan(v) else v

    def _kn(self, v):
        return v if self.knots else v * MS_TO_KN

    def _ms(self, v):
        return v / MS_TO_KN if self.knots else v

    def _var(self):
        """(valeur absolue en degrés, 'E'|'W') ou ('','')."""
        v = self._get("data/bearing/variation")
        if v is None:
            return "", ""
        d = deg(v)
        return f"{abs(d):.1f}", ("E" if d >= 0 else "W")

    # -- point d'entrée ---------------------------------------------------
    def handle(self, ts, path, val):
        """Retourne la liste des phrases NMEA pour cet événement RayDB.

        `val` est la valeur **native** — nombre, chaîne, ou None. Ce fut
        longtemps la valeur formatée, dont on refaisait un flottant : on perdait
        les chiffres au-delà du sixième (`%.6g`) pour les regagner aussitôt en
        NMEA. None dit que le MFD ne publie pas de valeur (il envoie un NaN, cf.
        `decode_native`) : rien à émettre.
        """
        if val is None:
            return []
        num = None
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            num = float(val)
            self.cache[path] = num

        if path == "data/position":
            return self._position(ts, val)
        if path == "data/position/accuracy":
            return self._gst(ts, num)
        if path == "data/sog":
            return self._vtg()
        if path == "data/heading/true":
            return [nmea(self.ii, f"HDT,{deg360(num):.1f},T")]
        if path == "data/heading/magnetic":
            var, ew = self._var()
            return [nmea(self.ii, f"HDM,{deg360(num):.1f},M"),
                    nmea(self.ii, f"HDG,{deg360(num):.1f},,,{var},{ew}")]
        if path == "data/wind/speed/apparent":
            return (self._mwv(num, "data/wind/direction/apparent", "R")
                    + self._vw(num, "data/wind/direction/apparent", "VWR"))
        if path == "data/wind/speed/true":
            return (self._mwv(num, "data/wind/direction/true", "T")
                    + self._vw(num, "data/wind/direction/true", "VWT")
                    + self._mwd(num))
        if path == "data/depth":
            return self._depth(num)
        if path == "data/stw":
            return self._vhw(num)
        if path == "data/rot":
            # ROT est en degrés/minute dans NMEA ; RayDB pousse des rad/s.
            return [nmea(self.ii, f"ROT,{deg(num) * 60:.1f},A")]
        if path == "data/rudder":
            return [nmea(self.ii, f"RSA,{deg(num):.1f},A,,V")]
        if path == "data/roll":
            return self._xdr(num)
        if path == "data/tide/drift":
            return self._vdr(num)
        return []

    # -- phrases composites ------------------------------------------------
    def _position(self, ts, val):
        ll = latlon_nmea(val)
        if ll is None:
            return []
        lat, ns, lon, ew = ll
        t = hms(ts)
        sog = self._get("data/sog")
        cog = self._get("data/cog")
        var, vew = self._var()
        alt = self._get("data/position/altitude")
        out = [nmea(self.gp,
                    f"RMC,{t},A,{lat},{ns},{lon},{ew},"
                    f"{'' if sog is None else f'{self._kn(sog):.1f}'},"
                    f"{'' if cog is None else f'{deg360(cog):.1f}'},"
                    f"{dmy(ts)},{var},{vew},A")]
        out.append(nmea(self.gp,
                        f"GGA,{t},{lat},{ns},{lon},{ew},1,,,"
                        f"{'' if alt is None else f'{alt:.1f}'},M,,M,,"))
        out.append(nmea(self.gp, f"GLL,{lat},{ns},{lon},{ew},{t},A,A"))
        return out

    def _vtg(self):
        sog = self._get("data/sog")
        if sog is None:
            return []
        cog = self._get("data/cog")
        cog_t = "" if cog is None else f"{deg360(cog):.1f}"
        cog_m = ""
        var = self._get("data/bearing/variation")
        if cog is not None and var is not None:
            cog_m = f"{deg360(cog - var):.1f}"
        kn = self._kn(sog)
        return [nmea(self.gp,
                     f"VTG,{cog_t},T,{cog_m},M,{kn:.1f},N,"
                     f"{kn * MS_TO_KMH / MS_TO_KN:.1f},K,A")]

    def _mwv(self, speed, angle_path, ref):
        angle = self._get(angle_path)
        if angle is None:
            return []
        return [nmea(self.ii,
                     f"MWV,{deg360(angle):.1f},{ref},{self._kn(speed):.1f},N,A")]

    def _vw(self, speed, angle_path, kind):
        """VWR / VWT — même trame que MWV, en angle 0..180 bâbord/tribord."""
        angle = self._get(angle_path)
        if angle is None:
            return []
        a, side = angle_lr(angle)
        kn, ms = self._kn(speed), self._ms(speed)
        return [nmea(self.ii, f"{kind},{a:.1f},{side},{kn:.1f},N,{ms:.1f},M,"
                              f"{ms * MS_TO_KMH:.1f},K")]

    def _mwd(self, speed):
        """MWD — direction du vent vrai référencée au **nord**, d'où l'ajout du
        cap : data/wind/direction/true est un angle relatif à l'étrave."""
        angle = self._get("data/wind/direction/true")
        hdt = self._get("data/heading/true")
        if angle is None or hdt is None:
            return []
        var = self._get("data/bearing/variation")
        dir_m = "" if var is None else f"{deg360(angle + hdt - var):.1f}"
        kn, ms = self._kn(speed), self._ms(speed)
        return [nmea(self.ii, f"MWD,{deg360(angle + hdt):.1f},T,{dir_m},M,"
                              f"{kn:.1f},N,{ms:.1f},M")]

    def _depth(self, depth):
        off = self._get("data/depth/offset")
        # DPT/DBT sont mesurées sous la sonde ; DBS sous la surface, d'où
        # l'ajout de l'offset quand il est positif (sonde → ligne de flottaison).
        surf = depth + off if off is not None and off > 0 else depth
        return [nmea(self.ii,
                     f"DPT,{depth:.1f},{'' if off is None else f'{off:.1f}'},"),
                nmea(self.ii,
                     f"DBT,{depth * M_TO_FT:.1f},f,{depth:.1f},M,"
                     f"{depth * M_TO_FATH:.1f},F"),
                nmea(self.ii,
                     f"DBS,{surf * M_TO_FT:.1f},f,{surf:.1f},M,"
                     f"{surf * M_TO_FATH:.1f},F")]

    def _gst(self, ts, acc):
        """GST — RayDB ne donne qu'une précision globale (data/position/accuracy,
        en mètres) : on la reporte en RMS et en erreurs lat/lon, et on laisse
        vides l'ellipse et l'erreur d'altitude. Approximation, pas une mesure."""
        return [nmea(self.gp, f"GST,{hms(ts)},{acc:.2f},,,,{acc:.2f},{acc:.2f},")]

    def _vhw(self, stw):
        hdt = self._get("data/heading/true")
        hdm = self._get("data/heading/magnetic")
        kn = self._kn(stw)
        return [nmea(self.ii,
                     f"VHW,{'' if hdt is None else f'{deg360(hdt):.1f}'},T,"
                     f"{'' if hdm is None else f'{deg360(hdm):.1f}'},M,"
                     f"{kn:.1f},N,{kn * MS_TO_KMH / MS_TO_KN:.1f},K")]

    def _xdr(self, roll):
        pitch = self._get("data/pitch")
        body = "XDR"
        if pitch is not None:
            body += f",A,{deg(pitch):.1f},D,PTCH"
        body += f",A,{deg(roll):.1f},D,ROLL"
        return [nmea(self.ii, body)]

    def _vdr(self, drift):
        set_ = self._get("data/tide/set")
        set_t = "" if set_ is None else f"{deg360(set_):.1f}"
        return [nmea(self.ii, f"VDR,{set_t},T,,M,{self._kn(drift):.1f},N")]


def open_udp(dest):
    """Prépare la diffusion UDP des phrases NMEA.
    `dest` : "HOST[:PORT]" ou "" → broadcast 255.255.255.255:10110.
    Retourne (socket, (host, port))."""
    host, _, port = dest.rpartition(":") if ":" in dest else ("", "", "")
    host = host or dest or NMEA_UDP_BROADCAST
    port = int(port) if port else NMEA_UDP_PORT
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return s, (host, port)


def run_nmea(state, stop, bridge, udp=None, udp_dest=None, logf=None):
    """Rend le fil en phrases NMEA 0183 : sur stdout, ou en UDP s'il y a un
    socket — jamais les deux.

    La diffusion est une sortie à part entière : les phrases y vont, et stdout
    reste muet. Les doubler à l'écran ne dirait rien de plus et salirait ce
    qu'on redirige. Le suivi (connexion, abonnements) continue sur stderr, et
    `--log` garde la trace complète.

    Le journal est tenu ici et non par `State.log` comme dans les autres modes :
    les phrases n'existent qu'à ce moment, une fois la valeur passée au pont.
    Les documents restent ceux de --json, augmentés de la clé « nmea » quand
    l'update a déclenché quelque chose — ce qui est l'exception, la plupart des
    chemins ne faisant qu'alimenter le cache.
    """
    def journal(ts, document):
        if logf:
            logf.write(json_line(ts, document) + "\n")

    def update(ts, path, _val, native):
        sentences = bridge.handle(ts, path, native)
        for s in sentences:
            if udp is None:
                print(s, flush=True)
                continue
            try:
                udp.sendto((s + "\r\n").encode("ascii"), udp_dest)
            except OSError as e:
                print(f"# UDP {udp_dest[0]}:{udp_dest[1]} : {e}",
                      file=sys.stderr, flush=True)
        document = {"path": path, "value": native}
        if sentences:
            document["nmea"] = sentences
        journal(ts, document)

    def note(ts, text, payload):
        print_note(ts, text)
        journal(ts, payload or {"event": "note", "message": text})

    run_stream(state, stop, update, note)


# ----------------------------------------------------------------- main -----
def main():
    ap = argparse.ArgumentParser(
        description="Client RayDB (TCP 23333) : TUI, texte, JSON ou NMEA 0183.")
    ap.add_argument("ip", nargs="?", default=None,
                    help="IP du MFD ; si omis, découverte auto via mcast 5800")
    ap.add_argument("--discover-timeout", type=float, default=15,
                    help="délai d'écoute des annonces 5800 (s, défaut : 15)")
    ap.add_argument("--replay", metavar="PCAP",
                    help="rejouer une capture au lieu de se connecter")
    ap.add_argument("--realtime", action="store_true",
                    help="rejouer à la cadence d'origine "
                         f"(attente plafonnée à {REALTIME_MAX_GAP:.0f} s)")
    # Les trois rendus du fil s'excluent : ils écrivent tous sur stdout, et rien
    # ne dirait lequel l'emporte. Sans aucun, c'est la TUI (ou --dump hors tty).
    sortie = ap.add_mutually_exclusive_group()
    sortie.add_argument("--dump", action="store_true",
                        help="sortie texte au lieu de la TUI")
    sortie.add_argument("--json", action="store_true",
                        help="comme --dump, mais un document JSON par ligne")
    sortie.add_argument("--nmea", action="store_true",
                        help="convertir le flux en phrases NMEA 0183 sur stdout")
    ap.add_argument("--knots", action="store_true",
                    help="--nmea : les vitesses RayDB sont déjà en nœuds "
                         "(défaut : m/s)")
    ap.add_argument("--udp", action="store_true",
                    help="diffuser aussi les phrases en broadcast UDP "
                         f"({NMEA_UDP_BROADCAST}:{NMEA_UDP_PORT}), "
                         "ce qui implique --nmea")
    ap.add_argument("--udp-to", metavar="HÔTE[:PORT]", dest="udp_to",
                    help="diffuser vers cette destination plutôt qu'en "
                         f"broadcast (port par défaut : {NMEA_UDP_PORT})")
    ap.add_argument("--log", metavar="FICHIER",
                    help="journaliser le fil au format --json, quel que soit "
                         "l'affichage (ajout en fin de fichier ; en --nmea, "
                         "chaque update porte en plus ses phrases)")
    ap.add_argument("--path", metavar="CHEMIN", action="append", dest="paths",
                    help="chemin à souscrire, répétable (défaut : "
                         + " ".join(DEFAULT_PATHS) + ")")
    args = ap.parse_args()
    if args.realtime and not args.replay:
        ap.error("--realtime ne vaut que pour --replay")
    diffuser = args.udp or args.udp_to is not None
    if diffuser:
        # Diffuser des phrases, c'est les produire : --udp allume --nmea plutôt
        # que d'exiger qu'on le répète. Il reste donc incompatible avec les deux
        # autres rendus, ce que le groupe exclusif ne peut pas dire à sa place.
        if args.dump or args.json:
            ap.error("--udp implique --nmea : pas de --dump ni de --json avec")
        args.nmea = True
    if args.knots and not args.nmea:
        ap.error("--knots ne vaut que pour --nmea")
    # `action="append"` ajoute à la valeur par défaut au lieu de la remplacer :
    # d'où le défaut à None, résolu ici.
    paths = args.paths or DEFAULT_PATHS

    state = State()
    stop = threading.Event()
    # La TUI ne vaut que sur un terminal : hors d'un tty, on déroule le fil.
    stream_mode = args.json or args.dump or args.nmea or not sys.stdout.isatty()
    if stream_mode:
        state.stream = queue.Queue()              # activer le streaming
    # Ouvert avant le thread : sinon les premières notes s'écriraient dans le
    # vide. En ajout, et ligne à ligne, pour se suivre au `tail -f`.
    logf = open(args.log, "a", buffering=1) if args.log else None
    # En --nmea, le journal est tenu par le rendu (les phrases n'existent qu'à
    # ce moment) ; partout ailleurs, par la production, ce qui le rend complet
    # jusque dans la TUI. Un seul des deux écrit, jamais les deux.
    state.log = None if args.nmea else logf
    if args.replay:
        t = threading.Thread(target=replay_thread,
                             args=(args.replay, state, stop, args.realtime),
                             daemon=True)
    else:
        t = threading.Thread(target=reader_thread,
                             args=(args.ip, state, stop, paths, args.discover_timeout),
                             daemon=True)
    t.start()

    udp = udp_dest = None
    if diffuser:
        udp, udp_dest = open_udp(args.udp_to or "")
        print(f"# diffusion UDP → {udp_dest[0]}:{udp_dest[1]}",
              file=sys.stderr, flush=True)
    try:
        if args.nmea:
            run_nmea(state, stop, Bridge(knots=args.knots), udp, udp_dest, logf)
        elif args.json:
            run_json(state, stop)
        elif stream_mode:
            run_dump(state, stop)
        else:
            run_tui(state, stop)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        # consommateur fermé (ex. « | head ») : rediriger stdout vers /dev/null
        # pour éviter une seconde BrokenPipeError au flush de fin, puis sortir.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    finally:
        stop.set()
        if logf is not None:
            logf.close()
        if udp is not None:
            udp.close()


if __name__ == "__main__":
    main()
