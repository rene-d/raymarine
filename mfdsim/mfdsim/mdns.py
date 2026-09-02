"""
mdns.py — annonce Bonjour/mDNS des services du MFD (multicast 224.0.0.251:5353).

Publie les trois types que `mfd_discover.py` cherche, avec les TXT relevés sur
le MFD réel :

    _raydb._tcp     bus clé/valeur RayDB
    _rym_rrc._tcp   télécommande tactile
    _rtsp._tcp      recopie d'écran

Le port annoncé pour `_raydb._tcp` est **49111**, alors que le trafic RayDB
passe en réalité par **23333** — divergence constatée sur le MFD réel et
reproduite telle quelle : un client qui fait confiance au port mDNS échouera
ici exactement comme sur le vrai appareil. `MFD_RAYDB_MDNS_PORT=23333` permet
d'annoncer le port utile si l'on veut tester le chemin « nominal ».
"""

from __future__ import annotations

import logging
import os
import socket

from zeroconf import ServiceInfo, Zeroconf

from . import config

log = logging.getLogger("mdns")


def _services(ip: str, raydb: bool, ray_remote: bool) -> list[ServiceInfo]:
    """Construit la liste des services à annoncer."""

    addr = socket.inet_aton(ip)
    server = f"{config.HOSTNAME}.local."
    # La version apparaît avec des underscores dans le nom d'instance RayDB.
    fw_tag = config.FIRMWARE.replace(".", "_")
    raydb_port = int(os.environ.get("MFD_RAYDB_MDNS_PORT", config.RAYDB_MDNS_PORT))

    services = []

    if raydb:
        services.append(
            ServiceInfo(
                "_raydb._tcp.local.",
                f"RayDBServer on {config.DEVICE_ID} {fw_tag}._raydb._tcp.local.",
                addresses=[addr],
                port=raydb_port,
                server=server,
                properties={
                    "id": config.SERIAL,
                    "name": config.DEVICE_ID,
                    "rank": "1",
                    "group": "MFD",
                },
            )
        )

    if ray_remote:
        services.extend([
            ServiceInfo(
                "_rym_rrc._tcp.local.",
                f"{config.DEVICE_ID}._rym_rrc._tcp.local.",
                addresses=[addr],
                port=config.RRCE_PORT,
                server=server,
                properties={"raymarine-mfd-rrc-version": config.RRC_VERSION},
            ),
            ServiceInfo(
                "_rtsp._tcp.local.",
                f"{config.DEVICE_ID}._rtsp._tcp.local.",
                addresses=[addr],
                port=config.RTSP_PORT,
                server=server,
                properties={
                    "raymarine-mfd-model": config.PRODUCT,
                    "raymarine-mfd-serial": config.DEVICE_ID,
                    "raymarine-mfd-rtsp-path": config.RTSP_PATH,
                },
            ),
        ])

    return services


class Advertiser:
    """Publie les services et les retire proprement à l'arrêt.

    Le retrait explicite envoie les paquets « goodbye » : sans lui, les clients
    gardent le MFD en cache plusieurs minutes après l'arrêt du simulateur.

    Rien n'est republié ensuite, et c'est conforme : zeroconf annonce à
    l'enregistrement puis **répond aux requêtes**, ce qui suffit. Les clients
    rafraîchissent d'eux-mêmes avant l'expiration (TTL de 120 s sur SRV/A,
    4500 s sur PTR) ; une réannonce périodique ne serait que du bruit.
    """

    def __init__(self, ip: str, raydb: bool, ray_remote: bool,
                 force: bool = False) -> None:
        self.zc = Zeroconf()
        self.infos = _services(ip, raydb, ray_remote)
        for info in self.infos:
            # strict=False : le type `_rym_rrc._tcp` porte un underscore à
            # l'intérieur du nom, ce que zeroconf refuse en mode strict alors
            # que le MFD réel l'annonce bien ainsi. On reproduit l'appareil,
            # pas le RFC.
            #
            # `force` (cooperating_responders) saute le contrôle d'unicité, qui
            # refuse un nom déjà annoncé sur le réseau — en pratique, un autre
            # simulateur, le nom portant l'identifiant et la version du modèle.
            # Le nom est alors conservé tel quel, là où l'autre échappatoire de
            # la bibliothèque (allow_name_change) le renommerait en « …-2 » et
            # cesserait donc d'imiter l'appareil.
            self.zc.register_service(info, strict=False,
                                     cooperating_responders=force)
            log.info("annoncé : %s sur %s:%d", info.type, ip, info.port)

    def close(self) -> None:
        for info in self.infos:
            try:
                self.zc.unregister_service(info)
            except Exception as e:  # noqa: BLE001 — arrêt best-effort
                log.debug("retrait de %s impossible : %s", info.name, e)
        self.zc.close()
