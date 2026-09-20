// Crea un proxy host en Nginx Proxy Manager usando su propio codigo (v2.15, ESM),
// para que el .conf salga igual que si se creara desde la interfaz.
//
// Se copia dentro del contenedor de NPM y se ejecuta alli:
//   docker cp npm_proxy.mjs nginx-proxy-manager:/tmp/
//   docker exec -e DOMINIO=finanzas.example.com -e CERT_ID=1 \
//     -e DESTINO=finanzas -e PUERTO=8000 nginx-proxy-manager node /tmp/npm_proxy.mjs
import ProxyHost from "/app/models/proxy_host.js";
import nginx from "/app/internal/nginx.js";

const DOMINIO = process.env.DOMINIO;
const CERT_ID = Number(process.env.CERT_ID || 0);   // id del certificado en NPM
const DESTINO = process.env.DESTINO || "finanzas"; // host al que llega NPM
const PUERTO = Number(process.env.PUERTO || 8000);

if (!DOMINIO) { console.error("Falta la variable DOMINIO"); process.exit(2); }
const existentes = await ProxyHost.query().where("is_deleted", 0);
const ya = existentes.find((h) => JSON.stringify(h.domain_names).includes(DOMINIO));
if (ya) { console.log("ya existe, id", ya.id); process.exit(0); }

const creado = await ProxyHost.query().insertAndFetch({
  owner_user_id: 1,
  domain_names: [DOMINIO],
  forward_scheme: "http",
  forward_host: DESTINO,
  forward_port: PUERTO,
  access_list_id: 0,
  certificate_id: CERT_ID,
  ssl_forced: true,
  caching_enabled: false,
  block_exploits: true,
  allow_websocket_upgrade: true,
  http2_support: false,
  hsts_enabled: false,
  hsts_subdomains: false,
  advanced_config: "",
  meta: { nginx_online: true, nginx_err: null },
  locations: [],
  enabled: true,
});
console.log("fila creada, id", creado.id);

const completo = await ProxyHost.query()
  .where("id", creado.id)
  .allowGraph("[owner,access_list,certificate]")
  .withGraphFetched("[owner,access_list,certificate]")
  .first();

await nginx.generateConfig("proxy_host", completo);
console.log("configuracion de nginx generada");
await nginx.reload();
console.log("nginx recargado");
process.exit(0);
