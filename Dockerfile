# Imagen de la app de finanzas.
# Se construye a mano y el stack de Portainer la usa por su etiqueta:
#     docker build -t finanzas-app:1.0.0 --build-arg VERSION=1.0.0 .
# La fase "test" ejecuta pytest: si falla una regla de cálculo, no hay imagen.
FROM python:3.12-alpine AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY pytest.ini .
COPY app ./app
COPY tests ./tests
RUN python -m pytest && touch /tests-ok

FROM base AS final
COPY --from=test /tests-ok /tmp/tests-ok
COPY app ./app
ARG VERSION=dev
ENV APP_VERSION=$VERSION DATA_DIR=/app/data BACKUP_DIR=/app/backups TZ=Europe/Madrid
RUN mkdir -p /app/data /app/backups && chown -R 1000:1000 /app/data /app/backups
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8000/salud >/dev/null || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
