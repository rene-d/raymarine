"""
config.py — identité du MFD simulé.

Les valeurs par défaut reproduisent un AXIOM 7 « E70363 1234567 ». Tout est surchargeable
par variable d'environnement, pour simuler un autre appareil sans toucher au code.

Le nom d'hôte mDNS, les TXT Bonjour et le nom des chemins `diag/mfd/<modèle> <série>/…`
dérivent tous de MODEL/SERIAL : changer ces deux variables suffit à obtenir un MFD
cohérent de bout en bout.
"""
from __future__ import annotations

import os
from pathlib import Path

# État persistant hors conteneur (clé d'hôte SSH, arbre SFTP de repli…).
STATE_DIR = Path(os.environ.get(
    "MFD_STATE_DIR", str(Path.home() / ".mfdsim")))

# ------------------------------------------------------------- identité MFD --
MODEL = os.environ.get("MFD_MODEL", "E70363")
SERIAL = os.environ.get("MFD_SERIAL", "1234567")
PRODUCT = os.environ.get("MFD_PRODUCT", "AXIOM 7")
# Version LightHouse annoncée : apparaît dans l'instance RayDB et les diag/.
FIRMWARE = os.environ.get("MFD_FIRMWARE", "4.11.13")

# « E70363 1234567 » : la forme avec espace, utilisée dans les chemins RayDB et
# le TXT raymarine-mfd-serial. La forme avec tiret sert au nom d'hôte mDNS.
DEVICE_ID = f"{MODEL} {SERIAL}"
HOSTNAME = f"{MODEL}-{SERIAL}"

# Nom du bateau, publié sous Settings/Data/-/7/13/-/-/-/- (cf. « 2. protocole-raydb-23333.md »).
BOAT_NAME = os.environ.get("MFD_BOAT_NAME", "MYBOAT")

# ------------------------------------------------------------------ réseau ---
# IP annoncée en mDNS et dans les diag/. Vide = auto-détection de l'IP de
# l'interface principale du conteneur (le cas normal en --network host).
ADVERTISED_IP = os.environ.get("MFD_IP", "")

# Adresse interne du backbone SeaTalkHS, telle qu'annoncée dans les trames 5800.
# Non joignable — c'est justement le piège que le protocole 5800 tend aux clients
# naïfs, et le simulateur doit le reproduire (cf. « 1. protocole-udp5800.md »).
BACKBONE_IP = os.environ.get("MFD_BACKBONE_IP", "198.18.0.233")

RAYDB_PORT = 23333        # bus clé/valeur (port réellement porteur de trafic)
RAYDB_MDNS_PORT = 49111   # port annoncé en mDNS par le vrai MFD — divergence voulue
RRCE_PORT = 50000         # canal tactile
RTSP_PORT = 8554          # flux vidéo (recopie d'écran), servi par rtsp.py
MSG8182_PORT = 8182       # protocole de messages (enrôlement clé SSH…) — écoute seule
# API REST de pilotage du simulateur (control.py) : hors protocoles Raymarine,
# le MFD réel n'expose rien de tel.
CONTROL_PORT = int(os.environ.get("MFD_CONTROL_PORT", "8088"))
SSH_PORT = int(os.environ.get("MFD_SSH_PORT", "22"))   # SSH/SFTP (sshd Python)

# ------------------------------------------------------------- SSH / SFTP ----
# Utilisés par le sshd Python (sshd.py), qui sert le port 22 dans le conteneur
# comme hors conteneur.
SSH_USER = os.environ.get("MFD_SSH_USER", "media_rw")
SSH_PASSWORD = os.environ.get("MFD_SSH_PASSWORD", "media_rw")
# Racine (chroot) du SFTP, créée et peuplée (arbre UserData) au démarrage. Défaut
# hors conteneur : le dossier `sftp/` du projet ; dans le conteneur, le Dockerfile
# pose MFD_SFTP_ROOT=/data/media/0/UserData (le chemin du vrai MFD).
SFTP_ROOT = os.environ.get(
    "MFD_SFTP_ROOT",
    str(Path(__file__).resolve().parent.parent / "sftp"))
# Clé d'hôte RSA du sshd Python, persistée pour une empreinte stable.
SSH_HOST_KEY = Path(os.environ.get(
    "MFD_SSH_HOST_KEY", str(STATE_DIR / "ssh_host_rsa_key")))
# Clé publique autorisée (comme MFD_AUTHORIZED_KEY côté conteneur). Vide = seul
# le mot de passe fonctionne.
SSH_AUTHORIZED_KEY = os.environ.get("MFD_AUTHORIZED_KEY", "")

# Vidéo servie en RTSP : l'écran réel reconstruit par video_extract.py depuis une
# capture (video/mfd_screen.h264). Absent, rtsp.py retombe sur une mire de test.
RTSP_VIDEO = Path(os.environ.get(
    "MFD_RTSP_VIDEO",
    str(Path(__file__).resolve().parent.parent / "video" / "mfd_screen.h264")))
RTSP_FPS = int(os.environ.get("MFD_RTSP_FPS", "20"))   # cadence du MFD (SDP d'origine)
# Verbosité du repli mediamtx, dont la sortie est recopiée dans notre journal
# (voir rtsp.py). `info` trace la vie des sessions — création, lecture, publieur
# à la demande, destruction — c'est ce qu'il faut pour voir pourquoi un client
# n'obtient pas son flux. `debug` ajoute chaque requête RTSP, utile pour
# chronométrer un SETUP lent.
RTSP_LOG_LEVEL = os.environ.get("MFD_RTSP_LOG", "info")

DISCOVERY_GROUP = "224.0.0.1"
DISCOVERY_PORT = 5800

# Cadence du beacon 5800, mesurée sur 21 255 trames : ~0,72 s par équipement.
BEACON_PERIOD = float(os.environ.get("MFD_BEACON_PERIOD", "0.72"))

# Cadence de rafraîchissement de la simulation de navigation (Hz).
SIM_HZ = float(os.environ.get("MFD_SIM_HZ", "4"))

RTSP_PATH = "RAYMARINEMFD"
RRC_VERSION = "1.10"
