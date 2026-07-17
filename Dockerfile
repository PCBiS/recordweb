FROM denoland/deno:bin-2.9.2 AS deno

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="recordWEB" \
      org.opencontainers.image.description="Docker image for recordWEB v1.2.9" \
      org.opencontainers.image.source="https://github.com/PCBiS/recordweb" \
      org.opencontainers.image.version="1.2.9"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        aria2 \
        ca-certificates \
        ffmpeg \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

RUN mkdir -p /opt/recordweb-defaults/json /recordings/chzzk \
    && cp -a /app/json/. /opt/recordweb-defaults/json/ \
    && chmod +x /app/docker-entrypoint.sh

EXPOSE 5000
VOLUME ["/app/json", "/recordings"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=3)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "recordWEB.py"]
