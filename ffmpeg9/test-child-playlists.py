#!/usr/bin/env python3
"""Offline fixture: allowed HTTP child; forbidden FTP and local .txt child.

Run against the old image with --expect vulnerable and the candidate with
--expect fixed. No external requests, credentials, or existing media are used.
"""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading

def execute(args):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expect', choices=('fixed', 'vulnerable'), required=True)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    options = parser.parse_args()
    if options.expect == 'fixed':
        http_help = execute([options.ffmpeg, '-hide_banner', '-h', 'protocol=http'])
        assert 'reconnect_max_retries' in http_help.stdout + http_help.stderr, 'CIME reconnect_max_retries unavailable'
        hls_help = execute([options.ffmpeg, '-hide_banner', '-h', 'demuxer=hls'])
        assert 'extension_picky' in hls_help.stdout + hls_help.stderr, 'CIME extension_picky unavailable'
    with tempfile.TemporaryDirectory(prefix='rw-hls-fixture-') as temp:
        root = Path(temp)
        media = root / 'sample.ts'
        generated = execute([options.ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                             '-i', 'color=c=black:s=64x64:r=10', '-t', '2', '-an', '-c:v', 'mpeg2video',
                             '-f', 'mpegts', str(media)])
        assert generated.returncode == 0, generated.stderr
        child = '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.0,\n' + str(media) + '\n#EXT-X-ENDLIST\n'
        (root / 'child.txt').write_text(child)
        # An ordinary HTTP master/child/segment path proves valid media still works.
        (root / 'child.m3u8').write_text(child.replace(str(media), 'sample.ts'))
        (root / 'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000\nchild.m3u8\n')
        (root / 'blocked.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000\n' + (root / 'child.txt').as_uri() + '\n')
        (root / 'ftp.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000\nftp://127.0.0.1:1/child.m3u8\n')
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=temp))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def read(url):
                return execute([options.ffmpeg, '-hide_banner', '-loglevel', 'debug', '-nostdin',
                    '-protocol_whitelist', 'file,http,tcp,crypto,data,ftp', '-allowed_extensions', 'm3u8,ts',
                    '-i', url, '-map', '0:v:0', '-t', '1', '-f', 'null', '-'])
            allowed = read(f'http://127.0.0.1:{server.server_port}/master.m3u8')
            assert allowed.returncode == 0, allowed.stderr[-5000:]
            blocked = read(str(root / 'blocked.m3u8'))
            ftp = read(str(root / 'ftp.m3u8'))
            if options.expect == 'fixed':
                assert blocked.returncode != 0, 'Local .txt child unexpectedly decoded'
                assert 'blocked for security reasons' in blocked.stderr, blocked.stderr[-5000:]
                assert "Opening 'ftp://" not in ftp.stderr, ftp.stderr[-5000:]
                assert 'Connection refused' not in ftp.stderr, ftp.stderr[-5000:]
            else:
                assert blocked.returncode == 0, blocked.stderr[-5000:]
                assert 'Connection refused' in ftp.stderr, ftp.stderr[-5000:]
            print(json.dumps({'expected': options.expect, 'http_child': 'decoded',
                              'local_txt_child_exit': blocked.returncode,
                              'ftp_child_exit': ftp.returncode,
                              'local_extension_policy_enforced': 'blocked for security reasons' in blocked.stderr,
                              'ftp_open_attempted': 'Connection refused' in ftp.stderr}, sort_keys=True))
        finally:
            server.shutdown()
            server.server_close()

if __name__ == '__main__':
    main()
