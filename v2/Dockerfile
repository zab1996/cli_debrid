# cli_debrid 2.0
# Phase 4 adds a Go build stage for the bundled mount engine (v2/engine)
# and fuse3 in the runtime image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLID2_DATA=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

VOLUME /data
EXPOSE 5060
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5060"]
