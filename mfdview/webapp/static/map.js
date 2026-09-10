/* map.js — la carte marine et le bateau dessus.
 *
 * Ne s'active que dans l'application native sur macOS : les tuiles viennent du
 * MBTiles lu en Rust et servies par le protocole « tiles: ». Ailleurs — dans un
 * navigateur derrière la passerelle Python, ou sur iOS — `map_available` rend
 * null, l'onglet reste caché et Leaflet n'est même pas chargé.
 *
 * Nord toujours en haut : c'est le seul mode de Leaflet, il n'y a rien à
 * désactiver. Le bateau, lui, tourne : sa flèche porte le cap vrai.
 *
 * La position arrive par le même événement `delta` que les instruments ; on
 * s'y abonne séparément plutôt que de lire les variables de `app.js`, pour que
 * les deux vues restent indépendantes. Seul `dm()` est emprunté à `app.js` : un
 * formateur pur, sans état — c'est le partage de l'*état* que l'on évite ici,
 * pas celui d'une mise en forme dont un second exemplaire divergerait.
 */

const MAP_URL = 'tiles://localhost/{z}/{x}/{y}';   // forme macOS du protocole

// Trace : on ne pose un point qu'au-delà de ce déplacement, sinon le mouillage
// en empilerait cinq par seconde au même endroit. Deux mètres laissent voir
// l'évitage (un cercle de 30 à 40 m de rayon) sans engraisser la polyligne.
const TRACK_MIN_MOVE = 2;      // mètres
const TRACK_MAX_POINTS = 5000; // au-delà, on oublie le plus ancien

let map = null;             // l'objet Leaflet, une fois la carte activée
let boat = null;            // le marqueur, créé à la première position
let track = null;           // la polyligne rouge du sillage
let points = [];            // ses sommets, du plus ancien au plus récent
let follow = true;          // la carte suit-elle le bateau ?
let last = null;            // dernière position connue [lat, lon]
let hover = null;           // point survolé (L.LatLng), ou null hors de la carte
let maxNative = 0;          // zoom au-delà duquel Leaflet agrandit les tuiles
let flash = null;           // confirmation affichée à la place du relevé
let flashTimer = null;

/* Charge une feuille de style ou un script, et attend qu'il soit prêt. */
function load(url) {
  return new Promise((resolve, reject) => {
    const css = url.endsWith('.css');
    const el = document.createElement(css ? 'link' : 'script');
    if (css) { el.rel = 'stylesheet'; el.href = url; } else { el.src = url; }
    el.onload = resolve;
    el.onerror = () => reject(new Error(url));
    document.head.appendChild(el);
  });
}

/* Le bateau : une flèche orientée au cap, dessinée en SVG dans un divIcon —
   pas d'image à charger, et la rotation se fait en CSS. */
function boatIcon() {
  return L.divIcon({
    className: 'boat-marker',
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    html: '<svg viewBox="-15 -15 30 30" width="30" height="30" aria-hidden="true">'
        + '<path d="M0,-13 L8,11 L0,6 L-8,11 Z" /></svg>',
  });
}

/* Applique la position et le cap reçus. Le cap vrai oriente la flèche ; à
   défaut (pas de compas) la route sur le fond fait l'affaire, mais au mouillage
   les deux diffèrent franchement — le bateau évite étrave au vent alors que le
   COG part dans tous les sens. */
/* Le sillage : là où le bateau est passé. Au mouillage il dessine la rosace de
   l'évitage, ce qui dit d'un coup d'œil si l'ancre tient ou si elle chasse. */
function trace(pos) {
  const prev = points[points.length - 1];
  if (prev && map.distance(prev, pos) < TRACK_MIN_MOVE) return;
  points.push(pos);
  if (points.length > TRACK_MAX_POINTS) points.shift();
  if (track) track.setLatLngs(points);
  else track = L.polyline(points, { className: 'track', weight: 2, interactive: false }).addTo(map);
}

/* Le niveau de zoom, en haut à droite. Au-delà du zoom du jeu, Leaflet agrandit
   la dernière tuile disponible : ce qu'on gagne n'est plus du détail de carte,
   mais des pixels étirés — le relevé le dit, sans quoi les deux régimes sont
   indiscernables à l'écran. */
function showZoom() {
  const z = map.getZoom();
  document.getElementById('map-zoom').textContent =
    z > maxNative ? `z${z} · agrandi` : `z${z}`;
}

/* Distance du bateau à un point, en unités du bord : le mètre tant qu'on est à
   l'échelle du mouillage, le mille nautique au-delà. */
function range(from, to) {
  const m = map.distance(from, to);
  return m < 1852 ? `${Math.round(m)} m` : `${(m / 1852).toFixed(2)} NM`;
}

/* Relèvement vrai du point depuis le bateau, « 000 » au nord — comme la flèche
   du bateau, qui porte le cap vrai. Formule du cap initial sur la sphère : sur
   les distances d'une vignette de carte, l'écart avec l'ellipsoïde est de deux
   ordres de grandeur sous le degré affiché. */
function bearing(from, to) {
  const rad = Math.PI / 180;
  const lat1 = from[0] * rad, lat2 = to[0] * rad, dLon = (to[1] - from[1]) * rad;
  const angle = Math.atan2(
    Math.sin(dLon) * Math.cos(lat2),
    Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon));
  // 359,7 arrondi donne 360 : le modulo le ramène à 000, comme dans `app.js`.
  const deg = Math.round((angle / rad + 360) % 360) % 360;
  return `${String(deg).padStart(3, '0')}°`;
}

/* Un mot à la place du relevé, le temps qu'on le lise, puis retour au relevé. */
function say(message) {
  flash = message;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { flash = null; readout(); }, 1500);
  readout();
}

/* Copie le point courant — celui qu'on survole, ou le bateau — en degrés
   décimaux à six décimales : la forme de `#pos-dec`, celle que recollent les
   autres outils (cartes en ligne, traceurs, tableurs). Onze centimètres de
   résolution, très en dessous de ce qu'un GPS de bord sait tenir.

   `map.js` ne tourne que dans l'app native, dont la page est servie depuis
   `tauri://localhost` : contexte sécurisé, `navigator.clipboard` présent, et le
   clic ou la frappe fournissent le geste que WebKit exige. Un refus reste
   possible — on le dit plutôt que de laisser croire à une copie. */
function copyPoint() {
  const p = hover || (last && { lat: last[0], lng: last[1] });
  if (!p) return;
  const text = `${p.lat.toFixed(6)}, ${p.lng.toFixed(6)}`;
  navigator.clipboard.writeText(text)
    .then(() => say(`${text} copié`))
    .catch(() => say('copie refusée'));
}

/* Le relevé du bas : le point survolé, et ce qui le sépare du bateau. Sans
   survol c'est le bateau qu'on décrit, de sorte que le bandeau ne soit jamais
   vide et que son rôle se comprenne sans avoir à promener la souris.

   Quatre décimales de minute, là où la carte de position se contente de trois :
   au zoom maximal un pixel vaut dix centimètres, et 0,001′ (1,85 m) figerait le
   dernier chiffre sur une vingtaine de pixels. */
function readout() {
  const el = document.getElementById('map-readout');
  if (flash) { el.innerHTML = `<span>${flash}</span>`; return; }
  const p = hover || (last && { lat: last[0], lng: last[1] });
  if (!p) { el.textContent = ''; return; }
  const parts = [`${dm(p.lat, 'N', 'S', 2, 4)} ${dm(p.lng, 'E', 'W', 3, 4)}`];
  // La distance n'a de sens qu'entre deux points distincts : sans survol, le
  // point *est* le bateau.
  if (hover && last) parts.push(`${range(last, p)} · ${bearing(last, [p.lat, p.lng])}`);
  el.innerHTML = parts.map((t) => `<span>${t}</span>`).join('');
}

function place(lat, lon, headingRad) {
  last = [lat, lon];
  trace(last);
  if (!boat) {
    boat = L.marker(last, { icon: boatIcon(), keyboard: false }).addTo(map);
    map.setView(last, Math.min(15, map.getMaxZoom()));
  } else {
    boat.setLatLng(last);
  }
  if (headingRad !== undefined) {
    const svg = boat.getElement()?.firstElementChild;
    if (svg) svg.style.transform = `rotate(${headingRad * 180 / Math.PI}deg)`;
  }
  if (follow) map.panTo(last, { animate: false });
  // Une position qui arrive ne doit pas effacer le point qu'on est en train de
  // pointer : `readout` repart de `hover`, qui a la priorité.
  readout();
}

function setFollow(on) {
  follow = on;
  document.getElementById('recenter').classList.toggle('on', on);
}

function recenter() {
  setFollow(true);
  if (last) map.setView(last, map.getZoom(), { animate: true });
}

/* Construit la carte une fois Leaflet chargé. `info` vient du Rust :
   { maxZoom, bounds: [ouest, sud, est, nord] | null }. */
function build(info) {
  const bounds = info.bounds
    && L.latLngBounds([info.bounds[1], info.bounds[0]], [info.bounds[3], info.bounds[2]]);

  map = L.map('map', {
    zoomControl: true,
    // L'attribution est dans le titre de la vignette : le bandeau de Leaflet
    // mangerait une ligne sur une carte haute de trois cents pixels.
    attributionControl: false,
    // Au-delà du zoom du jeu, Leaflet agrandit la dernière tuile disponible
    // plutôt que d'afficher du vide : utile ici, où le zoom 18 est partiel.
    maxZoom: info.maxZoom + 2,
    center: bounds ? bounds.getCenter() : [48.65, -3.88],
    zoom: 8,
  });

  L.tileLayer(MAP_URL, {
    minZoom: 1,
    maxZoom: info.maxZoom + 2,
    maxNativeZoom: info.maxZoom,
    tileSize: 256,
    bounds,                       // rien n'est demandé hors de l'emprise
  }).addTo(map);

  // Déplacer la carte à la main, c'est vouloir regarder ailleurs : le suivi
  // s'arrête, et seul le bouton le rétablit.
  map.on('dragstart', () => setFollow(false));
  document.getElementById('recenter').addEventListener('click', recenter);
  setFollow(true);

  // Le relevé. `mousemove` tire une soixantaine de fois par seconde pendant un
  // déplacement : on n'y fait qu'une mise en forme et une écriture de texte.
  maxNative = info.maxZoom;
  map.on('zoomend', showZoom);
  map.on('mousemove', (e) => { hover = e.latlng; readout(); });
  // Sortir de la carte — ou passer sur les commandes de Leaflet — rend le
  // relevé au bateau.
  map.on('mouseout', () => { hover = null; readout(); });
  showZoom();
  readout();

  // La copie. ⌥-clic plutôt que le clic nu : sur une carte, le clic sert à
  // viser et à faire glisser, on ne lui accroche pas un effet de bord que rien
  // n'annonce. Le curseur ne bouge pas, donc c'est bien le point visé qui part
  // — aller chercher un bouton l'aurait perdu en chemin.
  map.on('click', (e) => { if (e.originalEvent.altKey) copyPoint(); });
  // Et sans la souris : « c » copie ce que le bandeau montre — le point
  // survolé, ou le bateau. Sans modificateur, pour laisser ⌘C à la sélection ;
  // et pas depuis un champ de saisie, où la frappe appartient au champ.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'c' || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
    if (e.target instanceof HTMLInputElement) return;
    copyPoint();
  });
}

/* Point d'entrée, appelé par `app.js` au démarrage. */
async function initMap() {           // eslint-disable-line no-unused-vars
  if (!window.__TAURI__) return;     // navigateur : pas de source de tuiles
  const { invoke } = window.__TAURI__.core;
  const { listen, emit } = window.__TAURI__.event;

  const info = await invoke('map_available').catch(() => null);
  if (!info) return;                 // iOS, ou aucun fichier de tuiles trouvé

  await Promise.all([load('vendor/leaflet.css'), load('vendor/leaflet.js')]);
  // La vignette est cachée jusqu'ici : Leaflet ne saurait pas mesurer un
  // conteneur absent de la mise en page, il faut la montrer avant de bâtir.
  document.getElementById('map-card').hidden = false;
  build(info);

  await listen('delta', (e) => {
    const d = e.payload;
    if (d.lat !== undefined && d.lon !== undefined) place(d.lat, d.lon, d.hdg ?? d.cog);
    else if (last && (d.hdg !== undefined || d.cog !== undefined)) {
      place(last[0], last[1], d.hdg ?? d.cog);
    }
  });
  // Le Rust rejoue l'état courant à chaque « ready » : celui d'`app.js` est
  // peut-être déjà passé, on redemande pour ne pas attendre la prochaine
  // position — au mouillage, elle peut tarder.
  emit('ready');
}
