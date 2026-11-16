"""컨테이너 안 loopback 으로 /status 를 확인한다. 포트는 json/config.json 에서 읽는다."""
import json
import sys
import urllib.request

try:
    with open('/app/json/config.json', encoding='utf-8') as f:
        port = int(json.load(f).get('port', 5000))
except (OSError, ValueError, TypeError):
    port = 5000
try:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/status', timeout=4) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
