"""RAM-only transport for Naver HLS segment query authentication."""
from pathlib import Path
import re
import threading
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

import requests

from .errors import ReplayFailure, classify_exception, record_diagnostic

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_REWRITTEN_BYTES = 32 * 1024 * 1024


def _origin(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443):
            raise ValueError()
        return parsed.hostname.lower(), 443
    except ValueError:
        raise ReplayFailure('protocol_denied') from None


def _query_key(component):
    return unquote_plus(component.split('=', 1)[0])


def _naver_origin(url):
    origin = _origin(url)
    host = origin[0]
    if not (host == 'pstatic.net' or host.endswith('.pstatic.net')):
        raise ReplayFailure('protocol_denied')
    return origin


def is_signed_naver_hls(url):
    try:
        _naver_origin(url)
        return urlsplit(url).path.lower().endswith('.m3u8') and any(
            _query_key(part) == '_lsu_sa_' for part in urlsplit(url).query.split('&'))
    except (ReplayFailure, TypeError):
        return False


def _media_url(parent, child):
    absolute = urljoin(parent, child)
    child_origin = _naver_origin(absolute)
    if child_origin != _naver_origin(parent):
        return absolute
    parsed = urlsplit(absolute)
    query = parsed.query.split('&') if parsed.query else []
    keys = {_query_key(part) for part in query}
    for part in urlsplit(parent).query.split('&'):
        key = _query_key(part)
        if part and key not in keys:
            query.append(part)
            keys.add(key)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '&'.join(query), ''))


def rewrite_media_playlist(content, manifest_url):
    """Rewrite media URI lines and KEY/MAP URIs, preserving raw signed bytes."""
    if not is_signed_naver_hls(manifest_url):
        raise ReplayFailure('protocol_denied')
    if isinstance(content, bytes):
        if len(content) > MAX_MANIFEST_BYTES:
            raise ReplayFailure('invalid_media')
        try:
            content = content.decode('utf-8-sig')
        except UnicodeError:
            raise ReplayFailure('invalid_media') from None
    elif not isinstance(content, str) or len(content.encode('utf-8')) > MAX_MANIFEST_BYTES:
        raise ReplayFailure('invalid_media')
    lines = content.splitlines()
    if not lines or lines[0].strip() != '#EXTM3U' or '#EXT-X-ENDLIST' not in {line.strip() for line in lines}:
        raise ReplayFailure('invalid_media')
    if any(line.strip().startswith(('#EXT-X-STREAM-INF:', '#EXT-X-I-FRAME-STREAM-INF:', '#EXT-X-MEDIA:', '#EXT-X-SESSION-KEY:', '#EXT-X-SESSION-DATA:')) for line in lines):
        raise ReplayFailure('invalid_media')
    result, media_count, output_size = [], 0, 0
    for raw in lines:
        line = raw.strip()
        if line and not line.startswith('#'):
            line = _media_url(manifest_url, line)
            media_count += 1
        elif line.startswith(('#EXT-X-KEY:', '#EXT-X-MAP:')):
            uri = re.search(r'[:,]URI="([^"]*)"(?:,|$)', line)
            if uri:
                start, end = uri.span(1)
                line = line[:start] + _media_url(manifest_url, uri.group(1)) + line[end:]
            elif 'URI=' in line or line.startswith('#EXT-X-MAP:') or 'METHOD=NONE' not in line:
                raise ReplayFailure('invalid_media')
        output_size += len(line.encode('utf-8')) + 1
        if output_size > MAX_REWRITTEN_BYTES:
            raise ReplayFailure('invalid_media')
        result.append(line)
    if not media_count or not any(line.startswith('#EXTINF:') for line in lines):
        raise ReplayFailure('invalid_media')
    rewritten = ('\n'.join(result) + '\n').encode('utf-8')
    if len(rewritten) > MAX_REWRITTEN_BYTES:
        raise ReplayFailure('invalid_media')
    return rewritten


def _section_args(section):
    if not section:
        return [], []
    match = re.fullmatch(r'(\d{2}):([0-5]\d):([0-5]\d)[~-](\d{2}):([0-5]\d):([0-5]\d)', section.strip())
    if not match:
        raise ReplayFailure('invalid_media')
    a, b, c, d, e, f = map(int, match.groups())
    duration = d * 3600 + e * 60 + f - (a * 3600 + b * 60 + c)
    if duration <= 0:
        raise ReplayFailure('invalid_media')
    return ['-ss', f'{a:02}:{b:02}:{c:02}'], ['-t', str(duration)]


def _fetch_manifest(url, headers):
    # Avoid forwarding the operator's HTTP headers to a redirect destination.
    with requests.get(url, headers=headers, timeout=15, stream=True, allow_redirects=False) as response:
        if 300 <= response.status_code < 400:
            raise ReplayFailure('protocol_denied')
        response.raise_for_status()
        pieces, size = [], 0
        for part in response.iter_content(chunk_size=65536):
            size += len(part)
            if size > MAX_MANIFEST_BYTES:
                raise ReplayFailure('invalid_media')
            pieces.append(part)
        return b''.join(pieces)


def run_signed_hls(input_url, output_file, section, ffmpeg, headers, subprocess_module, set_processes, clear_processes):
    if not is_signed_naver_hls(input_url):
        raise ReplayFailure('protocol_denied')
    before, after = _section_args(section)
    try:
        payload = rewrite_media_playlist(_fetch_manifest(input_url, headers), input_url)
    except ReplayFailure:
        raise
    except Exception as exc:
        raise ReplayFailure(classify_exception(exc)) from None
    # Headers are deliberately absent: this input is a pipe, and each remote
    # segment authenticates with its own inherited signed query.
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', *before,
               '-protocol_whitelist', 'https,tls,tcp,crypto,pipe', '-f', 'hls', '-i', 'pipe:0',
               *after, '-c', 'copy', '-y', str(output_file)]
    process = subprocess_module.Popen(command, stdin=subprocess_module.PIPE, stderr=subprocess_module.PIPE)
    diagnostics = {}

    def write_manifest():
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def drain_stderr():
        tail = b''
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                break
            record_diagnostic(diagnostics, tail + chunk)
            tail = chunk[-256:]

    writer = threading.Thread(target=write_manifest, daemon=True)
    reader = threading.Thread(target=drain_stderr, daemon=True)
    try:
        set_processes([process])
        reader.start()
        writer.start()
        returncode = process.wait()
    finally:
        # The worker process group owns cancellation. On local Python errors,
        # close the native consumer so neither private pipe thread can linger.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess_module.TimeoutExpired:
                process.kill()
                process.wait()
        if writer.ident is not None:
            writer.join(timeout=5)
        if reader.ident is not None:
            reader.join(timeout=5)
        if not reader.is_alive():
            process.stderr.close()
        clear_processes()
    if returncode:
        raise ReplayFailure(diagnostics.get('code', 'engine_operation_failed'))
    if not Path(output_file).is_file() or Path(output_file).stat().st_size < 1024:
        raise ReplayFailure('invalid_media')
    return True
