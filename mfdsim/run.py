# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "zeroconf>=0.130",             # mDNS / Bonjour (mdns.py)
#     "paramiko>=3.0",               # sshd Python hors conteneur (sshd.py)
#     "cryptography>=3.3",           # importé par sshd.py (ssh-rsa/SHA-1) ; dep de paramiko
#     # RTSP (recopie d'écran) : PyGObject fournit `gi`, MAIS les namespaces
#     # Gst/GstRtspServer proviennent de typelibs SYSTÈME (gstreamer1.0-plugins-*,
#     # gir1.2-gst-rtsp-server-1.0), absents de PyPI. GStreamer ne peut donc pas
#     # être provisionné par PEP 723 : sous `uv run`, le venv isolé ne voit pas non
#     # plus le python3-gi système, donc RTSP est indisponible (= --no-rtsp).
#     # Pour la vidéo, utiliser le conteneur Docker (GStreamer y est installé).
#     # "PyGObject>=3.42",
# ]
# ///
"""
run.py — lanceur PEP 723 du simulateur : `uv run run.py [options]`.

Le simulateur est un *paquet* (`python -m mfdsim`), or les métadonnées PEP 723
ne sont lues que pour un script mono-fichier : ce lanceur porte donc l'entête et
délègue au paquet. `uv run run.py` provisionne un venv éphémère avec zeroconf et
paramiko, puis exécute le simulateur — sans installation manuelle.

Le paquet local `mfdsim` est importé depuis ce répertoire (placé en tête de
`sys.path`). GStreamer/PyGObject (RTSP) reste une dépendance *système*, hors
PyPI : sans elle, `--no-rtsp` (ou la désactivation automatique) s'applique.

Équivalences :
    uv run run.py                     # = python -m mfdsim
    uv run run.py --no-rtsp -v        # options transmises telles quelles
"""
import sys

from mfdsim.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
