"""authkeys.py — dépôt des clés publiques enrôlées par le service SSHAccess (8182).

Sur le MFD réel, le `PlatformServicesDaemon` reçoit la clé publique sur le port
8182 et l'écrit dans `/mnt/tmp/PlatformServicesDaemon/authorized_keys_external_apps`,
que le sshd lit ensuite (cf. « 5. protocole-messages-8182.md » §5) : l'enrôlement
8182 *est* ce qui ouvre l'accès SFTP du port 22. Ce module reproduit ce chaînon.

Le simulateur a deux sshd possibles, d'où un fichier au chemin configurable
(`MFD_AUTHORIZED_KEYS_FILE`) :

- le sshd Python (`sshd.py`), conteneur comme hors conteneur, qui s'abonne ici
  via `register()` pour accepter la clé **sans redémarrage** ;
- Raspberry Pi : l'OpenSSH système, sur `/etc/ssh/authorized_keys/media_rw`.

Dans ce dernier cas, rien à notifier : OpenSSH relit son `AuthorizedKeysFile` à
chaque authentification, l'écriture suffit.

RayConnect ré-enrôle à chaque connexion (≈ toutes les 7 s) : `add()` est
idempotente, le fichier ne grossit pas.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import threading
from pathlib import Path

from . import config

log = logging.getLogger("authkeys")

# Fichier où sont écrites les clés enrôlées, à l'image du
# `authorized_keys_external_apps` du MFD réel. Le sshd Python s'abonne via
# register() et n'en dépend pas ; sur la Pi il doit désigner le fichier que lit
# l'OpenSSH système (`/etc/ssh/authorized_keys/media_rw`). Défaut : sous l'état
# persistant du simulateur.
AUTHORIZED_KEYS_FILE = Path(os.environ.get(
    "MFD_AUTHORIZED_KEYS_FILE", str(config.STATE_DIR / "authorized_keys")))

# Une clé OpenSSH « <algo> <base64> », sans le commentaire. Filet de sécurité :
# msg8182.decode_request livre déjà la clé isolée (dans 1500007, clé et certificat
# sont chacun préfixés par leur longueur), mais cette regex retire un éventuel
# commentaire ou reste parasite, et sert aussi à relire les lignes du fichier de
# clés dans load().
KEY_RE = re.compile(
    r"(?:ssh-rsa|ssh-ed25519|ssh-dss|ecdsa-sha2-nistp(?:256|384|521))"
    r"\s+[A-Za-z0-9+/]+={0,3}")

_lock = threading.Lock()
_keys: set[str] = set()          # corps « algo base64 » déjà connus
_listeners: list = []            # callbacks (sshd Python) notifiés d'une nouvelle clé
_loaded = False


def extract(payload: bytes) -> str | None:
    """Isole la clé publique OpenSSH d'un payload 8182, ou None si absente.

    Le payload n'est pas garanti ASCII propre (rien ne le cadre côté MFD) : on
    décode en latin1 pour ne jamais lever, la regex fait le tri.
    """
    match = KEY_RE.search(payload.decode("latin1", "replace"))
    if match is None:
        return None
    return " ".join(match.group(0).split())     # « algo base64 », espace unique


def fingerprint(key: str) -> str:
    """Empreinte façon OpenSSH (`SHA256:…`), pour un journal lisible."""
    try:
        blob = base64.b64decode(key.split()[1], validate=True)
    except Exception:                            # noqa: BLE001
        return "SHA256:?"
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def load() -> list[str]:
    """Relit le fichier de clés (idempotent) et renvoie les clés connues.

    Appelée au démarrage des services : sans ça, une clé déjà présente dans le
    fichier serait ré-écrite au premier enrôlement.
    """
    global _loaded
    with _lock:
        if not _loaded:
            _loaded = True
            for source in (config.SSH_AUTHORIZED_KEY, _read_file()):
                for line in source.splitlines():
                    key = extract(line.encode("latin1", "replace"))
                    if key:
                        _keys.add(key)
            if _keys:
                log.info("%d clé(s) déjà autorisée(s) (%s)",
                         len(_keys), AUTHORIZED_KEYS_FILE)
        return sorted(_keys)


def register(callback) -> None:
    """Abonne un callback `f(key: str)` aux clés enrôlées après le démarrage."""
    with _lock:
        _listeners.append(callback)


def add(key: str, identity: str = "") -> bool:
    """Ajoute la clé au fichier autorisé et notifie les abonnés. False si échec.

    `identity` (l'email du compte RayConnect) devient le commentaire de la ligne,
    pour savoir qui a enrôlé quoi.
    """
    with _lock:
        load_needed = not _loaded
    if load_needed:
        load()

    with _lock:
        if key in _keys:
            log.info("clé %s déjà enrôlée — fichier inchangé", fingerprint(key))
            return True
        line = f"{key} {identity}".strip()
        path = AUTHORIZED_KEYS_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not path.exists()
            with path.open("a", encoding="latin1") as fh:
                fh.write(line + "\n")
            if fresh:
                # Fichier créé par nous : droits stricts, sinon OpenSSH le refuse.
                os.chmod(path, 0o600)
        except OSError as e:
            log.warning("clé %s non enrôlée : écriture de %s impossible (%s)",
                        fingerprint(key), path, e)
            return False
        _keys.add(key)
        listeners = list(_listeners)
        log.info("clé %s enrôlée pour %s → %s",
                 fingerprint(key), identity or "(sans identité)", path)

    for callback in listeners:
        try:
            callback(key)
        except Exception as e:                   # noqa: BLE001
            log.warning("abonné %r en erreur : %s", callback, e)
    return True


def _read_file() -> str:
    try:
        return AUTHORIZED_KEYS_FILE.read_text(encoding="latin1")
    except OSError:
        return ""
