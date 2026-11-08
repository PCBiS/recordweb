#!/usr/bin/env python3
import json
import hashlib
from pathlib import Path
import re
import subprocess

ROOT = Path('/opt/recordweb/ffmpeg-9.0.2')
packages = json.loads((ROOT / 'runtime-packages.json').read_text())
truststore = Path('/etc/ssl/certs/ca-certificates.crt')
truststore_before = hashlib.sha256(truststore.read_bytes()).hexdigest()
def run(*args):
    return subprocess.run(args, check=True, text=True)
source = Path('/etc/apt/sources.list.d/debian.sources')
source.write_text(source.read_text().replace('http://deb.debian.org', 'https://deb.debian.org'))
run('apt-get', 'update')
run('apt-get', 'install', '-y', '--no-install-recommends', *[k + '=' + v for k, v in sorted(packages.items())])
listing = subprocess.check_output(['dpkg-query', '-W', '-f=${binary:Package}\t${db:Status-Abbrev}\n'], text=True)
old = []
for line in listing.splitlines():
    name, state = line.split('\t')
    if state.startswith('ii') and re.match(r'^(?:ffmpeg(?::|$)|lib(?:avcodec|avformat|avutil|avfilter|avdevice|swscale|swresample|postproc)[0-9])', name):
        old.append(name)
assert not set(old).intersection(packages), 'New FFmpeg unexpectedly needs old FFmpeg packages'
if old:
    projected = subprocess.check_output(['apt-get', '--simulate', 'remove', *old], text=True)
    removals = sorted(set(re.findall(r'^Remv\s+(\S+)', projected, flags=re.M)))
    # libchromaprint1 is the single expected reverse dependency of old libavcodec.
    # Reject all other collateral removals before changing candidate packages.
    old_names = {name.split(':', 1)[0] for name in old}
    extra = [name for name in removals if name.split(':', 1)[0] not in old_names]
    assert set(extra) <= {'libchromaprint1'}, ('Unexpected package removal', extra)
    (ROOT / 'apt-removal-plan.json').write_text(json.dumps({'requested': old, 'projected': removals,
        'expected_reverse_dependency_removals': extra}, indent=2) + '\n')
    print('FFMPEG9_APT_REMOVAL_PLAN ' + json.dumps({'requested': old, 'projected': removals, 'extra': extra}), flush=True)
    run('apt-get', 'remove', '-y', *old)
for name in ('ffmpeg', 'ffprobe'):
    for bindir in ('/usr/bin', '/usr/local/bin'):
        link = Path(bindir) / name
        assert not link.exists() and not link.is_symlink(), str(link)
        link.symlink_to(ROOT / 'bin' / name)
    output = subprocess.check_output(['ldd', str(ROOT / 'bin' / name)], text=True)
    assert 'not found' not in output, output
    assert not re.search(r'lib(?:avcodec|avformat|avutil|avfilter|avdevice|swscale|swresample|postproc)\.so', output), output
    run(name, '-version')
run('apt-get', 'check')
truststore_after = hashlib.sha256(truststore.read_bytes()).hexdigest()
assert truststore_before == truststore_after, 'Runtime apt operations changed the reviewed TLS bundle'
(ROOT / 'truststore.sha256').write_text(truststore_after + '\n')
(ROOT / 'removed-debian-packages.json').write_text(json.dumps(old, indent=2) + '\n')
import shutil
shutil.rmtree('/var/lib/apt/lists')
Path('/var/lib/apt/lists').mkdir()
