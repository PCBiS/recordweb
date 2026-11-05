"""Docker execution-source transform; never modify the preserved upstream tree."""

OLD = '''            download_url = baseurl or rep_m3u
            if not download_url:
                continue

            download_type = "hls_mpd" if (".m3u8" in download_url or "video/mp2t" in mime_type) else "dash_file"
'''

NEW = '''            # HLS-MPD BaseURL is a segment directory, not a playable URL.
            # nvod:m3u carries the signed manifest for this representation.
            is_hls_mpd = "video/mp2t" in mime_type.lower()
            if is_hls_mpd:
                download_url = rep_m3u or (baseurl if ".m3u8" in baseurl.split("?", 1)[0].lower() else "")
                if rep_m3u and baseurl:
                    from urllib.parse import urljoin
                    download_url = urljoin(baseurl, rep_m3u)
            else:
                download_url = baseurl or rep_m3u
            if not download_url:
                continue

            download_type = "hls_mpd" if (".m3u8" in download_url or is_hls_mpd) else "dash_file"
'''


def transform(text):
    if text.count(OLD) != 1:
        raise ValueError('unknown_chzzk_mpd_collector_layout')
    marker = 'def _runFfmpegHttpCopy(input_url: str, output_file: str, download_section: str = None) -> bool:\n'
    if text.count(marker) != 1:
        raise ValueError('unknown_chzzk_http_copy_layout')
    hook = '''    from docker_support.replay.hls_transport import is_signed_naver_hls, run_signed_hls
    if is_signed_naver_hls(input_url):
        return run_signed_hls(input_url, output_file, download_section, getFFmpeg(),
                              getCookieHeaders(), subprocess, _set_current_processes, _clear_current_processes)
'''
    return text.replace(OLD, NEW, 1).replace(marker, marker + hook, 1)
