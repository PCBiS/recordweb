"""Run a disposable, network-isolated runtime smoke on the native CI host."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid

parser = argparse.ArgumentParser()
parser.add_argument('--image', required=True)
parser.add_argument('--arch', choices=('amd64', 'arm64'), required=True)
args = parser.parse_args()


def run(command, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, **kwargs)


architecture = run(['docker', 'image', 'inspect', '--format', '{{.Architecture}}', args.image]).stdout.decode().strip()
assert architecture == args.arch
versions = '''import subprocess,json
commands=[['ffmpeg','-version'],['ffprobe','-version'],['aria2c','--version'],['deno','--version'],['streamlink','--version'],['yt-dlp','--version']]
result={}
for command in commands:
 p=subprocess.run(command,check=True,capture_output=True,timeout=30)
 result[command[0]]=(p.stdout or p.stderr).decode(errors='replace').splitlines()[0]
print(json.dumps(result))
'''
tools = json.loads(run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', args.image, '-c', versions], timeout=180).stdout)
run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'ffmpeg', args.image,
     '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10', '-t', '1', '-threads', '1',
     '-c:v', 'libx264', '-f', 'null', '-'], timeout=45)

probe = '''import base64,json,pathlib,urllib.request,itsdangerous
p=pathlib.Path('/app/json');a=json.loads((p/'login.json').read_text())
cookie=itsdangerous.TimestampSigner(a['secret_key']).sign(base64.b64encode(json.dumps({'logged_in':True}).encode())).decode();del a
statuses={}
for path in ('/status','/config','/replay','/api/config','/api/replay/capabilities','/api/replay/jobs'):
 q=urllib.request.Request('http://127.0.0.1:5000'+path,headers={'Cookie':'session='+cookie})
 with urllib.request.urlopen(q,timeout=10) as r:
  assert r.status==200
  statuses[path]=r.status
  if path=='/api/replay/capabilities':
   value=json.load(r);assert value['enabled'] and value['mode']=='live'
print(json.dumps(statuses))
'''
with tempfile.TemporaryDirectory(prefix='recordweb-runtime-smoke-') as temporary:
    root = Path(temporary)
    for name in ('json', 'recordings', 'logs'):
        (root/name).mkdir()
    config = {'loginMode': True, 'autoRecordingMode': False, 'enableTray': False, 'port': 5000,
              'fileManagerEnabled': True, 'fileManagerMode': 'whitelist', 'fileManagerRoots': ['/recordings']}
    account = {'username': 'ci-fixture', 'password': '!disabled', 'secret_key': secrets.token_hex(32)}
    for name, value in [('config.json', config), ('channels.json', []), ('login.json', account),
                        ('cookie.json', {'chzzk': {}, 'cime': {}}), ('notification.json', {})]:
        path = root/'json'/name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    name = 'recordweb-smoke-' + uuid.uuid4().hex[:12]
    command = ['docker', 'create', '--name', name, '--init', '--network', 'none']
    for source, target in [('json', '/app/json'), ('recordings', '/recordings'), ('logs', '/var/log/recordweb')]:
        command += ['--mount', f'type=bind,src={root/source},dst={target}']
    command.append(args.image)
    run(command)
    try:
        run(['docker', 'start', name])
        ready = "import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:5000/status',timeout=2).status==200"
        for _ in range(90):
            result = subprocess.run(['docker', 'exec', name, 'python', '-c', ready], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('isolated runtime did not become ready')
        statuses = json.loads(run(['docker', 'exec', name, 'python', '-c', probe], timeout=90).stdout)
        run(['docker', 'stop', '--time', '20', name], timeout=30)
        state = json.loads(run(['docker', 'inspect', name]).stdout)[0]
        assert state['RestartCount'] == 0 and state['State']['ExitCode'] in (0, 143)
        print(json.dumps({'arch': architecture, 'tools': tools, 'http': statuses,
                          'synthetic_h264_encode': True, 'exit_code': state['State']['ExitCode']}))
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
