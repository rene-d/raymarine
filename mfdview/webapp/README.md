# webapp — instruments RayDB dans un navigateur

Affiche en direct, sur un téléphone, les données de navigation d'un MFD
Raymarine : vent réel et apparent sur une rose des vents, SOG, COG, cap,
position. Même source que `raydb_client.py` — le bus RayDB, TCP 23333.

    MFD ──TCP 23333──▶ raydb_bridge.py ──HTTP + SSE──▶ navigateur du téléphone

## Pourquoi une passerelle

Une page web **ne peut pas** parler au MFD directement, et il n'y a pas de
contournement : un navigateur n'ouvre pas de socket TCP brute (un WebSocket
n'est pas du TCP brut — il exige un handshake HTTP que le MFD ne fait pas) et
n'a aucune API mDNS/UDP. `raydb_bridge.py` fait donc les deux à sa place :

| | qui le fait | comment |
|---|---|---|
| découverte du MFD | passerelle | `raydb_client.discover_mfd` (mDNS puis beacon 5800) |
| bus RayDB | passerelle | HELLO + SUBSCRIBE `data/#`, décodage typé |
| transport vers la page | HTTP | Server-Sent Events (`/api/stream`) |
| affichage | navigateur | HTML/CSS/SVG, sans framework ni build |

**SSE plutôt que WebSocket** : le flux est unidirectionnel (le MFD pousse), SSE
tient en bibliothèque standard et `EventSource` se reconnecte tout seul quand le
téléphone sort de veille. La passerelle n'a **aucune dépendance** (`zeroconf`
n'est utile qu'en mode `auto`).

## Lancer

Avec le MFD réel :

```sh
./webapp/raydb_bridge.py 192.168.42.1     # IP du MFD
./webapp/raydb_bridge.py auto             # découverte mDNS puis multicast 5800
```

Sans MFD, avec le simulateur du dépôt (deux terminaux) :

```sh
cd mfdsim && uv run run.py --no-rtsp --no-ssh --no-8182   # sert RayDB sur 23333
./webapp/raydb_bridge.py                                  # 127.0.0.1 par défaut
```

La passerelle affiche les deux URL au démarrage :

```
MFD      : 127.0.0.1
page     : http://127.0.0.1:8080/
téléphone: http://192.168.1.24:8080/   (même Wi-Fi)
```

Options : `--http-port`, `--bind`, `-v`.

## Points d'entrée HTTP

| route | contenu |
|---|---|
| `/` | l'application (`static/`) |
| `/api/state` | instantané JSON : `{status, values}` |
| `/api/stream` | SSE : `snapshot` (état complet), `delta` (5 Hz), `status` |

Le nom du bateau vit hors de l'arbre de navigation
(`Settings/Data/-/7/13/-/-/-/-`) : la passerelle pose un **second abonnement**
pour lui. C'est une valeur « retained », poussée une fois à la souscription et
jamais rafraîchie — elle ne périme donc pas à l'affichage.

Les valeurs circulent **dans les unités de RayDB** — angles en radians, vitesses
en m/s, position en degrés décimaux. Nœuds, degrés et degrés-minutes sont
calculés dans `app.js` : un seul jeu d'unités sur le fil, une seule conversion.

Les angles de vent sont ceux du MFD, donc **relatifs à l'étrave** (cf.
`raydb_client.py --nmea`) : les flèches ne bougent pas de cet angle, étrave en haut.

C'est la **graduation** qui tourne, du cap vrai — une rose mobile de compas. Le
cap suivi passe donc sous l'étrave, et les flèches, sans avoir bougé, se lisent
en absolu sur la graduation. Sans cap connu, la rose reste nord en haut et
s'estompe : elle ne mesure plus que des écarts à l'étrave, et le dit.

Deux demi-droites partent du bateau : l'**axe de la coque**, vertical, et la
**route sur le fond**. L'angle entre elles est la dérive — vent et courant
mêlés, le MFD ne sait pas les séparer. Elle n'est pas chiffrée : les deux caps
qui la bornent sont en tête de la carte, cap vrai à gauche, COG à droite.

## L'affichage

Pas de framework : une rose des vents est une flèche qu'on fait tourner, pas un
graphique. Chart.js ou Plotly ne savent pas la dessiner et pèseraient 300 ko
pour rien ; les tuiles numériques sont du CSS.

- `index.html` structure, `style.css` thème (sombre par défaut, clair suivi de
  l'OS), `app.js` réception SSE + rendu.
- `map.js` et `vendor/leaflet.*` : la carte marine, **inertes ici**. Elles ne
  s'activent que dans l'application native sur macOS, seule à savoir servir des
  tuiles (`mfdview/src-tauri/src/mbtiles.rs`) ; derrière la passerelle, la vignette
  n'apparaît pas et Leaflet n'est même pas chargé. Voir `mfdview/README.md`.
- Bleu = vent réel, orange = vent apparent : palette validée pour le daltonisme,
  et l'information n'est **jamais** portée par la couleur seule — chaque flèche
  est étiquetée (« R », « A »), reprise en légende sous la rose, et les deux
  flèches occupent des couronnes différentes.
- Une valeur que le MFD ne rafraîchit plus est grisée au bout de 5 s puis
  effacée (`—`) au bout de 30 s, flèche comprise : un instrument figé ment.
- « Valeurs brutes RayDB » déplie la table des valeurs SI avec leur âge — vue
  accessible, et débogage.

## La polaire

La tuile « Polaire » charge un fichier `.pol` — CSV à point-virgule, les vents
(TWS) en première ligne, les angles (TWA) en première colonne, la vitesse cible
du bateau dans la table. La cible du moment est interpolée sur les deux axes
(bilinéaire) à partir du vent réel, et comparée au SOG : « 94 % de la cible ».

Un zéro dans la table n'est pas une vitesse, c'est un trou — trop près du vent,
ou plein arrière ; l'affichage le montre comme absent (`—`). Hors table, la
lecture est bornée au bord : une polaire ne s'extrapole pas.

Le fichier passe par un `<input type="file">`, sans plugin natif : la WKWebView
de l'iPhone ouvre elle-même le sélecteur de fichiers du système (vérifié au
simulateur). Le fichier n'est pas relu du disque à chaque fois — la table est
gardée dans le stockage local du navigateur, pour qu'un lancement à bord
retrouve sa polaire sans manipulation.

### La corriger en naviguant

Une polaire de constructeur ment toujours un peu : pas ce bateau-là, pas cette
carène, pas ces voiles après trois saisons. L'app la corrige en marchant.

Toutes les 250 ms, si l'angle, la force et la vitesse sont **fraîches** (moins
de 5 s), le point rejoint une fenêtre glissante de 10 s. La fenêtre fait un
« palier » quand elle est pleine **et** que le vent n'a pas bougé — moins de 5°
d'écart d'angle, moins de 1,5 nd d'écart de force. Le mot compte : une vitesse
relevée dans une empannage ou une risée qui tourne ne dit rien du bateau. Une
donnée qui manque rompt le palier et vide la fenêtre.

Un palier constitué donne un point — moyennes de l'angle, de la force et de la
vitesse — rangé dans la case la plus proche de la table. On garde le
**meilleur** de chaque case, pas la moyenne : une polaire promet ce que le
bateau *peut* faire, barreur distrait non compris. L'en-tête de la tuile compte
les cases mesurées, et les mesures survivent aux lancements.

Le second bouton exporte le résultat au format d'entrée, sous
`<nom>-mesure.pol`. Chaque case y garde la meilleure des deux valeurs, celle du
fichier ou celle mesurée : **on ne rabaisse jamais le fichier**, une
contre-performance ne prouvant que la journée. Les cases jamais visitées
sortent telles quelles.

Sur iPhone, l'export passe par la feuille de partage (« Enregistrer dans
Fichiers », Mail, AirDrop) : un téléchargement ordinaire n'y aboutirait nulle
part d'ouvrable. Ailleurs, c'est un téléchargement. Comme pour la lecture,
aucun plugin natif.

## Sur le téléphone

Ouvrir l'URL `téléphone:` affichée au démarrage (même Wi-Fi que la passerelle).
« Ajouter à l'écran d'accueil » donne une icône et le plein écran.

**Le mode hors-ligne (service worker) ne s'active qu'en contexte sécurisé** :
`https://` ou `http://localhost`. Sur `http://192.168.x.x`, l'enregistrement du
service worker échoue silencieusement — la page fonctionne normalement, elle ne
survit simplement pas à la perte de la passerelle. Pour l'obtenir : servir en
HTTPS (certificat auto-signé à accepter une fois), ou empaqueter en natif.

## Suites possibles

- **Vraiment autonome** (sans machine à bord) : c'est `mfdview/`, l'application
  Tauri. Elle embarque **ce même `static/`** (`frontendDist` pointe dessus) et
  refait le TCP en Rust ; `app.js` bascule tout seul de SSE aux événements Tauri
  quand il tourne dedans. Le TCP est fait (palier 1), le mDNS reste à porter.
- **À demeure sur le bateau** : la passerelle tourne sans peine sur un
  Raspberry Pi ; il suffit d'un service systemd et le téléphone n'a plus qu'à
  ouvrir une URL.
- Ajouter profondeur, STW, roulis : une ligne dans `NAV_PATHS` et une tuile.
