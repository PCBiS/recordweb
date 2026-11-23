# syntax=docker/dockerfile:1
# recordWEB v1.3.2b 를 Linux Headless 모드로 실행하는 컨테이너 이미지.
# 원본 ZIP 을 그대로 풀고 patches/apply.py 의 패치(컨테이너 맞춤 3건, 보안 1건)를 적용한다.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm

FROM ${PYTHON_IMAGE} AS source
RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/*
COPY upstream/ /upstream/
COPY patches/apply.py /patches/apply.py
RUN cd /upstream && sha256sum -c SHA256SUMS \
    && mkdir /app && unzip -q recordWEB_v1.3.2b.zip -d /app \
    && python -I /patches/apply.py /app \
    && chmod -R u=rwX,go=rX /app

FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    TZ=Asia/Seoul
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=source /app/module/install/requirements-linux.txt /tmp/requirements-linux.txt
RUN pip install -r /tmp/requirements-linux.txt \
    && pip check \
    && rm /tmp/requirements-linux.txt
COPY --from=source /app /app
COPY docker/entrypoint.sh /usr/local/bin/recordweb-entrypoint
COPY docker/healthcheck.py /usr/local/bin/recordweb-healthcheck
COPY docker/admin_forward.py /usr/local/bin/recordweb-admin-forward
RUN chmod 0755 /usr/local/bin/recordweb-entrypoint /usr/local/bin/recordweb-healthcheck \
    && python /app/module/install/check_linux_deps.py \
    && ffmpeg -version >/dev/null && ffprobe -version >/dev/null && streamlink --version \
    && groupadd --gid 10001 recordweb \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin recordweb \
    && mkdir -p /app/json /app/logs /app/output /app/.runtime \
    && chown 10001:10001 /app/json /app/logs /app/output /app/.runtime
USER 10001:10001
WORKDIR /app
VOLUME ["/app/json", "/app/logs", "/app/output"]
EXPOSE 5000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-I", "/usr/local/bin/recordweb-healthcheck"]
ENTRYPOINT ["tini", "--", "recordweb-entrypoint"]
