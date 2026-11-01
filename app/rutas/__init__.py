"""Las rutas de la app, por áreas. main.create_app las junta todas."""
from . import ajustes, api, bancos, cuentas, datos, inicio, movimientos, planificar, pwa, seguridad

ROUTERS = [inicio.router, movimientos.router, cuentas.router, planificar.router, ajustes.router,
           seguridad.router, datos.router, pwa.router, api.router, bancos.router]
