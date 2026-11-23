#!/usr/bin/env python3
"""recordWEB v1.3.2b 원본에 컨테이너용 패치를 적용한다.

사용법: apply.py <원본을 푼 폴더>

1. 접속 주소 허용 목록 (컨테이너 맞춤)
   RWEB_ALLOWED_HOSTS 에 적은 주소(쉼표 구분, 예: "192.168.0.10:5000,rec.example.com")를
   Host 헤더 검사에서 추가로 허용하고, 변경 요청의 Origin 검사에서 http://<주소>, https://<주소> 를
   같은 출처로 인정한다. 이 주소로 온 접속은 "다른 기기"로만 취급한다.
2. 관리 포트 (컨테이너 맞춤)
   RWEB_ADMIN_PORT 를 주면 Host 가 127.0.0.1/localhost 이고 포트가 그 값인 요청도 "서버 PC" 주소로 인정한다.
   컨테이너 안의 전달기(docker/admin_forward.py)가 이 포트로 받은 연결을 loopback 으로 넘긴다.
3. 원격 잠금 방지 (보안)
   다른 기기에서는 "다른 기기에서 접속 허용"과 서버 포트를 바꿀 수 없게 한다(원본의 서버 PC 전용 설정 목록에 추가).
4. HTTPS 리버스 프록시 (컨테이너 맞춤)
   원본은 uvicorn 을 proxy_headers=False 로 실행해 요청을 늘 http 로 보고, 화면의 JS·CSS 주소를
   http://<주소>/static/... 절대 주소로 만든다. HTTPS 프록시 뒤에서는 브라우저가 이를 혼합 콘텐츠로 막아
   화면이 하얗게 남는다. Host 가 RWEB_ALLOWED_HOSTS 의 주소이고 X-Forwarded-Proto 가 https 인 요청만
   https 요청으로 취급한다. 접속 IP(X-Forwarded-For)는 바꾸지 않는다.
(v1.3.0 판에 있던 다른 4번 "씨미 인증서 검증 실패 시 검증 없이 재요청 제거"는 원본 v1.3.1 이 같은 내용으로 고쳐 뺐다.)

환경변수를 주지 않으면 1·2·4 는 원본과 같게 동작한다.
원본 파일의 SHA-256 과 바꿀 문장이 정확히 한 번 나오는지를 확인하므로, 원본이 달라지면 빌드가 실패한다.
"""
import hashlib
import sys
from pathlib import Path

ACCESS_CONTROL_ADD = '''

# --- container patch: RWEB_ALLOWED_HOSTS / RWEB_ADMIN_PORT --------------------
import os as _os
import re as _re

_AUTHORITY = _re.compile(r'^(?=.{1,253}(?::|$))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$')


def _extra_authorities():
    result = set()
    for item in _os.environ.get('RWEB_ALLOWED_HOSTS', '').split(','):
        item = item.strip().lower()
        if not item:
            continue
        if not _AUTHORITY.match(item):
            print('[WARN] RWEB_ALLOWED_HOSTS: ignored invalid entry')
            continue
        result.add(item)
    return frozenset(result)


def _admin_port():
    value = _os.environ.get('RWEB_ADMIN_PORT', '').strip()
    if not value:
        return None
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1024 <= port <= 65535:
        print('[WARN] RWEB_ADMIN_PORT: ignored invalid value')
        return None
    return port


EXTRA_AUTHORITIES = _extra_authorities()
ADMIN_PORT = _admin_port()


def extra_authority(host):
    """Host header exactly matches an operator-listed address (remote access only)."""
    return isinstance(host, str) and host.lower() in EXTRA_AUTHORITIES


def forwarded_https(asgi_app):
    """Requests to an operator-listed address marked X-Forwarded-Proto: https are served as https."""
    if not EXTRA_AUTHORITIES:
        return asgi_app

    async def wrapper(scope, receive, send):
        if scope.get('type') in ('http', 'websocket') and scope.get('scheme') in ('http', 'ws'):
            headers = dict(scope.get('headers') or ())
            host = headers.get(b'host', b'').decode('latin-1')
            proto = headers.get(b'x-forwarded-proto', b'').decode('latin-1').split(',')[0].strip().lower()
            if proto == 'https' and extra_authority(host):
                scope = dict(scope, scheme='https' if scope['type'] == 'http' else 'wss')
        return await asgi_app(scope, receive, send)

    return wrapper


def allowed_origins(scheme, authority):
    authority = authority.lower()
    origins = [f'{scheme}://{authority}']
    if extra_authority(authority):
        origins += [f'http://{authority}', f'https://{authority}']
    return origins
'''

PATCHES = {
    'module/access_control.py': {
        'sha256': '6fe52680e2afebc6738131f3e73e6880fb4eb65d739bb374da6f12a47c4980c6',
        'append': ACCESS_CONTROL_ADD,
        'replace': [
            (
                "        if (parsed.port or 80) != actual_port:\n"
                "            return False\n",
                "        if (parsed.port or 80) != actual_port and not (\n"
                "                name in LOOPBACK and ADMIN_PORT is not None and parsed.port == ADMIN_PORT):\n"
                "            return False\n",
            ),
        ],
    },
    'module/recordWEB.py': {
        'sha256': 'a406db0219a8aa37f3de89dd13a4917f845fcf3b3d23b24bac3102ef8108c9f1',
        'append': '',
        'replace': [
            (
                "from module.access_control import LOOPBACK, allowed_host, interface_ipv4_addresses, interface_ipv4_display, validate_network",
                "from module.access_control import LOOPBACK, allowed_host, interface_ipv4_addresses, interface_ipv4_display, validate_network, extra_authority, allowed_origins, forwarded_https",
            ),
            (
                "        or not (local_authority or allowed_host(authority, actual_port, bound_remote))",
                "        or not (local_authority or allowed_host(authority, actual_port, bound_remote)\n"
                "                or (bound_remote and extra_authority(authority)))",
            ),
            (
                "        if (origins and origins != [expected_origin]) or (",
                "        if (origins and (len(origins) != 1 or origins[0] not in allowed_origins(request.url.scheme, authority))) or (",
            ),
            (
                "        'preset', 'preset_gpu1', 'postprocess_resolution', 'postprocess_resolution_gpu1',\n"
                "    ):",
                "        'preset', 'preset_gpu1', 'postprocess_resolution', 'postprocess_resolution_gpu1',\n"
                "        'remoteAccessEnabled', 'port',\n"
                "    ):",
            ),
            (
                "        config = uvicorn.Config(app, host=host,",
                "        config = uvicorn.Config(forwarded_https(app), host=host,",
            ),
        ],
    },
}


def main(root):
    root = Path(root)
    for name, spec in PATCHES.items():
        path = root / name
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != spec['sha256']:
            sys.exit(f'{name}: unexpected upstream file (sha256 {digest})')
        newline = b'\r\n' if b'\r\n' in data else b'\n'
        encode = lambda text: text.encode('utf-8').replace(b'\n', newline)
        for old, new in spec['replace']:
            if data.count(encode(old)) != 1:
                sys.exit(f'{name}: patch target not found exactly once: {old[:60]!r}')
            data = data.replace(encode(old), encode(new))
        if spec['append']:
            data = data.rstrip(b'\r\n') + newline + encode(spec['append'])
        path.write_bytes(data)
        print(f'patched {name}')


if __name__ == '__main__':
    main(sys.argv[1])
