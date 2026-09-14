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

Sans commande après `--`, la session SFTP interactive passe par un **REPL**
maison (`--no-repl` pour le sftp brut d'autrefois) : même jeu de commandes, mais
avec un historique conservé d'une session à l'autre (`~/.rm_ssh_history`, rappel
par ↑) et la **complétion TAB** des commandes et des chemins — distants comme
locaux. Le client sftp n'offre ni l'un ni l'autre dès que son entrée n'est pas
un terminal, ce qui est toujours le cas en mode `--docker` ; le REPL, lui,
pilote un sftp unique en mode batch et lui parle par tubes (cf. « REPL SFTP »).

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
    python3 rm_ssh.py settings.json --no-repl               # sftp brut, sans REPL
    python3 rm_ssh.py settings.json --print-command        # affiche juste la cmd
"""

import argparse
import atexit
import glob
import json
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid

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


def build_command(binary, key_path, args, remote_cmd, batch=False):
    cmd = [binary, "-i", key_path]
    cmd += COMPAT_SSH_OPTS
    if binary == "sftp":
        # Une commande distante devient un batch SFTP lu sur stdin (`-b -`) :
        # pas de fichier temporaire à monter, y compris en mode Docker. Le REPL
        # (batch=True) emprunte le même canal, ligne à ligne.
        if remote_cmd or batch:
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


# --------------------------------------------------------------- REPL SFTP ---
# Le REPL pilote UN client `sftp -b -` persistant (une seule connexion, un seul
# handshake) mais avec notre propre invite : d'où l'historique entre sessions
# (readline, HISTORY_FILE) et la complétion TAB, que sftp ne fournit pas dès que
# son entrée n'est plus un terminal — et elle ne l'est jamais en mode Docker.
#
# Le dialogue avec sftp découle de son comportement en mode batch :
#   1. il s'arrête à la PREMIÈRE erreur, sauf si la commande est préfixée de
#      « - » (« -ls /absent » signale puis continue) : on préfixe donc tout ;
#   2. il n'imprime « sftp> <commande> » qu'au moment où il LIT la commande, en
#      vidant alors son tampon stdio : il n'y a aucune invite de fin à guetter.
#      Chaque commande est donc suivie d'une ligne sentinelle « #<jeton> » — un
#      commentaire, que sftp ignore en silence — dont l'écho « sftp> #<jeton> »
#      borne la sortie ET force le vidage du tampon ;
#   3. les erreurs partent sur stderr, non tamponné : on le fusionne à stdout
#      (quitte à ce qu'un message devance l'écho de sa propre commande).
HISTORY_FILE = os.path.expanduser("~/.rm_ssh_history")
HISTORY_SIZE = 2000
LISTING_TTL = 10.0          # durée de validité d'un listing distant (complétion)

# Commandes du client sftp (OpenSSH), proposées à la complétion du premier mot.
SFTP_COMMANDS = [
    "bye", "cd", "chgrp", "chmod", "chown", "copy", "cp", "df", "exit", "get",
    "help", "lcd", "lls", "lmkdir", "ln", "lpwd", "ls", "lumask", "mkdir",
    "progress", "put", "pwd", "quit", "reget", "rename", "reput", "rm",
    "rmdir", "symlink", "version",
]


def _sftp_quote(path):
    """Échappe pour la ligne de commande sftp : espaces, guillemets et jokers.

    sftp découpe ses arguments lui-même (espaces séparateurs, « \\ » et
    guillemets protègent) et globalise `*?[]` : un nom de fichier quelconque
    doit donc être protégé caractère par caractère."""
    return re.sub(r"([\\ \t\"'*?\[\]])", r"\\\1", path)


def _sftp_unquote(word):
    """Inverse de _sftp_quote : rend le chemin réel d'un mot tel qu'il est tapé."""
    out, i, quote = [], 0, None
    while i < len(word):
        c = word[i]
        if c == "\\" and i + 1 < len(word):
            out.append(word[i + 1])
            i += 2
            continue
        if quote:
            quote = None if c == quote else quote
            if quote:
                out.append(c)
        elif c in "\"'":
            quote = c
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _split_words(line):
    """Découpe une ligne comme sftp, en couples (position, texte brut).

    La position sert à retrouver le mot que l'on est en train de taper : elle
    seule permet de recoller un mot que readline aurait coupé sur un espace
    échappé (« Mes\\ Routes »)."""
    words, i, n = [], 0, len(line)
    while i < n:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            break
        start, quote = i, None
        while i < n:
            c = line[i]
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if quote:
                quote = None if c == quote else quote
            elif c in "\"'":
                quote = c
            elif c in " \t":
                break
            i += 1
        words.append((start, line[start:i]))
    return words


class SftpSession:
    """Un `sftp -b -` persistant, piloté commande par commande (cf. supra)."""

    def __init__(self, cmd):
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0,
            # Session à part : un Ctrl-C à l'invite ne doit pas emporter le
            # sftp (il quitterait) ; pendant un transfert, c'est nous qui lui
            # relayons le signal, cf. interrupt().
            start_new_session=True)
        self._token = f"rm_ssh_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._seq = 0
        self._pending = b""          # octets lus au-delà du dernier marqueur
        self._listings = {}          # cache des listings distants (complétion)

    # ------------------------------------------------------- bas niveau ---
    def _sentinel(self):
        self._seq += 1
        return f"#{self._token}_{self._seq}"

    def _write(self, text):
        try:
            self.proc.stdin.write(text.encode())
            self.proc.stdin.flush()
        except OSError as e:               # BrokenPipeError inclus
            raise ConnectionError(f"sftp a fermé son entrée ({e})") from e

    def _read_until(self, marker):
        """Lit jusqu'au marqueur et renvoie ce qui précède.

        ConnectionError si sftp s'arrête avant : le message porte alors ce qui
        avait été lu (échec d'authentification, hôte injoignable…)."""
        data, mark = self._pending, marker.encode()
        self._pending = b""
        fd = self.proc.stdout.fileno()
        while mark not in data:
            try:
                chunk = os.read(fd, 65536)
            except KeyboardInterrupt:
                self.interrupt()           # Ctrl-C : abréger le transfert
                continue
            except OSError as e:
                raise ConnectionError(f"lecture sftp impossible : {e}") from e
            if not chunk:
                raise ConnectionError(data.decode("utf-8", "replace"))
            data += chunk
        head, _, self._pending = data.partition(mark)
        return head.decode("utf-8", "replace")

    def interrupt(self):
        """Relaie un Ctrl-C au sftp, qui vit dans sa propre session."""
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
        except OSError:
            pass

    # ------------------------------------------------------- haut niveau ---
    def start(self):
        """Attend que la connexion soit faite ; renvoie la bannière de sftp."""
        sentinel = self._sentinel()
        self._write(sentinel + "\n")
        return self._read_until(f"sftp> {sentinel}\n")

    def run(self, command):
        """Joue une commande de l'utilisateur et renvoie sa sortie.

        Toute commande peut changer ce qu'un listing montrerait (`cd`, `rm`,
        `mkdir`, `rename`…) : le cache de complétion est donc vidé."""
        self._listings.clear()
        return self._run(command)

    def _run(self, command):
        """Envoie une commande, renvoie sa sortie (écho et sentinelle retirés)."""
        # Le « - » qui neutralise l'arrêt sur erreur, sauf si l'utilisateur l'a
        # déjà mis lui-même (sftp refuserait « --ls »).
        sent = command if command.startswith("-") else "-" + command
        sentinel = self._sentinel()
        self._write(f"{sent}\n{sentinel}\n")
        out = self._read_until(f"sftp> {sentinel}\n")
        return out.replace(f"sftp> {sent}\n", "", 1)

    def close(self):
        """Termine proprement la session et renvoie le code de sortie de sftp."""
        if self.proc.poll() is None:
            try:
                self._write("quit\n")
                self.proc.wait(timeout=5)
            except (ConnectionError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait()
        return self.proc.returncode

    def listdir(self, path):
        """Entrées distantes de `path` : liste de (nom, est_un_dossier).

        Mémorisée LISTING_TTL secondes, et jusqu'à la prochaine commande de
        l'utilisateur : une frappe de TAB coûte sinon un aller-retour SFTP, et
        readline en déclenche deux pour afficher les candidats. Sortie analysée = celle de `ls -la`, dont le nom (dernier
        champ) peut contenir des espaces, d'où le découpage en 9 champs."""
        now = time.monotonic()
        cached = self._listings.get(path)
        if cached and now - cached[0] < LISTING_TTL:
            return cached[1]
        try:
            out = self._run("ls -la " + (_sftp_quote(path) if path else "."))
        except ConnectionError:
            return []
        entries = []
        for line in out.splitlines():
            fields = line.split(None, 8)
            if len(fields) < 9 or len(fields[0]) < 10:   # ni droits ni nom
                continue
            name = fields[8].split(" -> ", 1)[0]         # lien symbolique
            name = name.rstrip("/").rsplit("/", 1)[-1]   # certains serveurs
            if name in ("", ".", ".."):                  # renvoient un chemin
                continue
            entries.append((name, fields[0].startswith("d")))
        self._listings[path] = (now, entries)
        return entries


class SftpCompleter:
    """Complétion TAB : commandes sftp, chemins distants (interrogés sur la
    session en cours) et chemins locaux (lcd, lls, put, `get <cible>`)."""

    def __init__(self, session):
        self.session = session
        self.matches = []

    def complete(self, text, state):
        if state == 0:
            try:
                self.matches = self._compute()
            except Exception:              # noqa: BLE001 — readline avale tout
                self.matches = []          # et laisserait la complétion muette
        return self.matches[state] if state < len(self.matches) else None

    def _compute(self):
        import readline

        line = readline.get_line_buffer()
        begidx, endidx = readline.get_begidx(), readline.get_endidx()
        words = _split_words(line)
        # Mot en cours = celui qui couvre le curseur ; sinon un mot vide qui
        # commence là (« ls <TAB> »). On le retrouve par sa position, car
        # readline, lui, coupe sur les espaces même échappés.
        start = endidx
        for pos, word in words:
            if pos <= endidx <= pos + len(word):
                start = pos
                break
        index = sum(1 for pos, _ in words if pos < start)   # 0 = la commande
        typed = _sftp_unquote(line[start:endidx])           # préfixe à compléter
        kept = _sftp_unquote(line[start:begidx])            # part non remplacée

        if index == 0:
            return [c[len(kept):] for c in SFTP_COMMANDS
                    if c.startswith(typed)]

        kind = self._arg_kind(words[0][1] if words else "", index)
        if kind == "remote":
            candidates = self._remote(typed)
        elif kind == "local":
            candidates = self._local(typed)
        else:
            return []
        # readline remplace [begidx, endidx) : on rend le candidat complet
        # (ré-échappé) privé de ce qui précède begidx et reste donc à l'écran.
        return [_sftp_quote(c[len(kept):]) for c in candidates]

    @staticmethod
    def _arg_kind(command, index):
        """Le n-ième argument de `command` désigne-t-il un chemin local, distant
        ou autre chose (mode chmod, aucun argument attendu…) ?"""
        cmd = command.lstrip("-").lower()
        if cmd.startswith("!"):
            return "local"
        if cmd in ("lcd", "lls", "lmkdir", "lumask"):
            return "local"
        if cmd in ("get", "reget"):
            return "remote" if index == 1 else "local"
        if cmd in ("put", "reput"):
            return "local" if index == 1 else "remote"
        if cmd in ("chmod", "chown", "chgrp"):
            return None if index == 1 else "remote"
        if cmd in ("pwd", "lpwd", "version", "progress", "help", "?",
                   "quit", "exit", "bye"):
            return None
        return "remote"

    def _remote(self, word):
        head, sep, prefix = word.rpartition("/")
        entries = self.session.listdir(head + sep if sep else "")
        return sorted(head + sep + name + ("/" if isdir else "")
                      for name, isdir in entries if name.startswith(prefix))

    @staticmethod
    def _local(word):
        expanded = os.path.expanduser(word)
        # Le candidat doit prolonger le mot tel qu'il est tapé : on recolle le
        # « ~ » que expanduser a déplié.
        return sorted(word + c[len(expanded):] + ("/" if os.path.isdir(c) else "")
                      for c in glob.glob(glob.escape(expanded) + "*"))


def _save_history(readline):
    try:
        readline.write_history_file(HISTORY_FILE)
        os.chmod(HISTORY_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def setup_readline(session):
    """Historique persistant (HISTORY_FILE) et complétion TAB.

    Sans readline (build Python sans la bibliothèque), le REPL fonctionne
    toujours, simplement sans édition de ligne."""
    try:
        import readline
    except ImportError:
        print("[*] readline absent : ni historique ni complétion", file=sys.stderr)
        return
    try:
        readline.read_history_file(HISTORY_FILE)
    except OSError:
        pass                               # premier lancement, ou fichier illisible
    readline.set_history_length(HISTORY_SIZE)
    atexit.register(_save_history, readline)
    readline.set_completer(SftpCompleter(session).complete)
    # Séparateurs : les blancs (comme sftp) et « / ». readline ne s'en sert que
    # pour délimiter la portion qu'il remplacera — le complèteur, lui, relit
    # toute la ligne (_split_words) et voit donc les chemins entiers. Garder
    # « / » évite d'afficher le chemin complet devant chaque candidat.
    readline.set_completer_delims(" \t\n/")
    # macOS livre libedit, dont la syntaxe de binding n'est pas celle de GNU
    # readline. `readline.backend` date de Python 3.13, d'où le repli sur la
    # docstring du module, qui nomme l'implémentation.
    if (getattr(readline, "backend", "") == "editline"
            or "libedit" in (readline.__doc__ or "")):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


def run_repl(cmd, prompt="sftp> "):
    """Boucle interactive au-dessus d'un sftp persistant ; renvoie son code."""
    try:
        session = SftpSession(cmd)
    except OSError as e:
        sys.exit(f"[!] impossible de lancer sftp : {e}")

    try:
        sys.stdout.write(session.start())          # bannière (« Connected to… »)
    except ConnectionError as e:
        sys.stderr.write(str(e))
        return session.close() or 1

    interactive = sys.stdin.isatty()
    if interactive:
        setup_readline(session)
        print("[*] REPL SFTP : TAB complète, ↑ rappelle l'historique "
              f"({HISTORY_FILE}), « help » liste les commandes, "
              "« quit » ou Ctrl-D sort.", file=sys.stderr)
    try:
        while True:
            try:
                line = input(prompt if interactive else "")
            except EOFError:
                if interactive:
                    print()
                break
            except KeyboardInterrupt:              # Ctrl-C : abandonne la ligne
                print()
                continue
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line in ("quit", "exit", "bye"):
                break
            if line == "!":
                # `!` seul ouvre un shell interactif SUR L'ENTRÉE de sftp — ici
                # notre tube : il avalerait les commandes suivantes du REPL.
                print("[!] shell interactif indisponible depuis le REPL ; "
                      "utiliser « !commande »", file=sys.stderr)
                continue
            try:
                sys.stdout.write(session.run(line))
            except ConnectionError as e:
                sys.stderr.write(str(e))
                print("[!] session sftp perdue", file=sys.stderr)
                break
    finally:
        rc = session.close()
    return rc


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
    p.add_argument("--no-repl", action="store_true",
                   help="Session sftp brute : ni historique, ni complétion TAB "
                        "(le REPL est le mode interactif par défaut)")
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
    # Sans commande distante, une session SFTP interactive passe par notre REPL
    # (historique + complétion TAB, cf. « REPL SFTP » plus haut), qui pilote un
    # sftp en mode batch : il lui faut donc `-b -` et des tubes, pas de tty.
    use_repl = (binary == "sftp" and not remote_cmd and not args.no_repl
                and not args.print_command)
    # En mode Docker, ssh -i pointe la clé recopiée dans le conteneur.
    cmd = build_command(binary, CONTAINER_KEY if args.docker else key_path,
                        args, remote_cmd, batch=use_repl)
    if args.docker:
        # tty seulement pour une session interactive (shell ou sftp), pas pour
        # une commande distante (exec ou batch SFTP), ni derrière un pipe, ni
        # pour le REPL — qui parle à sftp par des tubes.
        tty = sys.stdin.isatty() and not remote_cmd and not use_repl
        cmd = build_docker_command(cmd, keydir, args.docker_image, tty)

    if args.print_command:
        leftover = keydir if keydir else key_path
        print("Clé privée temporaire :", leftover, "(pensez à la supprimer)")
        print(" ".join(f"'{c}'" if " " in c else c for c in cmd))
        return

    try:
        print(f"[*] Connexion {binary} vers {args.user}@{args.host}:{args.port}"
              f"{' (via Docker)' if args.docker else ''} …", file=sys.stderr)
        if use_repl:
            rc = run_repl(cmd)
        elif batch_input is not None:
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
