/* Service worker : coquille hors-ligne.
 *
 * Réseau d'abord, cache en secours — on veut toujours la dernière version des
 * fichiers quand la passerelle répond, et une page qui s'ouvre quand même
 * lorsqu'elle ne répond pas (téléphone hors de portée du bateau).
 *
 * /api/ n'est JAMAIS mis en cache ni intercepté : /api/stream est un flux SSE
 * qui ne se termine pas, le passer par cache.put() le bloquerait.
 */
const CACHE = 'mfdview-v2';
const SHELL = ['.', 'index.html', 'style.css', 'app.js', 'map.js', 'manifest.webmanifest', 'icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  // Les tuiles de la carte ne passent pas par ici : elles viennent du protocole
  // « tiles: » servi par l'app native (sur Windows, il prendrait la forme
  // http://tiles.localhost/…, donc interceptable). Le jeu pèse trois
  // gigaoctets — le cache du service worker n'a rien à en faire.
  if (url.origin !== self.location.origin) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request).then((hit) => hit || caches.match('index.html')))
  );
});
