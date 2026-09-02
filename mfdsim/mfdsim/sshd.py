"""
sshd.py — serveur SSH/SFTP en Python (paramiko), le seul du simulateur.

Sert le port 22 aussi bien dans le conteneur qu'hors conteneur (`python -m
mfdsim` sur la machine nue) : plus de `sshd` système à provisionner, une seule
implémentation pour une émulation complète.

- utilisateur `media_rw`, authentification par **mot de passe ou clé publique**
  (écart de commodité assumé de banc d'essai ; le MFD réel est clé seule) ;
- subsystem **SFTP** raciné sur l'arbre `UserData` que RayConnect rapatrie
  (`Tracks/`, `Routes/`, `Waypoints/`, `Screenshots/`, `Logs/`).

paramiko est une dépendance optionnelle : absente, le module se signale et
s'efface, exactement comme le RTSP sans GStreamer — les autres services tournent.
Le port 22 étant privilégié, l'écoute peut échouer sans les droits (ou si un
sshd occupe déjà le port) : on dégrade alors proprement avec un message clair.
"""
from __future__ import annotations

import logging
import os
import socket
import threading

from . import authkeys, config

log = logging.getLogger("sshd")

# Import paresseux et tolérant : paramiko n'est pas toujours installé.
try:
    import paramiko

    _IMPORT_ERROR: Exception | None = None
except ImportError as _exc:                      # pragma: no cover
    paramiko = None                              # type: ignore[assignment]
    _IMPORT_ERROR = _exc


# ----------------------------------------------- ssh-rsa (RSA/SHA-1) ---------
def _enable_legacy_rsa_sha1() -> None:
    """Réactive `ssh-rsa` (RSA/SHA-1) côté serveur, retiré de paramiko récent.

    Le MFD réel tourne un OpenSSH ~6.8 antérieur à rsa-sha2 : il n'accepte que
    des signatures **ssh-rsa (SHA-1)**, et c'est ce que pousse `rm_ssh.py` avec
    la clé RSA-2048 de RayConnect (cf. « 4. ssh-mfd-analyse.md »). paramiko
    moderne a supprimé ce type (négociation *et* vérification) ; sans lui, un
    client calé sur l'appareil réel se voit refuser « pubkey algorithm 'ssh-rsa'
    unsupported or disabled ». On le réinjecte pour rester fidèle au MFD.

    On n'AJOUTE que ssh-rsa : rsa-sha2-256/512 restent préférés (annoncés en
    tête via server-sig-algs), donc un client moderne continue d'utiliser SHA-2.
    Best-effort : si les internes paramiko diffèrent, on renonce sans planter —
    les clients rsa-sha2 fonctionnent de toute façon.
    """
    try:
        from cryptography.hazmat.primitives import hashes

        paramiko.RSAKey.HASHES.setdefault("ssh-rsa", hashes.SHA1)
        paramiko.Transport._key_info.setdefault("ssh-rsa", paramiko.RSAKey)
        if "ssh-rsa" not in paramiko.Transport._preferred_pubkeys:
            # En queue : SHA-2 reste préféré, SHA-1 en repli comme sur le MFD.
            paramiko.Transport._preferred_pubkeys = (
                paramiko.Transport._preferred_pubkeys + ("ssh-rsa",))
    except Exception as e:                       # noqa: BLE001
        log.warning("ssh-rsa (SHA-1) non réactivé (%s) — clients SHA-1 refusés", e)


# --------------------------------------------------------- arbre SFTP --------
# Arbre reproduit à l'identique par les trois portages (conteneur, ce sshd
# Python, Pi 4) : dossiers utilisateur + points de montage `mnt/` (cartes SD des
# deux lecteurs, clé USB), que le MFD expose même sans support inséré.
_USERDATA_DIRS = (
    "Tracks", "Routes", "Waypoints", "Screenshots", "Logs",
    "mnt/internal_slot1", "mnt/internal_slot2",
    "mnt/external0_slot1", "mnt/external0_slot2", "mnt/usb_media0",
)


def _ensure_userdata(root: str) -> str:
    """Crée l'arbre UserData sous `root` s'il manque ; repli sur STATE_DIR si interdit.

    Par défaut `root` est le dossier `sftp/` du projet (chroot du serveur de
    test) ; s'il n'est pas inscriptible (dépôt en lecture seule, chemin imposé
    inaccessible), on retombe sur `STATE_DIR/UserData` dans le home.
    """
    for candidate in (root, str(config.STATE_DIR / "UserData")):
        try:
            for sub in _USERDATA_DIRS:
                os.makedirs(os.path.join(candidate, sub), exist_ok=True)
            readme = os.path.join(candidate, "README.txt")
            if not os.path.exists(readme):
                with open(readme, "w", encoding="utf-8") as fh:
                    fh.write(f"MFD {config.DEVICE_ID} — données utilisateur simulées\n")
            if candidate != root:
                log.warning("racine SFTP « %s » inaccessible, repli sur « %s »",
                            root, candidate)
            return candidate
        except OSError as e:
            log.debug("racine SFTP « %s » impossible : %s", candidate, e)
    # Dernier recours : le répertoire courant, pour ne jamais planter le service.
    log.warning("aucune racine SFTP inscriptible — utilisation de « . »")
    return os.getcwd()


# --------------------------------------------------- clé d'hôte persistée ----
def _host_key():
    """Charge la clé d'hôte RSA, ou la génère et la persiste au premier appel."""
    path = config.SSH_HOST_KEY
    if path.exists():
        return paramiko.RSAKey(filename=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info("génération de la clé d'hôte RSA (%s)", path)
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(str(path))
    return key


def _parse_key(raw: str):
    """Convertit une ligne OpenSSH en clé paramiko. None si illisible."""
    import base64

    key_classes = {
        "ssh-rsa": paramiko.RSAKey,
        "ssh-ed25519": paramiko.Ed25519Key,
        "ecdsa-sha2-nistp256": paramiko.ECDSAKey,
        "ecdsa-sha2-nistp384": paramiko.ECDSAKey,
        "ecdsa-sha2-nistp521": paramiko.ECDSAKey,
    }
    try:
        parts = raw.split()
        # « <type> AAAA… [commentaire] » ; défaut ssh-rsa si le type manque.
        if len(parts) >= 2 and parts[0] in key_classes:
            algo, b64 = parts[0], parts[1]
        else:
            algo, b64 = "ssh-rsa", parts[0]
        return key_classes[algo](data=base64.b64decode(b64))
    except Exception as e:                  # noqa: BLE001
        log.warning("clé publique illisible, ignorée : %s", e)
        return None


def _authorized_keys() -> list:
    """Clés publiques acceptées : MFD_AUTHORIZED_KEY + celles enrôlées via 8182.

    Les clés enrôlées sont relues du fichier `authkeys.AUTHORIZED_KEYS_FILE` :
    celles reçues lors d'un précédent lancement valent toujours, comme le
    `authorized_keys` d'un vrai sshd.
    """
    keys = []
    raw = config.SSH_AUTHORIZED_KEY.strip()
    if raw:
        key = _parse_key(raw)
        if key is not None:
            keys.append(key)
    for line in authkeys.load():
        key = _parse_key(line)
        if key is not None and key not in keys:
            keys.append(key)
    return keys


# ------------------------------------------------- interface serveur SSH -----
class _Server(paramiko.ServerInterface):
    """Politique d'authentification et d'ouverture de canal."""

    def __init__(self, authorized: list, peer: str = "") -> None:
        self._authorized = authorized
        self.peer = peer

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username: str) -> str:
        return "password,publickey"

    def check_auth_password(self, username: str, password: str) -> int:
        if username == config.SSH_USER and password == config.SSH_PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key) -> int:
        if username == config.SSH_USER and any(key == k for k in self._authorized):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED


# -------------------------------------------------- interface SFTP rootée ----
def _make_sftp_interface():
    """Construit la classe SFTPServerInterface racinée sur `_SFTP_ROOT`.

    paramiko instancie l'interface sans nos arguments ; on referme donc la racine
    par variable de module (une seule racine par processus, ce qui suffit ici).
    """

    root = _SFTP_ROOT

    class _SFTPHandle(paramiko.SFTPHandle):
        def stat(self):
            try:
                return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def chattr(self, attr):
            return paramiko.SFTP_OK

    class _SFTPInterface(paramiko.SFTPServerInterface):
        def __init__(self, server, *args, **kwargs):
            # La base paramiko ne conserve pas `server` : on le garde pour le peer.
            super().__init__(server, *args, **kwargs)
            self.server = server

        def _real(self, path: str) -> str:
            # Empêche toute évasion hors de la racine (`..`, chemins absolus).
            path = os.path.normpath("/" + path).lstrip("/")
            return os.path.join(root, path)

        def _log(self, command: str) -> None:
            """Journalise la commande SFTP telle que vue du client."""
            peer = getattr(self.server, "peer", "")
            log.info("%s SFTP %s", peer, command)

        def list_folder(self, path):
            self._log(f"ls {path}")
            real = self._real(path)
            try:
                out = []
                for name in os.listdir(real):
                    attr = paramiko.SFTPAttributes.from_stat(
                        os.stat(os.path.join(real, name)))
                    attr.filename = name
                    out.append(attr)
                return out
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def lstat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def open(self, path, flags, attr):
            writing = flags & (os.O_WRONLY | os.O_RDWR)
            self._log(f"{'put' if writing else 'get'} {path}")
            real = self._real(path)
            try:
                binary = os.open(real, flags, getattr(attr, "st_mode", None) or 0o644)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            if flags & os.O_WRONLY:
                mode = "ab" if flags & os.O_APPEND else "wb"
            elif flags & os.O_RDWR:
                mode = "a+b" if flags & os.O_APPEND else "r+b"
            else:
                mode = "rb"
            try:
                fobj = os.fdopen(binary, mode)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            handle = _SFTPHandle(flags)
            handle.filename = real
            handle.readfile = fobj
            handle.writefile = fobj
            return handle

        def remove(self, path):
            self._log(f"rm {path}")
            try:
                os.remove(self._real(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rename(self, oldpath, newpath):
            self._log(f"rename {oldpath} -> {newpath}")
            try:
                os.rename(self._real(oldpath), self._real(newpath))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def mkdir(self, path, attr):
            self._log(f"mkdir {path}")
            try:
                os.mkdir(self._real(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rmdir(self, path):
            self._log(f"rmdir {path}")
            try:
                os.rmdir(self._real(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def chattr(self, path, attr):
            return paramiko.SFTP_OK

    return _SFTPInterface


# --------------------------------------------------------------- serveur -----
_SFTP_ROOT = ""


def _handle(client: socket.socket, addr, host_key, authorized, sftp_if) -> None:
    peer = f"{addr[0]}:{addr[1]}"
    try:
        transport = paramiko.Transport(client)
        transport.add_server_key(host_key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, sftp_if)
        transport.start_server(server=_Server(authorized, peer))
        log.info("connexion de %s", peer)
        # La session vit tant que le transport est ouvert ; SFTP tourne dans ses
        # propres threads gérés par paramiko.
        chan = transport.accept(20)
        if chan is not None:
            while transport.is_active():
                transport.join(1)
    except (paramiko.SSHException, EOFError, OSError) as e:
        log.info("%s : %s", peer, e)
    finally:
        try:
            transport.close()
        except Exception:                        # noqa: BLE001
            pass
        log.info("déconnexion de %s", peer)


def _accept_loop(sock: socket.socket, host_key, authorized, sftp_if) -> None:
    while True:
        try:
            client, addr = sock.accept()
        except OSError:
            break
        threading.Thread(target=_handle,
                         args=(client, addr, host_key, authorized, sftp_if),
                         daemon=True).start()


def serve():
    """Démarre le sshd Python dans un thread. None si indisponible.

    Indisponible = paramiko absent, ou impossible d'écouter sur le port (droits
    insuffisants pour le port 22, ou déjà pris par un autre sshd). Dans tous ces
    cas on journalise et on renvoie None sans emporter les autres services.
    """
    global _SFTP_ROOT
    if paramiko is None:
        log.warning("SSH désactivé : paramiko absent (%s) — `pip install paramiko` "
                    "pour l'émulation SSH/SFTP hors conteneur", _IMPORT_ERROR)
        return None

    _enable_legacy_rsa_sha1()
    _SFTP_ROOT = _ensure_userdata(config.SFTP_ROOT)
    sftp_if = _make_sftp_interface()
    host_key = _host_key()
    authorized = _authorized_keys()

    def _enrolled(line: str) -> None:
        """Clé poussée sur 8182 : acceptée aussitôt, sans redémarrer le sshd.

        `authorized` est la liste que partagent toutes les sessions (`_Server`
        n'en garde qu'une référence) : l'ajouter ici suffit à l'ouvrir.
        """
        key = _parse_key(line)
        if key is not None and key not in authorized:
            authorized.append(key)
            log.info("clé %s enrôlée via 8182 — accès %s ouvert",
                     authkeys.fingerprint(line), config.SSH_USER)

    authkeys.register(_enrolled)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", config.SSH_PORT))
        sock.listen(16)
    except OSError as e:
        sock.close()
        hint = " (port privilégié : lancer en root, ou MFD_SSH_PORT=2222)" \
            if config.SSH_PORT < 1024 else ""
        log.warning("SSH désactivé : écoute impossible sur %d (%s)%s",
                    config.SSH_PORT, e, hint)
        return None

    threading.Thread(target=_accept_loop,
                     args=(sock, host_key, authorized, sftp_if),
                     daemon=True).start()
    log.info("SSH/SFTP à l'écoute sur 0.0.0.0:%d (utilisateur %s, racine SFTP %s)",
             config.SSH_PORT, config.SSH_USER, _SFTP_ROOT)
    return sock
