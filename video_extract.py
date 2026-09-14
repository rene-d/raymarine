#!/usr/bin/env python3
"""
video_extract.py — reconstitue le flux vidéo H.264 d'une capture RTSP/RTP.

Le MFD Raymarine diffuse la recopie de son écran via un serveur RTSP (TCP 8554,
« GStreamer RTSP server ») : le client fait `OPTIONS → DESCRIBE → SETUP → PLAY`,
et le MFD pousse la vidéo en RTP/H.264 sur UDP. Ce script relit une capture de
cette session et en reconstruit un flux H.264 lisible, sans rien coder en dur :

  1. **SDP** — la réponse au DESCRIBE porte le SDP, dont on extrait, par flux :
     le type de charge utile RTP, le codec, et les `sprop-parameter-sets`
     (SPS/PPS en base64). Ces paramètres sont indispensables au décodage.
  2. **RTP** — on énumère les flux (par SSRC) et on récupère leurs charges
     utiles, réordonnées par numéro de séquence (avec gestion du rebouclage).
  3. **Dépaquétisation H.264** (RFC 6184) — NAL simple (1..23), agrégat STAP-A
     (24) et fragment FU-A (28) sont recombinés en un train Annex-B (préfixe
     `00 00 00 01`), précédé des SPS/PPS du SDP.

Capture sans le RTSP (session ouverte avant le début de la capture) : le script
se replie automatiquement, puisque c'est le RTSP qui apprend normalement à
tshark quels ports UDP dissèquer en RTP, et le SDP qui donne SPS/PPS.

  - **flux** — deuxième passe avec l'heuristique RTP de tshark
    (`-o rtp.heuristic_rtp:TRUE`), qui reconnaît les en-têtes RTP sans « decode
    as » ;
  - **codec** — faute de SDP, il est déduit des charges utiles : un flux dont
    les types de NAL sont plausibles est traité comme H.264 ;
  - **SPS/PPS** — d'abord cherchés en bande dans le flux ; sinon pris dans la
    table des jeux relevés dans les SDP des captures du projet (`--param-sets`,
    par défaut choisi automatiquement en essayant lequel décode proprement), ou
    imposés avec `--sps`/`--pps`.

Sortie : un fichier `.h264` (Annex-B) par flux vidéo. `--mp4` le remuxe en MP4
via ffmpeg (le train Annex-B n'a pas d'horloge : la cadence ne sert qu'au remux).

Les pertes de paquets UDP se traduisent par des macroblocs corrompus dans
quelques images — c'est attendu, pas un bug du script.

Mode direct (`--mfd`) : au lieu de relire une capture, découvre le MFD (même
mécanisme que raydb_client / rm_ssh : `discover_mfd`, mDNS puis multicast 5800)
et enregistre son flux RTSP en direct via ffmpeg (`-c copy`, sans réencodage).
Aucun .pcap n'est lu dans ce mode.

Usage :
    ./video_extract.py pcap/remote.pcapng                # écrit remote.h264
    ./video_extract.py cap.pcapng -o ecran.h264          # nom de sortie imposé
    ./video_extract.py cap.pcapng --mp4                  # remux MP4 (ffmpeg)
    ./video_extract.py cap.pcapng --list                 # lister les flux, ne rien écrire
    ./video_extract.py cap.pcapng --ssrc 0x016e2295      # un flux précis
    ./video_extract.py --mfd                             # enregistre le live (découverte auto)
    ./video_extract.py --mfd 192.168.42.1 -o ecran.mp4   # IP imposée, sortie MP4
    ./video_extract.py --mfd --duration 30               # enregistre 30 s puis s'arrête
    ./video_extract.py clic.pcap --param-sets axiom7     # capture sans RTSP : SPS/PPS imposés
"""
from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
from pathlib import Path

# tshark n'est pas toujours dans le PATH (paquet Wireshark sur macOS) : on tente
# le PATH puis les emplacements usuels avant d'abandonner.
TSHARK_FALLBACKS = [
    "/Applications/Wireshark.app/Contents/MacOS/tshark",
    "/usr/local/bin/tshark",
]

START_CODE = b"\x00\x00\x00\x01"        # préfixe de NAL en Annex-B
NAL_TYPE_SPS = 7
NAL_TYPE_PPS = 8
NAL_TYPE_STAP_A = 24
NAL_TYPE_FU_A = 28
SEQ_MOD = 1 << 16                        # les numéros de séquence RTP sont sur 16 bits

# Repli quand la capture ne contient pas le RTSP : sans lui, tshark ne sait pas
# quels ports UDP disséquer en RTP et ne voit qu'un flux « data ». L'heuristique
# reconnaît les en-têtes RTP d'elle-même ; on ne l'active qu'en second passage,
# pour ne pas risquer de faux positifs quand le RTSP est là.
RTP_HEURISTIC = ["-o", "rtp.heuristic_rtp:TRUE"]

# SPS/PPS des MFD, relevés dans les `sprop-parameter-sets` des SDP des captures
# du projet. Ils ne servent que si la capture n'a ni SDP ni SPS/PPS en bande ;
# la résolution affichée est relue du SPS, pas écrite en dur.
KNOWN_PARAM_SETS: dict[str, tuple[str, list[str]]] = {
    # nom        (capture d'origine,       [SPS, PPS] en base64)
    "axiom7":    ("rm1/rm2/rm6/rm7, E70363",
                  ["J0LgH41oDIPaEAAAAwAQAAADAUDxB6g=", "KM4ySA=="]),
    "axiom9":    ("rm13_axiom9, E70481",
                  ["J0LgH41oBQBboQAAAwABAAADABQPEHqA", "KM4ySA=="]),
}

# Découverte du MFD en mode --mfd (repris tel quel de discover_mfd()).
DISCOVERY_GROUP = "224.0.0.1"
DISCOVERY_PORT = 5800


# ------------------------------------------------------------ un flux RTP ----
class Stream:
    """Un flux RTP identifié par son SSRC, avec ses paramètres SDP."""

    def __init__(self, ssrc: int, p_type: int) -> None:
        self.ssrc = ssrc
        self.p_type = p_type
        self.codec = ""                 # renseigné depuis le SDP (« H264 »…)
        self.guessed = False            # codec déduit des charges, faute de SDP
        self.param_sets: list[bytes] = []   # SPS/PPS décodés du sprop
        self.count = 0                  # nombre de paquets RTP

    @property
    def is_h264(self) -> bool:
        return self.codec.upper() == "H264"


# ---------------------------------------------------------- outils tshark ----
def find_binary(name: str, override: str | None, fallbacks: list[str]) -> str:
    """Localise un exécutable : override explicite, puis PATH, puis repli connu."""
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    for cand in fallbacks:
        if Path(cand).exists():
            return cand
    return name                         # laissera échouer avec un message clair


def run_tshark(tshark: str, pcap: Path, display_filter: str,
               fields: list[str], prefs: list[str] | None = None) -> list[list[str]]:
    """Lance tshark en mode « -T fields » et renvoie les lignes découpées.

    Le séparateur de champ est la tabulation ; les champs à occurrences
    multiples sont regroupés par tshark avec une virgule, qu'on gère au cas par
    cas côté appelant. `prefs` passe des options `-o` (voir RTP_HEURISTIC).
    """
    cmd = [tshark, "-r", str(pcap), "-n", *(prefs or []),
           "-Y", display_filter, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"tshark a échoué : {proc.stderr.strip()}")
    rows = []
    for line in proc.stdout.splitlines():
        rows.append(line.split("\t"))
    return rows


# --------------------------------------------------------------- SDP ---------
def _param_sets_from_fmtp(fmtp: str) -> list[bytes]:
    """Décode les `sprop-parameter-sets` d'une ligne fmtp en NAL bruts.

    tshark a déjà éclaté le fmtp sur les virgules, or la valeur du sprop est
    elle-même une liste de NAL séparés par virgule : on repart du jeton
    `sprop-parameter-sets=<b64>` puis on consomme les jetons suivants tant
    qu'ils se décodent en base64 vers une unité NAL valide (ce qui s'arrête
    naturellement sur `profile-level-id=…`, non base64).
    """
    tokens = fmtp.split(",")
    sets: list[str] = []
    start = -1
    for i, tok in enumerate(tokens):
        if tok.startswith("sprop-parameter-sets="):
            sets.append(tok.split("=", 1)[1])
            start = i + 1
            break
    if start < 0:
        return []
    for tok in tokens[start:]:
        if _b64_nal(tok) is None:
            break
        sets.append(tok)
    return [d for d in (_b64_nal(s) for s in sets) if d is not None]


def _b64_nal(token: str) -> bytes | None:
    """Décode un jeton base64 s'il représente une unité NAL H.264 plausible."""
    token = token.strip()
    if not token:
        return None
    try:
        data = base64.b64decode(token, validate=True)
    except ValueError:                  # binascii.Error : jeton non base64
        return None
    if not data or not (1 <= (data[0] & 0x1F) <= 23):
        return None
    return data


def parse_sdp(tshark: str, pcap: Path) -> dict[int, tuple[str, list[bytes]]]:
    """Cartographie type de charge utile RTP → (codec, paramètres SPS/PPS).

    Lue depuis le SDP de la réponse DESCRIBE. tshark dissèque le SDP même quand
    Wireshark le marque « Malformed », donc on s'appuie sur ses champs plutôt
    que de reparser le texte brut.
    """
    rows = run_tshark(tshark, pcap, "sdp",
                      ["sdp.media", "sdp.mime.type", "sdp.fmtp.parameter"])
    table: dict[int, tuple[str, list[bytes]]] = {}
    for row in rows:
        media = row[0] if len(row) > 0 else ""
        codec = row[1] if len(row) > 1 else ""
        fmtp = row[2] if len(row) > 2 else ""
        # « video 0 RTP/AVP 96 » → type de charge utile = dernier champ.
        parts = media.split()
        if len(parts) < 4 or not parts[-1].isdigit():
            continue
        p_type = int(parts[-1])
        table[p_type] = (codec, _param_sets_from_fmtp(fmtp))
    return table


# ----------------------------------------------- faute de SDP : déductions ---
class _BitReader:
    """Lecture bit à bit d'un RBSP H.264, octets anti-émulation retirés.

    Dans un NAL, la séquence `00 00 03` code un `00 00` littéral : le `03`
    n'appartient pas au flux de bits et doit disparaître avant toute lecture.
    """

    def __init__(self, data: bytes) -> None:
        rbsp = bytearray()
        i = 0
        while i < len(data):
            if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
                rbsp += data[i:i + 2]
                i += 3
            else:
                rbsp.append(data[i])
                i += 1
        self.data = bytes(rbsp)
        self.pos = 0

    def u(self, n: int) -> int:
        """n bits non signés."""
        value = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]        # IndexError = SPS tronqué
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def ue(self) -> int:
        """Exp-Golomb non signé."""
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("exp-Golomb aberrant")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        """Exp-Golomb signé."""
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def sps_resolution(sps: bytes) -> tuple[int, int] | None:
    """Largeur et hauteur en pixels lues dans un SPS, ou None s'il est illisible.

    Sert uniquement à étiqueter les jeux de paramètres dans les messages : on
    préfère relire la résolution que la coder en dur à côté du base64. Tout NAL
    qui n'est pas un SPS (un PPS seul passé à `--pps`, par exemple) est refusé
    plutôt que parcouru au hasard.
    """
    if not sps or (sps[0] & 0x1F) != NAL_TYPE_SPS:
        return None
    try:
        br = _BitReader(sps[1:])                   # saut de l'octet d'en-tête NAL
        profile = br.u(8)
        br.u(8)                                    # contraintes + réservé
        br.u(8)                                    # niveau
        br.ue()                                    # seq_parameter_set_id
        chroma = 1                                 # 4:2:0 par défaut (baseline)
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma = br.ue()
            if chroma == 3:
                br.u(1)                            # separate_colour_plane_flag
            br.ue()                                # bit_depth_luma_minus8
            br.ue()                                # bit_depth_chroma_minus8
            br.u(1)                                # qpprime_y_zero_transform_bypass
            if br.u(1):                            # seq_scaling_matrix_present
                for i in range(8 if chroma != 3 else 12):
                    if br.u(1):
                        last = nxt = 8
                        for _ in range(16 if i < 6 else 64):
                            if nxt:
                                nxt = (last + br.se() + 256) % 256
                            last = nxt or last
        br.ue()                                    # log2_max_frame_num_minus4
        poc_type = br.ue()
        if poc_type == 0:
            br.ue()
        elif poc_type == 1:
            br.u(1)
            br.se()
            br.se()
            for _ in range(br.ue()):
                br.se()
        br.ue()                                    # max_num_ref_frames
        br.u(1)                                    # gaps_in_frame_num_value_allowed
        width = (br.ue() + 1) * 16
        height_map = br.ue() + 1
        frame_mbs_only = br.u(1)
        if not frame_mbs_only:
            br.u(1)                                # mb_adaptive_frame_field_flag
        height = (2 - frame_mbs_only) * height_map * 16
        br.u(1)                                    # direct_8x8_inference_flag
        if br.u(1):                                # frame_cropping_flag
            if chroma == 0:
                sub_x = sub_y = 1
            elif chroma == 1:
                sub_x = sub_y = 2
            elif chroma == 2:
                sub_x, sub_y = 2, 1
            else:
                sub_x = sub_y = 1
            left, right, top, bottom = br.ue(), br.ue(), br.ue(), br.ue()
            width -= sub_x * (left + right)
            height -= sub_y * (2 - frame_mbs_only) * (top + bottom)
        if width <= 0 or height <= 0:
            return None
        return width, height
    except (IndexError, ValueError):
        return None


def looks_like_h264(payloads: list[bytes], sample: int = 200) -> bool:
    """Devine si les charges utiles RTP d'un flux portent du H.264.

    Sans SDP il n'y a pas de nom de codec : on regarde l'octet d'en-tête NAL des
    premières charges. Un flux H.264 a `forbidden_zero_bit` à 0 et un type dans
    1..28 sur la quasi-totalité des paquets — c'est assez discriminant pour ne
    pas confondre avec un flux audio ou une charge opaque.
    """
    seen = plausible = 0
    for payload in payloads[:sample]:
        if not payload:
            continue
        seen += 1
        if not payload[0] & 0x80 and 1 <= (payload[0] & 0x1F) <= 28:
            plausible += 1
    return seen > 0 and plausible >= 0.95 * seen


def decode_param_set(token: str) -> bytes:
    """Décode un SPS/PPS donné en ligne de commande, base64 ou hexadécimal."""
    nal = _b64_nal(token)
    if nal is not None:
        return nal
    cleaned = token.strip().replace(":", "").replace(" ", "")
    try:
        nal = bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError(f"ni base64 ni hexadécimal : {token!r}") from None
    if not nal or not 1 <= (nal[0] & 0x1F) <= 23:
        raise ValueError(f"pas une unité NAL plausible : {token!r}")
    return nal


def inband_param_sets(annexb: bytes) -> list[bytes]:
    """Premiers SPS/PPS portés par le train lui-même, à remonter en tête.

    Le MFD réémet périodiquement ses SPS/PPS, mais une capture commence rarement
    dessus : les laisser à leur place rendrait indécodable tout ce qui précède
    (« non-existing PPS 0 referenced »). Les hisser devant est sans risque, un
    décodeur relit sans broncher un jeu identique rencontré plus loin.

    Le découpage sur la magie de 4 octets est sûr : l'encodage anti-émulation
    interdit `00 00 00` à l'intérieur d'une unité NAL.
    """
    sps = pps = b""
    for nal in annexb.split(START_CODE):
        if not nal:
            continue
        kind = nal[0] & 0x1F
        if kind == NAL_TYPE_SPS and not sps:
            sps = nal
        elif kind == NAL_TYPE_PPS and not pps:
            pps = nal
        if sps and pps:
            break
    return [n for n in (sps, pps) if n]


def count_decode_errors(annexb: bytes, ffmpeg: str, frames: int = 12) -> int | None:
    """Décode le début du train et compte les plaintes de ffmpeg.

    Sert à départager des SPS candidats : avec le mauvais, la taille d'image est
    fausse et le décodeur se plaint dès le premier macrobloc. Renvoie None si
    ffmpeg est introuvable (on ne pourra pas départager).
    """
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
           "-frames:v", str(frames), "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, input=annexb, capture_output=True, check=False)
    except FileNotFoundError:
        return None
    return len([ln for ln in proc.stderr.decode(errors="replace").splitlines() if ln.strip()])


def choose_param_sets(body: bytes, ffmpeg: str) -> tuple[list[bytes], str]:
    """Choisit un jeu de la table en essayant lequel décode le plus proprement.

    Les MFD du projet n'ont que deux jeux connus, qui ne diffèrent que par la
    résolution (800×480 et 1280×720) : le mauvais fait échouer le décodage dès
    les premiers macroblocs, ce qui suffit à trancher. Faute de ffmpeg, on prend
    le premier de la table en le signalant à l'appelant.
    """
    candidates = [(name, [decode_param_set(t) for t in tokens])
                  for name, (_origin, tokens) in KNOWN_PARAM_SETS.items()]
    head = body[:400_000]
    best: tuple[int, str, list[bytes]] | None = None
    for name, sets in candidates:
        errors = count_decode_errors(b"".join(START_CODE + n for n in sets) + head, ffmpeg)
        if errors is None:                         # pas de ffmpeg : rien à départager
            fallback, fallback_sets = candidates[0]
            return fallback_sets, f"{fallback} (table, NON vérifié : ffmpeg absent)"
        if best is None or errors < best[0]:
            best = (errors, name, sets)
        if errors == 0:
            break
    assert best is not None                        # la table n'est jamais vide
    errors, name, sets = best
    suffix = "" if errors == 0 else f", {errors} erreur(s) de décodage résiduelle(s)"
    return sets, f"{name} (table, choisi automatiquement{suffix})"


def resolve_param_sets(st: Stream, body: bytes, cli_sets: list[bytes],
                       choice: str, ffmpeg: str) -> tuple[list[bytes], str]:
    """Décide quels SPS/PPS préfixer au train, et dit d'où ils sortent.

    Ordre de priorité : les jeux imposés en ligne de commande (`--sps/--pps`,
    puis un `--param-sets` explicite), le SDP, ce que le flux porte lui-même en
    bande, et en dernier ressort la table des jeux connus — ce sont les deux
    derniers cas qui rattrapent une capture sans RTSP.
    """
    if cli_sets:
        return cli_sets, "imposés (--sps/--pps)"
    if choice in KNOWN_PARAM_SETS:
        return [decode_param_set(t) for t in KNOWN_PARAM_SETS[choice][1]], f"{choice} (table)"
    if choice == "none":
        return [], "aucun (--param-sets none)"
    if st.param_sets:
        return st.param_sets, "du SDP"
    inband = inband_param_sets(body)
    if inband:
        return inband, "en bande, remontés en tête"
    return choose_param_sets(body, ffmpeg)


# --------------------------------------------------------------- RTP ---------
def list_streams(tshark: str, pcap: Path,
                 prefs: list[str] | None = None) -> list[Stream]:
    """Énumère les flux RTP présents (un par SSRC), avec leur type de charge."""
    rows = run_tshark(tshark, pcap, "rtp", ["rtp.ssrc", "rtp.p_type"], prefs)
    streams: dict[int, Stream] = {}
    for row in rows:
        if len(row) < 2 or not row[0]:
            continue
        try:
            ssrc = int(row[0], 16) if row[0].lower().startswith("0x") else int(row[0])
            p_type = int(row[1])
        except ValueError:
            continue
        st = streams.get(ssrc)
        if st is None:
            st = streams[ssrc] = Stream(ssrc, p_type)
        st.count += 1
    return list(streams.values())


def rtp_payloads(tshark: str, pcap: Path, ssrc: int,
                 prefs: list[str] | None = None) -> list[tuple[int, bytes]]:
    """Charges utiles RTP d'un flux, en (seq, octets), dans l'ordre de capture."""
    flt = f"rtp.ssrc==0x{ssrc:08x} && rtp.payload"
    rows = run_tshark(tshark, pcap, flt, ["rtp.seq", "rtp.payload"], prefs)
    out: list[tuple[int, bytes]] = []
    for row in rows:
        if len(row) < 2 or not row[0] or not row[1]:
            continue
        payload = bytes.fromhex(row[1].replace(":", ""))
        out.append((int(row[0]), payload))
    return out


def order_by_seq(packets: list[tuple[int, bytes]]) -> tuple[list[bytes], int]:
    """Réordonne par numéro de séquence en déroulant le rebouclage 16 bits.

    Déduplique les séquences répétées (retransmissions RTP ou doublons de
    capture) : les réémettre injecterait un fragment FU-A en double, ce qui peut
    corrompre le réassemblage. Renvoie les charges utiles ordonnées et le nombre
    de paquets manquants (trous dans la séquence — pertes UDP typiques de RTP).
    """
    extended: list[tuple[int, bytes]] = []
    base = 0
    prev: int | None = None
    for seq, payload in packets:
        if prev is not None and prev - seq > SEQ_MOD // 2:
            base += SEQ_MOD             # franchissement 65535 → 0
        extended.append((base + seq, payload))
        prev = seq
    extended.sort(key=lambda x: x[0])

    ordered: list[bytes] = []
    seen: set[int] = set()
    for eseq, payload in extended:
        if eseq not in seen:
            seen.add(eseq)
            ordered.append(payload)
    lost = 0
    if extended:
        span = extended[-1][0] - extended[0][0] + 1
        lost = max(0, span - len(seen))
    return ordered, lost


# ------------------------------------------------ dépaquétisation H.264 ------
def depacketize(payloads: list[bytes]) -> bytes:
    """Recombine les charges RTP H.264 en train Annex-B (RFC 6184)."""
    out = bytearray()
    fu_buffer = bytearray()
    fu_active = False
    for p in payloads:
        if not p:
            continue
        nal_type = p[0] & 0x1F
        if 1 <= nal_type <= 23:                 # NAL transmis tel quel
            out += START_CODE + p
        elif nal_type == NAL_TYPE_STAP_A:       # plusieurs NAL agrégés
            i = 1
            while i + 2 <= len(p):
                size = int.from_bytes(p[i:i + 2], "big")
                i += 2
                if size == 0 or i + size > len(p):
                    break
                out += START_CODE + p[i:i + size]
                i += size
        elif nal_type == NAL_TYPE_FU_A:         # NAL fragmenté sur plusieurs RTP
            if len(p) < 2:
                continue
            fu_header = p[1]
            if fu_header & 0x80:                # bit Start
                nal_header = (p[0] & 0xE0) | (fu_header & 0x1F)
                fu_buffer = bytearray([nal_header])
                fu_active = True
            if fu_active:
                fu_buffer += p[2:]
            if fu_header & 0x40 and fu_active:  # bit End
                out += START_CODE + fu_buffer
                fu_active = False
    return bytes(out)


# ------------------------------------------------------------- remux ---------
def remux_mp4(h264: Path, mp4: Path, fps: float, ffmpeg: str) -> None:
    """Remuxe le train Annex-B en MP4 (copie de flux, cadence imposée)."""
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-y",
           "-r", str(fps), "-f", "h264", "-i", str(h264),
           "-c", "copy", str(mp4)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg a échoué : {proc.stderr.strip()}")


# ------------------------------------------------- enregistrement direct -----
def record_from_mfd(ip: str | None, url_tpl: str, out_path: Path, ffmpeg: str,
                    transport: str, duration: float | None,
                    discover_timeout: float) -> int:
    """Découvre le MFD (si `ip` absent) et enregistre son flux RTSP via ffmpeg.

    Réutilise `discover_mfd()` — le même mécanisme que les autres clients (mDNS
    puis multicast 5800). ffmpeg copie le flux (`-c copy`, pas de réencodage) ;
    le conteneur de sortie découle de l'extension de `out_path`. Renvoie le code
    de sortie de ffmpeg."""
    if not ip:
        from rm_ssh import discover_mfd  # même découverte que rm_ssh
        print(f"[*] découverte MFD (mDNS puis mcast {DISCOVERY_GROUP}:{DISCOVERY_PORT})…",
              file=sys.stderr)
        ip = discover_mfd(discover_timeout)
        if not ip:
            sys.exit("aucun MFD découvert — vérifier le WiFi du bord, ou --mfd <IP>")

    url = url_tpl.format(ip=ip)
    cmd = [ffmpeg, "-nostdin", "-loglevel", "info",
           "-rtsp_transport", transport, "-i", url, "-c", "copy"]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-y", str(out_path)]

    print(f"[*] enregistrement {url} → {out_path}"
          f"{f' ({duration:g}s)' if duration else ' (Ctrl-C pour arrêter)'}",
          file=sys.stderr)
    try:
        proc = subprocess.Popen(cmd)
    except FileNotFoundError:
        sys.exit(f"ffmpeg introuvable ({ffmpeg}) — l'installer, ou --ffmpeg <chemin>")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Le SIGINT est aussi allé à ffmpeg (même groupe de processus) : il
        # arrête l'enregistrement et finalise le conteneur. On le laisse finir.
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return proc.wait()


# --------------------------------------------------------------- main --------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Reconstitue le flux vidéo H.264 d'une capture RTSP/RTP.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("pcap", type=Path, nargs="?",
                    help="capture .pcap/.pcapng à relire (omis en mode --mfd)")
    ap.add_argument("-o", "--output", type=Path,
                    help="fichier de sortie (pcap : <capture>.h264, un par flux ; "
                         "--mfd : défaut mfd_screen.mp4)")
    ap.add_argument("--mfd", nargs="?", const="", metavar="IP",
                    help="enregistrer le flux vidéo en direct depuis le MFD "
                         "(RTSP → ffmpeg, -c copy) au lieu de lire un .pcap ; sans "
                         "valeur, découverte auto (discover_mfd : mDNS puis mcast "
                         "5800), sinon IP imposée")
    ap.add_argument("--url", default="rtsp://{ip}:8554/RAYMARINEMFD",
                    help="gabarit d'URL RTSP en mode --mfd ({ip} = IP du MFD)")
    ap.add_argument("--rtsp-transport", choices=["tcp", "udp"], default="tcp",
                    help="transport RTSP en mode --mfd (défaut tcp, fiable)")
    ap.add_argument("--duration", type=float,
                    help="durée d'enregistrement en s (mode --mfd ; défaut : jusqu'à Ctrl-C)")
    ap.add_argument("--discover-timeout", type=float, default=15,
                    help="délai de découverte du MFD en mode --mfd (s, défaut 15)")
    ap.add_argument("--ssrc", help="ne traiter que ce flux (ex. 0x016e2295)")
    ap.add_argument("--param-sets", default="auto",
                    choices=["auto", "none", *KNOWN_PARAM_SETS],
                    help="capture sans RTSP ni SPS/PPS en bande : jeu de "
                         "paramètres à préfixer (défaut auto = essayer lequel "
                         "de la table décode proprement ; none = ne rien "
                         "préfixer). Voir --list pour la table.")
    ap.add_argument("--sps", help="SPS imposé (base64 ou hexadécimal), "
                                  "prioritaire sur le SDP et sur --param-sets")
    ap.add_argument("--pps", help="PPS imposé (base64 ou hexadécimal)")
    ap.add_argument("--mp4", action="store_true",
                    help="remuxer aussi en MP4 via ffmpeg")
    ap.add_argument("--fps", type=float, default=20.0,
                    help="cadence pour le remux MP4 (défaut 20 ; sans effet sur "
                         "le .h264)")
    ap.add_argument("--list", action="store_true",
                    help="lister les flux RTP et quitter")
    ap.add_argument("--tshark", help="chemin de l'exécutable tshark")
    ap.add_argument("--ffmpeg", help="chemin de l'exécutable ffmpeg")
    args = ap.parse_args()

    # Mode direct : RTSP → ffmpeg, aucun .pcap lu.
    if args.mfd is not None:
        if args.pcap is not None:
            ap.error("--mfd et un fichier .pcap sont exclusifs")
        ffmpeg = find_binary("ffmpeg", args.ffmpeg, [])
        out_path = args.output or Path("mfd_screen.mp4")
        sys.exit(record_from_mfd(args.mfd or None, args.url, out_path, ffmpeg,
                                 args.rtsp_transport, args.duration,
                                 args.discover_timeout))

    if args.pcap is None:
        ap.error("préciser une capture .pcap, ou --mfd pour l'enregistrement direct")
    if not args.pcap.exists():
        sys.exit(f"{ap.prog}: capture introuvable : {args.pcap}")

    try:
        cli_sets = [decode_param_set(t) for t in (args.sps, args.pps) if t]
    except ValueError as exc:
        ap.error(str(exc))

    tshark = find_binary("tshark", args.tshark, TSHARK_FALLBACKS)
    ffmpeg = find_binary("ffmpeg", args.ffmpeg, [])
    prefs: list[str] = []
    try:
        streams = list_streams(tshark, args.pcap)
        if not streams:
            # Sans RTSP dans la capture, tshark n'a reçu aucune consigne de
            # dissection : les paquets vidéo restent de simples « data ». On
            # refait une passe avec l'heuristique RTP, qui reconnaît les
            # en-têtes d'elle-même.
            prefs = RTP_HEURISTIC
            streams = list_streams(tshark, args.pcap, prefs)
            if streams:
                print(f"[*] pas de RTSP dans la capture : {len(streams)} flux "
                      "retrouvé(s) par l'heuristique RTP", file=sys.stderr)
        sdp = parse_sdp(tshark, args.pcap)
    except (RuntimeError, FileNotFoundError) as exc:
        sys.exit(f"{ap.prog}: {exc}")

    if not streams:
        sys.exit(f"{ap.prog}: aucun flux RTP dans {args.pcap}, même avec "
                 "l'heuristique (la capture contient-elle de la vidéo ?)")

    # Enrichit chaque flux avec son codec et ses paramètres SDP.
    for st in streams:
        codec, param_sets = sdp.get(st.p_type, ("", []))
        st.codec = codec
        st.param_sets = param_sets

    # Charges utiles mises en cache : le repli en a besoin pour deviner le
    # codec, et l'extraction les relirait sinon une seconde fois.
    cache: dict[int, list[tuple[int, bytes]]] = {}

    def payloads_of(st: Stream) -> list[tuple[int, bytes]]:
        if st.ssrc not in cache:
            cache[st.ssrc] = rtp_payloads(tshark, args.pcap, st.ssrc, prefs)
        return cache[st.ssrc]

    # Faute de SDP il n'y a pas de nom de codec : on le déduit des charges.
    for st in streams:
        if not st.codec and looks_like_h264([pl for _seq, pl in payloads_of(st)]):
            st.codec, st.guessed = "H264", True

    if args.list:
        print(f"# {len(streams)} flux RTP dans {args.pcap.name}")
        for st in streams:
            sets = f"{len(st.param_sets)} param-sets" if st.param_sets else "sans SDP"
            codec = (st.codec + " (déduit)" if st.guessed else st.codec) or "?"
            print(f"  SSRC 0x{st.ssrc:08x}  PT {st.p_type}  "
                  f"{codec:16}  {st.count:5d} paquets  {sets}")
        print("# jeux SPS/PPS connus (--param-sets, utiles sans RTSP) :")
        for name, (origin, tokens) in KNOWN_PARAM_SETS.items():
            res = sps_resolution(decode_param_set(tokens[0]))
            dims = f"{res[0]}×{res[1]}" if res else "résolution illisible"
            print(f"  {name:10} {dims:16} {origin}")
        return

    wanted = None
    if args.ssrc:
        wanted = int(args.ssrc, 16) if args.ssrc.lower().startswith("0x") else int(args.ssrc)

    targets = [s for s in streams if s.is_h264 and (wanted is None or s.ssrc == wanted)]
    if not targets:
        sys.exit(f"{ap.prog}: aucun flux H.264 à extraire "
                 f"({'SSRC absent' if wanted else 'aucun flux H264 vu'}). "
                 "Voir --list.")

    multi = len(targets) > 1
    for st in targets:
        ordered, lost = order_by_seq(payloads_of(st))
        body = depacketize(ordered)
        param_sets, origin = resolve_param_sets(st, body, cli_sets,
                                                args.param_sets, ffmpeg)
        annexb = b"".join(START_CODE + ns for ns in param_sets) + body

        if args.output and not multi:
            out_path = args.output
        elif args.output:               # plusieurs flux : suffixe SSRC
            out_path = args.output.with_suffix(f".{st.ssrc:08x}.h264")
        else:
            stem = args.pcap.stem + (f".{st.ssrc:08x}" if multi else "")
            out_path = args.pcap.with_name(stem + ".h264")

        out_path.write_bytes(annexb)
        loss = f", {lost} paquet(s) perdu(s)" if lost else ""
        res = next((r for r in map(sps_resolution, param_sets) if r), None)
        dims = f", {res[0]}×{res[1]}" if res else ""
        print(f"écrit {out_path}  ({len(annexb)} octets, {len(ordered)} paquets"
              f"{loss}, SPS/PPS {origin}{dims})")

        if args.mp4:
            mp4_path = out_path.with_suffix(".mp4")
            try:
                remux_mp4(out_path, mp4_path, args.fps, ffmpeg)
                print(f"      → {mp4_path}  (MP4 @ {args.fps:g} fps)")
            except (RuntimeError, FileNotFoundError) as exc:
                print(f"      remux MP4 impossible : {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
