// Service worker: guarda los estáticos y la última versión de cada pantalla,
// para que la app se abra y se pueda leer aunque no haya conexión (fuera de
// casa sin Tailscale, NAS apagado...). Lo que apuntes sin red se queda en el
// móvil y lo envía app.js cuando vuelve.
const VERSION = '{{ version }}';
const STATIC_CACHE = 'finanzas-estaticos-' + VERSION;
const PAGES_CACHE = 'finanzas-pantallas-' + VERSION;
const ASSETS = [
  '/static/css/app.css?v=' + VERSION,
  '/static/js/app.js?v=' + VERSION,
  '/static/js/htmx.min.js?v=' + VERSION,
  '/static/js/chart.umd.min.js?v=' + VERSION,
  '/static/icons/icon-192.png',
  '/offline',
];
const VIGENTES = [STATIC_CACHE, PAGES_CACHE];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(STATIC_CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => VIGENTES.indexOf(n) < 0).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

function avisoSinConexion(fecha) {
  let cuando = '';
  try {
    const d = new Date(fecha);
    cuando = ' · datos del ' + d.toLocaleDateString('es-ES') + ' a las ' +
             d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' });
  } catch (e) { /* sin fecha */ }
  return '<div class="sin-conexion" role="status">Sin conexión' + cuando + '</div>';
}

// Devuelve la copia guardada de una pantalla, con un aviso arriba.
async function pantallaGuardada(req) {
  const guardada = await caches.match(req, { ignoreSearch: false }) ||
                   await caches.match(req.url.split('?')[0]);
  if (!guardada) return caches.match('/offline');
  const html = await guardada.text();
  const conAviso = html.replace('</body>', avisoSinConexion(guardada.headers.get('date')) + '</body>');
  return new Response(conAviso, {
    status: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;                 // apuntar, borrar... siempre a la red
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/static/')) {        // estáticos: de la caché, que no cambian
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copia = res.clone();
        caches.open(STATIC_CACHE).then((c) => c.put(req, copia));
        return res;
      }))
    );
    return;
  }

  // Las pantallas: siempre de la red (los números tienen que estar al día) y se
  // guarda una copia. Si no hay red, se enseña la última copia con su aviso.
  if (req.mode === 'navigate' || req.headers.get('HX-Request')) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const guardable = res.ok && !res.headers.get('X-Sin-Copia') &&
                            (res.headers.get('Content-Type') || '').indexOf('text/html') >= 0;
          if (guardable) {
            const copia = res.clone();
            caches.open(PAGES_CACHE).then((c) => c.put(req, copia));
          }
          return res;
        })
        .catch(() => pantallaGuardada(req))
    );
  }
});
