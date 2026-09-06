# raynmea — passerelle RayDB → NMEA 0183 (Go)

Ce que fait `raydb_client.py --udp`, en un binaire statique — une seule
dépendance, `github.com/hashicorp/mdns` (qui amène `miekg/dns` et `x/net`) :

- **découverte mDNS permanente** du MFD (`_raydb._tcp.local.`) — pas un
  « une fois au démarrage » : une requête toutes les dix secondes (`-mdns-interval`)
  sur chaque interface porteuse d'une IPv4, si bien qu'un MFD qui apparaît plus
  tard ou qui change d'adresse (bail DHCP) est suivi ;
- **connexion RayDB** (TCP 23333), HELLO et abonnement à `data/#`, avec
  **reconnexion** sur fermeture, sur silence (30 s) et sur changement d'adresse ;
- **traduction en NMEA 0183** — la même table que le client Python (RMC, GGA,
  GLL, GST, VTG, HDT, HDM, HDG, MWV, VWR, VWT, MWD, DPT, DBT, DBS, VHW, ROT,
  RSA, XDR, VDR), portée phrase pour phrase ;
- **diffusion UDP par défaut** vers `127.0.0.1:10110` — sans option, c'est ce que
  fait le programme ; ailleurs ou en broadcast avec `-udp-to`, `-no-udp` pour s'en
  passer ;
- **phrases** sur stdout ou dans un fichier ;
- **journal des UPDATE** sur stdout ou dans un fichier ;
- **TUI minimaliste** : SOG, COG, GPS, profondeur, TWS, TWA.

Le port annoncé en mDNS est ignoré : le MFD publie 49111 alors que RayDB écoute
sur **23333** (divergence constatée sur le MFD réel, cf. `mfdsim/mfdsim/mdns.py`).

## Compilation

```sh
go build            # ./raynmea
go test ./...       # trames, décodage, phrases NMEA, adoption d'une annonce
GOOS=linux GOARCH=arm64 go build   # pour le Raspberry Pi du bord
```

Le binaire reste statique et se cross-compile sans rien de plus ; la dépendance
pèse ~2 Mo (3,7 → 5,9 Mo sur darwin/arm64).

Le `.justfile` réunit ces commandes et les suivantes — `just` seul en donne la
liste, `just build`, `just test`, `just install` (dans `~/.local/bin`), `just cross`
(linux/arm64), `just app` (l'app macOS), `just mfd` et `just listen` pour l'essai
sans MFD.

## Usage

```sh
raynmea                              # diffusion UDP 127.0.0.1:10110, MFD trouvé en mDNS
raynmea 192.168.42.1                 # IP imposée (pas de découverte)
raynmea -udp-to 192.168.1.42         # diffuser ailleurs (port 10110 par défaut)
raynmea -udp-to 255.255.255.255      # en broadcast sur tout le réseau
raynmea -udp-to 10.0.0.5:10110 -udp-to 10.0.0.6   # plusieurs destinations
raynmea -nmea phrases.log            # diffuser *et* garder la trace
raynmea -nmea -                      # ... ou les voir passer sur stdout
raynmea -log updates.log             # journal des UPDATE dans un fichier
raynmea -tui                         # écran de veille + diffusion
raynmea -no-udp -nmea -              # pas de diffusion du tout
raynmea -path data/sog -path 'Settings/#'   # abonnements choisis
```

La **diffusion est le défaut** : sans option, les phrases partent vers
`127.0.0.1:10110`, et stdout reste muet (le suivi — découverte, connexion,
abonnements, erreurs — va toujours sur **stderr**, jamais dans le flux qu'on
redirige). `-udp-to` remplace la destination et se répète ; `-no-udp` coupe la
diffusion, et alors les phrases retombent sur stdout faute d'autre sortie.

Les autres sorties se **cumulent** avec elle : `-nmea` et `-log` prennent un
chemin de fichier, ou `-` pour stdout (l'un des deux seulement, et pas avec
`-tui` qui occupe l'écran).

Pour contrôler la diffusion : `socat -u UDP4-RECV:10110,reuseaddr -`.

## Essai sans MFD

Le simulateur du dépôt suffit, mDNS comprise :

```sh
cd ../mfdsim && MFD_RAYDB_MDNS_PORT=23333 python3 -m mfdsim \
    --no-ssh --no-rtsp --no-8182 --no-control
cd ../raynmea && go run . -tui
```

(`MFD_RAYDB_MDNS_PORT` n'est pas nécessaire — `raynmea` ignore le port annoncé —
mais il rend le simulateur conforme au chemin « nominal ».)

## L'app macOS (barre de menus)

`raynmea.app` est la même passerelle, posée dans la barre de menus : la vitesse
fond dans la barre, le bateau et le MFD en tête de menu, les valeurs de la TUI
en dessous, et de quoi régler la diffusion sans repasser par la ligne de
commande.

```sh
just app          # construit et signe raynmea.app
just app-run      # ... et la lance
just app-install  # la copie dans ~/Applications
just app-log      # suit son journal de suivi
```

La première ligne dit à qui l'on parle — `MYBOAT — 192.168.42.1`, les compteurs
en dessous. Le nom du bateau n'est pas une donnée de navigation mais un réglage
du MFD (`Settings/Data/-/7/13/-/-/-/-`, cf. `gateway.PathBoatName`) : l'app le
souscrit nommément, en plus de `data/#`. S'il manque, la ligne se contente de
l'adresse.

Ce que le menu règle — la diffusion UDP et ses destinations (« Ajouter… »
demande l'adresse), le MFD (découverte mDNS ou IP imposée), et
l'enregistrement. Chaque réglage est **gardé** (les NSUserDefaults de l'app) et
**relance le moteur** : la session RayDB se refait, ce qui vaut mieux que de
reconfigurer une connexion en cours. « Start at Login » et « Quit » viennent de
menuet.

Deux écritures dans `~/Library/Logs/raynmea/` :

- `suivi.log`, **toujours** : découverte, connexion, erreurs. C'est la boîte
  noire de l'app, plafonnée à 8 Mo et roulée en `.1`.
- l'**enregistrement**, armé depuis le menu : `raynmea-20260906-235500.log`, un
  fichier par séance, où le suivi, les valeurs reçues (`UPDATE`) et les phrases
  émises (`NMEA`) se suivent **dans l'ordre** — de quoi relire une navigation
  sans recoller trois journaux. L'en-tête du fichier porte la date, le MFD, les
  destinations et les abonnements, les lignes n'ayant que l'heure.

  Le nom est fixé quand on arme : un autre réglage relance le moteur, la séance
  se poursuit dans le même fichier (et y note son nouvel en-tête). Un second
  item plafonne l'enregistrement à 8 Mo, armé par défaut ; désarmé, le fichier
  va jusqu'où on l'arrête — compter ~600 ko par minute.

Les **icônes** sont dessinées en SVG dans `cmd/raynmea-menu/icons/` et
regénérées par `just icons` (rsvg-convert, iconutil) : `menubar.pdf`, vectoriel
et monochrome — menuet le pose en *template*, macOS le tinte donc selon le
thème —, et `raynmea.icns` pour le Finder et les Réglages. Les deux fichiers
produits sont versionnés, `just app` n'a rien à installer.

Trois choses à savoir :

- l'app est un **binaire à part** (`cmd/raynmea-menu`), parce que la barre de
  menus passe par cgo et AppKit (`github.com/caseymrm/menuet/v2`) : rien de tout
  cela n'entre dans `raynmea`, ni donc dans `just cross` ;
- menuet **exige un bundle** — lancer le binaire seul ne donne rien de bon, d'où
  `just app` ; hors `.app`, le programme le dit et s'arrête ;
- depuis macOS 15, mDNS et l'UDP local demandent l'autorisation **« réseau
  local »**. Le `.app` a la sienne, distincte de celle du terminal. Signée ad hoc
  (le défaut), chaque recompilation est une app *nouvelle* pour macOS, qui
  redemande l'autorisation ; avec une vraie identité, elle tient :

  ```sh
  security find-identity -v -p codesigning
  RAYNMEA_CODESIGN_ID="Apple Development: …" just app
  ```

`just app-reset` oublie les options gardées (l'app arrêtée). Un `kill -HUP`
relance le moteur sans quitter l'app — mais ne **relit** pas les options :
menuet mémorise en Go ce qu'il a écrit dans les NSUserDefaults, si bien qu'un
`defaults write` extérieur n'est vu qu'au lancement suivant.

## Organisation

| fichier | rôle |
| --- | --- |
| `main.go` | le programme en ligne de commande : options et choix de l'affichage |
| `internal/gateway/gateway.go` | le moteur : `Config`, `Observer`, `Run` — le câblage et le fil d'événements |
| `internal/gateway/raydb.go` | protocole RayDB : requêtes, lecture des trames, décodage des valeurs |
| `internal/gateway/mdns.go` | boucle de découverte mDNS (une requête par interface) et cible courante |
| `internal/gateway/client.go` | session TCP : HELLO, abonnements, reconnexion |
| `internal/gateway/nmea.go` | pont RayDB → phrases NMEA 0183 (portage de `Bridge`) |
| `internal/gateway/sinks.go` | sorties fichier/stdout (avec rotation) et diffusion UDP |
| `internal/gateway/dash.go` | l'état affichable : les six valeurs, converties et mises en forme |
| `internal/gateway/tui.go` | l'écran des six valeurs |
| `cmd/raynmea-menu/` | l'app macOS de la barre de menus (darwin, cgo) |

Le moteur ne connaît aucun affichage : il rend ce qu'il voit à un `Observer`.
La TUI en est un, l'app de la barre de menus en est un autre, et tous deux
lisent le **même** `Dashboard` — deux façons de montrer les mêmes six valeurs,
pas deux modèles qui divergent.

Le fil est unique : la connexion et la découverte y poussent updates et notes, et
la boucle principale les rend dans l'ordre où ils se sont produits — c'est la
même architecture que `raydb_client.py`, où une note (« connecté à … ») se lit
avant les valeurs que la connexion vient de rendre possibles.

Une seule dépendance dans `raynmea` lui-même ; la seconde du module
(`menuet/v2`) ne sert qu'à `cmd/raynmea-menu` et ne part jamais sur le Pi.

## Ce qui n'y est pas

- Le **repli sur le beacon multicast 224.0.0.1:5800** du client Python : ici la
  découverte est mDNS seule (l'IP en argument reste le recours si mDNS ne passe pas).
- L'**écoute passive** entre deux requêtes : `mdns.Query` referme ses sockets, si
  bien qu'une annonce spontanée est vue au tour suivant, pas à l'instant même. Un
  changement d'adresse du MFD coûte donc jusqu'à `-mdns-interval` avant la
  reconnexion — mesuré à 7 s contre `mfdsim`.
- Le **rejeu de capture** (`--replay`), les rendus `--dump` et `--json`, et le
  journal JSON : `raydb_client.py` les fait mieux, tshark sous la main.
- Aucune touche dans la TUI : Ctrl-C pour quitter.
