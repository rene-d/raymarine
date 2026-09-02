#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["zeroconf>=0.130"]
# ///
"""
mfd_discover.py — découverte des services Raymarine annoncés en mDNS/Bonjour.

Un MFD Raymarine publie ses services sur le réseau local via mDNS (multicast
224.0.0.251:5353). Trois types nous intéressent :

    _raydb._tcp     bus clé/valeur RayDB       (cf. raydb_client.py)
    _rym_rrc._tcp   télécommande tactile RRCE  (cf. rrce_touch.py)
    _rtsp._tcp      flux vidéo de l'écran      (cf. mfd_remote.py)

Les enregistrements TXT donnent modèle, numéro de série et version sans avoir à
interroger le protocole de découverte UDP 5800 :

    raymarine-mfd-model=AXIOM 7
    raymarine-mfd-serial=E70363 1234567
    raymarine-mfd-rtsp-path=RAYMARINEMFD
    raymarine-mfd-rrc-version=1.10

Équivalent Bonjour en ligne de commande (macOS, sans dépendance) :
    dns-sd -B _raydb._tcp                     # lister les instances
    dns-sd -L "RayDBServer on …" _raydb._tcp  # résoudre hôte, port et TXT

Le script porte ses dépendances (PEP 723) : ./mfd_discover.py suffit, uv crée
l'environnement à la volée. Sans uv : pip install zeroconf, puis python3 mfd_discover.py

Usage :
    ./mfd_discover.py                    # les 3 services Raymarine, 5 s d'écoute
    ./mfd_discover.py --timeout 10       # écoute plus longue (réseau lent)
    ./mfd_discover.py --all              # tous les types mDNS du réseau
    ./mfd_discover.py --service _http._tcp.local.   # un type supplémentaire
    ./mfd_discover.py --json             # sortie JSON sur stdout
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping
from typing import Any

try:
    from zeroconf import (
        ServiceBrowser,
        ServiceStateChange,
        Zeroconf,
        ZeroconfServiceTypes,
    )
except ImportError:
    sys.exit("module zeroconf absent — lancer ./mfd_discover.py (uv), "
             "ou installer avec : pip install zeroconf")

# type mDNS -> libellé lisible
RAYMARINE_SERVICES: dict[str, str] = {
    "_raydb._tcp.local.": "RayDB",
    "_rym_rrc._tcp.local.": "Télécommande RRCE",
    "_rtsp._tcp.local.": "Vidéo RTSP",
}

# un service résolu, tel qu'exporté en JSON
Service = dict[str, Any]


def browse(types: list[str], timeout: float) -> list[Service]:
    """Écoute les annonces pendant `timeout` secondes, puis résout chaque instance.

    La résolution est faite après le parcours et non dans le callback : appeler
    get_service_info() depuis le thread de zeroconf le bloquerait.
    """
    found: list[tuple[str, str]] = []          # (type, nom d'instance)

    def on_change(zeroconf: Zeroconf, service_type: str, name: str,
                  state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Added and (service_type, name) not in found:
            found.append((service_type, name))

    zc = Zeroconf()
    services: list[Service] = []
    try:
        browser = ServiceBrowser(zc, types, handlers=[on_change])
        time.sleep(timeout)
        browser.cancel()
        for stype, name in found:
            info = zc.get_service_info(stype, name, timeout=int(timeout * 1000))
            if info is None:                   # instance annoncée puis muette
                continue
            services.append({
                "service": stype.removesuffix(".local."),
                "label": RAYMARINE_SERVICES.get(stype, ""),
                "name": name.removesuffix("." + stype),
                "host": (info.server or "").rstrip("."),
                "addresses": info.parsed_addresses(),
                "port": info.port,
                "txt": _decode_txt(info.properties),
            })
    finally:
        zc.close()
    return sorted(services, key=lambda s: (s["service"], s["name"]))


def _decode_txt(props: Mapping[Any, Any]) -> dict[str, str]:
    """Convertit les propriétés TXT en dict de chaînes lisibles.

    Le type exact varie selon la version de zeroconf (clés/valeurs en bytes ou
    déjà décodées) : on normalise à l'exécution plutôt que de s'y river.
    """
    txt: dict[str, str] = {}
    for k, v in props.items():
        key = k.decode("latin1", "replace") if isinstance(k, bytes) else str(k)
        if v is None:
            txt[key] = ""
        elif isinstance(v, bytes):
            txt[key] = v.decode("latin1", "replace")
        else:
            txt[key] = str(v)
    return txt


def print_table(services: list[Service]) -> None:
    for s in services:
        label = f"  [{s['label']}]" if s["label"] else ""
        addrs = ", ".join(s["addresses"]) or "(pas d'adresse)"
        print(f"\n{s['service']}{label}")
        print(f"  instance : {s['name']}")
        print(f"  adresse  : {addrs}:{s['port']}   ({s['host']})")
        for k, v in sorted(s["txt"].items()):
            print(f"  {k:24} = {v}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="durée d'écoute en secondes (défaut 5)")
    ap.add_argument("--service", action="append", metavar="TYPE", default=[],
                    help="type mDNS supplémentaire (ex. _http._tcp.local.)")
    ap.add_argument("--all", action="store_true",
                    help="parcourir tous les types annoncés sur le réseau")
    ap.add_argument("--json", action="store_true", help="sortie JSON sur stdout")
    args = ap.parse_args()

    types = list(RAYMARINE_SERVICES) + args.service
    if args.all:
        types = sorted(ZeroconfServiceTypes.find(timeout=args.timeout))

    if not args.json:
        print(f"# écoute mDNS {args.timeout:g} s sur {len(types)} type(s)…",
              file=sys.stderr)

    services = browse(types, args.timeout)

    if args.json:
        json.dump(services, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif services:
        print_table(services)
    else:
        print("aucun service trouvé — MFD éteint, hors du réseau, ou mDNS filtré "
              "(essayer --timeout 15, ou --all pour voir ce qui est annoncé).",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
