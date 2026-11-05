"""Pure validation and a narrow adapter around the preserved upstream engine.

Provider objects, cookies and signed media URLs remain in this worker's memory.
The public metadata projection is deliberately small. This is an application
boundary, not an OS network sandbox: downloader manifest redirects still require
operator controlled egress if untrusted provider responses are in the threat model.
"""
import contextlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import types
from urllib.parse import parse_qs, urlsplit, urlunsplit
from .errors import ERROR_MESSAGES, ReplayFailure, classify_diagnostic, classify_exception, record_diagnostic

MAX_ITEMS = 50
GENERIC_ERROR = ERROR_MESSAGES['engine_operation_failed']


def parse_url(value, allow_playlist=True):
    if not isinstance(value, str) or len(value) > 2048 or value != value.strip() or re.search(r'[\x00-\x20\\]', value):
        raise ValueError('invalid_url')
    p = urlsplit(value)
    if p.scheme != 'https' or p.username is not None or p.password is not None or p.fragment or p.port not in (None, 443):
        raise ValueError('invalid_url')
    host = p.hostname
    if host in ('chzzk.naver.com', 'www.chzzk.naver.com') and re.fullmatch(r'/video/\d+/?', p.path) and not p.query:
        ident = p.path.rstrip('/').rsplit('/', 1)[1]
        return {'url': 'https://chzzk.naver.com/video/' + ident, 'platform': 'chzzk', 'video_id': ident, 'playlist': False}
    if host in ('chzzk.naver.com', 'www.chzzk.naver.com') and re.fullmatch(r'/clips/[A-Za-z0-9]{1,100}/?', p.path) and not p.query:
        ident = p.path.rstrip('/').rsplit('/', 1)[1]
        return {'url': 'https://chzzk.naver.com/clips/' + ident, 'platform': 'chzzk', 'video_id': ident, 'playlist': False, 'clip': True}
    if host in ('ci.me', 'www.ci.me') and re.fullmatch(r'/@[A-Za-z0-9_.-]{1,100}/vods/\d+/?', p.path) and not p.query:
        return {'url': 'https://ci.me' + p.path.rstrip('/'), 'platform': 'cime', 'video_id': p.path.rstrip('/').rsplit('/', 1)[1], 'playlist': False}
    query = parse_qs(p.query, keep_blank_values=True)
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        if p.path == '/playlist' and allow_playlist and len(query.get('list', [])) == 1 and re.fullmatch(r'[A-Za-z0-9_-]{1,150}', query['list'][0]):
            return {'url': 'https://www.youtube.com/playlist?list=' + query['list'][0], 'platform': 'youtube', 'video_id': query['list'][0], 'playlist': True}
        ident = query.get('v', [''])[0] if p.path == '/watch' and len(query.get('v', [])) == 1 else ''
    elif host == 'youtu.be' and re.fullmatch(r'/[A-Za-z0-9_-]{11}/?', p.path):
        ident = p.path.strip('/')
    else:
        ident = ''
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', ident):
        return {'url': 'https://www.youtube.com/watch?v=' + ident, 'platform': 'youtube', 'video_id': ident, 'playlist': False}
    raise ValueError('unsupported_url')


def validate_options(item, metadata):
    if not isinstance(item, dict) or item.get('quality') not in {q['id'] for q in metadata['qualities']}:
        raise ValueError('분석 결과에 있는 품질을 선택해 주세요.')
    section = item.get('section') or None
    if section:
        if not isinstance(section, str) or not metadata['supports']['section']:
            raise ValueError('이 영상은 구간 옵션을 지원하지 않습니다.')
        match = re.fullmatch(r'(\d{2}):([0-5]\d):([0-5]\d)-(\d{2}):([0-5]\d):([0-5]\d)', section)
        if not match:
            raise ValueError('구간은 HH:MM:SS-HH:MM:SS 형식으로 입력해 주세요.')
        a, b, c, d, e, f = map(int, match.groups())
        end = d * 3600 + e * 60 + f
        duration = metadata.get('duration_seconds')
        if a * 3600 + b * 60 + c >= end or (duration is not None and end > duration):
            raise ValueError('영상 길이 안에서 시작보다 늦은 종료 시간을 입력해 주세요.')
    elif item.get('section') is not None and not isinstance(item['section'], str):
        raise ValueError('구간은 문자열이어야 합니다.')
    split = item.get('split', 1)
    if type(split) is not int or split not in (1, 4, 8, 12, 16) or (split != 1 and not metadata['supports']['split']):
        raise ValueError('분할 옵션이 올바르지 않습니다.')
    speed = item.get('speed_limit') or ''
    if item.get('speed_limit') is not None and not isinstance(item['speed_limit'], str):
        raise ValueError('속도 제한은 문자열이어야 합니다.')
    if speed and (not metadata['supports']['speed'] or not re.fullmatch(r'[1-9]\d{0,5}[KMG]?', speed)):
        raise ValueError('속도 제한은 YouTube에서 5M 같은 값으로 입력해 주세요.')
    return {key: metadata[key] for key in ('url', 'platform', 'title')} | {
        'quality': item['quality'], 'section': section, 'split': split, 'speed_limit': speed}


def clean_text(value, length=500):
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or ''))
    # Metadata labels are display text, never an alternate channel for stream URLs.
    text = re.sub(r'https?://\S+', '[주소]', text, flags=re.IGNORECASE)
    return text[:length]


def metadata(core, url):
    parsed = parse_url(url, allow_playlist=False)
    platform = parsed['platform']
    if parsed.get('clip'):
        from .clips import resolve_clip
        info = resolve_clip(parsed['video_id'])
        return {key: parsed[key] for key in ('url', 'platform', 'video_id')} | {
            'title': clean_text(info['title']), 'duration_seconds': info['duration'],
            'qualities': [{'id': q['id'], 'label': f"{q['width']}×{q['height']} · MP4"} for q in info['formats']],
            'supports': {'section': False, 'split': False, 'speed': False}, 'error': None}
    func = getattr(core, {'youtube': 'getYoutubeQualities', 'chzzk': 'getVODQualities', 'cime': 'getCimeVODQualities'}[platform])
    qualities, info = func(parsed['url'])
    if not qualities or not isinstance(info, dict):
        code = classify_diagnostic(getattr(core, '_LAST_CHZZK_VOD_ERROR', '')) if platform == 'chzzk' else 'engine_operation_failed'
        raise ReplayFailure(code)
    public = []
    seen = set()
    for q in qualities:
        if isinstance(q, (tuple, list)) and len(q) >= 2:
            label, ident = q[:2]
        elif isinstance(q, dict):
            ident = q.get('id') or q.get('quality')
            label = q.get('label') or q.get('quality') or ident
            height, fps = q.get('height'), q.get('frameRate')
            if height and not q.get('label'):
                label = str(height) + 'p' + (str(fps) if fps else '')
            transport = str(q.get('downloadType') or '')
            if 'hls' in transport:
                label = str(label) + ' · HLS'
            elif 'dash' in transport:
                label = str(label) + ' · DASH'
        else:
            continue
        ident = str(ident or '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', ident) or ident in seen:
            continue
        seen.add(ident)
        public.append({'id': ident, 'label': clean_text(label, 200)})
    if not public:
        raise ValueError('metadata_failed')
    duration = info.get('duration_seconds', info.get('duration', info.get('durationInSeconds')))
    try:
        duration = float(duration)
        if not math.isfinite(duration) or duration <= 0:
            duration = None
    except (TypeError, ValueError):
        duration = None
    hls = platform == 'chzzk' and info.get('inKey') is None
    # Distinguish equal resolution representations without changing their IDs.
    for q in public:
        if sum(other['label'] == q['label'] for other in public) > 1:
            q['label'] += ' · ' + q['id']
    return {key: parsed[key] for key in ('url', 'platform', 'video_id')} | {
        'title': clean_text(info.get('title') or info.get('videoTitle') or '영상'),
        'duration_seconds': duration, 'qualities': public,
        'supports': {'section': platform == 'cime' or (platform == 'chzzk' and not hls),
                     'split': platform == 'chzzk' and not hls, 'speed': platform == 'youtube'}, 'error': None}


def analyze_urls(core, urls):
    expanded = []
    for value in urls:
        p = parse_url(value)
        if p['playlist']:
            entries = core.getPlaylistItems(p['url'])
            if not entries:
                expanded.append(p | {'error': GENERIC_ERROR})
                continue
            for entry in entries[:MAX_ITEMS]:
                if re.fullmatch(r'[A-Za-z0-9_-]{11}', str(entry)):
                    entry = 'https://www.youtube.com/watch?v=' + str(entry)
                try:
                    expanded.append(parse_url(entry, allow_playlist=False))
                except ValueError:
                    continue
        else:
            expanded.append(p)
        if len(expanded) >= MAX_ITEMS:
            break
    results, seen = [], set()
    for p in expanded[:MAX_ITEMS]:
        if p['url'] in seen:
            continue
        seen.add(p['url'])
        try:
            if p.get('error'):
                raise ValueError('playlist_failed')
            results.append(metadata(core, p['url']))
        except (Exception, SystemExit) as exc:
            results.append({key: p[key] for key in ('url', 'platform', 'video_id')} | {
                'title': '', 'duration_seconds': None, 'qualities': [],
                'supports': {'section': False, 'split': False, 'speed': False}, 'error': ERROR_MESSAGES[classify_exception(exc)]})
    return results


PROVIDER_DOMAINS = {
    'chzzk': ('naver.com', 'navercorp.com', 'pstatic.net', 'akamaized.net'),
    'cime': ('ci.me',),
    'youtube': ('youtube.com', 'youtu.be', 'googlevideo.com', 'ytimg.com', 'google.com'),
}


def provider_url(url, platform):
    p = urlsplit(url)
    if p.scheme != 'https' or p.username is not None or p.password is not None or p.fragment or p.port not in (None, 443):
        raise ValueError('unsafe_provider_url')
    host = p.hostname or ''
    try:
        ipaddress.ip_address(host)
        raise ValueError('unsafe_provider_url')
    except ValueError as exc:
        if str(exc) == 'unsafe_provider_url':
            raise
    suffixes = PROVIDER_DOMAINS[platform] if platform else sum(PROVIDER_DOMAINS.values(), ())
    if not any(host == suffix or host.endswith('.' + suffix) for suffix in suffixes):
        raise ValueError('unsafe_provider_host')
    return url


def media_extension(containers, path):
    # ffprobe uses the joint demuxer name "matroska,webm" for MKV too.
    # Preserve an explicit WebM name only after probing that container family;
    # extensionless/forced-MKV outputs use the general Matroska extension.
    if 'matroska' in containers:
        return '.webm' if Path(path).suffix.lower() == '.webm' else '.mkv'
    if 'webm' in containers:
        return '.webm'
    return '.mp4' if any(x in containers for x in ('mov', 'mp4')) else None


def probe_file(path):
    tool = shutil.which('ffprobe')
    if not tool:
        raise ValueError('ffprobe_missing')
    result = subprocess.run([tool, '-v', 'error', '-show_entries', 'format=duration,format_name:stream=codec_type',
                             '-of', 'json', str(path)], capture_output=True, timeout=45, check=True)
    data = json.loads(result.stdout)
    duration = float(data.get('format', {}).get('duration', 0))
    if not math.isfinite(duration) or duration <= 0 or not any(s.get('codec_type') == 'video' for s in data.get('streams', [])):
        raise ValueError('invalid_media')
    names = data.get('format', {}).get('format_name', '').split(',')
    ext = media_extension(names, path)
    if ext is None:
        raise ValueError('unsupported_media_container')
    return {'duration_seconds': duration, 'extension': ext}


@contextlib.contextmanager
def guarded_core(core, platform, emit, duration=None, diagnostics=None):
    """Observe ffmpeg on a dedicated FD, leaving Streamlink's media pipe intact."""
    original_subprocess = core.subprocess
    original_popen = original_subprocess.Popen
    threads = []
    children = []
    diagnostics = diagnostics if diagnostics is not None else {}

    def popen(args, *positional, **kwargs):
        if not isinstance(args, (tuple, list)) or kwargs.get('shell') or kwargs.get('start_new_session') or kwargs.get('preexec_fn'):
            raise ValueError('unsafe_command')
        argv = list(args)
        tool = Path(str(argv[0])).name
        if tool not in ('ffmpeg', 'ffprobe', 'streamlink', 'aria2c', 'yt-dlp', 'deno'):
            raise ValueError('unexpected_tool')
        for value in argv[1:]:
            if isinstance(value, str) and value.startswith(('http://', 'https://')):
                provider_url(value, platform)
        if tool == 'yt-dlp' and '--flat-playlist' in argv:
            argv += ['--playlist-end', str(MAX_ITEMS)]
        read_fd = write_fd = None
        capture_stderr = False
        if tool == 'ffmpeg':
            for index, arg in enumerate(argv[:-1]):
                if arg == '-i' and argv[index + 1] != 'pipe:0':
                    provider_url(argv[index + 1], platform)
            read_fd, write_fd = os.pipe()
            argv[1:1] = ['-progress', 'pipe:' + str(write_fd), '-nostats', '-protocol_whitelist', 'https,tls,tcp,crypto,pipe']
            kwargs['pass_fds'] = tuple(kwargs.get('pass_fds', ())) + (write_fd,)
            # Capture only inherited stderr on direct ffmpeg operations. Existing
            # PIPE readers (especially Streamlink's pipeline) retain ownership.
            direct_input = '-i' in argv and argv.index('-i') + 1 < len(argv) and argv[argv.index('-i') + 1] != 'pipe:0'
            if kwargs.get('stderr') is None and not positional and direct_input:
                kwargs['stderr'] = subprocess.PIPE
                capture_stderr = True
        try:
            process = original_popen(argv, *positional, **kwargs)
            children.append(process)
        except BaseException:
            if read_fd is not None:
                os.close(read_fd)
            raise
        finally:
            if write_fd is not None:
                os.close(write_fd)
        if read_fd is not None:
            def drain():
                with os.fdopen(read_fd, 'r', encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        key, _, value = line.strip().partition('=')
                        if key == 'out_time_us':
                            try:
                                seconds = max(0, int(value)) / 1_000_000
                                measured_duration = getattr(core, '_replay_duration', duration)
                                emit({'event': 'progress', 'stage': 'downloading',
                                      'progress': min(99.9, seconds / measured_duration * 100) if measured_duration else None})
                            except (TypeError, ValueError):
                                pass
            thread = threading.Thread(target=drain, daemon=True)
            threads.append(thread)
            thread.start()
        if capture_stderr:
            def drain_stderr():
                # Bounded reads prevent an unbroken native line from growing
                # memory. Keep only fixed classification codes after each read.
                tail = '' if process.text_mode else b''
                while True:
                    chunk = process.stderr.read(4096)
                    if not chunk:
                        break
                    value = tail + chunk
                    record_diagnostic(diagnostics, value)
                    tail = value[-256:]
            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            threads.append(stderr_thread)
            stderr_thread.start()
        return process

    proxy = types.SimpleNamespace(**{name: getattr(original_subprocess, name) for name in dir(original_subprocess) if not name.startswith('__')})
    proxy.Popen = popen
    # subprocess.run uses its module globals; rebind it so metadata requests pass
    # through the same command/URL guard without changing the shared stdlib module.
    proxy.run = types.FunctionType(original_subprocess.run.__code__, dict(original_subprocess.run.__globals__, Popen=popen),
                                   original_subprocess.run.__name__, original_subprocess.run.__defaults__, original_subprocess.run.__closure__)
    proxy.run.__kwdefaults__ = original_subprocess.run.__kwdefaults__
    core.subprocess = proxy
    try:
        import requests
        old_send = requests.sessions.Session.send

        def send(session, request, **kwargs):
            provider_url(request.url, platform)
            try:
                response = old_send(session, request, **kwargs)
            except Exception as exc:
                code = classify_exception(exc)
                if code != 'engine_operation_failed':
                    diagnostics['code'] = code
                raise
            if response.status_code in (400, 401, 403):
                diagnostics['code'] = 'http_' + str(response.status_code)
            return response

        requests.sessions.Session.send = send
        yield
    finally:
        requests.sessions.Session.send = old_send
        core.subprocess = original_subprocess
        for thread in threads:
            thread.join(timeout=1)
        # The preserved Streamlink branch waits for both children, but leaves
        # their stderr wrappers open. This adapter owns those native handles.
        for child in children:
            if child.poll() is not None:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()


def run_download(core, task, work_dir, emit, probe=probe_file):
    parsed = parse_url(task['url'], allow_playlist=False)
    directory = Path(work_dir)
    if directory.is_symlink() or not directory.is_dir() or any(directory.iterdir()):
        raise ValueError('dedicated_empty_work_required')
    directory = directory.resolve(strict=True)
    current = metadata(core, parsed['url'])
    if task.get('quality') not in {q['id'] for q in current['qualities']}:
        raise ReplayFailure('quality_changed')
    options = validate_options(task, current)
    core._replay_duration = current.get('duration_seconds')
    if options['section']:
        start, end = options['section'].split('-')
        def seconds(hms):
            h, m, s = map(int, hms.split(':'))
            return h * 3600 + m * 60 + s
        core._replay_duration = seconds(end) - seconds(start)
    if parsed.get('clip'):
        from .clips import resolve_clip, validate_media_url
        info = resolve_clip(parsed['video_id'])
        selected = next((q for q in info['formats'] if q['id'] == options['quality']), None)
        if selected is None:
            raise ReplayFailure('quality_changed')
        url = validate_media_url(selected['url'])
        target = directory / 'video.mp4'
        emit({'event': 'stage', 'stage': 'downloading'})
        with core.subprocess.Popen([core.getFFmpeg(), '-nostdin', '-n', '-i', url,
                                    '-map', '0:v:0', '-map', '0:a?', '-c', 'copy', str(target)],
                                   stdout=core.subprocess.DEVNULL) as process:
            if process.wait() != 0:
                raise ReplayFailure('engine_operation_failed')
        emit({'event': 'stage', 'stage': 'validating'})
        details = probe(target)
        return {'name': target.name, 'size': target.stat().st_size, **details}
    original_duplicate = core.checkDuplicateFile
    replacements = {}

    def duplicate(path):
        p = Path(path)
        if p.parent.resolve() != directory or p.is_symlink() or p.exists():
            raise ValueError('output_conflict')
        return True, ''

    # Every second lookup inside the original download function must retain the
    # selected exact ID. The original core's permissive height fallback cannot run.
    for name in ('getYoutubeQualities', 'getVODQualities', 'getCimeVODQualities'):
        old = getattr(core, name, None)
        if old is None:
            continue
        replacements[name] = old

        def exact(*args, _old=old, **kwargs):
            result = _old(*args, **kwargs)
            qs = result[0] or []
            ids = {str(q[1] if isinstance(q, (list, tuple)) else q.get('id') or q.get('quality')) for q in qs}
            if options['quality'] not in ids:
                raise ReplayFailure('quality_changed')
            return result

        setattr(core, name, exact)
    core.checkDuplicateFile = duplicate
    section = options['section'].replace('-', '~') if options['section'] else None
    emit({'event': 'stage', 'stage': 'downloading'})
    try:
        if parsed['platform'] == 'youtube':
            result = core.downloadYoutube(parsed['url'], options['quality'], str(directory), 'video', options['speed_limit'])
        elif parsed['platform'] == 'cime':
            result = core.downloadCimeVOD(parsed['url'], options['quality'], str(directory), 'video.mp4', section)
        else:
            split = {1: '분할 없음', 4: '25%', 8: '50%', 12: '75%', 16: '100%'}[options['split']]
            result = core.downloadVOD(parsed['url'], options['quality'], str(directory), 'video.mp4', split, section)
    finally:
        core.checkDuplicateFile = original_duplicate
        for name, previous in replacements.items():
            setattr(core, name, previous)
    if result is False:
        raise ValueError('download_failed')
    emit({'event': 'stage', 'stage': 'validating'})
    candidates = []
    for path in directory.iterdir():
        if path.is_symlink() or path.is_dir():
            raise ValueError('unsafe_output')
        # yt-dlp can produce a complete extensionless `video` file. Probe decides
        # its container; extension is never treated as proof of a valid video.
        if path.name == 'video' or path.suffix.lower() in ('.mp4', '.mkv', '.webm'):
            if path.stat().st_size <= 0:
                continue
            details = probe(path)
            candidates.append({'name': path.name, 'size': path.stat().st_size, **details})
    if len(candidates) != 1:
        raise ValueError('single_valid_media_required')
    return candidates[0]
