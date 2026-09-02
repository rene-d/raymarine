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
raynmea -knots                       # les vitesses RayDB sont déjà en nœuds
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

## Organisation

| fichier | rôle |
| --- | --- |
| `raydb.go` | protocole RayDB : requêtes, lecture des trames, décodage des valeurs |
| `mdns.go` | boucle de découverte mDNS (une requête par interface) et cible courante |
| `client.go` | session TCP : HELLO, abonnements, reconnexion |
| `nmea.go` | pont RayDB → phrases NMEA 0183 (portage de `Bridge`) |
| `sinks.go` | sorties fichier/stdout et diffusion UDP |
| `tui.go` | l'écran des six valeurs |
| `main.go` | options, câblage, fil d'événements |

Le fil est unique : la connexion et la découverte y poussent updates et notes, et
la boucle principale les rend dans l'ordre où ils se sont produits — c'est la
même architecture que `raydb_client.py`, où une note (« connecté à … ») se lit
avant les valeurs que la connexion vient de rendre possibles.

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
