"""
mfdsim — simulateur réseau d'un MFD Raymarine (gamme AXIOM / LightHouse).

Rejoue, côté serveur, les protocoles rétro-conçus dans ce dépôt :

    mdns.py        annonce Bonjour des services (_raydb / _rym_rrc / _rtsp)
    disco5800.py   beacon de découverte propriétaire (multicast UDP 5800)
    raydb.py       bus publish/subscribe clé→valeur (TCP 23333)
    rrce.py        canal de télécommande tactile (TCP 50000)
    rtsp.py        recopie d'écran RTSP/H.264 via GStreamer (TCP 8554)
    sim.py         bateau virtuel alimentant les chemins `data/…` — en
                   navigation (`BoatSim`) ou au mouillage (`AnchorSim`)

Un module ne rejoue rien du MFD et n'existe que pour le simulateur :

    control.py     API REST de pilotage des scénarios (TCP 8088)

Le SSH/SFTP (TCP 22) est assuré par un vrai `sshd` dans le conteneur, pas par ce
paquet.

But : disposer d'une cible pour développer et tester les clients du dépôt
(`raydb_client.py`, `mfd_discover.py`, `rrce_touch.py`…)
sans le MFD physique ni le bateau.
"""

__version__ = "1.0.0"
