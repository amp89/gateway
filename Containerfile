# Gateway prod image: FastAPI + uvicorn, one worker, plus the built admin UI. No Node at runtime.
# Base images pinned by digest; Python packages hash-locked; npm packages locked (npm ci).

# Admin UI build (Svelte + Vite). Only dist/ leaves this stage.
FROM docker.io/library/node:24.21.0-trixie-slim@sha256:8ec5d7557396cfe32d21c3f9c13072355ceab22b584578ca4bb28af31120cffe AS ui
# IPv4 only: on some hosts Node's happy-eyeballs times out against the npm registry from inside containers.
ENV NODE_OPTIONS=--no-network-family-autoselection NPM_CONFIG_UPDATE_NOTIFIER=false
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund --ignore-scripts
COPY ui/index.html ui/vite.config.ts ui/tsconfig.json ./
COPY ui/src ./src
RUN npm run build

FROM docker.io/library/python:3.14-slim-trixie@sha256:89fb7d3da20043c370643435258bdd7ab755d326d359001d02988ed15ae5219e AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /srv
COPY requirements.lock ./
# Hash-checked wheels only: nothing resolved, nothing built from source.
RUN pip install --require-hashes --no-deps --only-binary :all: -r requirements.lock

FROM base AS prod
COPY app ./app
COPY packs ./packs
COPY --from=ui /ui/dist ./ui
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=4s --start-period=10s --retries=3 CMD ["python", "-m", "app.healthcheck"]
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header", "--no-proxy-headers"]
