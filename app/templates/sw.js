// Service worker mínimo: sirve los estáticos desde caché y enseña una página
// decente cuando no hay conexión (fuera de casa sin Tailscale, NAS apagado...).
const VERSION = '{{ version }}';
const STATIC_CACHE = 'finanzas-estaticos-' + VERSION;
const ASSETS = [
  '/static/css/app.css?v=' + VERSION,
  '/static/js/app.js?v=' + VERSION,
  '/static/js/htmx.min.js?v=' + VERSION,
  '/static/js/chart.umd.min.js?v=' + VERSION,
  '/static/icons/icon-192.png',
  '/offline',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(STATIC_CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n !== STATIC_CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(STATIC_CACHE).then((c) => c.put(req, copy));
        return res;
      }))
    );
    return;
  }

  // Las páginas y los datos siempre de la red: los números tienen que estar al día.
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match('/offline')));
  }
});
