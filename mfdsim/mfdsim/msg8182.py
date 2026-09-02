"""
msg8182.py — protocole de messages Raymarine (TCP 8182), service SSHAccess.

Le MFD réel expose sur ce port plusieurs services (enrôlement de clé SSH par
RayConnect, cartographie, espace disque, propriété — cf. « 5. protocole-messages-8182.md »).
On modélise ici le service **SSHAccess** (le seul observé en capture) : le client
pousse son identité (email) et sa clé publique SSH ; le MFD accuse réception avec
un statut `KeyAddSuccess`.

Cadrage (little-endian) — la trame vaut `8 + len` octets, en-tête commun puis
un corps qui dépend de la commande :

    [u32 command][u32 len][u32 appType][u32 msgType]  puis :
      1500000 SSHAccessRequest : [u32 id_len][id][clé publique jusqu'à la fin]
      1500007 RequestOwnership : [u32 user_len][user][u32 ssh_len][sshKey]
                                 [u32 cert_len][certKey]

où `len` couvre tout ce qui suit le champ `len`. 1500000 (`sendSSHKey`) porte la
clé seule, en dernier champ **sans longueur propre** ; 1500007
(`sendRequestOwnerAuthCommand`) enchaîne trois champs **chacun préfixé par sa
longueur** : username, clé SSH, certificat. La réponse reprend `command+1`,
ré-écho de l'`appType` et de l'identité, et le résultat dans `msgType` (énum
`SSHAccessResponseMessageType`). Les 5 captures montrent toujours `KeyAddSuccess`
(1) immédiat, sans approbation : on reproduit ce comportement.

L'enrôlement n'est pas qu'un accusé de réception : comme le MFD réel, la clé
reçue est **ajoutée aux clés autorisées du sshd** (cf. `authkeys.py`), si bien
qu'un client peut enchaîner sur le SFTP du port 22 avec sa clé privée. Un payload
sans clé exploitable, ou un fichier non inscriptible, donne `KeyAddFail` (2).

Les commandes non reconnues (autres services) sont journalisées en hexa, sans
réponse — comme avant, pour capturer ce qui reste à modéliser.
"""
from __future__ import annotations

import logging
import socketserver
import struct
import threading

from . import authkeys, config

log = logging.getLogger("msg8182")

# Commandes SSHAccess (cf. doc §3/§6).
CMD_SSH_ACCESS_REQUEST = 1500000     # sendSSHKey : clé seule
CMD_REQUEST_OWNERSHIP = 1500007      # sendRequestOwnerAuthCommand : clé + cert
REQUEST_COMMANDS = {CMD_SSH_ACCESS_REQUEST, CMD_REQUEST_OWNERSHIP}
COMMAND_NAMES = {
    CMD_SSH_ACCESS_REQUEST: "SSHAccessRequest",
    CMD_REQUEST_OWNERSHIP: "RequestOwnership",
}

# Statuts SSHAccessResponseMessageType.
MSG_NONE = 0
MSG_KEY_ADD_SUCCESS = 1
MSG_KEY_ADD_FAIL = 2
MSG_AUTH_REJECTED = 3
MSG_AUTH_IN_PROGRESS = 4

HEADER_LEN = 8               # command + len
MAX_FRAME = 1 << 20         # garde-fou : aucune trame légitime n'approche 1 Mo


def _hexdump(data: bytes, width: int = 16) -> str:
    """Rendu hex + ASCII façon `hexdump -C`, indenté pour le journal."""
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexa = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in chunk)
        lines.append(f"    {off:08x}  {hexa:<{width * 3}} |{text}|")
    return "\n".join(lines)


# ------------------------------------------------------------- décodage ------
def parse_frames(buf: bytearray) -> list[tuple[int, bytes]]:
    """Extrait du buffer les trames complètes `(command, frame)`, et les retire.

    Chaque trame vaut `8 + len`. Le buffer est modifié en place : ce qui reste
    est le début d'une trame incomplète, à compléter au prochain segment TCP.
    """
    out: list[tuple[int, bytes]] = []
    while len(buf) >= HEADER_LEN:
        command, length = struct.unpack_from("<II", buf, 0)
        if length > MAX_FRAME:
            raise ValueError(f"longueur de trame aberrante : {length}")
        if len(buf) < HEADER_LEN + length:
            break
        frame = bytes(buf[:HEADER_LEN + length])
        del buf[:HEADER_LEN + length]
        out.append((command, frame))
    return out


def _read_lp(frame: bytes, off: int) -> tuple[bytes, int]:
    """Lit un champ préfixé par un u32 de longueur (LE) à `off`.

    Renvoie `(valeur, offset_suivant)`. Défensif : un préfixe qui déborde de la
    trame donne une valeur vide plutôt qu'une exception.
    """
    if off + 4 > len(frame):
        return b"", len(frame)
    (n,) = struct.unpack_from("<I", frame, off)
    start = off + 4
    return frame[start:start + n], start + n


def decode_request(command: int, frame: bytes) -> tuple[int, int, str, bytes, bytes | None]:
    """Décode `(appType, msgType, identity, sshKey, cert)` d'une trame de requête.

    L'identité (u32 de longueur + octets) se lit pareil pour les deux commandes ;
    ensuite le corps diffère (cf. docstring du module) :
      - 1500000 : la clé publique est le **dernier** champ, jusqu'à la fin (pas de
        longueur propre) ; pas de certificat (`cert = None`) ;
      - 1500007 : la clé SSH **puis** le certificat sont chacun préfixés par leur
        longueur (u32). C'est ce préfixe qui, mal interprété comme 1500000,
        faisait précéder la clé d'un octet parasite (« }… ») dans le journal.
    """
    app_type, msg_type = struct.unpack_from("<II", frame, 8)
    ident_b, off = _read_lp(frame, 16)
    identity = ident_b.decode("latin1", "replace")
    if command == CMD_REQUEST_OWNERSHIP:
        ssh_key, off = _read_lp(frame, off)
        cert, _ = _read_lp(frame, off)
        return app_type, msg_type, identity, ssh_key, cert
    return app_type, msg_type, identity, frame[off:], None


# ------------------------------------------------------------- encodage ------
def build_response(command: int, app_type: int, identity: str,
                   msg_type: int = MSG_KEY_ADD_SUCCESS) -> bytes:
    """Réponse SSHAccess : `command+1`, ré-écho appType/identité, statut `msg_type`.

    Ne contient pas la clé (comme le MFD réel) : identité ré-écho + résultat.
    """
    id_b = identity.encode("latin1")
    body = struct.pack("<III", app_type, msg_type, len(id_b)) + id_b
    return struct.pack("<II", command + 1, len(body)) + body


# --------------------------------------------------------------- serveur -----
class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        log.info("connexion de %s", peer)
        buf = bytearray()
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                try:
                    frames = parse_frames(buf)
                except ValueError as e:
                    log.warning("%s : %s — connexion fermée", peer, e)
                    break
                for command, frame in frames:
                    self._dispatch(peer, command, frame)
        except OSError as e:
            log.info("%s : %s", peer, e)
        finally:
            log.info("déconnexion de %s", peer)

    def _dispatch(self, peer: str, command: int, frame: bytes) -> None:
        name = COMMAND_NAMES.get(command)
        if command not in REQUEST_COMMANDS:
            # Autre service (cartographie, espace disque…) : pas encore modélisé.
            log.info("%s commande %d%s non modélisée, %d octet(s) :\n%s",
                     peer, command, f" ({name})" if name else "",
                     len(frame), _hexdump(frame))
            return

        app_type, msg_type, identity, ssh_key, cert = decode_request(command, frame)
        # Aperçu lisible de la clé publique (« ssh-rsa … »), désormais isolée de
        # son préfixe de longueur et du certificat (cf. decode_request).
        preview = ssh_key[:48].decode("latin1", "replace").replace("\n", " ")
        cert_info = f" cert={len(cert)} o" if cert is not None else ""
        log.info("%s %s (cmd=%d) appType=%d msgType=%d identité=%r "
                 "clé=%d octet(s)%s « %s… »",
                 peer, name, command, app_type, msg_type, identity,
                 len(ssh_key), cert_info, preview)

        # Comme le PlatformServicesDaemon du MFD, on autorise la clé reçue pour
        # le compte SFTP : c'est l'enrôlement 8182 qui ouvre l'accès au port 22.
        key = authkeys.extract(ssh_key)
        if key is None:
            log.warning("%s aucune clé publique dans le payload — KeyAddFail", peer)
            status = MSG_KEY_ADD_FAIL
        else:
            status = (MSG_KEY_ADD_SUCCESS if authkeys.add(key, identity)
                      else MSG_KEY_ADD_FAIL)

        resp = build_response(command, app_type, identity, status)
        try:
            self.request.sendall(resp)
            log.info("%s → réponse cmd=%d msgType=%d (%s)", peer, command + 1, status,
                     "KeyAddSuccess" if status == MSG_KEY_ADD_SUCCESS else "KeyAddFail")
        except OSError as e:
            log.info("%s : envoi réponse impossible (%s)", peer, e)


class Msg8182Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve() -> Msg8182Server:
    """Démarre l'écoute 8182 dans un thread et la renvoie."""
    srv = Msg8182Server(("0.0.0.0", config.MSG8182_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("messages 8182 à l'écoute sur 0.0.0.0:%d (SSHAccess : enrôlement de clé)",
             config.MSG8182_PORT)
    return srv
