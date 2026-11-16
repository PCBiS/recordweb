#!/bin/sh
# 첫 실행 준비 후 recordWEB 백엔드를 Headless 로 시작한다.
set -eu
cd /app

for dir in json logs output .runtime; do
    if ! [ -d "$dir" ] || ! [ -w "$dir" ]; then
        echo "[recordweb-docker] /app/$dir 에 쓸 수 없습니다. 볼륨 소유자를 UID $(id -u) 로 맞추거나 user: 설정을 확인하세요." >&2
        exit 20
    fi
done

# 설정이 없는 첫 실행에만 원본 기본값을 복사한다(원본 recordWEB.sh 와 같은 파일).
if [ ! -e json/config.json ]; then
    for name in config.json channels.json channel_groups.json cookie.json notification.json; do
        [ -e "json/$name" ] || cp "defaults/json/$name" "json/$name"
    done
    chmod 600 json/*.json
    # 컨테이너 밖에서 접속하려면 원본 설정의 "다른 기기에서 접속 허용"이 켜져 있어야 한다.
    if [ "${RWEB_REMOTE_ACCESS:-true}" = "true" ]; then
        python -I - <<'PY'
import json
path = 'json/config.json'
with open(path, encoding='utf-8') as f:
    config = json.load(f)
config['remoteAccessEnabled'] = True
config['runMode'] = 'headless'
with open(path, 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
PY
        echo "[recordweb-docker] 첫 실행: 기본 설정을 만들고 원격 접속을 켰습니다. 로그인은 꺼져 있습니다."
    fi
fi

unset DISPLAY WAYLAND_DISPLAY RWEB_LINUX_GUI PYTHONHOME PYTHONPATH

# 관리 포트(선택). 호스트의 127.0.0.1 에만 게시해서 쓴다.
if [ -n "${RWEB_ADMIN_PORT:-}" ]; then
    python -I /usr/local/bin/recordweb-admin-forward &
fi
umask 077
exec python -u /app/module/recordWEB.py --backend
