# mfdsim — un MFD Raymarine simulé, en conteneur

Cible de développement pour les clients du dépôt : le conteneur se comporte, vu
du réseau, comme un MFD AXIOM sous LightHouse. Il permet de travailler sur
`raydb_client.py`, `mfd_discover.py` ou `rrce_touch.py` sans le MFD physique —
et sans être à bord.

Il implémente le côté **serveur** des protocoles rétro-conçus dans les documents
`2.` à `5.` du dépôt.

| Service | Port | Rôle | Fidélité |
|---|---|---|---|
| mDNS / Bonjour | `5353/udp` | annonce `_raydb` / `_rym_rrc` / `_rtsp` | TXT identiques au MFD réel |
| Découverte propriétaire | `5800/udp` | beacon multicast `224.0.0.1`, 4 équipements | trames types 0/1/2, TLV |
| RayDB | `23333/tcp` | bus publish/subscribe clé→valeur | HELLO/SUBSCRIBE/UPDATE/KEEPALIVE |
| RRCE | `50000/tcp` | canal d'entrée de la télécommande | records `ECRR` tactiles, boutons et molette, unidirectionnel ; types inconnus journalisés en hexa |
| RTSP | `8554/tcp` | recopie d'écran H.264 (`/RAYMARINEMFD`) | serveur **GStreamer**, comme le MFD ; repli **ffmpeg+mediamtx** sans GStreamer ; rejoue l'écran réel reconstitué |
| Messages | `8182/tcp` | protocole de messages, service **SSHAccess** (enrôlement clé SSH) | requêtes `1500000`/`1500007` → clé **ajoutée aux `authorized_keys`** de `media_rw` puis `KeyAddSuccess` ; autres services journalisés en hexa |
| SSH / SFTP | `22/tcp` | accès `media_rw`, arbre `/data/media/0/UserData` | **sshd Python** (paramiko), conteneur comme hors conteneur ; MFD réel = clé seule, ici clé **ou** mot de passe (commodité) |

Le flux RTSP rejoue en boucle l'écran réel d'un Axiom 7
(`video/mfd_screen.h264`, reconstitué depuis une capture par `video_extract.py`),
servi par la même brique que le MFD — le **GStreamer RTSP server**. La pipeline
décode puis ré-encode à cadence fixe (boucle propre) ; le serveur ne consomme du
CPU que lorsqu'un client est connecté. `--no-rtsp` le désactive.

## Démarrer

Deux modes, **exclusifs**, chacun sous son profil Compose — n'en lancer qu'un
(les deux ensemble se disputent les mêmes ports) :

```sh
docker compose --profile host   up --build   # Linux : réseau host, découverte OK
docker compose --profile bridge up --build   # macOS/Windows : ports mappés, sans découverte
```

Le mode **host** (`network_mode: host`) est nécessaire à la découverte : le
multicast (5800 et mDNS) ne franchit pas le pont NAT de Docker. Sous macOS /
Windows, « host » désigne la VM Linux de Docker Desktop et n'atteint pas le
réseau de la machine — d'où le mode **bridge** (RayDB, RRCE, RTSP, SFTP
joignables sur les ports publiés ; découverte indisponible).

> Un `docker compose up` **nu ne démarre rien** : aucun profil n'est actif, il
> faut choisir `--profile host` ou `--profile bridge`. Le flag `--profile` est
> **global** (avant `up`), pas après.

Sans le plugin Compose (`docker compose` absent), les mêmes modes en `docker run`
direct — voir « Sans Docker Compose » plus bas.

Sans Docker du tout, le simulateur tourne directement. Le SSH/SFTP est alors
servi par un **sshd Python** (dépendance optionnelle `paramiko`) pour une
émulation complète hors conteneur ; sans `paramiko`, ce service se désactive et
les autres tournent. Le port 22 étant privilégié, lancer en root — ou fixer
`MFD_SSH_PORT=2222`.

Pour la **recopie d'écran RTSP**, deux voies (la meilleure disponible est choisie
automatiquement) : **GStreamer + `python3-gi`** (fidélité maximale, même brique
que le MFD, à la demande), ou, à défaut, un repli **ffmpeg + mediamtx** — plus
léger à installer (`brew install ffmpeg mediamtx`), à la fidélité moindre
(bannière serveur différente, flux poussé en continu). Sans aucun des deux, RTSP
se désactive et les autres services tournent.

> ⚠ **L'app Raymarine a besoin de la voie GStreamer.** mediamtx retarde d'une
> **seconde entière** sa réponse au `SETUP` quand le `User-Agent` commence par
> `GStreamer` — un contournement câblé dans son binaire. Mesuré : 1001 ms avec
> `GStreamer/1.20.4`, 0,2 ms avec `LIVE555…`. L'app **RayControl** (LIVE555) n'en
> souffre pas, RayConnect (GStreamer) abandonne souvent avant le `PLAY`,
> d'où une vidéo qui ne s'accroche qu'une fois de temps en temps. Sous le repli,
> ce n'est pas réglable ; installer GStreamer résout le problème.

La sortie de mediamtx est reprise dans le journal du simulateur, préfixée
`mediamtx |`. `MFD_RTSP_LOG` en règle la verbosité (`info` par défaut : vie des
sessions ; `debug` : chaque requête RTSP).

```sh
pip install "zeroconf>=0.130" paramiko
sudo -E python3 -m mfdsim -v          # root pour écouter sur le port 22
# ou, sans privilèges :
MFD_SSH_PORT=2222 python3 -m mfdsim -v
```

Avec [uv](https://docs.astral.sh/uv/), le lanceur `run.py` porte un entête
**PEP 723** : les dépendances PyPI (zeroconf, paramiko, cryptography) sont
provisionnées dans un venv éphémère, sans `pip install` préalable :

```sh
uv run run.py -v                      # = python -m mfdsim, deps auto
MFD_SSH_PORT=2222 uv run run.py -v    # sans privilèges
```

**RTSP sous `uv run`** : GStreamer n'est pas accessible (le venv isolé d'uv ne
voit pas le `python3-gi` système, et `GstRtspServer` n'est de toute façon pas un
paquet PyPI). En revanche le **repli ffmpeg+mediamtx fonctionne** : ce sont des
binaires système trouvés sur le `PATH`, indépendants du venv — installe-les
(`brew install ffmpeg mediamtx`) et `uv run run.py` sert la vidéo. Sinon, RTSP
est simplement désactivé et le reste tourne.

### Sans Docker Compose

Le plugin `docker compose` n'est pas toujours installé (ex. CLI Docker via
Homebrew : `brew install docker-compose` l'ajoute). Le `docker-compose.yml`
n'est qu'un raccourci ; les deux modes s'obtiennent en `docker run` direct :

```sh
docker build -t raymarine-mfd .

# bridge (macOS/Windows) — équivaut au profil bridge
docker run -d --name raymarine-mfd \
  -p 2222:22 -p 23333:23333 -p 50000:50000 -p 8554:8554 \
  raymarine-mfd --no-mdns --no-5800

# host (Linux) — équivaut au profil host
docker run -d --name raymarine-mfd --network host raymarine-mfd
```

## Vérifier avec les outils du dépôt

```sh
./mfd_discover.py --timeout 8            # doit lister les 3 services
./raydb_client.py                        # découverte auto puis TUI
./raydb_client.py --nmea <ip>            # flux NMEA 0183 sur stdout
./raydb_client.py --udp <ip>             # ... diffusé en broadcast, stdout muet
socat -u UDP4-RECV:10110,reuseaddr -     # ... et écouté (nc s'attache au 1er émetteur)
./rrce_touch.py <ip> tap --frac 0.5 0.5  # toucher, journalisé par le conteneur
./rrce_touch.py <ip> key home            # bouton de façade, idem
./rrce_touch.py <ip> wheel 5             # 5 crans de molette
./mfd_remote.py <ip>                     # recopie d'écran RTSP (VLC)
ffplay -rtsp_transport tcp rtsp://<ip>:8554/RAYMARINEMFD   # ou en direct
sftp -P 2222 media_rw@127.0.0.1          # mot de passe : media_rw
```

Les entrées reçues apparaissent dans `docker logs`, décodées :

```
rrce  172.17.0.1:47876 DOWN doigt=0  X=16384 ( 25.0%)  Y=49151 ( 75.0%)
rrce  172.17.0.1:47876 KEY    HOME   0x76  enfoncé
rrce  172.17.0.1:47876 WHEEL  delta=  +25  cumul=   +25
rrce  172.17.0.1:47876 ????   en-tête 010009  charge utile deadbeef
```

## Ce que le simulateur reproduit volontairement

Deux pièges du MFD réel sont **conservés**, parce que c'est justement contre eux
qu'on veut tester un client :

- **L'IP annoncée dans les trames 5800 est `198.18.0.233`**, une adresse du
  backbone interne SeaTalkHS, non joignable. Un client correct se connecte à
  l'**IP source du datagramme**. `raydb_client.py` le fait ; un client naïf
  échouera ici comme sur le vrai appareil.
- **Le port annoncé pour `_raydb._tcp` est `49111`**, alors que RayDB écoute sur
  `23333`. Poser `MFD_RAYDB_MDNS_PORT=23333` pour annoncer le port utile.

Le trafic est en clair et non authentifié (hors SSH), comme sur l'appareil : le
canal RRCE accepte n'importe quelle connexion et injecte touchers, boutons et
molette sans validation.

**L'enrôlement de clé SSH est fonctionnel**, comme sur le MFD : la clé publique
reçue sur 8182 est ajoutée aux `authorized_keys` de `media_rw` (`authkeys.py`),
donc un client peut enchaîner sur le SFTP du port 22 avec sa clé privée sans
qu'aucune clé ait été provisionnée à l'avance — exactement le chemin
`8182 → authorized_keys_external_apps → 22` du `PlatformServicesDaemon`
(cf. « 5. protocole-messages-8182.md » §5). Le sshd Python (conteneur comme hors
conteneur) prend la clé en compte immédiatement ; l'OpenSSH de la Pi relit son
fichier à chaque authentification. Aucune approbation n'est demandée, là encore
comme l'appareil réel.

## Données de navigation

`sim.py` fait naviguer un bateau virtuel plutôt que de rejouer une capture : le
bateau avance en estime, le vent apparent est recalculé depuis le vent réel et
la vitesse, un courant de marée écarte la route de fond du cap, et la houle
alimente roulis/tangage. Les valeurs restent donc **cohérentes entre elles** —
`raydb_client.py --nmea` en tire des phrases qui se tiennent :

```
$IIMWV,60.5,T,14.0,N,A*3D     vent réel     60° / 14,0 nd   (allure « reaching »)
$IIMWV,40.8,R,18.7,N,A*3F     vent apparent 41° / 18,7 nd   (devant, plus fort)
$IIROT,2.5,A*21               giration 2,5°/min
```

Unités RayDB : angles en **radians**, vitesses en **m/s**, profondeurs en
**mètres**, `data/position` en chaîne `"lat,lon"`. Les 29 chemins `data/…`, un
sous-arbre `diag/mfd/<modèle> <série>/…` et le nom du bateau
(`Settings/Data/-/7/13/-/-/-/-`) sont servis, l'état courant (« retained »)
partant dès la souscription. Un abonnement peut porter un `#` en fin (sous-arbre)
**ou** au milieu du chemin (`diag/mfd/#/network/mac_address`, un segment).

### En navigation — position, cap, vitesse, allure

Le point de départ et la conduite du bateau se posent en ligne de commande, puis
se reprennent à chaud par l'API (ci-dessous) :

```bash
python3 -m mfdsim --position 43.12,5.93          # départ devant Toulon
python3 -m mfdsim --heading 118 --speed 6        # cap et vitesse visés
python3 -m mfdsim --allure "grand largue"        # d'où le vent réel à l'étrave
```

L'**allure** donne le TWA — c'est elle qui place le vent, pas l'inverse :

| Allure | TWA | Allure | TWA |
|---|---|---|---|
| `pres` | 45° | `largue` | 120° |
| `reaching` | 60° | `grand-largue` | 150° |
| `travers` | 90° | | |

Un angle en degrés fait aussi l'affaire (`--allure 75`), négatif à bâbord amure.
La **vitesse du vent suit celle du bateau** — le double, à peu près : c'est le
vent qui fait avancer, un voilier qui tient 7 nœuds n'en a pas 3 de brise. Régler
la vitesse règle donc le TWS avec, faute de quoi 15 nœuds sous 14 de vent
donneraient un vent apparent de dos… au près.

Les consignes (cap, vitesse, allure) ne sont pas des téléportations : le bateau
**abat** vers son nouveau cap à 6°/s au plus, accélère et change d'allure en
quelques secondes. `data/rot` et `data/rudder` racontent la manœuvre — un
demi-tour commandé se lit ~360°/min de giration et une vingtaine de degrés de
barre, contre ±13°/min et ~1° en tenue de route. `/state` publie côte à côte la
valeur instantanée et la consigne, si bien qu'un ordre reste lisible avant même
d'avoir pris effet.

### Au mouillage — `--anchor`, `POST /anchor`

`AnchorSim` (classe dérivée de `BoatSim`) remplace la navigation par un bateau
au mouillage : il évite autour d'une ancre fixe, puis dérape sur commande.

```bash
python3 -m mfdsim --anchor                            # ancre par défaut, évitage 30–40 m
python3 -m mfdsim --anchor 48.654286,-3.879216        # ancre choisie
python3 -m mfdsim --anchor --swing 25,35              # rayon d'évitage choisi
```

Le mouillage n'est plus figé au lancement : `POST /anchor` mouille en cours de
route, `POST /underway` relève l'ancre. La façade `Simulation` porte le bateau du
moment et le remplace à chaud ; RayDB ne voit qu'un objet stable, et le nouveau
bateau republie tout son état d'un bloc — le même lot d'UPDATE qu'à la
souscription, plutôt qu'un état à moitié périmé chez les clients. Le bateau de
navigation est **mis de côté**, pas détruit : appareiller retrouve ses consignes
de vitesse et d'allure sans avoir à les redonner. Le cap, lui, repart de celui où
l'ancre tenait le bateau — on quitte un mouillage sur l'étrave qu'on a.

```bash
curl -X POST localhost:8088/anchor                    # mouille ici, l'ancre par l'avant
curl -X POST 'localhost:8088/anchor?lat=48.65&lon=-3.88'
curl -X POST 'localhost:8088/underway?heading=310&speed=8'
```

L'évitage n'est pas une rotation imposée : le bateau se met **bout au vent**,
donc l'ancre par l'avant, et se place sous le vent d'elle au bout de sa chaîne.
C'est la bascule lente de la direction du vent — un tour complet en 20 min par
défaut — qui le promène autour du point de mouillage, tandis que l'embardée le
fait osciller de ±12°. Le rayon, lui, suit la **marée** : à longueur de chaîne
constante, le bateau s'écarte de son ancre quand l'eau baisse et s'en rapproche
quand elle monte, d'où l'oscillation entre les deux bornes. La même phase de
marée pilote la sonde, en opposition.
SOG et COG sont déduits du déplacement réellement parcouru, si bien qu'un client
NMEA voit un bateau tournant à ~0,4 nœud autour de sa position, étrave au vent.

Mesuré sur 20 min de simulation : rayon 30,0–40,0 m, rotation cumulée 366°,
SOG 0,09–0,72 nœud.

### API REST de pilotage — port 8088

La conduite à chaud passe par `control.py`, une petite API REST qui n'imite rien
du MFD réel : c'est une commande de simulateur, sur un port à part.

| Route | Mode | Effet |
|---|---|---|
| `GET /` | — | **page de pilotage** : l'état, rafraîchi chaque seconde, et les commandes en formulaires |
| `GET /help` | — | **page d'aide** : les endpoints, leurs paramètres et un `curl` prêt à copier ; `?format=json` pour la même table en JSON |
| `GET /state` | — | position, cap, route, vitesses, vent, sonde, consignes, et l'état du mouillage |
| `POST /anchor` | — | **mouille** — sur place, l'ancre par l'avant ; ou aux coordonnées `lat`/`lon` |
| `POST /underway` | — | **appareille**, et pose au passage `heading`, `speed`, `allure`, `amure` |
| `POST /position` | passage | téléporte le bateau — `lat`, `lon` |
| `POST /heading` | passage | change le cap visé — `heading` (° vrais) |
| `POST /speed` | passage | change la vitesse visée — `speed` (nœuds) ; le vent réel suit |
| `POST /sail` | passage | change l'allure, donc le TWA — `allure` (nom ou degrés), `amure` (`tribord`/`babord`) |
| `POST /drag` | mouillage | l'ancre décroche — `course` (° vrais, **au hasard** si omis), `speed` (nœuds, défaut 0,5) |

Les paramètres se passent en query string **ou** en corps JSON :

```bash
curl localhost:8088/state
curl -X POST 'localhost:8088/position?lat=48.35&lon=-4.90'
curl -X POST 'localhost:8088/heading?heading=310'
curl -X POST 'localhost:8088/speed?speed=9'            # ⇒ ~18 nd de vent réel
curl -X POST 'localhost:8088/sail?allure=grand+largue&amure=babord'
curl -X POST localhost:8088/sail -d '{"allure": -95}'  # 95° à bâbord, soit travers
curl -X POST localhost:8088/anchor                     # mouille ici
curl -X POST localhost:8088/drag                       # direction aléatoire, 0,5 nd
curl -X POST 'localhost:8088/drag?course=120&speed=0.8'
curl -X POST localhost:8088/underway                   # appareille
```

Au dérapage, le bateau s'éloigne en ligne droite et passe donc **au-delà** de
l'ancre : 0,5 nœud pendant 10 min = 154 m parcourus. `POST /anchor` sans
coordonnées mouille sur place sans à-coup ; avec des coordonnées, un bateau déjà
au mouillage rejoint son nouveau cercle d'évitage à vitesse bornée (1,5 nœud) au
lieu de s'y téléporter, tandis qu'un bateau en route s'y retrouve d'un bond —
c'est un changement de décor, pas une manœuvre.

Réponses en JSON : `400` sur paramètre manquant, non numérique, hors bornes ou
corps JSON invalide, `409` sur une commande étrangère au mode en cours (router
un bateau au mouillage, faire déraper une navigation), `404` sur route inconnue.
`--no-control` supprime l'API *et* les pages.

Les deux pages sont **autonomes** — aucune ressource externe, aucune dépendance.
`GET /` affiche l'état complet et n'expose que les commandes du mode en cours,
mouillage compris : on y mouille, dérape, remouille et appareille à la souris.
`GET /help` sert la même API au scripteur : chaque route avec ses paramètres, la
ligne `curl` correspondante — engendrée depuis la table de routage, donc
incapable de diverger — et un scénario complet à copier. Le `curl` porte l'hôte
tel qu'on a demandé la page, donc utilisable tel quel depuis une autre machine.

> La simulation n'avance que lorsqu'un **client RayDB est connecté** — elle est
> pilotée par les demandes de changements, pas par une horloge propre. Sans
> client, `/state` reste donc figé et une dérive demandée ne commence qu'à la
> connexion suivante. Le pas de temps est borné à 1 s pour qu'une longue pause
> ne se rattrape pas d'un bond.

## Configuration

Tout passe par l'environnement (voir `docker-compose.yml`) :

| Variable | Défaut | Effet |
|---|---|---|
| `MFD_MODEL` / `MFD_SERIAL` | `E70363` / `1234567` | identité, propagée aux TXT, chemins `diag/` et nom mDNS |
| `MFD_PRODUCT` / `MFD_FIRMWARE` | `AXIOM 7` / `4.11.13` | modèle annoncé, version |
| `MFD_IP` | auto-détection | IP annoncée en mDNS |
| `MFD_BOAT_NAME` | `MYBOAT` | nom du bateau, servi sous `Settings/Data/-/7/13/-/-/-/-` |
| `MFD_SSH_USER` / `MFD_SSH_PASSWORD` | `media_rw` / `media_rw` | compte SFTP |
| `MFD_AUTHORIZED_KEY` | — | clé publique autorisée (sinon monter `./keys:/keys:ro`) |
| `MFD_AUTHORIZED_KEYS_FILE` | `MFD_STATE_DIR/authorized_keys` | fichier où le service 8182 écrit les clés enrôlées (le sshd Python s'y abonne ; sur la Pi, poser le fichier de l'OpenSSH système `/etc/ssh/authorized_keys/media_rw`) |
| `MFD_SSH_PORT` | `22` | port du sshd Python (port haut si non-root hors conteneur) |
| `MFD_SFTP_ROOT` | `mfdsim/sftp/` (conteneur : `/data/media/0/UserData`) | racine (chroot) du sshd Python (repli `MFD_STATE_DIR/UserData` si inaccessible) |
| `MFD_SSH_HOST_KEY` | `MFD_STATE_DIR/ssh_host_rsa_key` (conteneur : `/etc/ssh/keys/…`) | clé d'hôte RSA du sshd Python, persistée pour une empreinte stable |
| `MFD_STATE_DIR` | `~/.mfdsim` | état persistant hors conteneur (clé d'hôte SSH, racine SFTP de repli) |
| `MFD_BEACON_PERIOD` | `0.72` | cadence du beacon 5800, en secondes |
| `MFD_CONTROL_PORT` | `8088` | port de l'API REST et de la page de pilotage (`--no-control` pour les couper) |
| `MFD_RTSP_VIDEO` | `video/mfd_screen.h264` | vidéo servie en RTSP (absente → mire de test) |
| `MFD_RTSP_FPS` | `20` | cadence du flux RTSP (celle du MFD réel) |

Changer `MFD_MODEL`/`MFD_SERIAL` suffit à obtenir un MFD cohérent de bout en bout.

## Limites connues

- **macOS / Windows** : `network_mode: host` désigne la VM Linux de Docker
  Desktop, pas la machine. Le multicast n'atteint donc pas le réseau du Mac —
  pour tester la découverte, lancer le simulateur **directement**
  (`python3 -m mfdsim`), ce qui fonctionne (validé avec `mfd_discover.py`).
- **Taille de l'image (~1 Go)** : le serveur RTSP impose GStreamer, que seul
  Debian *package* (Alpine n'a pas `gst-rtsp-server`). L'image est donc basée
  sur Debian et alourdie par la pile GStreamer complète. Sans besoin de vidéo,
  `--no-rtsp` évite d'ouvrir le port 8554 — mais GStreamer reste dans l'image.
- **Bannière SSH** : le MFD réel annonce `SSH-2.0-OpenSSH_6.8` ; le sshd Python
  (paramiko) expose sa propre bannière `SSH-2.0-paramiko_…`. En revanche le
  module réactive `ssh-rsa` (RSA/SHA-1), l'algorithme de clé publique du MFD
  (`_enable_legacy_rsa_sha1` dans `sshd.py`), donc les clients calibrés sur le
  vrai appareil (rm_ssh.py) s'authentifient sans option supplémentaire.
- **Authentification SSH** : le MFD réel est en **clé seule**
  (`PasswordAuthentication no` dans son firmware, cf. « 4. ssh-mfd-analyse.md »
  §4). Le simulateur autorise **en plus** le mot de passe, par commodité de banc
  d'essai (`check_auth_password` dans `sshd.py`). Pour coller à l'appareil, ne
  fournir qu'une clé via `MFD_AUTHORIZED_KEY` et ignorer le mot de passe.
- **Nom mDNS déjà pris** : le nom d'instance porte l'identifiant et la version
  du modèle (`RayDBServer on E70363 1234567 4_11_13`). Un **second simulateur**
  sur le même réseau se voit donc refuser l'enregistrement, et le simulateur
  dégrade proprement (avertissement, autres services actifs) au lieu de
  s'arrêter. `--mdns-force` passe outre en gardant le nom. Le port 5353 n'y est
  pour rien : le simulateur s'y lie sans peine à côté du `mDNSResponder` de
  macOS, les deux écoutant le même socket multicast.
- **RTSP fidèle au contenu, pas au bitstream** : la vidéo est le vrai écran d'un
  Axiom, servi par un vrai GStreamer RTSP server, mais la pipeline **ré-encode**
  (pour une boucle propre) — les SPS/PPS diffèrent donc de ceux du MFD réel. Sans
  effet pour un client, qui lit le SDP annoncé.
- Le beacon 5800 émet des **valeurs de télémétrie plausibles mais non
  sémantiques** : la signification des sous-types `0x08XX` reste non résolue
  (cf. « 1. protocole-udp5800.md » §8), on ne peut donc pas la simuler fidèlement.
