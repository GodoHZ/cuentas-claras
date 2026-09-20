#!/usr/bin/env python3
"""Crea o actualiza el stack "finanzas" en Portainer usando su API.

El token NO está aquí: se lee de ~/.config/finanzas/portainer.env (fuera del
repositorio, chmod 600). Si no hay token, el script lo dice y no hace nada:
entonces se pega el docker-compose.yml a mano en Portainer.

    python3 scripts/portainer.py estado      qué hay ahora
    python3 scripts/portainer.py desplegar   crea el stack o lo actualiza
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# docker-compose.local.yml (si existe) manda: ahí van las rutas y redes de cada
# instalación, que no tienen por qué estar en el repositorio.
COMPOSE = RAIZ / "docker-compose.local.yml"
if not COMPOSE.is_file():
    COMPOSE = RAIZ / "docker-compose.yml"
ENV_FILE = Path(os.environ.get("FINANZAS_ENV", Path.home() / ".config/finanzas/portainer.env"))
STACK = "finanzas"
SIN_VERIFICAR = ssl._create_unverified_context()      # Portainer local con certificado propio


def leer_env() -> dict:
    datos = {}
    if ENV_FILE.is_file():
        for linea in ENV_FILE.read_text().splitlines():
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                clave, valor = linea.split("=", 1)
                datos[clave.strip()] = valor.strip()
    return datos


def api(metodo: str, ruta: str, token: str, base: str, cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    peticion = urllib.request.Request(base.rstrip("/") + ruta, data=datos, method=metodo,
                                      headers={"X-API-Key": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(peticion, timeout=60, context=SIN_VERIFICAR) as respuesta:
        texto = respuesta.read().decode()
    return json.loads(texto) if texto.strip() else None


def entorno_local(token: str, base: str) -> int:
    for e in api("GET", "/api/endpoints", token, base):
        if e.get("Type") in (1, 2):                   # Docker local o agente
            return e["Id"]
    raise SystemExit("No encuentro ningún entorno Docker en Portainer.")


def buscar_stack(token: str, base: str):
    for s in api("GET", "/api/stacks", token, base):
        if s.get("Name") == STACK:
            return s
    return None


def main() -> int:
    orden = sys.argv[1] if len(sys.argv) > 1 else "estado"
    env = leer_env()
    token, base = env.get("PORTAINER_TOKEN", ""), env.get("PORTAINER_URL", "https://localhost:9443")
    if not token:
        print(f"No hay token en {ENV_FILE}.\n"
              f"Ponlo detrás de PORTAINER_TOKEN= o pega {COMPOSE} a mano en Portainer.")
        return 1

    endpoint = entorno_local(token, base)
    existente = buscar_stack(token, base)
    if orden == "estado":
        print(f"Entorno Portainer: {endpoint}")
        print(f"Stack «{STACK}»: " + (f"existe (id {existente['Id']})" if existente else "no existe todavía"))
        return 0
    if orden != "desplegar":
        print(__doc__)
        return 2

    contenido = COMPOSE.read_text()
    try:
        if existente:
            api("PUT", f"/api/stacks/{existente['Id']}?endpointId={endpoint}", token, base,
                {"stackFileContent": contenido, "env": existente.get("Env", []),
                 "prune": True, "pullImage": False})
            print(f"Stack «{STACK}» actualizado (id {existente['Id']}).")
        else:
            creado = api("POST", f"/api/stacks/create/standalone/string?endpointId={endpoint}", token, base,
                         {"name": STACK, "stackFileContent": contenido, "env": []})
            print(f"Stack «{STACK}» creado (id {creado['Id']}).")
    except urllib.error.HTTPError as e:
        print(f"Portainer ha respondido {e.code}: {e.read().decode()[:400]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
