"""
rtsp.py — serveur RTSP (TCP 8554) : recopie d'écran, comme le vrai MFD.

Le MFD Raymarine diffuse son écran via un « GStreamer RTSP server » (bannière
vue en capture), sur `rtsp://IP:8554/RAYMARINEMFD` en RTP/H.264. Ce module rejoue
la **même techno** (GStreamer `GstRtspServer`) et, par défaut, le **vrai écran**
d'un Axiom 7 reconstitué depuis une capture par `video_extract.py`
(`video/mfd_screen.h264`), diffusé en boucle.

La pipeline **décode puis ré-encode** le fichier (via `videorate` à cadence fixe)
plutôt que de recopier le train d'origine : c'est ce qui donne une boucle propre
et un horodatage correct. Conséquence assumée : les SPS/PPS diffèrent de ceux du
MFD réel — sans importance pour un client (VLC/`mfd_remote.py`), qui lit le SDP
annoncé par le serveur.

Le serveur ne fait tourner la pipeline **que lorsqu'un client est connecté**
(`GstRtspServer` est à la demande) : aucun coût CPU au repos.

GStreamer + `gst-rtsp-server` (bindings `gi`) donnent la meilleure fidélité (même
brique que le MFD, service à la demande). Absents, on tente un **repli sans
GStreamer** : le serveur RTSP autonome **mediamtx**, qui lance **ffmpeg** *à la
demande* (`runOnDemand`) pour pousser la boucle H.264 (`-c copy`, sans
ré-encodage) dès qu'un client se connecte, le **relance** s'il meurt, et l'arrête
au départ du dernier client. Le repli est fonctionnellement équivalent pour un
client (VLC, `mfd_remote.py`) ; seule la bannière serveur diffère (« gortsplib »
au lieu de « GStreamer »). Si ni GStreamer ni ffmpeg+mediamtx ne sont là, le
module s'efface et les autres services du simulateur tournent quand même.
"""
from __future__ import annotations

import atexit
import logging
import shutil
import socket
import subprocess
import threading
import time

from . import config

log = logging.getLogger("rtsp")

# Niveaux de mediamtx (`2026/08/05 23:44:06 INF [RTSP] …`) vers les nôtres, pour
# qu'un avertissement du serveur en reste un dans notre journal.
_MEDIAMTX_LEVELS = {"DEB": logging.DEBUG, "INF": logging.INFO,
                    "WRN": logging.WARNING, "ERR": logging.ERROR}

# Import paresseux et tolérant : gi/GStreamer n'est pas toujours là.
try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstRtspServer", "1.0")
    from gi.repository import GLib, Gst, GstRtspServer

    AVAILABLE = True
except (ImportError, ValueError) as _exc:      # ValueError : version gi absente
    AVAILABLE = False
    _IMPORT_ERROR = str(_exc)


def _pipeline() -> str:
    """Chaîne de lancement GStreamer de la fabrique RTSP.

    - `multifilesrc … loop=true` relit le fichier H.264 sans fin ;
    - décodage puis `videorate` fixent une cadence stable ;
    - `x264enc tune=zerolatency` réencode en flux RTP-compatible ;
    - `rtph264pay name=pay0 pt=96` : le nom `pay0` est ce que `GstRtspServer`
      attend pour brancher le flux, `pt=96` reprend le type de charge du MFD.

    À défaut de fichier vidéo, on retombe sur une mire (`videotestsrc`) avec
    incrustation, pour que le service reste démontrable.
    """
    fps = config.RTSP_FPS
    if config.RTSP_VIDEO.exists():
        source = (f"multifilesrc location={config.RTSP_VIDEO} loop=true "
                  f"! h264parse ! avdec_h264 ! videoconvert ! videorate "
                  f"! video/x-raw,framerate={fps}/1")
    else:
        log.warning("vidéo %s absente — repli sur une mire de test",
                    config.RTSP_VIDEO)
        source = (f"videotestsrc is-live=true ! video/x-raw,width=800,height=480,"
                  f"framerate={fps}/1 ! textoverlay text=\"{config.DEVICE_ID}\" "
                  f"valignment=top halignment=left font-desc=\"Sans 24\"")
    return (f"( {source} ! x264enc tune=zerolatency bitrate=2500 "
            f"key-int-max={int(fps) * 2} ! rtph264pay name=pay0 pt=96 "
            f"config-interval=1 )")


class RtspServer:
    """Serveur RTSP GStreamer, piloté par sa propre boucle GLib dans un thread."""

    def __init__(self) -> None:
        Gst.init(None)
        self._loop = GLib.MainLoop()
        self._server = GstRtspServer.RTSPServer()
        self._server.set_address("0.0.0.0")
        self._server.set_service(str(config.RTSP_PORT))

        factory = GstRtspServer.RTSPMediaFactory()
        factory.set_launch(_pipeline())
        factory.set_shared(True)               # un seul flux partagé entre clients
        mount = "/" + config.RTSP_PATH
        self._server.get_mount_points().add_factory(mount, factory)
        self._server.attach(None)
        self._mount = mount

    def run(self) -> None:
        self._loop.run()

    def stop(self) -> None:
        self._loop.quit()


# ------------------------------------- repli sans GStreamer : ffmpeg+mediamtx --
class FfmpegRtspServer:
    """Repli RTSP sans GStreamer : mediamtx, qui lance ffmpeg *à la demande*.

    Notre seul sous-processus est **mediamtx** ; c'est lui qui démarre, relance et
    arrête **ffmpeg** (le publieur H.264) selon la présence de clients, via les
    options `runOnDemand*` du path. Plus de publieur à superviser côté Python, ni
    de flux orphelin qui laisserait les DESCRIBE en 404.

    ⚠ **Piège connu — l'app Raymarine ne s'accroche qu'une fois sur cinq.**
    mediamtx **retarde d'une seconde entière** sa réponse au `SETUP` quand le
    `User-Agent` du client commence par `GStreamer` (contournement d'un bogue de
    `rtspsrc`, câblé dans le binaire). Mesuré ici : 1001 ms avec
    `GStreamer/1.20.4`, 0,2 ms avec `LIVE555…` ou VLC. L'app **RayControl**
    (LIVE555) n'est donc pas concernée, RayConnect (GStreamer 1.20.4)
    l'est de plein fouet : elle abandonne souvent avant le `PLAY`, d'où une
    connexion vidéo intermittente. Le vrai MFD, lui, sert du GStreamer sans ce
    délai. Correctif : installer GStreamer pour que `RtspServer` ci-dessus
    reprenne la main — c'est de toute façon le chemin fidèle.
    """

    def __init__(self, mediamtx_bin: str, ffmpeg_bin: str) -> None:
        self._mediamtx_bin = mediamtx_bin
        self._ffmpeg_bin = ffmpeg_bin
        self._procs: list[subprocess.Popen] = []

    def start(self) -> bool:
        port = config.RTSP_PORT
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Config mediamtx : RTSP seul (les autres protocoles ouvriraient des ports
        # et, pour MoQ, généreraient un certificat dans le cwd). Le path est « à la
        # demande » : mediamtx lance ffmpeg quand un client arrive, le RELANCE s'il
        # meurt (runOnDemandRestart) et l'arrête après le départ du dernier client
        # (runOnDemandCloseAfter) — plus de publieur orphelin qui laisserait les
        # DESCRIBE en 404, et fidèle au « à la demande » du vrai MFD.
        conf = config.STATE_DIR / "mediamtx.yml"
        cmd_yaml = self._ondemand_cmd().replace("'", "''")   # échappement YAML '↦''
        conf.write_text(
            f"logLevel: {config.RTSP_LOG_LEVEL}\n"
            f"rtspAddress: :{port}\n"
            "rtmp: no\n"
            "hls: no\n"
            "webrtc: no\n"
            "srt: no\n"
            "moq: no\n"
            "paths:\n"
            f"  {config.RTSP_PATH}:\n"
            f"    runOnDemand: '{cmd_yaml}'\n"
            "    runOnDemandRestart: yes\n"
            "    runOnDemandStartTimeout: 10s\n"
            "    runOnDemandCloseAfter: 5s\n"
        )
        try:
            # La sortie de mediamtx est reprise dans notre journal plutôt que
            # jetée : c'est la seule fenêtre sur ce qu'il fait des sessions (qui
            # lit, qui publie, qui est détruit et pourquoi). Sans elle, une panne
            # côté serveur ne se voit que par ce qui manque sur le fil.
            mtx = subprocess.Popen(
                [self._mediamtx_bin, str(conf)], cwd=str(config.STATE_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except OSError as e:
            log.warning("repli RTSP : mediamtx n'a pas démarré (%s)", e)
            return False
        self._procs.append(mtx)
        threading.Thread(target=self._pump_logs, args=(mtx,),
                         name="mediamtx logs", daemon=True).start()

        if not self._wait_port(port, 3.0):
            log.warning("repli RTSP : mediamtx n'écoute pas sur %d", port)
            self.stop()
            return False

        atexit.register(self.stop)
        return True

    def _ondemand_cmd(self) -> str:
        """Commande shell que mediamtx lance à la demande pour publier le flux.

        mediamtx l'exécute via `sh -c` (d'où les guillemets autour des chemins,
        pour tolérer les espaces) et substitue `$RTSP_PORT`/`$MTX_PATH`. `-c copy`
        si le fichier H.264 existe (aucun ré-encodage), sinon une mire libx264.

        `-pkt_size 1400` borne la taille des paquets RTP produits par ffmpeg. Par
        défaut il vise 1460 octets, au-dessus du plafond de mediamtx (1440) : ce
        dernier journalisait alors « RTP packets are too big, remuxing them into
        smaller ones » et redécoupait *tous* les paquets du flux. 1400 laisse la
        marge des en-têtes et supprime ce ré-empaquetage.
        """
        fps = int(config.RTSP_FPS)
        ff = f'"{self._ffmpeg_bin}" -hide_banner -loglevel error -re'
        out = ("-f rtsp -rtsp_transport tcp -pkt_size 1400 "
               "rtsp://localhost:$RTSP_PORT/$MTX_PATH")
        if config.RTSP_VIDEO.exists():
            return f'{ff} -stream_loop -1 -i "{config.RTSP_VIDEO}" -c copy {out}'
        log.warning("vidéo %s absente — repli sur une mire de test",
                    config.RTSP_VIDEO)
        return (f"{ff} -f lavfi -i testsrc=size=800x480:rate={fps} "
                f"-c:v libx264 -tune zerolatency -pix_fmt yuv420p -g {fps * 2} "
                f"{out}")

    def _pump_logs(self, proc: subprocess.Popen) -> None:
        """Recopie la sortie de mediamtx dans notre journal, ligne à ligne.

        Tourne dans un thread démon : le tube doit être lu en continu, sinon
        mediamtx finirait par bloquer sur une écriture. L'horodatage de mediamtx
        est retiré (le nôtre le porte déjà) et son niveau est conservé.
        """
        if proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            level, msg = logging.INFO, line
            parts = line.split(" ", 3)
            # `date heure NIV reste` : on ne retient que le niveau et le reste.
            if len(parts) == 4 and parts[2] in _MEDIAMTX_LEVELS:
                level, msg = _MEDIAMTX_LEVELS[parts[2]], parts[3]
            log.log(level, "mediamtx | %s", msg)

    def _wait_port(self, port: int, timeout: float) -> bool:
        """Attend que mediamtx écoute (et n'ait pas déjà quitté)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._procs[0].poll() is not None:
                return False                       # mediamtx a quitté (port pris ?)
            with socket.socket() as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return True
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        for p in reversed(self._procs):
            try:
                p.terminate()
            except OSError:
                pass
        self._procs.clear()


def _serve_ffmpeg() -> FfmpegRtspServer | None:
    """Repli RTSP quand GStreamer manque ; None si les outils sont absents."""
    ffmpeg = shutil.which("ffmpeg")
    mediamtx = shutil.which("mediamtx")
    if not ffmpeg or not mediamtx:
        missing = ", ".join(n for n, p in (("ffmpeg", ffmpeg),
                                           ("mediamtx", mediamtx)) if not p)
        log.warning("RTSP désactivé : ni GStreamer, ni le repli (%s absent) — "
                    "`brew install gstreamer`, ou `brew install mediamtx ffmpeg`",
                    missing)
        return None
    srv = FfmpegRtspServer(mediamtx, ffmpeg)
    if not srv.start():
        return None
    log.info("RTSP (repli ffmpeg+mediamtx) à l'écoute sur rtsp://0.0.0.0:%d/%s",
             config.RTSP_PORT, config.RTSP_PATH)
    return srv


def serve() -> RtspServer | FfmpegRtspServer | None:
    """Démarre le serveur RTSP ; GStreamer si possible, sinon repli ffmpeg+mediamtx."""
    if AVAILABLE:
        try:
            srv = RtspServer()
            threading.Thread(target=srv.run, daemon=True).start()
            log.info("RTSP (GStreamer) à l'écoute sur rtsp://0.0.0.0:%d/%s",
                     config.RTSP_PORT, config.RTSP_PATH)
            return srv
        except Exception as exc:               # noqa: BLE001 — démarrage best-effort
            log.warning("RTSP GStreamer indisponible (%s) — essai du repli ffmpeg",
                        exc)
    else:
        log.info("GStreamer/gi absent (%s) — essai du repli ffmpeg+mediamtx",
                 _IMPORT_ERROR)
    return _serve_ffmpeg()
