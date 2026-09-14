#!/usr/bin/env python3
"""
rm_ownerauth.py — revendiquer la propriété d'un MFD Raymarine en envoyant la
commande 1500007 (MessageTypeRequestOwnership) sur le port TCP 8182.

C'est le *pendant émetteur* du canal d'enrôlement : là où RayConnect envoie
d'ordinaire la commande `1500000` (clé SSH seule, `Messages::sendSSHKey`, cf.
« 5. protocole-messages-8182.md »), le flux « owner request » de l'app passe
par `1500007`
(`Messages::sendRequestOwnerAuthCommand{username, sshKey, certKey}`) : il enrôle
la clé publique SSH *et* le certificat, ce qui revendique la propriété du bateau.

Format de trame — confirmé à l'octet près au désassemblage de `libwp.so`
(`sendRequestOwnerAuthCommand`, little-endian, tout est préfixé en longueur) :

    [u32 cmd=1500007][u32 len][u32 appType=2][u32 msgType=0]
    [u32 username_len][username]
    [u32 sshkey_len ][sshKey  ]        ("ssh-rsa AAAA…")
    [u32 certkey_len][certKey ]        (PEM "-----BEGIN PUBLIC KEY-----…")

`len` couvre tout ce qui suit l'octet 8, soit 0x14 + les trois longueurs. La
réponse (16 o lus par le natif) porte le résultat dans son 4e u32
(SSHAccessResponseMessageType) : 1=KeyAddSuccess, 2=KeyAddFail, 3=AuthRejected,
4=AuthInProgress.

Découverte : sans --ip, le client rejoint le groupe multicast 224.0.0.1:5800 et
se connecte à l'IP SOURCE de la première annonce Raymarine (adresse WiFi/LAN du
MFD) — et NON à l'IP interne 198.18.x.x contenue dans l'annonce (cf.
raydb_client.py, « 1. protocole-udp5800.md »).

La clé (et le certificat) proviennent au choix :
  - d'un fichier `user_settings_*.json` de RayConnect (champs SshPublicKey +
    CertPublicKey ; à défaut SshPublicKey est dérivé de SshPrivateKey) ;
  - d'une clé publique littérale « ssh-rsa AAAA… » (sans certificat, sauf
    --cert) — la clé qu'on veut faire autoriser en SFTP media_rw ensuite.

Usage :
    ./rm_ownerauth.py user_settings_<uuid>.json              # découverte auto
    ./rm_ownerauth.py user_settings_….json --ip 192.168.42.1 # IP imposée
    ./rm_ownerauth.py "ssh-rsa AAAAB3Nza…"                    # clé littérale, sans cert
    ./rm_ownerauth.py "ssh-rsa AAAA…" --cert cert.pem --email moi@exemple.fr
    ./rm_ownerauth.py user_settings_….json --dry-run         # afficher la trame, ne rien envoyer

Une fois la clé enrôlée, on s'y connecte en SFTP avec `rm_ssh.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

# --- 8182 : service de messages du MFD -------------------------------------
MSG_PORT = 8182
APP_TYPE_SSH = 2                       # appType SSHAccess (cf. §3 de la doc 8182)

CMD_OWNER_AUTH = 0x0016E367           # 1500007, MessageTypeRequestOwnership
CMD_SSH_KEY = 0x0016E360              # 1500000, SSHAccessRequest (clé seule)

# 4e u32 de la réponse = SSHAccessResponseMessageType.
STATUS = {
    0: "None", 1: "KeyAddSuccess", 2: "KeyAddFail",
    3: "AuthRejected", 4: "AuthInProgress",
}

# --- découverte : les MFD annoncent en multicast sur 224.0.0.1:5800 --------
# On se fie à l'IP SOURCE du datagramme (adresse WiFi du MFD), pas à l'IP
# interne 198.18.x.x annoncée dans la charge utile.
DISCOVERY_GROUP = "224.0.0.1"
DISCOVERY_PORT = 5800

# L'identité revendiquée est purement déclarative côté MFD (cf. §7 de la doc
# 8182) : ce défaut n'est qu'un exemple. Celle observée dans les 5 captures
# d'enrôlement est l'adresse du compte RayConnect — la passer par --email.
DEFAULT_EMAIL = "owner@example-boat.org"


# --------------------------------------------- découverte UDP 5800 (mcast) ---
def parse_5800(payload: bytes) -> dict | None:
    """Décode une annonce de découverte Raymarine (type 1/2), sinon None.

    Types 1 et 2 = le MÊME enregistrement, avec une queue de longueur variable
    (@52 = u16 donnant le nombre d'octets à partir de @54 : 2 pour le type 1,
    16 pour le type 2, d'où 56 et 70 octets) :
    [u32 type][u32 u1][4 handle][u32 descriptor][4 ip LE][nom ASCIIZ @20].
    u1 (@4) : rôle NON RÉSOLU — varie selon le device ET le type de message.
    descriptor (@12) : type/modèle, PARTAGÉ par devices identiques ; ses octets
    hauts forment le « mot de classe » (0x840b nœud/MFD, 0x0000 radar/capteur).
    Le nom se coupe au premier NUL : au-delà, buffer réutilisé non nettoyé.
    Heuristique MFD : mot de classe non nul (0x840b0067) ; radars <= 0xff (0xa2, 0xcd).
    Cf. « 1. protocole-udp5800.md » §4."""
    if len(payload) < 32:
        return None
    mtype = struct.unpack_from("<I", payload, 0)[0]
    if mtype not in (1, 2):
        return None
    descriptor = struct.unpack_from("<I", payload, 12)[0]
    ip = ".".join(str(payload[16 + 3 - i]) for i in range(4))       # little-endian
    name = payload[20:52].split(b"\0")[0].decode("latin1", "replace")
    return {"descriptor": descriptor, "announced_ip": ip, "name": name,
            "is_mfd": (descriptor & 0xFFFFFF00) != 0}


# Services mDNS Raymarine interrogés avant le beacon 5800 : leur enregistrement
# porte déjà l'IP joignable du MFD (on ignore le port annoncé).
MDNS_SERVICES = ["_raydb._tcp.local.", "_rym_rrc._tcp.local."]


def _discover_via_mdns(timeout: float) -> str | None:
    """Cherche les services Raymarine (_raydb._tcp, _rym_rrc._tcp) en mDNS et
    renvoie l'adresse IPv4 de la première instance résolue, **sans le port**.
    None si zeroconf est absent, ou si rien n'est annoncé dans le délai."""
    try:
        from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
    except ImportError:
        return None

    found: list = []

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            found.append((service_type, name))

    zc = Zeroconf()
    seen: set = set()
    try:
        ServiceBrowser(zc, MDNS_SERVICES, handlers=[on_change])
        deadline = time.time() + timeout
        while time.time() < deadline:
            for entry in found:
                if entry in seen:
                    continue
                seen.add(entry)
                info = zc.get_service_info(entry[0], entry[1], timeout=1500)
                if info is None:
                    continue
                for addr in info.parsed_addresses():
                    if ":" not in addr:            # IPv4 seulement
                        return addr
            time.sleep(0.2)
        return None
    finally:
        zc.close()


def discover_mfd(timeout: float) -> str | None:
    """Découvre l'IP du MFD. D'ABORD via mDNS (_raydb._tcp / _rym_rrc._tcp) : si
    un service répond, on prend son IP et on **ne lit pas** le multicast 5800.
    Sinon, repli sur le beacon 224.0.0.1:5800 (IP SOURCE de l'annonce du MFD)."""
    ip = _discover_via_mdns(timeout)
    if ip is not None:
        print(f"[*] MFD découvert (mDNS) : {ip}", file=sys.stderr)
        return ip

    # Repli : le beacon multicast 5800.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", DISCOVERY_PORT))
    mreq = struct.pack("=4sl", socket.inet_aton(DISCOVERY_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(1.0)

    deadline = time.time() + timeout
    fallback = None
    try:
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except TimeoutError:
                continue
            info = parse_5800(data)
            if not info:
                continue
            src = addr[0]
            label = info["name"] or info["announced_ip"]
            if info["is_mfd"]:
                print(f"[*] MFD découvert : {src} ({label})", file=sys.stderr)
                return src
            fallback = fallback or src
            print(f"[*] annonce {label} depuis {src}…", file=sys.stderr)
        return fallback
    finally:
        sock.close()


# ------------------------------------------------- matériel de clé (arg) -----
def _pubkey_from_private(priv_pem: str) -> str:
    """Dérive la clé publique OpenSSH (ssh-rsa AAAA…) d'une clé privée PEM via
    `ssh-keygen -y` (aucune dépendance Python)."""
    priv_pem = priv_pem.replace("\r\n", "\n").replace("\r", "\n")
    if not priv_pem.endswith("\n"):
        priv_pem += "\n"
    fd, path = tempfile.mkstemp(prefix="rm_owner_priv_")
    try:
        os.write(fd, priv_pem.encode())
        os.close(fd)
        os.chmod(path, 0o600)
        out = subprocess.run(["ssh-keygen", "-y", "-f", path],
                             capture_output=True, text=True, check=False)
        if out.returncode != 0:
            sys.exit(f"[!] ssh-keygen -y a échoué : {out.stderr.strip()}")
        return out.stdout.strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def load_material(arg: str, cert_path: str | None, email: str) -> tuple[str, str, str]:
    """Renvoie (username, ssh_key, cert_key) à partir du paramètre positionnel.

    Le paramètre est soit une clé publique littérale « ssh-rsa AAAA… », soit un
    chemin de `user_settings_*.json`."""
    cert_key = ""
    if cert_path:
        with open(cert_path) as f:
            cert_key = f.read().strip()

    if arg.startswith(("ssh-rsa ", "ssh-ed25519 ", "ecdsa-", "ssh-dss ")):
        return email, arg.strip(), cert_key                # clé littérale

    if not os.path.isfile(arg):
        sys.exit(f"[!] '{arg}' n'est ni une clé « ssh-rsa … » ni un fichier existant")

    try:
        with open(arg) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"[!] lecture de {arg} : {e}")

    ssh_key = (data.get("SshPublicKey") or "").strip()
    if not ssh_key:
        priv = data.get("SshPrivateKey")
        if not priv:
            sys.exit(f"[!] ni 'SshPublicKey' ni 'SshPrivateKey' dans {arg}")
        ssh_key = _pubkey_from_private(priv)
    # Le certificat explicite (--cert) l'emporte ; sinon celui du JSON.
    if not cert_key:
        cert_key = (data.get("CertPublicKey") or "").strip()
    cert_key = cert_key.replace("\r\n", "\n").replace("\r", "\n")
    return email, ssh_key, cert_key


# ----------------------------------------------------- trame 1500007 --------
def build_owner_auth(app_type: int, username: str, ssh_key: str, cert_key: str) -> bytes:
    """Construit la trame 1500007 (MessageTypeRequestOwnership), little-endian.

    Layout confirmé sur `Messages::sendRequestOwnerAuthCommand` (libwp.so) :
    len = 0x14 + username_len + sshkey_len + certkey_len (tout ce qui suit l'octet 8)."""
    u = username.encode("latin1")
    s = ssh_key.encode("latin1")
    c = cert_key.encode("latin1")
    body_len = 0x14 + len(u) + len(s) + len(c)
    return (struct.pack("<IIII", CMD_OWNER_AUTH, body_len, app_type, 0)
            + struct.pack("<I", len(u)) + u
            + struct.pack("<I", len(s)) + s
            + struct.pack("<I", len(c)) + c)


# ----------------------------------------------------- trame 1500000 --------
def build_ssh_key(app_type: int, username: str, ssh_key: str) -> bytes:
    """Construit la trame 1500000 (SSHAccessRequest), little-endian.

    Layout confirmé sur `Messages::sendSSHKey` (libwp.so) : pas de certificat, et
    la clé est le *dernier* champ — sans longueur propre, elle court jusqu'au bout
    de la trame (cf. §3 de « 5. protocole-messages-8182.md ») :

        [u32 cmd=1500000][u32 len][u32 appType=2][u32 msgType=0]
        [u32 id_len][id][sshKey…]

    len = 0xC + id_len + sshkey_len (appType+msgType+id_len, puis id et la clé)."""
    u = username.encode("latin1")
    s = ssh_key.encode("latin1")
    body_len = 0xC + len(u) + len(s)
    return (struct.pack("<IIII", CMD_SSH_KEY, body_len, app_type, 0)
            + struct.pack("<I", len(u)) + u
            + s)                              # clé : dernier champ, sans longueur


def parse_response(buf: bytes) -> str:
    """Décode la réponse (>= 16 o) : [cmd][len][appType][msgType]."""
    if len(buf) < 16:
        return f"réponse tronquée ({len(buf)} o) : {buf.hex()}"
    cmd, length, app_type, msg_type = struct.unpack_from("<IIII", buf, 0)
    verdict = STATUS.get(msg_type, f"msgType={msg_type}")
    return f"cmd=0x{cmd:06X} len={length} appType={app_type} → {verdict}"


def send_frame(host: str, port: int, frame: bytes, cmd_label: str, timeout: float) -> int:
    print(f"[*] connexion {host}:{port} — envoi {cmd_label} ({len(frame)} o)…",
          file=sys.stderr)
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(frame)
            s.settimeout(timeout)
            buf = b""
            try:
                while len(buf) < 16:
                    chunk = s.recv(64)
                    if not chunk:
                        break
                    buf += chunk
            except TimeoutError:
                pass
    except OSError as e:
        sys.exit(f"[!] échec 8182 vers {host}:{port} : {e}")

    if not buf:
        print("[!] aucune réponse (le MFD a peut-être fermé sans accuser réception)",
              file=sys.stderr)
        return 1
    print(f"[+] {parse_response(buf)}")
    # Succès seulement sur KeyAddSuccess (1).
    return 0 if struct.unpack_from("<I", buf, 12)[0] == 1 else 2


# ---------------------------------------------------------------- main ------
def _hexdump(frame: bytes) -> None:
    for off in range(0, len(frame), 16):
        row = frame[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in row)
        print(f"  {off:04x}  {hexs}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Envoie la commande 1500007 (RequestOwnership) à un MFD Raymarine (TCP 8182).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Le paramètre est un user_settings_*.json OU une clé « ssh-rsa AAAA… ».",
    )
    ap.add_argument("key_or_settings",
                    help="fichier user_settings_*.json, ou clé publique « ssh-rsa AAAA… »")
    ap.add_argument("--ip", metavar="ADRESSE",
                    help="IP du MFD ; si omis, découverte auto via mcast 5800")
    ap.add_argument("--port", type=int, default=MSG_PORT,
                    help=f"port du service de messages (défaut {MSG_PORT})")
    ap.add_argument("--email", default=DEFAULT_EMAIL,
                    help=f"identité/username revendiqué (défaut {DEFAULT_EMAIL})")
    ap.add_argument("--cert", metavar="FICHIER",
                    help="certificat (PEM) à joindre ; défaut : CertPublicKey du JSON")
    ap.add_argument("--ssh-key-only", action="store_true",
                    help="envoyer 1500000 (SSHAccessRequest, clé seule, sendSSHKey) "
                         "au lieu de 1500007 ; le certificat est ignoré")
    ap.add_argument("--discover-timeout", type=float, default=15,
                    help="délai d'écoute des annonces 5800 (s, défaut 15)")
    ap.add_argument("--timeout", type=float, default=10,
                    help="délai réseau TCP 8182 (s, défaut 10)")
    ap.add_argument("--dry-run", action="store_true",
                    help="construire et afficher la trame sans l'envoyer")
    args = ap.parse_args()

    email, ssh_key, cert_key = load_material(args.key_or_settings, args.cert, args.email)

    if args.ssh_key_only:
        if cert_key:
            print("[!] --ssh-key-only : certificat ignoré (1500000 = clé seule)",
                  file=sys.stderr)
        cert_key = ""
        frame = build_ssh_key(APP_TYPE_SSH, email, ssh_key)
        cmd_label = "1500000"
    else:
        if not cert_key:
            print("[!] aucun certificat : trame 1500007 avec certKey vide "
                  "(le MFD peut la refuser — fournir --cert, un user_settings avec "
                  "CertPublicKey, ou --ssh-key-only pour la trame 1500000)",
                  file=sys.stderr)
        frame = build_owner_auth(APP_TYPE_SSH, email, ssh_key, cert_key)
        cmd_label = "1500007"

    print(f"[*] username : {email}", file=sys.stderr)
    print(f"[*] sshKey   : {ssh_key[:40]}… ({len(ssh_key)} o)", file=sys.stderr)
    print(f"[*] certKey  : {len(cert_key)} o", file=sys.stderr)

    if args.dry_run:
        print(f"# trame {cmd_label}, {len(frame)} octets :")
        _hexdump(frame)
        return

    host = args.ip
    if host is None:
        print(f"[*] découverte MFD (mDNS puis mcast {DISCOVERY_GROUP}:{DISCOVERY_PORT})…",
              file=sys.stderr)
        host = discover_mfd(args.discover_timeout)
        if host is None:
            sys.exit("[!] aucun MFD découvert — vérifier le WiFi du bord, "
                     "ou imposer --ip")

    sys.exit(send_frame(host, args.port, frame, cmd_label, args.timeout))


if __name__ == "__main__":
    main()
