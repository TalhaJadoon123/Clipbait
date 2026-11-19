# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# 1. Build the SPA. The compiled bundle is also committed, but building inside
#    the image keeps the container reproducible from source.
# ---------------------------------------------------------------------------
FROM node:20-alpine AS web

WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY apps/web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# 2. Python runtime + ffmpeg + yt-dlp.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
    && curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Source first, then the package, so `pip install .` can resolve the layout.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY --from=web /web/dist ./apps/web/dist

RUN pip install .

# Non-root runtime. /data is the only writable surface.
RUN useradd --create-home --uid 10001 clipbait \
    && mkdir -p /data \
    && chown -R clipbait:clipbait /data /app
USER clipbait

ENV CLIPBAIT_DATA_DIR=/data \
    CLIPBAIT_HOST=0.0.0.0 \
    CLIPBAIT_PORT=8000 \
    CLIPBAIT_WEB_DIST=/app/apps/web/dist

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/healthz', timeout=3).status == 200 else 1)"

CMD ["clipbait", "serve", "--host", "0.0.0.0", "--port", "8000"]