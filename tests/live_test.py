"""실수신 시험: 지금 방송 중인 치지직 공개 채널을 짧게 녹화해 본다.
GitHub Actions 러너에서 `python3 tests/live_test.py <image>` 로 실행한다. 표준 라이브러리와 docker CLI 만 쓴다."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

IMAGE = sys.argv[1]
OUT = os.path.abspath('live-out')
ADMIN = 5001
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'


def sh(*args, check=True, quiet=False):
    result = subprocess.run(args, capture_output=True, text=True)
    if not quiet:
        print('$', ' '.join(args), '->', result.returncode, flush=True)
        if result.stdout.strip():
            print(result.stdout.strip()[-3000:], flush=True)
        if result.stderr.strip():
            print(result.stderr.strip()[-1500:], flush=True)
    if check and result.returncode:
        raise SystemExit(f'command failed: {args[0]} {args[1]}')
    return result


def find_live_channels():
    request = urllib.request.Request(
        'https://api.chzzk.naver.com/service/v1/lives?size=30&sortType=POPULAR', headers={'User-Agent': UA})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.load(response)
    found = []

    def walk(node):
        if isinstance(node, dict):
            channel = node.get('channel')
            if isinstance(channel, dict) and isinstance(channel.get('channelId'), str) and not node.get('adult'):
                found.append((channel['channelId'], node.get('concurrentUserCount', 0)))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(body)
    return [cid for cid, _ in found]


def call(ip, method, path, body=None, timeout=60):
    headers = {'Host': f'127.0.0.1:{ADMIN}', 'Origin': f'http://127.0.0.1:{ADMIN}', 'Content-Type': 'application/json'}
    request = urllib.request.Request(f'http://{ip}:{ADMIN}{path}', method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8', 'replace')


def start(name):
    sh('docker', 'rm', '-f', name, check=False, quiet=True)
    sh('docker', 'run', '-d', '--name', name, '-e', f'RWEB_ADMIN_PORT={ADMIN}',
       '-v', f'{OUT}:/app/output', '-v', 'rw-json:/app/json', IMAGE)
    for _ in range(60):
        state = sh('docker', 'inspect', '-f', '{{.State.Health.Status}}', name, quiet=True).stdout.strip()
        if state == 'healthy':
            break
        time.sleep(2)
    else:
        sh('docker', 'logs', name)
        raise SystemExit('container did not become healthy')
    return sh('docker', 'inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', name, quiet=True).stdout.strip()


def files():
    result = []
    for root, _, names in os.walk(OUT):
        for name in names:
            path = os.path.join(root, name)
            result.append((os.path.relpath(path, OUT), os.path.getsize(path)))
    return sorted(result)


def probe(relative):
    result = sh('docker', 'run', '--rm', '--entrypoint', 'ffprobe', '-v', f'{OUT}:/app/output:ro', IMAGE,
                '-v', 'error', '-show_entries', 'format=duration,format_name:stream=codec_type,codec_name,width,height',
                '-of', 'json', '/app/output/' + relative, check=False, quiet=True)
    try:
        info = json.loads(result.stdout)
        duration = float(info['format']['duration'])
        streams = [(s.get('codec_type'), s.get('codec_name'), s.get('width'), s.get('height')) for s in info['streams']]
    except (ValueError, KeyError, TypeError):
        print('  ffprobe failed:', relative, result.stderr.strip()[-300:])
        return 0.0, []
    print(f'  {relative}: {duration:.1f}s {info["format"]["format_name"]} {streams}')
    return duration, streams


def wait_recording(ip, cid, seconds):
    seen = False
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, text = call(ip, 'GET', '/status')
        state = json.loads(text).get(cid, {}) if code == 200 else {}
        growing = [f for f in files() if f[1] > 0]
        print(f'  status={json.dumps(state, ensure_ascii=False)[:300]} files={growing}', flush=True)
        if growing:
            seen = True
        time.sleep(10)
    return seen


def main():
    os.makedirs(OUT, exist_ok=True)
    sh('sudo', 'chown', '10001:10001', OUT)
    sh('docker', 'volume', 'rm', '-f', 'rw-json', check=False, quiet=True)
    candidates = find_live_channels()
    print('live candidates:', len(candidates))
    if not candidates:
        raise SystemExit('no live channel found (API blocked from this runner?)')

    print('== A. record, stop from the UI, post-process')
    ip = start('rwlive')
    cid = None
    for candidate in candidates[:5]:
        code, text = call(ip, 'POST', '/api/channels', {
            'platform': 'chzzk', 'id': candidate, 'name': 'livetest', 'output_dir': '/app/output',
            'quality': 'best', 'extension': '.mp4', 'record_enabled': True})
        print('add channel', code, text[:300])
        if code != 200:
            continue
        code, text = call(ip, 'POST', f'/api/start_recording/{candidate}', {'is_user_request': True}, timeout=120)
        print('start', code, text[:300])
        if code == 200 and wait_recording(ip, candidate, 40):
            cid = candidate
            break
        call(ip, 'POST', f'/api/stop_recording/{candidate}')
        code, text = call(ip, 'DELETE', f'/api/channels/{candidate}')
        print('remove channel', code)
    if cid is None:
        sh('docker', 'logs', '--tail', '150', 'rwlive')
        raise SystemExit('recording did not start for any candidate')
    wait_recording(ip, cid, 50)
    code, text = call(ip, 'POST', f'/api/stop_recording/{cid}', timeout=120)
    print('stop', code, text[:300])
    for _ in range(36):
        time.sleep(5)
        code, text = call(ip, 'GET', '/status')
        names = [name for name, _ in files()]
        print('  after stop:', files(), flush=True)
        if names and all(name.endswith('.mp4') and 'fixed_' not in name and '.part' not in name for name in names):
            code, history = call(ip, 'GET', '/api/postprocess-history')
            if '"running"' not in history:
                break
    code, text = call(ip, 'GET', '/api/postprocess-history')
    print('postprocess history', code, text[:600])
    code, text = call(ip, 'GET', '/api/postprocess-failures')
    print('postprocess failures', code, text[:600])
    sh('docker', 'logs', '--tail', '80', 'rwlive')
    first = [probe(name) for name, size in files()]
    ok_a = any(duration >= 30 and any(s[0] == 'video' for s in streams) and any(s[0] == 'audio' for s in streams)
               for duration, streams in first)
    print('A result:', 'PASS' if ok_a else 'FAIL')
    before = {name for name, _ in files()}

    print('== B. container stop while recording')
    code, text = call(ip, 'POST', f'/api/start_recording/{cid}', {'is_user_request': True}, timeout=120)
    print('start', code, text[:300])
    started = wait_recording(ip, cid, 50)
    began = time.time()
    sh('docker', 'stop', '-t', '90', 'rwlive')
    print(f'stop took {time.time() - began:.1f}s, exit code',
          sh('docker', 'inspect', '-f', '{{.State.ExitCode}}', 'rwlive', quiet=True).stdout.strip())
    sh('docker', 'logs', '--tail', '60', 'rwlive')
    print('files after stop:', files())
    second = [(name, probe(name)) for name, _ in files() if name not in before]
    playable = [name for name, (duration, streams) in second if duration >= 15]
    print('B result:', 'recording started' if started else 'recording did NOT start', '| new files:', [n for n, _ in second],
          '| playable:', playable)

    print('== C. restart: interrupted work recovery')
    ip = start('rwlive')
    time.sleep(45)
    code, text = call(ip, 'GET', '/api/postprocess-history')
    print('postprocess history', code, text[:600])
    sh('docker', 'logs', '--tail', '60', 'rwlive')
    print('files after restart:', files())
    third = [(name, probe(name)) for name, _ in files() if name not in before]
    sh('docker', 'stop', '-t', '60', 'rwlive')

    print('SUMMARY A(record+postprocess)=%s B(stop while recording: playable files)=%s C(after restart: playable)=%s' % (
        'PASS' if ok_a else 'FAIL', playable, [name for name, (d, _) in third if d >= 15]))
    if not ok_a:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
