# The map's UI, bundled. Versions pinned in web/package-lock.json.
FROM node:24.21.0-alpine3.24 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# The generator and the server: Python, plus git to read the checkout's
# commit. Dependencies are installed from requirements.txt, which
# `uv export --no-header` writes from uv.lock with hashes.
FROM python:3.13.15-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY generator/ generator/
COPY server/ server/
COPY --from=web /web/dist/ web/

# git-sync's checkout is owned by its own uid; git refuses to read a repository
# another user owns unless told it is safe.
ENV GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=safe.directory \
    GIT_CONFIG_VALUE_0=* \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_DIR=/app/web

USER 65532:65532
EXPOSE 8080
CMD ["python", "-m", "server.serve"]
