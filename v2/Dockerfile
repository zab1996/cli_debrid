# cli_debrid 2.0: one image = Python app + bundled mount engine (Go).

# ---- engine build -------------------------------------------------------
FROM golang:1.26-bookworm AS engine
WORKDIR /src
COPY engine/go.mod engine/go.sum ./
RUN go mod download
COPY engine/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/clid2-engine .

# ---- runtime ------------------------------------------------------------
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends fuse3 ffmpeg ca-certificates tzdata \
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
EXPOSE 5060
STOPSIGNAL SIGTERM
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5060"]
