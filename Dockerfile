# syntax=docker/dockerfile:1.7
FROM node:24-bookworm-slim AS web-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DRASIL_WEB_DIST=/opt/drasil/web \
    DRASIL_AUTH_DATABASE_PATH=/var/lib/drasil/drasil-auth.db \
    DRASIL_ACCOUNT_DATA_ROOT=/var/lib/drasil/accounts \
    DRASIL_LEGACY_DATABASE_PATH=/var/lib/drasil/drasil.db

RUN groupadd --system drasil \
    && useradd --system --gid drasil --home-dir /opt/drasil drasil \
    && mkdir -p /opt/drasil/backend /opt/drasil/web /var/lib/drasil \
    && chown -R drasil:drasil /opt/drasil /var/lib/drasil

WORKDIR /opt/drasil/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=drasil:drasil backend/app ./app
COPY --from=web-build --chown=drasil:drasil /build/frontend/dist /opt/drasil/web

USER drasil
EXPOSE 8000
VOLUME ["/var/lib/drasil"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
