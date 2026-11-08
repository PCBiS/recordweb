"""Offline security contracts for a RecordWEB dependency candidate.

Run with Python in an isolated container (--network none, no host mounts).
Only generated fixture bytes and a temporary synthetic file are accessed.
"""
import ctypes
import http.client
import importlib.metadata
import io
import json
import pathlib
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brotli
from streamlink import Streamlink
from streamlink.exceptions import PluginError
from streamlink.stream.dash import DASHStream
from urllib3.exceptions import ProtocolError
from urllib3.response import HTTPResponse, _MAX_CHUNK_LINE_LENGTH

EXPECTED = {'Brotli': '1.2.1', 'urllib3': '2.8.0', 'aiohttp': '3.14.5', 'streamlink': '8.6.3', 'Pillow': '13.0.0', 'yt-dlp': '2026.10.26', 'certifi': '2026.10.23', 'cryptography': '50.0.2', 'multidict': '6.9.1', 'yarl': '1.25.1'}
SYSTEM_EXPECTED = '1.0.9-2+deb12u1'


def versions():
    actual = {name: importlib.metadata.version(name) for name in EXPECTED}
    assert actual == EXPECTED, actual
    system = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'libbrotli1'], text=True)
    assert system == SYSTEM_EXPECTED, system
    subprocess.run(['python', '-m', 'pip', 'check'], capture_output=True, check=True)
    from cryptography.fernet import Fernet
    from cryptography.hazmat.backends.openssl import backend
    from PIL import Image
    import ssl
    system_ssl = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'libssl3'], text=True)
    assert system_ssl == '3.0.22-1~deb12u2', system_ssl
    assert ssl.OPENSSL_VERSION.startswith('OpenSSL 3.0.22') and backend.openssl_version_text().startswith('OpenSSL 4.0.3')
    fernet = Fernet(Fernet.generate_key())
    assert fernet.decrypt(fernet.encrypt(b'synthetic')) == b'synthetic'
    for image_format in ('PNG', 'JPEG2000'):
        data = io.BytesIO()
        Image.new('RGB', (16, 16)).save(data, format=image_format)
        data.seek(0)
        with Image.open(data) as decoded:
            decoded.load()
            assert decoded.size == (16, 16)
    return {'python_packages': actual, 'libbrotli1': system, 'pip_check': 'passed'}


def brotli_contracts():
    payload = (b'Generated Brotli roundtrip fixture.\x00\xff' * 2048)
    compressed = brotli.compress(payload)
    assert brotli.decompress(compressed) == payload
    encoder = ctypes.CDLL('libbrotlienc.so.1')
    decoder = ctypes.CDLL('libbrotlidec.so.1')
    size_t = ctypes.c_size_t
    byte_p = ctypes.POINTER(ctypes.c_ubyte)
    encoder.BrotliEncoderMaxCompressedSize.argtypes = [size_t]
    encoder.BrotliEncoderMaxCompressedSize.restype = size_t
    encoder.BrotliEncoderCompress.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, size_t, byte_p, ctypes.POINTER(size_t), byte_p]
    encoder.BrotliEncoderCompress.restype = ctypes.c_int
    decoder.BrotliDecoderDecompress.argtypes = [size_t, byte_p, ctypes.POINTER(size_t), byte_p]
    decoder.BrotliDecoderDecompress.restype = ctypes.c_int
    source = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    capacity = encoder.BrotliEncoderMaxCompressedSize(len(payload))
    encoded = (ctypes.c_ubyte * capacity)()
    encoded_size = size_t(capacity)
    assert encoder.BrotliEncoderCompress(5, 22, 0, len(payload), source, ctypes.byref(encoded_size), encoded) == 1
    decoded = (ctypes.c_ubyte * len(payload))()
    decoded_size = size_t(len(payload))
    assert decoder.BrotliDecoderDecompress(encoded_size.value, encoded, ctypes.byref(decoded_size), decoded) == 1
    assert bytes(decoded[:decoded_size.value]) == payload
    return {'python_roundtrip': True, 'debian_native_roundtrip': True, 'input_bytes': len(payload)}


class TrackedBytes(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.readline_sizes = []
        self.bytes_read_by_readline = 0

    def readline(self, size=-1):
        self.readline_sizes.append(size)
        result = super().readline(size)
        self.bytes_read_by_readline += len(result)
        return result


class SyntheticSocket:
    def __init__(self, data):
        self.stream = TrackedBytes(data)

    def makefile(self, mode):
        return self.stream


def chunk_response(line):
    socket = SyntheticSocket(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + line + b'abc\r\n0\r\n\r\n')
    raw = http.client.HTTPResponse(socket, method="GET")
    raw.begin()
    socket.stream.readline_sizes.clear()
    socket.stream.bytes_read_by_readline = 0
    response = HTTPResponse(body=raw, original_response=raw, headers=dict(raw.headers.items()), preload_content=False, request_method='GET')
    return response, socket.stream


def chunk_contracts():
    limit = _MAX_CHUNK_LINE_LENGTH
    assert limit == 65536
    # A valid extension-filled size line exactly at the limit remains usable.
    accepted_line = b'3;' + b'x' * (limit - 4) + b'\r\n'
    response, trace = chunk_response(accepted_line)
    assert b''.join(response.read_chunked()) == b'abc'
    assert trace.readline_sizes[0] == limit + 1
    response.close()
    # Hex-only excessive input must be bounded before arbitrary-size integer conversion.
    response, trace = chunk_response(b'f' * (limit + 1024) + b'\r\n')
    try:
        list(response.read_chunked())
    except ProtocolError as error:
        assert 'chunk size line exceeded maximum allowed length' in str(error)
    else:
        raise AssertionError('overlong chunk size unexpectedly accepted')
    assert trace.readline_sizes == [limit + 1], trace.readline_sizes
    assert trace.bytes_read_by_readline == limit + 1
    response.close()
    return {'maximum_line_bytes': limit, 'exact_limit_valid_line_accepted': True, 'overlong_rejected': True, 'rejection_read_bytes': trace.bytes_read_by_readline}


def manifest(location):
    return ('<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
            'profiles="urn:mpeg:dash:profile:isoff-main:2011" minBufferTime="PT1S" mediaPresentationDuration="PT1S">'
            + ('<Location>' + location + '</Location>' if location else '')
            + '<Period duration="PT1S"><AdaptationSet mimeType="video/mp4"><Representation id="v" '
            'bandwidth="1000" width="16" height="16"><SegmentTemplate media="seg-$Number$.m4s" '
            'initialization="init.mp4" duration="1" timescale="1" startNumber="1"/>'
            '</Representation></AdaptationSet></Period></MPD>')


def streamlink_contracts():
    fixture = b'synthetic-file-fixture; no operational data'
    with tempfile.TemporaryDirectory(prefix='dependency-security-') as temporary:
        target = pathlib.Path(temporary, 'fixture.txt')
        target.write_bytes(fixture)
        file_uri = target.as_uri()
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hits.append(self.path)
                if self.path in ('/redirect-normal', '/redirect-file'):
                    self.send_response(302)
                    self.send_header('Location', '/ok' if self.path.endswith('normal') else file_uri)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/ok':
                    body = b'generated-normal-http-response'
                elif self.path == '/mpd-bad':
                    body = manifest(file_uri).encode()
                elif self.path == '/mpd-good':
                    body = manifest(f'http://127.0.0.1:{self.server.server_port}/replacement.mpd').encode()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = Streamlink()
        session.http.trust_env = False
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            # Establish that the generated file is readable directly, so the negative
            # redirect test cannot pass merely because the target does not exist.
            assert session.http.get(file_uri).content == fixture
            assert session.http.get(base + '/redirect-normal').content == b'generated-normal-http-response'
            try:
                session.http.get(base + '/redirect-file')
            except PluginError as error:
                assert type(error.err).__name__ == 'InvalidRedirectURL'
                assert 'Disallowed redirection to file://' in str(error)
                redirect_error = type(error.err).__name__
            else:
                raise AssertionError('HTTP redirect to file unexpectedly accepted')
            try:
                DASHStream.parse_manifest(session, base + '/mpd-bad')
            except PluginError as error:
                assert 'Prevented access to insecure MPD location' in str(error)
                assert "scheme='file'" in str(error)
                mpd_error = type(error.__cause__).__name__
                assert mpd_error == 'MPDParsingError'
            else:
                raise AssertionError('DASH file Location unexpectedly accepted')
            streams = DASHStream.parse_manifest(session, base + '/mpd-good')
            assert streams, 'normal HTTP DASH manifest produced no streams'
            mpd = DASHStream.parse_mpd(manifest(base + '/replacement.mpd'), {'url': base + '/original.mpd', 'base_url': base + '/original.mpd'})
            assert mpd.url == base + '/replacement.mpd'
            assert all(path in hits for path in ('/redirect-normal', '/ok', '/redirect-file', '/mpd-bad', '/mpd-good'))
            return {'synthetic_file_positive_control': True, 'same_scheme_http_redirect_accepted': True,
                    'http_to_file_redirect_blocked': redirect_error, 'dash_file_location_blocked': mpd_error,
                    'same_scheme_dash_location_accepted': True, 'normal_dash_streams': len(streams)}
        finally:
            session.http.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    print(json.dumps({'versions': versions(), 'brotli': brotli_contracts(), 'urllib3': chunk_contracts(),
                      'streamlink': streamlink_contracts(), 'result': 'passed'}, sort_keys=True))
