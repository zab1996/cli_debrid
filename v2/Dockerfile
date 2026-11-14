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
 && apt-get install -y --no-install-recommends fuse libfuse2 ffmpeg ca-certificates tzdata \
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

VOLUME /data
EXPOSE 5070
STOPSIGNAL SIGTERM
# 5070, not 5060: browsers refuse 5060/5061 (SIP) as "unsafe ports".
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${CLID2_PORT:-5070}"]
