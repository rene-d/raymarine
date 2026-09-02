"""
__main__.py — lance le MFD simulé : mDNS + beacon 5800 + RayDB + RRCE + RTSP +
messages 8182 + SSH/SFTP.

Le SSH/SFTP est servi par `sshd.py`, un sshd en pur Python (paramiko), aussi
bien dans le conteneur qu'hors conteneur — plus de sshd système à provisionner.

Usage :
    python -m mfdsim                    # tous les services
    python -m mfdsim --position 48.32,-4.80 --heading 310 --allure travers
    python -m mfdsim --anchor           # démarrer au mouillage plutôt qu'en route
    python -m mfdsim --anchor 48.65,-3.88 --swing 25,35   # ancre et évitage choisis
    python -m mfdsim --no-mdns          # sans annonce Bonjour
    python -m mfdsim --mdns-force       # annoncer malgré un nom déjà pris
    python -m mfdsim --no-rtsp          # sans recopie d'écran (pas de GStreamer)
    python -m mfdsim --no-ssh           # sans sshd Python (port 22 non servi)
    python -m mfdsim --ip 192.168.42.1  # IP annoncée imposée
    python -m mfdsim -v                 # journalise chaque toucher / trame
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import sys
import threading

from . import config, control, disco5800, mdns, msg8182, raydb, rrce, rtsp, sshd
from .sim import (
    DEFAULT_ANCHOR_LAT,
    DEFAULT_ANCHOR_LON,
    DEFAULT_HEADING,
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_POINT_OF_SAIL,
    DEFAULT_SPEED,
    DEFAULT_SWING_MAX,
    DEFAULT_SWING_MIN,
    POINTS_OF_SAIL,
    BoatSim,
    Simulation,
)

log = logging.getLogger("mfdsim")


def local_ip() -> str:
    """IP de l'interface qui sort vers le réseau, sans dépendre du DNS.

    Le socket UDP n'émet rien : `connect()` sert seulement à demander au noyau
    quelle interface il choisirait, ce qui donne l'adresse à annoncer.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, jamais routé
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def parse_pair(text: str, default_a: float, default_b: float,
               option: str) -> tuple[float, float]:
    """Lit un couple « a,b » d'option ; vide = les valeurs par défaut."""
    if not text:
        return default_a, default_b
    try:
        a, b = (float(part) for part in text.split(","))
    except ValueError:
        raise SystemExit(f"{option} attend deux nombres séparés par une virgule, "
                         f"reçu : {text!r}") from None
    return a, b


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", default=config.ADVERTISED_IP, help="IP annoncée (défaut : auto-détection)")
    ap.add_argument("--position", default="", metavar="LAT,LON",
                    help="position initiale du bateau (défaut : "
                         f"{DEFAULT_LATITUDE:g},{DEFAULT_LONGITUDE:g}, en mer "
                         "d'Iroise) ; sous --anchor, c'est l'ancre qui place le "
                         "bateau jusqu'à ce qu'il appareille")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED, metavar="S",
                    help=f"vitesse surface de base, en nœuds (défaut : {DEFAULT_SPEED})")
    ap.add_argument("--heading", type=float, default=DEFAULT_HEADING, metavar="CAP",
                    help=f"cap vrai initial, en degrés (défaut : {DEFAULT_HEADING:g})")
    ap.add_argument("--allure", default=DEFAULT_POINT_OF_SAIL, metavar="ALLURE",
                    help=f"allure, d'où le vent réel à l'étrave : {', '.join(POINTS_OF_SAIL)}, "
                         f"ou un angle en degrés, négatif à bâbord amure "
                         f"(défaut : {DEFAULT_POINT_OF_SAIL})")
    ap.add_argument("--anchor", nargs="?", const="", metavar="LAT,LON",
                    help="démarrer au mouillage plutôt qu'en navigation ; position "
                         f"de l'ancre optionnelle (défaut : {DEFAULT_ANCHOR_LAT:.6f},"
                         f"{DEFAULT_ANCHOR_LON:.6f}). Le mode se change ensuite à "
                         "chaud : POST /anchor, POST /underway")
    ap.add_argument("--swing", default=f"{DEFAULT_SWING_MIN:g},{DEFAULT_SWING_MAX:g}",
                    metavar="MIN,MAX",
                    help="rayon d'évitage en mètres (défaut : "
                         f"{DEFAULT_SWING_MIN:g},{DEFAULT_SWING_MAX:g})")
    ap.add_argument("--no-control", action="store_true",
                    help=f"pas d'API REST de pilotage (port {config.CONTROL_PORT})")
    ap.add_argument("--no-mdns", action="store_true", help="pas d'annonce mDNS/Bonjour")
    ap.add_argument("--mdns-force", action="store_true",
                    help="annoncer même si le nom est déjà pris sur le réseau "
                         "(autre simulateur en marche)")
    ap.add_argument("--no-5800", action="store_true", help="pas de beacon multicast 5800")
    ap.add_argument("--no-rtsp", action="store_true", help="pas de recopie d'écran RTSP+télécommande")
    ap.add_argument("--no-8182", action="store_true", help="pas d'écoute du protocole de messages 8182")
    ap.add_argument("--no-ssh", action="store_true", help="pas de sshd Python (le port 22 n'est alors pas servi)")
    ap.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)-9s %(message)s",
        datefmt="%H:%M:%S",
    )

    ip = args.ip or local_ip()
    log.info("MFD simulé « %s » (%s) sur %s", config.DEVICE_ID, config.PRODUCT, ip)

    # Le bateau de navigation est toujours construit, même sous --anchor : le
    # mouillage n'est plus qu'un mode dont on entre et sort à chaud (POST
    # /anchor, /underway), et appareiller doit retrouver ces consignes-là.
    lat, lon = parse_pair(args.position, DEFAULT_LATITUDE, DEFAULT_LONGITUDE,
                          "--position")
    swing = parse_pair(args.swing, DEFAULT_SWING_MIN, DEFAULT_SWING_MAX, "--swing")
    try:
        boat = BoatSim(lat=lat, lon=lon, speed=args.speed,
                       heading=args.heading, allure=args.allure)
    except ValueError as e:
        raise SystemExit(f"--allure : {e}") from None
    sim = Simulation(boat, swing=swing)

    if args.anchor is not None:
        lat, lon = parse_pair(args.anchor, DEFAULT_ANCHOR_LAT, DEFAULT_ANCHOR_LON,
                              "--anchor")
        sim.anchor(lat, lon)
        log.info("au mouillage sur %.6f,%.6f — évitage %g–%g m",
                 lat, lon, *swing)
    else:
        log.info("en route depuis %.6f,%.6f — cap %g°, %g nd, allure %s",
                 lat, lon, args.heading, args.speed, boat.allure or args.allure)

    raydb.serve(sim, ip)

    if not args.no_control:
        control.serve(sim)

    if not args.no_8182:
        msg8182.serve()

    if not args.no_ssh:
        sshd.serve()

    if not args.no_rtsp:
        rrce.serve()
        rtsp.serve()

    if not args.no_5800:
        disco5800.serve(ip)

    if not args.no_mdns:
        try:
            advertiser = mdns.Advertiser(ip, True, ray_remote=not args.no_rtsp,
                                         force=args.mdns_force)
        except Exception as e:  # noqa: BLE001
            # Le mDNS est le service le plus fragile (port 5353 déjà pris par le
            # mDNSResponder de l'hôte, type refusé par la lib…). Il ne doit
            # jamais emporter RayDB et RRCE avec lui : on dégrade, on continue.
            #
            # Le nom de la classe, faute de message : NonUniqueNameException —
            # le cas courant, un autre simulateur annonçant déjà ce nom — n'en
            # porte aucun, et l'avertissement se lisait « impossible () ».
            log.warning("annonce mDNS impossible (%s) — le reste du MFD "
                        "fonctionne ; --mdns-force pour passer outre",
                        str(e) or type(e).__name__)
            advertiser = None
    else:
        advertiser = None

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        log.info("arrêt")
        if advertiser:
            advertiser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
