# cli_debrid 2.0: one image = Python app + bundled mount engine (Go).

# ---- engine build -------------------------------------------------------
FROM golang:1.26-bookworm AS engine
# cgofuse needs the libfuse headers at build time (it dlopens libfuse.so.2 at runtime).
RUN apt-get update && apt-get install -y --no-install-recommends libfuse-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY engine/go.mod engine/go.sum ./
RUN go mod download
COPY engine/ ./
RUN CGO_ENABLED=1 go build -trimpath -ldflags="-s -w" -o /out/clid2-engine .

# ---- runtime ------------------------------------------------------------
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends fuse libfuse2 ffmpeg ca-certificates tzdata tini \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLID2_DATA=/data \
    CLID2_ENGINE_BIN=/app/bin/clid2-engine

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=engine /out/clid2-engine /app/bin/clid2-engine
COPY app ./app
COPY run.sh /app/run.sh
COPY restart-app /usr/local/bin/restart-app
RUN chmod +x /app/run.sh /usr/local/bin/restart-app

VOLUME /data
EXPOSE 5070
STOPSIGNAL SIGTERM
# run.sh keeps the app and the mount engine alive independently, so
# `docker exec <container> restart-app` reloads Python without touching the mount.
# UI port: CLID2_PORT (5070 by default; browsers refuse 5060 as "unsafe").
ENTRYPOINT ["tini", "--", "/app/run.sh"]
