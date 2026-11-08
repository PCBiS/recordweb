#!/usr/bin/env python3
"""Versioned FFmpeg tool suite; static internal FFmpeg, dynamic Debian codecs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import urllib.request

ROOT = Path('/opt/ffmpeg9-build')
WORK = ROOT / 'work'
PREFIX = Path('/opt/recordweb/ffmpeg-9.0.2')
COMMIT = '946fcce07b6dcd0331c8cc609192aeff5e1924f8'
TAG_OBJECT = 'ce8f11b9fac4cd7ccfb41ca7c01bc17f5cda8cc3'
ARCHIVE_SHA256 = '0aa2b1de2a5698b20a23e93d539a9a8e82ca0117496c5bdf05d198805f42bb3b'
URL = 'https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/' + COMMIT
NV_COMMIT = '43d91706e097565f57b311e567f0219838bcc2f6'
NV_SHA256 = 'd6e7408cb0742b057173bce156c0deefe79263fdf03c3ca92d78dface21e8ac3'

def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)

def capture(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()

def prepare():
    sources = list(Path('/etc/apt/sources.list.d').glob('*.sources'))
    for p in sources:
        text = p.read_text().replace('http://deb.debian.org', 'https://deb.debian.org')
        p.write_text(re.sub(r'(?m)^Types:\s*deb\s*$', 'Types: deb deb-src', text))
    assert any('deb-src' in p.read_text() for p in sources), 'No signed Debian source repository'

def build():
    tag = json.load(urllib.request.urlopen('https://api.github.com/repos/FFmpeg/FFmpeg/git/tags/' + TAG_OBJECT, timeout=45))
    assert tag['tag'] == 'n9.0.2' and tag['object']['sha'] == COMMIT
    assert tag['verification']['verified'] is True and tag['verification']['reason'] == 'valid'
    WORK.mkdir(parents=True, exist_ok=True)
    archive = WORK / 'ffmpeg.tar.gz'
    with urllib.request.urlopen(URL, timeout=90) as response, archive.open('wb') as out:
        while block := response.read(1024 * 1024):
            out.write(block)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sha == ARCHIVE_SHA256, sha
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        # GitHub's fixed-commit archive has one predictable top directory.
        assert all(m.name.startswith('FFmpeg-' + COMMIT + '/') or m.name == 'FFmpeg-' + COMMIT for m in members)
        assert not any(m.islnk() or m.issym() for m in members), 'Unexpected source archive link'
        tar.extractall(WORK)
    source = WORK / ('FFmpeg-' + COMMIT)
    # Header-only upstream patch release preserves the original 11.1 driver API
    # while satisfying FFmpeg9's >=11.1.5.3 compatibility check. No CUDA toolkit.
    nv_url = 'https://codeload.github.com/FFmpeg/nv-codec-headers/tar.gz/' + NV_COMMIT
    nv_data = urllib.request.urlopen(nv_url, timeout=45).read()
    assert hashlib.sha256(nv_data).hexdigest() == NV_SHA256
    import io
    with tarfile.open(fileobj=io.BytesIO(nv_data), mode='r:gz') as tar:
        assert all(m.name.startswith('nv-codec-headers-' + NV_COMMIT + '/') or m.name == 'nv-codec-headers-' + NV_COMMIT for m in tar.getmembers())
        assert not any(m.islnk() or m.issym() for m in tar.getmembers())
        tar.extractall(WORK)
    nv_source = WORK / ('nv-codec-headers-' + NV_COMMIT)
    nv_prefix = ROOT / 'nvheaders'
    run('make', 'PREFIX=' + str(nv_prefix), 'install', cwd=nv_source)
    # Git mirror archive lacks the release tarball VERSION marker.
    (source / 'VERSION').write_text('9.0.2\n')
    arch = capture('dpkg', '--print-architecture')
    assert arch in ('amd64', 'arm64'), arch
    features = [
        'gnutls', 'gpl', 'libx264', 'libx265', 'libvpx', 'libaom', 'libdav1d', 'libsvtav1',
        'libopus', 'libvorbis', 'libmp3lame', 'libass', 'libfreetype', 'libfontconfig',
        'libfribidi', 'libharfbuzz', 'libwebp', 'libopenjpeg', 'libtheora', 'libzimg',
        'libsoxr', 'libsrt', 'libssh', 'libxml2', 'libzmq', 'librubberband', 'libvidstab',
        'libgsm', 'libspeex', 'libtwolame', 'libshine', 'libsnappy', 'libmysofa', 'librsvg',
        'libjxl', 'libopenmpt', 'libgme', 'libbluray', 'libbs2b', 'libcodec2', 'libdc1394',
        'libdrm', 'libiec61883', 'openal', 'opencl', 'opengl', 'sdl2', 'lv2', 'ladspa',
        'libpulse', 'libjack', 'libcdio', 'libcaca', 'libflite', 'libzvbi', 'librabbitmq',
        'pocketsphinx', 'frei0r',
    ]
    if arch == 'amd64':
        features += ['libmfx']
    args = ['--prefix=' + str(PREFIX), '--disable-shared', '--enable-static',
            '--disable-doc', '--disable-ffplay', '--disable-debug', '--toolchain=hardened',
            '--extra-version=recordweb-20261108', '--disable-vulkan', '--disable-libplacebo',
            '--disable-chromaprint', '--enable-ffnvcodec', '--enable-nvenc', '--enable-nvdec', '--enable-cuvid',
            *['--enable-' + x for x in features]]
    env = os.environ.copy()
    env.update({'LC_ALL': 'C', 'TZ': 'UTC', 'SOURCE_DATE_EPOCH': '1789689600'})
    env['PKG_CONFIG_PATH'] = str(nv_prefix / 'lib/pkgconfig')
    for key in ('CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS'):
        env[key] = capture('dpkg-buildflags', '--get', key)
    run('./configure', *args, cwd=source, env=env)
    run('make', '-j' + os.environ.get('FFMPEG_BUILD_JOBS', '2'), 'ffmpeg', 'ffprobe', cwd=source, env=env)
    run('make', 'install', cwd=source, env=env)
    licenses = PREFIX / 'licenses'
    licenses.mkdir(parents=True, exist_ok=True)
    import shutil
    for license_file in source.glob('COPYING*'):
        shutil.copy2(license_file, licenses / license_file.name)
    for license_file in source.glob('LICENSE*'):
        shutil.copy2(license_file, licenses / license_file.name)
    nv_licenses = licenses / 'nv-codec-headers'
    nv_licenses.mkdir()
    for license_file in (nv_source / 'include/ffnvcodec').glob('*.h'):
        shutil.copy2(license_file, nv_licenses / license_file.name)
    # Keep executable tools only: no FFmpeg .a/.so or headers in runtime image.
    for name in ('include', 'lib', 'share'):
        shutil.rmtree(PREFIX / name, ignore_errors=True)
    libraries = {}
    packages = {}
    for name in ('ffmpeg', 'ffprobe'):
        binary = PREFIX / 'bin' / name
        output = capture('ldd', str(binary))
        assert 'not found' not in output, output
        # No dependency on any distro FFmpeg library is permitted.
        assert not re.search(r'lib(?:avcodec|avformat|avutil|avfilter|avdevice|swscale|swresample|postproc)\.so', output), output
        paths = re.findall(r'(?:=>\s*)?(/[^\s]+)', output)
        libraries[name] = output
        for item in paths:
            candidates = [item, str(Path(item).resolve())]
            if item.startswith('/lib/'):
                candidates.append('/usr' + item)
            found = None
            for candidate in candidates:
                result = subprocess.run(['dpkg-query', '-S', candidate], text=True, capture_output=True)
                if result.returncode == 0:
                    found = result.stdout.splitlines()[0].rsplit(': ', 1)[0]
                    break
            assert found, 'Unowned dynamic dependency ' + item
            packages[found] = capture('dpkg-query', '-W', '-f=${Version}', found)
    (ROOT / 'runtime-packages.json').write_text(json.dumps(packages, indent=2) + '\n')
    manifest = {
        'version': '9.0.2', 'architecture': arch, 'source_url': URL,
        'source_sha256': sha, 'tag': 'n9.0.2', 'tag_object': TAG_OBJECT, 'commit': COMMIT,
        'source_authentication': 'Fixed mirror commit bound to GitHub verified-valid signed upstream tag; pinned archive SHA256',
        'nvcodec_headers': {'version': '11.1.5.3', 'commit': NV_COMMIT, 'source_url': nv_url,
                            'source_sha256': NV_SHA256, 'signed_tag': False,
                            'authentication': 'Official FFmpeg repository, HTTPS, fixed commit and SHA256; upstream header tag is unsigned'},
        'configure': args, 'runtime_packages': packages, 'ldd': libraries,
        'intentional_differences': [
            'FFmpeg internal libraries statically linked into ffmpeg/ffprobe; external distro codec libraries remain dynamic',
            'OMX and old libglslang configure options removed upstream',
            'libplacebo and Vulkan disabled: Bookworm versions do not meet FFmpeg9 minimums',
            'chromaprint disabled to avoid pulling original Debian FFmpeg libraries back into the tool suite',
            'ffplay omitted in the headless service; audio inputs/filters and SDL/OpenGL/OpenCL support retained',
            'QSV libmfx enabled on AMD64 only; device access/hardware operation requires separate validation',
        ],
    }
    for name in ('ffmpeg', 'ffprobe'):
        manifest[name + '_sha256'] = hashlib.sha256((PREFIX / 'bin' / name).read_bytes()).hexdigest()
    (PREFIX / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    capabilities = {}
    for command in ('-version', '-encoders', '-decoders', '-filters', '-protocols', '-hwaccels', '-demuxers', '-muxers'):
        output = capture(str(PREFIX / 'bin/ffmpeg'), command, stderr=subprocess.STDOUT)
        capabilities[command] = output
        (PREFIX / ('features' + command + '.txt')).write_text(output + '\n')
    required = {
        '-encoders': ['libx264', 'libx265', 'aac', 'libopus', 'libmp3lame', 'h264_nvenc', 'hevc_nvenc'],
        '-filters': ['scale', 'format', 'fps', 'concat', 'aresample', 'volume', 'subtitles', 'overlay', 'drawtext'],
        '-protocols': ['http', 'https', 'tls', 'tcp', 'pipe', 'crypto', 'file'],
        '-demuxers': ['hls', 'dash', 'mov'],
        '-muxers': ['mp4', 'mpegts', 'matroska'],
        '-hwaccels': ['cuda', 'vaapi'],
    }
    if arch == 'amd64':
        required['-encoders'] += ['h264_qsv']
        required['-hwaccels'] += ['qsv']
    for command, names in required.items():
        missing = [name for name in names if not re.search(r'\b' + re.escape(name) + r'\b', capabilities[command])]
        assert not missing, (command, missing)

if __name__ == '__main__':
    import sys
    {'prepare': prepare, 'build': build}[sys.argv[1]]()
