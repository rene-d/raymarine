/* MFDView — client SSE et rendu des instruments.
 *
 * La passerelle (raydb_bridge.py) pousse les valeurs RayDB **dans leurs unités
 * d'origine** : angles en radians, vitesses en m/s, position en degrés décimaux.
 * Toute la conversion (nœuds, degrés, degrés-minutes) est faite ici.
 *
 * Trois événements SSE : « snapshot » (état complet, à chaque connexion),
 * « delta » (les clés qui ont changé, 5 Hz) et « status » (état de la liaison
 * MFD, qui est distinct de l'état de la liaison navigateur→passerelle).
 */
'use strict';

const MS_TO_KN = 1.943844;
const STALE_S = 5;      // au-delà : valeur grisée
const GONE_S = 30;      // au-delà : valeur effacée

// Table brute : clé → libellé + unité SI (l'ordre est celui de l'affichage).
const RAW_ROWS = [
  ['boat', 'Settings/…/7/13/…', ''],
  ['sog', 'sog', 'm/s'], ['cog', 'cog', 'rad'], ['hdg', 'heading/true', 'rad'],
  ['hdgMag', 'heading/magnetic', 'rad'], ['variation', 'bearing/variation', 'rad'],
  ['tws', 'wind/speed/true', 'm/s'], ['twa', 'wind/direction/true', 'rad'],
  ['aws', 'wind/speed/apparent', 'm/s'], ['awa', 'wind/direction/apparent', 'rad'],
  ['depth', 'depth', 'm'],
  ['lat', 'position (lat)', '°'], ['lon', 'position (lon)', '°'],
  ['posAcc', 'position/accuracy', 'm'],
];

const values = {};      // clé → valeur SI
const seen = {};        // clé → horodatage local de réception (ms)
const $ = (id) => document.getElementById(id);

// Historique des graphes défilants : clé → [{t, v}] sur les HISTORY_S dernières
// secondes. Les valeurs y sont déjà en unité d'affichage (degrés, mètres).
const HISTORY_S = 30;
const history = { sog: [], depth: [] };

/* ------------------------------------------------------------- réception -- */
function merge(delta) {
  const now = Date.now();
  for (const [k, v] of Object.entries(delta)) {
    values[k] = v;
    seen[k] = now;
    if (history[k]) push(k, now, v);
  }
}

/** Ajoute un point à l'historique et oublie ce qui dépasse la fenêtre. */
function push(key, t, v) {
  const h = history[key];
  h.push({ t, v: key === 'sog' ? v * MS_TO_KN : v });
  const cutoff = t - HISTORY_S * 1000;
  while (h.length && h[0].t < cutoff) h.shift();
}

function reset() {
  for (const k of Object.keys(values)) { delete values[k]; delete seen[k]; }
}

function setStatus(s) {
  $('status').textContent = s.text;
  $('dot').classList.toggle('on', !!s.connected);
}

/* Deux transports pour les mêmes messages : l'application native (Tauri) parle
 * au MFD elle-même et pousse ses événements dans la page ; sinon la page est
 * servie par raydb_bridge.py et lit son flux SSE. Le reste du fichier ignore
 * lequel des deux l'alimente. */
let stream = null;   // l'EventSource, en mode passerelle

function connect() {
  if (window.__TAURI__) {                       // application native
    const { listen, emit } = window.__TAURI__.event;
    setStatus({ text: 'démarrage…', connected: false });
    // Les événements Tauri ne se rejouent pas et la page démarre après le
    // thread MFD : on réclame l'état une fois les écoutes en place — `listen`
    // est asynchrone, la réponse arriverait sinon avant elles. Côté passerelle,
    // c'est l'événement « snapshot » qui joue ce rôle.
    Promise.all([
      listen('delta', (e) => merge(e.payload)),
      listen('status', (e) => setStatus(e.payload)),
    ]).then(() => emit('ready'));
    return;
  }
  stream = new EventSource('api/stream');       // passerelle HTTP
  stream.addEventListener('snapshot', (e) => { reset(); merge(JSON.parse(e.data)); });
  stream.addEventListener('delta', (e) => merge(JSON.parse(e.data)));
  stream.addEventListener('status', (e) => setStatus(JSON.parse(e.data)));
  stream.addEventListener('error', () =>
    setStatus({ text: 'passerelle injoignable — reconnexion…', connected: false }));
}

/* -------------------------------------------------------------- calculs -- */
const deg = (rad) => rad * 180 / Math.PI;
const norm360 = (d) => ((d % 360) + 360) % 360;

/** Âge d'une valeur, en secondes ; +∞ si jamais reçue. */
function age(key) {
  return seen[key] === undefined ? Infinity : (Date.now() - seen[key]) / 1000;
}

/** Écrit une valeur numérique dans un <span>, en gérant absence et péremption. */
function put(id, key, text) {
  const el = $(id);
  const a = age(key);
  el.textContent = (a > GONE_S || text === null) ? '—' : text;
  el.classList.toggle('stale', a > STALE_S);
}

/** Angle relatif à l'étrave → { deg 0..180, côté }. */
function relative(rad) {
  const d = norm360(deg(rad));
  return d > 180 ? { a: 360 - d, side: 'bâbord' } : { a: d, side: 'tribord' };
}

/** Degrés décimaux → « 43° 17,726′ N » (format usuel des GPS marins). */
function dm(v, positive, negative, width) {
  const hemi = v >= 0 ? positive : negative;
  const abs = Math.abs(v);
  const d = Math.floor(abs);
  const m = (abs - d) * 60;
  return `${String(d).padStart(width, '0')}° ${m.toFixed(3).padStart(6, '0')}′ ${hemi}`;
}

/* -------------------------------------------------------------- polaire -- */
/* Fichier `.pol` : un CSV à point-virgule. Première ligne, les vents (TWS) en
 * nœuds ; première colonne, les angles de vent (TWA) en degrés ; le reste, la
 * vitesse cible du bateau, en nœuds. Un zéro dans la table n'est pas une
 * vitesse : c'est « pas de donnée » — trop près du vent, ou plein arrière.
 *
 * La polaire est relue au démarrage depuis le stockage du navigateur : à bord,
 * on ne la recharge pas à chaque lancement. Elle tient en quelques ko. */
let polar = null;          // { name, tws: [], twa: [], target: [[]] }

function parsePolar(text) {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length < 2) return null;
  // Le point-virgule est l'usage des `.pol`, mais un vrai CSV à virgules se
  // rencontre. Avec le point-virgule, une virgule ne peut être qu'un séparateur
  // décimal — celui des tableurs français —, qu'on ramène au point : sans ça,
  // « 6,5 » devient NaN et la polaire est silencieusement pleine de zéros.
  const semicolons = lines[0].includes(';');
  const rows = lines.map((line) => (semicolons
    ? line.split(';').map((cell) => cell.replace(',', '.'))
    : line.split(',')));

  // La première case porte un intitulé (« TWA\TWS ») : elle ne se lit pas.
  const tws = rows[0].slice(1).map(Number);
  if (!tws.length || !tws.every(Number.isFinite)) return null;

  const twa = [], target = [];
  for (const row of rows.slice(1)) {
    const angle = Number(row[0]);
    if (!Number.isFinite(angle)) continue;
    twa.push(angle);
    target.push(tws.map((_, i) => Number(row[i + 1]) || 0));
  }
  return twa.length ? { tws, twa, target } : null;
}

/** Encadrement d'une valeur dans une table croissante → [avant, après, part].
 *  Hors table, on rend le bord : une polaire ne s'extrapole pas. */
function bracket(values, x) {
  const last = values.length - 1;
  if (!(x > values[0])) return [0, 0, 0];
  if (x >= values[last]) return [last, last, 0];
  let i = 0;
  while (values[i + 1] < x) i += 1;
  const part = (x - values[i]) / (values[i + 1] - values[i]);
  // Pile sur une case : on la rend seule, pour que l'affichage des cases lues
  // ne montre pas un écart là où il n'y a pas eu d'interpolation.
  if (part === 0) return [i, i, 0];
  if (part === 1) return [i + 1, i + 1, 0];
  return [i, i + 1, part];
}

/** Vitesse cible pour un angle et une force, interpolée sur les deux axes.
 *  Rend aussi les cases qui ont servi : c'est ce qui permet de juger la
 *  lecture, une cible tirée d'un large écart valant moins qu'une case pleine.
 *  La vitesse est 0 là où la table ne dit rien — l'affichage la traite comme
 *  absente. */
function targetSpeed(twaDeg, twsKn) {
  if (!polar) return null;
  const [a0, a1, fa] = bracket(polar.twa, twaDeg);
  const [w0, w1, fw] = bracket(polar.tws, twsKn);
  const at = (a) => polar.target[a][w0] + (polar.target[a][w1] - polar.target[a][w0]) * fw;
  const lo = at(a0);
  return { speed: lo + (at(a1) - lo) * fa, twa: [a0, a1], tws: [w0, w1] };
}

/** « 90 » ou « 90–95 » : les bornes d'une interpolation, ou la case seule. */
function span(values, [lo, hi]) {
  return lo === hi ? `${values[lo]}` : `${values[lo]}–${values[hi]}`;
}

/* Mesures du bord. Une polaire se corrige en naviguant : on retient, par case
 * de la table, la meilleure vitesse tenue sur un palier stable. « Stable » est
 * le mot important — une vitesse relevée dans une empannage ou une risée qui
 * tourne ne dit rien du bateau. D'où la fenêtre glissante ci-dessous : il faut
 * WINDOW_S de vent et d'angle constants pour qu'un point compte.
 *
 * On garde le meilleur et non la moyenne : une polaire est une promesse de ce
 * que le bateau *peut* faire, pas de ce qu'il a fait en moyenne, barreur
 * distrait compris. */
const WINDOW_S = 10;        // durée d'un palier
const TWA_SPREAD = 5;       // ° d'écart tolérés sur le palier
const TWS_SPREAD = 1.5;     // nd d'écart tolérés sur le palier

let measures = {};          // « ligne,colonne » → meilleure vitesse mesurée
const palier = [];          // le palier en cours de constitution

/** Indice de la case la plus proche dans une table croissante. */
function nearest(values, x) {
  let best = 0;
  for (let i = 1; i < values.length; i += 1) {
    if (Math.abs(values[i] - x) < Math.abs(values[best] - x)) best = i;
  }
  return best;
}

/** Empile un point et rend le palier s'il est constitué et stable, sinon null. */
function stable(twaDeg, twsKn, sogKn) {
  const now = Date.now();
  palier.push({ t: now, twa: twaDeg, tws: twsKn, sog: sogKn });
  while (palier.length && palier[0].t < now - WINDOW_S * 1000) palier.shift();
  // Le plus vieux point gardé a forcément moins de WINDOW_S : exiger la durée
  // pleine ne se produirait qu'au millième de seconde près, et jamais avec la
  // gigue du rendu. On demande donc neuf dixièmes de la fenêtre.
  if (now - palier[0].t < WINDOW_S * 900) return null;

  const spread = (k) => Math.max(...palier.map((s) => s[k]))
                      - Math.min(...palier.map((s) => s[k]));
  if (spread('twa') > TWA_SPREAD || spread('tws') > TWS_SPREAD) return null;

  const mean = (k) => palier.reduce((sum, s) => sum + s[k], 0) / palier.length;
  return { twa: mean('twa'), tws: mean('tws'), sog: mean('sog') };
}

/** Range un palier dans sa case, s'il y bat le record. */
function keepBest(m) {
  const key = `${nearest(polar.twa, m.twa)},${nearest(polar.tws, m.tws)}`;
  if (measures[key] >= m.sog) return;
  measures[key] = m.sog;
  try { localStorage.setItem('polarMeasures', JSON.stringify(measures)); } catch { /* plein */ }
  showPolarName();
}

function showPolarName() {
  const n = Object.keys(measures).length;
  $('polar-name').textContent = !polar ? 'aucun fichier'
    : n ? `${polar.name} · ${n} ${n > 1 ? 'cases mesurées' : 'case mesurée'}`
        : polar.name;
  $('polar-export').disabled = !polar || !n;
}

function usePolar(p) {
  polar = p;
  showPolarName();
}

function loadPolar(file) {
  const reader = new FileReader();
  const failed = () => { $('polar-name').textContent = `${file.name} : illisible`; };
  reader.onerror = failed;
  reader.onload = () => {
    const parsed = parsePolar(String(reader.result));
    if (!parsed) { failed(); return; }
    parsed.name = file.name;
    try { localStorage.setItem('polar', JSON.stringify(parsed)); } catch { /* plein */ }
    usePolar(parsed);
  };
  reader.readAsText(file);
}

/** La polaire relevée des mesures, au format du fichier d'entrée.
 *
 * Chaque case garde la **meilleure** des deux valeurs : on ne rabaisse jamais
 * le fichier. Une case mesurée sous sa valeur ne prouve pas que le bateau ne
 * sait pas faire mieux — seulement qu'il ne l'a pas fait ce jour-là, sous cette
 * mer, avec ce réglage. Les cases jamais visitées sortent telles quelles.
 */
function polarCsv() {
  const rows = polar.twa.map((angle, i) => [
    angle.toFixed(1),
    ...polar.tws.map((_, j) => Math.max(polar.target[i][j], measures[`${i},${j}`] ?? 0).toFixed(1)),
  ].join(';'));
  return [['TWA\\TWS', ...polar.tws].join(';'), ...rows].join('\n') + '\n';
}

/** Sort le fichier de l'app — feuille de partage sur iPhone, téléchargement
 *  ailleurs. Sans plugin natif, comme pour la lecture. */
async function exportPolar() {
  const name = `${polar.name.replace(/\.pol$/i, '')}-mesure.pol`;
  const file = new File([polarCsv()], name, { type: 'text/csv' });

  // Sur iPhone, un téléchargement n'aboutit nulle part que l'utilisateur puisse
  // ouvrir : la feuille de partage (« Enregistrer dans Fichiers », Mail,
  // AirDrop) est la seule sortie utile.
  if (navigator.canShare?.({ files: [file] })) {
    try {
      await navigator.share({ files: [file], title: name });
      return;
    } catch (e) {
      if (e.name === 'AbortError') return;    // annulé : ne pas insister
    }
  }
  const url = URL.createObjectURL(file);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ------------------------------------------------------ pièces tournantes - */
/* Les angles sont « déroulés » avant interpolation : sans cela, passer de 359°
 * à 1° ferait faire un tour complet à l'aiguille dans le mauvais sens. */
const spinners = {
  'twa-needle': { cur: null, target: 0, tag: 'twa-tag' },
  'awa-needle': { cur: null, target: 0, tag: 'awa-tag' },
  'cog-ray': { cur: null, target: 0, tag: null },      // route sur le fond, sans étiquette
  // La rose graduée elle-même : ses libellés sont redressés un par un.
  'ticks': { cur: null, target: 0, tag: null, upright: true },
};

/** Les <text> de la graduation, gardés sous la main : ils sont contre-tournés à
 *  chaque image et il serait dommage de les rechercher à chaque fois. */
let tickLabels = [];

function aim(id, degrees) {
  const n = spinners[id];
  if (n.cur === null) { n.cur = degrees; }
  const delta = ((degrees - n.cur + 540) % 360) - 180;   // écart dans [-180, 180[
  n.target = n.cur + delta;
}

function animate() {
  for (const [id, n] of Object.entries(spinners)) {
    if (n.cur === null) continue;
    n.cur += (n.target - n.cur) * 0.18;                  // lissage exponentiel
    const el = $(id);
    el.setAttribute('transform', `rotate(${n.cur.toFixed(2)})`);
    // La rose tourne, mais on lit ses chiffres sans pencher la tête : chacun
    // est contre-tourné autour de son propre point d'ancrage.
    if (n.upright) {
      const back = (-n.cur).toFixed(2);
      for (const t of tickLabels) {
        t.setAttribute('transform',
          `rotate(${back}, ${t.getAttribute('x')}, ${t.getAttribute('y')})`);
      }
      continue;
    }
    if (!n.tag) continue;
    // Le texte suit l'aiguille mais doit rester lisible : on le contre-tourne
    // autour de son propre point d'ancrage.
    const tag = $(n.tag);
    tag.setAttribute('transform',
      `rotate(${(-n.cur).toFixed(2)}, 0, ${tag.getAttribute('y')})`);
  }
  if (!document.hidden) requestAnimationFrame(animate);
}

/* ------------------------------------------------- graphes défilants ----- */
/* Le temps mappe la largeur (0 = il y a 30 s, 100 = maintenant) : la courbe
 * défile d'elle-même vers la gauche à chaque rendu, et un flux interrompu se
 * voit — la ligne se décolle du bord droit au lieu de rester plate. */
const SPARK_TOP = 3, SPARK_BOTTOM = 23;

function spark(id, key, { invert = false, minSpan = 1 } = {}) {
  const h = history[key];
  const el = $(id);
  if (h.length < 2) { el.setAttribute('d', ''); return; }

  let lo = Infinity, hi = -Infinity;
  for (const p of h) { if (p.v < lo) lo = p.v; if (p.v > hi) hi = p.v; }
  if (hi - lo < minSpan) {                    // évite d'amplifier le bruit
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }

  const now = Date.now();
  const span = SPARK_BOTTOM - SPARK_TOP;
  const d = h.map((p, i) => {
    const x = 100 * (1 - (now - p.t) / (HISTORY_S * 1000));
    const frac = (p.v - lo) / (hi - lo);
    const y = invert ? SPARK_TOP + span * frac : SPARK_BOTTOM - span * frac;
    return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  el.setAttribute('d', d);
}

/* --------------------------------------------------------------- rendu --- */
function render() {
  const v = values;

  // La rose porte des caps absolus : on la tourne de −cap pour amener le cap
  // suivi en haut, sous l'étrave. Les aiguilles ne bougent pas de leur angle à
  // l'étrave — c'est la graduation qui défile dessous, comme la rose mobile
  // d'un compas, et elles s'y lisent du coup en absolu.
  const hdgKnown = v.hdg !== undefined && age('hdg') <= GONE_S;
  if (hdgKnown) aim('ticks', norm360(-deg(v.hdg)));
  // Sans cap, la rose reste nord en haut : elle ne se lit plus en absolu, mais
  // elle garde sa raison d'être — mesurer l'écart des aiguilles à l'étrave. On
  // l'estompe plutôt que de la retirer.
  $('ticks').classList.toggle('stale', !hdgKnown || age('hdg') > STALE_S);

  // Vent — angles relatifs à l'étrave, comme les publie le MFD.
  for (const [key, spanA, spanS, needle] of
       [['twa', 'twa', 'twa-side', 'twa-needle'], ['awa', 'awa', 'awa-side', 'awa-needle']]) {
    const known = v[key] !== undefined && age(key) <= GONE_S;
    const r = known ? relative(v[key]) : null;
    put(spanA, key, r ? r.a.toFixed(0) : null);
    $(spanS).textContent = r ? r.side : '';
    if (known) aim(needle, norm360(deg(v[key])));
    // Une aiguille figée sur une vieille valeur ment : on l'estompe puis on la
    // retire, comme la valeur chiffrée qu'elle double.
    $(needle).classList.toggle('stale', age(key) > STALE_S);
    $(needle).classList.toggle('gone', !known);
  }
  put('tws', 'tws', v.tws === undefined ? null : (v.tws * MS_TO_KN).toFixed(1));
  put('aws', 'aws', v.aws === undefined ? null : (v.aws * MS_TO_KN).toFixed(1));

  // Route sur le fond, ramenée à l'étrave comme le reste de la rose : la
  // demi-droite s'écarte de la ligne de foi de l'angle de dérive (vent,
  // courant). Il faut donc le cap ; sans lui, la demi-droite disparaît, faute
  // de savoir de quoi elle s'écarte.
  const cogKnown = v.cog !== undefined && v.hdg !== undefined
    && age('cog') <= GONE_S && age('hdg') <= GONE_S;
  if (cogKnown) aim('cog-ray', norm360(deg(v.cog - v.hdg)));
  $('cog-ray').classList.toggle('stale', Math.max(age('cog'), age('hdg')) > STALE_S);
  $('cog-ray').classList.toggle('gone', !cogKnown);

  // Vitesse et sonde
  put('sog', 'sog', v.sog === undefined ? null : (v.sog * MS_TO_KN).toFixed(1));
  // Les deux caps sont en tête de la rose : ils chiffrent l'écart que le dessin
  // montre. Le cap magnétique n'est plus affiché — il reste dans la table des
  // valeurs brutes, avec la déclinaison.
  for (const [id, key] of [['rose-hdg', 'hdg'], ['rose-cog', 'cog']]) {
    put(id, key, v[key] === undefined ? null
      : String(Math.round(norm360(deg(v[key]))) % 360).padStart(3, '0'));
  }
  put('depth', 'depth', v.depth === undefined ? null : v.depth.toFixed(1));

  // Vitesse cible : ce que la polaire promet pour le vent du moment. L'angle
  // est ramené à 0…180 — une polaire est symétrique, elle ignore le bord.
  const windKnown = v.twa !== undefined && v.tws !== undefined
    && age('twa') <= GONE_S && age('tws') <= GONE_S;
  const cible = windKnown ? targetSpeed(relative(v.twa).a, v.tws * MS_TO_KN) : null;
  $('target').textContent = cible && cible.speed ? cible.speed.toFixed(1) : '—';
  $('target').classList.toggle('stale', Math.max(age('twa'), age('tws')) > STALE_S);
  // Les cases lues, même quand la cible est vide : elles disent alors pourquoi
  // (l'angle est hors de la partie renseignée de la table).
  // Deux groupes insécables (cf. .polar-cells span) : la place est comptée, la
  // ligne peut passer entre TWA et TWS mais jamais au milieu d'un intervalle.
  // Le contenu est fait de nombres relus du fichier, rien d'autre.
  $('polar-range').innerHTML = cible
    ? `<span>TWA ${span(polar.twa, cible.twa)}°</span> ·`
      + ` <span>TWS ${span(polar.tws, cible.tws)} nd</span>`
    : '';
  // Le rendement n'a de sens que si les deux vitesses sont fraîches : comparer
  // une vitesse d'il y a une minute à une cible d'il y a une seconde ne dit
  // rien de la conduite du bateau.
  const sogKnown = v.sog !== undefined && age('sog') <= GONE_S;
  $('perf').textContent = cible && cible.speed && sogKnown
    ? `${Math.round(100 * v.sog * MS_TO_KN / cible.speed)} % de la cible` : '';

  // Relevé du bord : un palier stable nourrit sa case dans la table. On exige
  // des valeurs *fraîches*, pas seulement présentes — mesurer avec un vent
  // d'il y a vingt secondes ne mesure rien. La moindre lacune rompt le palier.
  const measurable = polar && [ 'twa', 'tws', 'sog' ].every((k) => age(k) <= STALE_S);
  if (measurable) {
    const held = stable(relative(v.twa).a, v.tws * MS_TO_KN, v.sog * MS_TO_KN);
    if (held) keepBest(held);
  } else {
    palier.length = 0;
  }
  spark('sog-spark', 'sog', { minSpan: 1 });                      // au moins 1 nd
  spark('depth-spark', 'depth', { invert: true, minSpan: 2 });    // au moins 2 m

  // Position
  const hasPos = v.lat !== undefined && v.lon !== undefined;
  put('lat', 'lat', hasPos ? dm(v.lat, 'N', 'S', 2) : null);
  put('lon', 'lon', hasPos ? dm(v.lon, 'E', 'W', 3) : null);
  $('pos-dec').textContent = hasPos ? `${v.lat.toFixed(6)}, ${v.lon.toFixed(6)}` : '—';
  $('pos-acc').textContent = v.posAcc === undefined ? '' : `± ${v.posAcc.toFixed(1)} m`;

  // Nom du bateau : valeur de configuration, poussée une fois à l'abonnement.
  // Elle ne périme donc pas — pas de put(), qui la griserait au bout de 5 s.
  $('boat').textContent = v.boat === undefined ? '' : v.boat;

  if ($('raw-details').open) renderRaw();
  $('clock').textContent = new Date().toLocaleTimeString('fr-FR');
}

function renderRaw() {
  $('raw-body').innerHTML = RAW_ROWS.map(([key, label, unit]) => {
    const v = values[key];
    const a = age(key);
    const shown = v === undefined ? '—'
      : (typeof v === 'number' ? v.toFixed(4) : v) + ' ' + unit;
    return `<tr><td>${label}</td><td>${shown}</td>` +
           `<td>${a === Infinity ? '—' : a.toFixed(1) + ' s'}</td></tr>`;
  }).join('');
}

/* --------------------------------------------------------------- rose ---- */
/** Graduation : trait tous les 10°, trait long + libellé tous les 30°.
 *
 * La rose porte des **caps absolus** et tourne avec le cap vrai (voir
 * `render`) : ce qui passe sous l'étrave, en haut, est le cap suivi. Les
 * aiguilles, elles, ne bougent pas de leur angle relatif à l'étrave — elles se
 * lisent donc en absolu sur la graduation, comme sur un compas à rose mobile.
 */
const CARDINALS = { 0: 'N', 90: 'E', 180: 'S', 270: 'O' };

function buildTicks() {
  const parts = [];
  for (let a = 0; a < 360; a += 10) {
    const major = a % 30 === 0;
    const rad = (a - 90) * Math.PI / 180;         // 0° de la rose = haut de l'écran
    const [c, s] = [Math.cos(rad), Math.sin(rad)];
    const r0 = major ? 82 : 89;
    parts.push(`<line class="${major ? 'major' : ''}" x1="${(c * r0).toFixed(1)}"`
      + ` y1="${(s * r0).toFixed(1)}" x2="${(c * 96).toFixed(1)}" y2="${(s * 96).toFixed(1)}"/>`);
    if (major) {
      const cardinal = CARDINALS[a];
      // Rayon 104 et non 106 : contre-tourné, un libellé de trois chiffres
      // garde sa boîte horizontale et touchait le bord du cadre en haut.
      parts.push(`<text class="${cardinal ? 'cardinal' : ''}"`
        + ` x="${(c * 104).toFixed(1)}" y="${(s * 104).toFixed(1)}">${cardinal ?? a}</text>`);
    }
  }
  $('ticks').innerHTML = parts.join('');
  tickLabels = [...$('ticks').querySelectorAll('text')];
}

/* ---------------------------------------------------------- démarrage ---- */
buildTicks();

$('polar-file').addEventListener('change', (e) => {
  const [file] = e.target.files;
  if (file) loadPolar(file);
  e.target.value = '';       // rejouable : recharger le même fichier corrigé
});
$('polar-export').addEventListener('click', exportPolar);

// La polaire de la dernière fois, et les mesures accumulées depuis. Les mesures
// se chargent d'abord : c'est `usePolar` qui les compte à l'écran.
try { measures = JSON.parse(localStorage.getItem('polarMeasures')) || {}; } catch { measures = {}; }
try { usePolar(JSON.parse(localStorage.getItem('polar'))); } catch { usePolar(null); }

// La carte (map.js) décide seule si elle a lieu d'être : elle ne s'active que
// dans l'app native sur macOS, avec un jeu de tuiles sous la main, et c'est
// elle qui révèle sa vignette.
initMap();

connect();
render();
setInterval(render, 250);
requestAnimationFrame(animate);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) requestAnimationFrame(animate);   // relance après veille
});

if ('serviceWorker' in navigator && !window.__TAURI__) {
  // Échoue silencieusement hors contexte sécurisé (http://IP-du-Mac) : la page
  // fonctionne quand même, seul le mode hors-ligne est perdu. Cf. README.
  navigator.serviceWorker.register('sw.js').catch(() => {});
}
