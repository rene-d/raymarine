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
 * les deux vues restent indépendantes.
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
