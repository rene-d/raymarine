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

La capture doit contenir le RTSP : c'est lui qui apprend à tshark quels ports
UDP dissèquer en RTP. Pour un flux H.264 « nu » (RTP sans RTSP), il faudrait un
« decode as » que ce script ne met pas en place.

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
NAL_TYPE_STAP_A = 24
NAL_TYPE_FU_A = 28
SEQ_MOD = 1 << 16                        # les numéros de séquence RTP sont sur 16 bits

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
               fields: list[str]) -> list[list[str]]:
    """Lance tshark en mode « -T fields » et renvoie les lignes découpées.

    Le séparateur de champ est la tabulation ; les champs à occurrences
    multiples sont regroupés par tshark avec une virgule, qu'on gère au cas par
    cas côté appelant.
    """
    cmd = [tshark, "-r", str(pcap), "-n", "-Y", display_filter, "-T", "fields"]
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


# --------------------------------------------------------------- RTP ---------
def list_streams(tshark: str, pcap: Path) -> list[Stream]:
    """Énumère les flux RTP présents (un par SSRC), avec leur type de charge."""
    rows = run_tshark(tshark, pcap, "rtp", ["rtp.ssrc", "rtp.p_type"])
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


def rtp_payloads(tshark: str, pcap: Path, ssrc: int) -> list[tuple[int, bytes]]:
    """Charges utiles RTP d'un flux, en (seq, octets), dans l'ordre de capture."""
    flt = f"rtp.ssrc==0x{ssrc:08x} && rtp.payload"
    rows = run_tshark(tshark, pcap, flt, ["rtp.seq", "rtp.payload"])
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

    tshark = find_binary("tshark", args.tshark, TSHARK_FALLBACKS)
    try:
        streams = list_streams(tshark, args.pcap)
        sdp = parse_sdp(tshark, args.pcap)
    except (RuntimeError, FileNotFoundError) as exc:
        sys.exit(f"{ap.prog}: {exc}")

    # Enrichit chaque flux avec son codec et ses paramètres SDP.
    for st in streams:
        codec, param_sets = sdp.get(st.p_type, ("", []))
        st.codec = codec
        st.param_sets = param_sets

    if not streams:
        sys.exit(f"{ap.prog}: aucun flux RTP dans {args.pcap} "
                 "(la capture contient-elle bien le RTSP ?)")

    if args.list:
        print(f"# {len(streams)} flux RTP dans {args.pcap.name}")
        for st in streams:
            sets = f"{len(st.param_sets)} param-sets" if st.param_sets else "sans SDP"
            print(f"  SSRC 0x{st.ssrc:08x}  PT {st.p_type}  "
                  f"{st.codec or '?':6}  {st.count:5d} paquets  {sets}")
        return

    wanted = None
    if args.ssrc:
        wanted = int(args.ssrc, 16) if args.ssrc.lower().startswith("0x") else int(args.ssrc)

    targets = [s for s in streams if s.is_h264 and (wanted is None or s.ssrc == wanted)]
    if not targets:
        sys.exit(f"{ap.prog}: aucun flux H.264 à extraire "
                 f"({'SSRC absent' if wanted else 'aucun flux H264 vu'}). "
                 "Voir --list.")

    ffmpeg = find_binary("ffmpeg", args.ffmpeg, [])
    multi = len(targets) > 1
    for st in targets:
        payloads = rtp_payloads(tshark, args.pcap, st.ssrc)
        ordered, lost = order_by_seq(payloads)
        preamble = b"".join(START_CODE + ns for ns in st.param_sets)
        annexb = preamble + depacketize(ordered)

        if args.output and not multi:
            out_path = args.output
        elif args.output:               # plusieurs flux : suffixe SSRC
            out_path = args.output.with_suffix(f".{st.ssrc:08x}.h264")
        else:
            stem = args.pcap.stem + (f".{st.ssrc:08x}" if multi else "")
            out_path = args.pcap.with_name(stem + ".h264")

        out_path.write_bytes(annexb)
        loss = f", {lost} paquet(s) perdu(s)" if lost else ""
        sets = "SPS/PPS du SDP" if st.param_sets else "sans SPS/PPS (in-band ?)"
        print(f"écrit {out_path}  ({len(annexb)} octets, {len(ordered)} paquets"
              f"{loss}, {sets})")

        if args.mp4:
            mp4_path = out_path.with_suffix(".mp4")
            try:
                remux_mp4(out_path, mp4_path, args.fps, ffmpeg)
                print(f"      → {mp4_path}  (MP4 @ {args.fps:g} fps)")
            except (RuntimeError, FileNotFoundError) as exc:
                print(f"      remux MP4 impossible : {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
