#!/usr/bin/env python3
"""
rm_ssh.py — Se connecter en SSH/SFTP à un traceur Raymarine (MFD Axiom)
en utilisant la clé privée stockée dans le fichier user_settings_*.json.

Paramètres reconstitués à partir des logs de l'app (SftpService / Chilkat) :
    - utilisateur : media_rw
    - port        : 22
    - hôte        : 192.168.42.1 (Wi-Fi direct du traceur) ou <serial>.local
    - auth        : clé publique RSA 2048 (champ SshPrivateKey du JSON)

Pourquoi un vieux SSH ? Le MFD tourne une pile OpenSSH très ancienne côté
serveur. Son sshd_config contient encore RSAAuthentication,
Protocol 2 et UsePrivilegeSeparation — des directives retirées d'OpenSSH entre
7.5 et 7.8 : c'est donc un OpenSSH < 7.5. Concrètement :
    - KEX proposés : curve25519 + diffie-hellman-group{14,-exchange,1}-sha1 (SHA-1) ;
    - clé d'hôte RSA (+ ed25519) ;
    - auth par la clé RSA-2048 de RayConnect => signatures ssh-rsa (RSA/SHA-1).
Or OpenSSH >= 8.8 (dont le ssh d'Apple livré avec macOS) désactive ssh-rsa (SHA-1)
par défaut, et les versions récentes retirent une partie des KEX hérités. Les
options -o ci-dessous réactivent ssh-rsa et les KEX SHA-1 tant que le client les
connaît encore ; si le client local refuse malgré tout, on passe par un OpenSSH
ancien fourni par Docker (--docker, voir ssh/Dockerfile.client : OpenSSH 7.9).

Transport : **SFTP par défaut**. Le MFD (comme le simulateur mfdsim) n'expose que
le *subsystem* SFTP : le compte `media_rw` n'a pas de shell interactif
(`nologin`), donc un `ssh` classique — session shell ou exec `-- cmd` — ne
donne rien. Une session SFTP est le canal réellement utilisé par RayConnect
(SftpService) pour rapatrier les fichiers. `--ssh` force malgré tout l'ancien
mode (utile seulement face à une cible dotée d'un shell) ; une commande passée
après `--` est alors soit exécutée par le shell (mode `--ssh`), soit jouée comme
commande **batch SFTP** (`ls`, `get`, `put`, `cd`… ; mode SFTP par défaut).

Découverte : sans --host, le MFD est découvert en rejoignant le groupe multicast
224.0.0.1:5800 (mêmes annonces que raydb_client.py) ; on se
connecte à l'IP SOURCE du datagramme (adresse WiFi/LAN du MFD), et NON à l'IP
interne 198.18.x.x contenue dans l'annonce.

Exemples :
    python3 rm_ssh.py user_settings_<uuid>.json            # découverte auto → SFTP
    python3 rm_ssh.py settings.json --host 192.168.42.1    # IP imposée (pas de découverte)
    python3 rm_ssh.py settings.json --host E70363-1234567.local
    python3 rm_ssh.py settings.json --docker                # via OpenSSH 7.9 (Docker)
    python3 rm_ssh.py settings.json -- ls -la /            # commande batch SFTP
    python3 rm_ssh.py settings.json -- get /Screenshots/x.png   # télécharge un fichier
    python3 rm_ssh.py settings.json --ssh -- ls -la /      # exec shell (cible avec shell)
    python3 rm_ssh.py settings.json --print-command        # affiche juste la cmd
"""

import argparse
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

DEFAULT_USER = "media_rw"
DEFAULT_PORT = 22

# Découverte : les MFD Raymarine annoncent en multicast sur 224.0.0.1:5800. On se
# fie à l'IP SOURCE du datagramme (adresse WiFi/LAN du MFD, où écoute le sshd),
# pas à l'IP interne 198.18.x.x contenue dans l'annonce (cf. raydb_client.py,
# « docs/1. protocole-udp5800.md »).
DISCOVERY_GROUP = "224.0.0.1"
DISCOVERY_PORT = 5800

# Image Docker à OpenSSH ancien (voir ssh/Dockerfile.client) et emplacements de la clé
# dans le conteneur. On monte le *répertoire* contenant la clé (Docker Desktop
# macOS échoue à monter un fichier seul), en lecture seule ; l'entrypoint la
# recopie en 0600 (ssh exige une clé possédée par l'utilisateur courant).
DOCKER_IMAGE = "rm-ssh-legacy"
CONTAINER_MOUNT_DIR = "/tmp/rm_keydir"  # montage lecture seule du dossier de la clé
CONTAINER_KEY = "/root/rm_key"          # clé recopiée, utilisée par ssh -i

# Options nécessaires face au serveur OpenSSH < 7.5 du MFD (clé RSA/SHA-1, KEX
# en SHA-1) et à une clé d'hôte absente de ~/.ssh/known_hosts.
# NB : on n'utilise que des noms d'options connus à la fois du vieux client
# OpenSSH 7.9 (mode --docker) et du ssh moderne de macOS. En particulier
# PubkeyAcceptedKeyTypes (et non PubkeyAcceptedAlgorithms, introduit en 8.5 et
# donc fatal avant) : le nom historique est encore accepté comme alias en 9.x.
COMPAT_SSH_OPTS = [
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
    # KEX hérités que le serveur propose, au cas où
    # curve25519 ne suffirait pas à la négociation.
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "IdentitiesOnly=yes",
    "-o", "ConnectTimeout=10",
    "-o", "LogLevel=ERROR",
]


# --------------------------------------------- découverte UDP 5800 (mcast) ---
def parse_5800(payload):
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
    Cf. « docs/1. protocole-udp5800.md » §4."""
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


def _discover_via_mdns(timeout):
    """Cherche les services Raymarine (_raydb._tcp, _rym_rrc._tcp) en mDNS et
    renvoie l'adresse IPv4 de la première instance résolue, **sans le port**.
    None si zeroconf est absent, ou si rien n'est annoncé dans le délai."""
    try:
        from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
    except ImportError:
        return None

    found = []

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            found.append((service_type, name))

    zc = Zeroconf()
    seen = set()
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


def discover_mfd(timeout):
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


def load_private_key(settings_path):
    with open(settings_path, "r") as f:
        data = json.load(f)
    key = data.get("SshPrivateKey")
    if not key:
        sys.exit(f"[!] Champ 'SshPrivateKey' introuvable dans {settings_path}")
    # Le JSON stocke les retours chariot en \r\n ; ssh accepte, on normalise en \n.
    key = key.replace("\r\n", "\n").replace("\r", "\n")
    if not key.endswith("\n"):
        key += "\n"
    return key


def write_temp_key(key_text):
    fd, path = tempfile.mkstemp(prefix="rm_id_rsa_")
    os.write(fd, key_text.encode())
    os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, obligatoire pour ssh
    return path


def write_temp_keydir(key_text):
    """Clé dans un répertoire temporaire sous le home, pour le montage Docker :
    Docker Desktop (macOS) ne partage que ~/ (ni /tmp ni /var/folders).
    Renvoie (répertoire, chemin de la clé)."""
    d = tempfile.mkdtemp(prefix=".rm_ssh_", dir=os.path.expanduser("~"))
    os.chmod(d, 0o700)
    key_file = os.path.join(d, "id")
    with open(key_file, "w") as f:
        f.write(key_text)
    os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
    return d, key_file


def build_command(binary, key_path, args, remote_cmd):
    cmd = [binary, "-i", key_path]
    cmd += COMPAT_SSH_OPTS
    if binary == "sftp":
        # Une commande distante devient un batch SFTP lu sur stdin (`-b -`) :
        # pas de fichier temporaire à monter, y compris en mode Docker.
        if remote_cmd:
            cmd += ["-b", "-"]
        cmd += ["-P", str(args.port), f"{args.user}@{args.host}"]
    else:  # ssh
        cmd += ["-p", str(args.port), f"{args.user}@{args.host}"]
        if remote_cmd:
            cmd += remote_cmd
    return cmd


def build_docker_command(inner_cmd, host_keydir, image, tty):
    """Enveloppe la commande ssh/sftp dans `docker run`, le dossier de la clé
    étant monté en lecture seule (l'entrypoint de l'image la recopie en 0600)."""
    run = ["docker", "run", "--rm", "-i"]
    if tty:
        run.append("-t")
    run += ["-v", f"{os.path.realpath(host_keydir)}:{CONTAINER_MOUNT_DIR}:ro", image]
    return run + inner_cmd


def main():
    p = argparse.ArgumentParser(
        description="Connexion SSH/SFTP au traceur Raymarine via user_settings JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("settings", help="Chemin du fichier user_settings_*.json")
    p.add_argument("--host", default=None,
                   help="Hôte du traceur ; si omis, découverte auto via mcast 5800")
    p.add_argument("--discover-timeout", type=float, default=15,
                   help="Délai d'écoute des annonces 5800 (s, défaut: 15)")
    p.add_argument("--user", default=DEFAULT_USER,
                   help=f"Utilisateur SSH (défaut: {DEFAULT_USER})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"Port SSH (défaut: {DEFAULT_PORT})")
    p.add_argument("--ssh", action="store_true",
                   help="Forcer un ssh classique (shell/exec) au lieu de SFTP "
                        "— n'a de sens que face à une cible dotée d'un shell")
    p.add_argument("--sftp", action="store_true",
                   help=argparse.SUPPRESS)   # déprécié : SFTP est désormais le défaut
    p.add_argument("--docker", action="store_true",
                   help="Passer par un OpenSSH ancien dans Docker "
                        f"(image {DOCKER_IMAGE} ; cf. ssh/Dockerfile.client)")
    p.add_argument("--docker-image", default=DOCKER_IMAGE,
                   help=f"Image Docker à utiliser (défaut: {DOCKER_IMAGE})")
    p.add_argument("--print-command", action="store_true",
                   help="Afficher la commande (clé écrite dans un fichier temporaire) sans l'exécuter")
    p.epilog = "Toute commande distante se place après un '--' :  rm_ssh.py settings.json -- ls -la /"

    # Tout ce qui suit le premier '--' isolé est la commande distante ;
    # le reste part dans argparse (évite que les flags soient avalés).
    argv = sys.argv[1:]
    remote_cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, remote_cmd = argv[:i], argv[i + 1:]
    args = p.parse_args(argv)

    if args.host is None:                       # pas d'hôte imposé → découvrir
        print(f"[*] découverte MFD (mDNS puis mcast {DISCOVERY_GROUP}:{DISCOVERY_PORT})…",
              file=sys.stderr)
        args.host = discover_mfd(args.discover_timeout)
        if args.host is None:
            sys.exit("[!] aucun MFD découvert — vérifier le WiFi du bord, "
                     "ou imposer --host")

    key_text = load_private_key(args.settings)
    if args.docker:
        keydir, key_path = write_temp_keydir(key_text)
    else:
        keydir, key_path = None, write_temp_key(key_text)

    binary = "ssh" if args.ssh else "sftp"
    # En mode SFTP, une commande distante est jouée en batch : sftp la lit sur
    # stdin (`-b -`, cf. build_command), on la lui fournit ici, une par ligne.
    batch_input = None
    if binary == "sftp" and remote_cmd:
        batch_input = " ".join(remote_cmd) + "\n"
    # En mode Docker, ssh -i pointe la clé recopiée dans le conteneur.
    cmd = build_command(binary, CONTAINER_KEY if args.docker else key_path,
                        args, remote_cmd)
    if args.docker:
        # tty seulement pour une session interactive (shell ou sftp), pas pour
        # une commande distante (exec ou batch SFTP) ni derrière un pipe.
        tty = sys.stdin.isatty() and not remote_cmd
        cmd = build_docker_command(cmd, keydir, args.docker_image, tty)

    if args.print_command:
        leftover = keydir if keydir else key_path
        print("Clé privée temporaire :", leftover, "(pensez à la supprimer)")
        print(" ".join(f"'{c}'" if " " in c else c for c in cmd))
        return

    try:
        print(f"[*] Connexion {binary} vers {args.user}@{args.host}:{args.port}"
              f"{' (via Docker)' if args.docker else ''} …", file=sys.stderr)
        if batch_input is not None:
            # Le code de retour est celui de ssh/sftp : on le relaie tel quel
            # (cf. sys.exit plus bas), il n'a pas à lever ici.
            rc = subprocess.run(cmd, input=batch_input, text=True,
                                check=False).returncode
        else:
            rc = subprocess.call(cmd)
        sys.exit(rc)
    finally:
        # ne jamais laisser traîner la clé privée
        if keydir:
            shutil.rmtree(keydir, ignore_errors=True)
        else:
            try:
                os.remove(key_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
