# Build on the personal computer; the target only loads this ONE image.
ARG DEPENDENCY_IMAGE=python:3.13-slim-trixie
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM qdrant/qdrant:v1.18.3 AS qdrant
FROM python:3.13-slim-trixie AS python
FROM ${DEPENDENCY_IMAGE} AS dependencies
FROM postgres:17.10-trixie
# Python and PostgreSQL share Debian trixie's ABI. Preserve postgres's gosu.
COPY --from=python /usr/local/ /usr/local/
COPY --from=python /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN --mount=type=secret,id=build_ca \
    cp /etc/ssl/certs/ca-certificates.crt /tmp/original-ca.crt \
 && if [ -f /run/secrets/build_ca ]; then cat /run/secrets/build_ca >> /etc/ssl/certs/ca-certificates.crt; fi \
 && sed -i 's|http://|https://|g' /etc/apt/sources.list.d/* \
 && apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 update \
 && apt-get install -y --no-install-recommends ca-certificates nginx tini libgomp1 \
 && cp /tmp/original-ca.crt /etc/ssl/certs/ca-certificates.crt \
 && rm /tmp/original-ca.crt \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PGDATA=/data/postgres \
    DATABASE_URL=postgresql://agenticai@127.0.0.1:5432/agenticai \
    QDRANT_URL=http://127.0.0.1:6333 \
    WORKFLOW_EXECUTOR=worker WORKFLOW_GRAPH_ADAPTER=graph.adapter:ADAPTER
WORKDIR /app/backend
COPY backend/requirements.txt ./
# Optional compatible local backend image seeds already-installed dependencies.
# pip still verifies requirements; no app code or environment is copied from it.
COPY --from=dependencies /usr/local/lib/python3.13/site-packages/ /usr/local/lib/python3.13/site-packages/
RUN --mount=type=secret,id=build_ca \
    cp /etc/ssl/certs/ca-certificates.crt /tmp/build-ca.crt \
 && if [ -f /run/secrets/build_ca ]; then cat /run/secrets/build_ca >> /tmp/build-ca.crt; fi \
 && pip install --cert /tmp/build-ca.crt --no-cache-dir -r requirements.txt \
 && rm /tmp/build-ca.crt
COPY backend/ ./
COPY --from=qdrant /qdrant/qdrant /opt/qdrant/qdrant
COPY --from=qdrant /lib/x86_64-linux-gnu/libunwind*.so* /opt/qdrant/lib/
COPY --from=qdrant /lib/x86_64-linux-gnu/liblzma.so* /opt/qdrant/lib/
COPY --from=qdrant /qdrant/config/ /opt/qdrant/config/
COPY --from=frontend /build/dist/novelty-lens/browser/ /app/web/
COPY docker-single/nginx.conf /etc/nginx/nginx.conf
COPY docker-single/launcher.py docker-single/healthcheck.py /opt/novelty-lens/
RUN useradd --system --create-home novelty \
 && mkdir -p /data /run/postgresql \
 && chown postgres:postgres /run/postgresql \
 && printf 'window.NOVELTY_LENS_API_URL = window.location.origin;\n' > /app/web/env-config.js \
 && python --version && postgres --version \
 && LD_LIBRARY_PATH=/opt/qdrant/lib /opt/qdrant/qdrant --version
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=10s --timeout=15s --start-period=90s --retries=3 \
  CMD ["python", "/opt/novelty-lens/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/opt/novelty-lens/launcher.py"]
