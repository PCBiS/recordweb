import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
import requests
import json
import html as html_lib
from urllib.parse import urljoin, unquote

from module import data_manager

_this_dir = os.path.dirname(os.path.abspath(__file__))

if os.path.basename(_this_dir).lower() == "module":
    base_dir = os.path.dirname(_this_dir)
else:
    base_dir = _this_dir

VERSION = "치지직/유튜브/씨미 VOD 다운로더 v1.1.0f"

CHZZK_HLS_QUALITY_RE = re.compile(
    r"^(best|worst|(?:144|240|360|480|720|1080|1440|2160)p)$",
    re.IGNORECASE
)

NVOD_NS = "urn:naver:vod:2020"
MPD_NS = "urn:mpeg:dash:schema:mpd:2011"


def _to_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def normalizeChzzkHlsQuality(value, fallback: str = "best") -> str:
    q = str(value or "").strip().lower()

    if CHZZK_HLS_QUALITY_RE.fullmatch(q):
        return q

    m = re.match(r"^(144|240|360|480|720|1080|1440|2160)(?:p)?(?:\d{2,3})?$", q)
    if m:
        return f"{m.group(1)}p"

    return fallback


def makeChzzkHlsQualityLabel(video_height) -> str:
    h = _to_int(video_height, 0)
    if h <= 0:
        return ""
    return f"{h}p"


def _streamlinkHeaderArgs() -> list[str]:
    headers = getCookieHeaders()
    args = []

    cookie_str = headers.get("Cookie", "")
    user_agent = headers.get("User-Agent", "")
    referer = headers.get("Referer", "")
    origin = headers.get("Origin", "")

    if cookie_str:
        args += ["--http-header", f"Cookie={cookie_str}"]
    if referer:
        args += ["--http-header", f"Referer={referer}"]
    if origin:
        args += ["--http-header", f"Origin={origin}"]
    if user_agent:
        args += ["--http-header", f"User-Agent={user_agent}"]

    return args


def _ffmpegHeaderArgs() -> list[str]:
    headers = getCookieHeaders()
    lines = []

    if headers.get("Cookie"):
        lines.append(f"Cookie: {headers.get('Cookie')}")
    if headers.get("User-Agent"):
        lines.append(f"User-Agent: {headers.get('User-Agent')}")
    if headers.get("Referer"):
        lines.append(f"Referer: {headers.get('Referer')}")
    if headers.get("Origin"):
        lines.append(f"Origin: {headers.get('Origin')}")

    if not lines:
        return []
    return ["-headers", "\r\n".join(lines) + "\r\n"]


def _hideCookieArgs(args: list[str]) -> list[str]:
    out = []
    hide_next = False
    for a in args:
        if hide_next:
            out.append("Cookie=<hidden>")
            hide_next = False
            continue
        out.append(a)
        if a == "--http-header":
            hide_next = False
        if isinstance(a, str) and a.lower().startswith("cookie="):
            out[-1] = "Cookie=<hidden>"
    return out


def _formatCmdForLog(args: list[str]) -> str:
    safe = []
    for a in args:
        s = str(a)
        low = s.lower()
        if low.startswith("cookie=") or low.startswith("cookie:"):
            s = s.split("=", 1)[0] + "=<hidden>" if "=" in s else "Cookie: <hidden>"
        elif any(x in low for x in ("nid_ses=", "nid_aut=", "mauth-authorization-code=", "session-id=")):
            s = "<headers hidden>"
        safe.append(s)
    return " ".join(safe)


def collectChzzkHlsQualitiesFromPlaybackJson(playback_json) -> list[dict]:
    if isinstance(playback_json, str):
        playback_data = json.loads(playback_json)
    elif isinstance(playback_json, dict):
        playback_data = playback_json
    else:
        return []

    media_list = playback_data.get("media") or []
    q_by_label = {}

    for media_entry in media_list:
        for track in media_entry.get("encodingTrack", []) or []:
            try:
                if track.get("audioOnly"):
                    continue

                h = _to_int(track.get("videoHeight") or 0, 0)
                label = makeChzzkHlsQualityLabel(h)
                if not label:
                    continue

                bitrate = _to_int(track.get("videoBitRate") or 0, 0)
                item = {
                    "id": label,
                    "quality": label,
                    "bandwidth": bitrate,
                    "width": track.get("videoWidth"),
                    "height": h,
                    "frameRate": track.get("videoFrameRate"),
                    "trackId": str(track.get("encodingTrackId") or "").strip(),
                    "downloadType": "streamlink_hls"
                }

                old = q_by_label.get(label)
                old_bw = _to_int((old or {}).get("bandwidth") or 0, 0)
                if old is None or bitrate > old_bw:
                    q_by_label[label] = item
            except Exception:
                continue

    q_list = list(q_by_label.values())
    q_list.sort(key=lambda x: _to_int(x.get("height") or 0, 0), reverse=True)
    return q_list


def resolveChzzkHlsQualityFromVodInfo(vod_info: dict, quality: str, fallback: str = "best") -> str:
    raw_quality = str(quality or "").strip().lower()

    normalized = normalizeChzzkHlsQuality(raw_quality, "")
    if normalized:
        return normalized

    try:
        live_rewind_json_str = vod_info.get("liveRewindPlaybackJson")
        if not live_rewind_json_str:
            return fallback

        playback_data = json.loads(live_rewind_json_str)
        for media_entry in playback_data.get("media") or []:
            for track in media_entry.get("encodingTrack", []) or []:
                if track.get("audioOnly"):
                    continue

                track_id = str(track.get("encodingTrackId") or "").strip().lower()
                if track_id and track_id == raw_quality:
                    label = makeChzzkHlsQualityLabel(track.get("videoHeight"))
                    if label:
                        return label
    except Exception:
        pass

    return fallback


def _nvodAttr(elem, name: str) -> str:
    return elem.get(f"{{{NVOD_NS}}}{name}") or elem.get(f"nvod:{name}") or ""


def _findNvodLabel(rep, kind: str, ns: dict) -> str:
    elem = rep.find(f"nvod:Label[@kind='{kind}']", ns)
    if elem is not None and elem.text:
        return elem.text.strip()
    return ""


def collectChzzkMpdQualities(mpd_content) -> list[dict]:
    if isinstance(mpd_content, bytes):
        mpd_content = mpd_content.strip()
    else:
        mpd_content = str(mpd_content or "").strip().encode("utf-8")

    root = ET.fromstring(mpd_content)
    ns = {'mpd': MPD_NS, 'nvod': NVOD_NS}
    q_list = []

    for adaptation in root.findall('.//mpd:AdaptationSet', ns):
        mime_type = adaptation.get('mimeType') or ""
        if "video/" not in mime_type:
            continue

        adaptation_m3u = _nvodAttr(adaptation, "m3u")

        for rep in adaptation.findall('mpd:Representation', ns):
            rep_id = str(rep.get('id') or "").strip()
            if not rep_id:
                continue

            bandwidth = rep.get('bandwidth')
            width = rep.get('width')
            height = rep.get('height')
            frame_rate = rep.get('frameRate') or _findNvodLabel(rep, 'fps', ns)
            quality_value = _findNvodLabel(rep, 'qualityId', ns) or rep_id
            resolution = _findNvodLabel(rep, 'resolution', ns) or height
            rep_m3u = _nvodAttr(rep, "m3u") or adaptation_m3u

            baseurl_elem = rep.find('mpd:BaseURL', ns)
            baseurl = baseurl_elem.text.strip() if baseurl_elem is not None and baseurl_elem.text else ""

            download_url = baseurl or rep_m3u
            if not download_url:
                continue

            download_type = "hls_mpd" if (".m3u8" in download_url or "video/mp2t" in mime_type) else "dash_file"

            q_list.append({
                'id': rep_id,
                'quality': quality_value,
                'bandwidth': bandwidth,
                'width': width,
                'height': resolution or height,
                'frameRate': frame_rate,
                'baseurl': download_url,
                'downloadType': download_type,
                'mimeType': mime_type
            })

    q_list.sort(key=lambda x: (_to_int(x.get("height") or 0, 0), _to_int(x.get("bandwidth") or 0, 0)), reverse=True)
    return q_list


def _runFfmpegHttpCopy(input_url: str, output_file: str, download_section: str = None) -> bool:
    cmd = [getFFmpeg()]

    if download_section:
        ds = download_section.strip()
        if "~" in ds:
            start_section, end_section = ds.split("~", 1)
        elif "-" in ds:
            start_section, end_section = ds.split("-", 1)
        else:
            raise Exception("download_section 형식이 올바르지 않습니다. (예: 00:10:00~00:20:00 또는 00:10:00-00:20:00)")

        start_section = start_section.strip()
        end_section = end_section.strip()

        def hms_to_seconds(hms):
            h, m, s = map(int, hms.split(':'))
            return h * 3600 + m * 60 + s

        start_seconds = hms_to_seconds(start_section)
        end_seconds = hms_to_seconds(end_section)
        if end_seconds <= start_seconds:
            raise Exception("구간 다운로드: 종료 시간이 시작 시간보다 작거나 같습니다.")

        cmd += ["-ss", start_section]
        cmd += _ffmpegHeaderArgs()
        cmd += ["-i", input_url, "-t", str(end_seconds - start_seconds)]
    else:
        cmd += _ffmpegHeaderArgs()
        cmd += ["-i", input_url]

    cmd += ["-c", "copy", "-y", "-stats", "-loglevel", "info", output_file]

    print("\n[INFO] VOD ffmpeg 다운로드")
    print("ffmpeg CMD:", _formatCmdForLog(cmd), "\n")

    p = subprocess.Popen(cmd)
    _set_current_processes([p])

    try:
        rc = p.wait()
    finally:
        _clear_current_processes()

    if rc != 0:
        raise Exception(f"ffmpeg 다운로드 실패 (returncode={rc})")

    if (not os.path.exists(output_file)) or os.path.getsize(output_file) < 1024:
        raise Exception("ffmpeg 다운로드 실패: 출력 파일이 생성되지 않았습니다.")

    return True


CIME_VOD_PAGE_RE = re.compile(r"https?://(?:www\.)?ci\.me/@([^/]+)/vods/(\d+)", re.IGNORECASE)
CIME_MASTER_RE = re.compile(r"https?://streaming\.cf\.ci\.me/[^\s\"'<>\\]+?/media/hls/master\.m3u8", re.IGNORECASE)
CIME_PLAYLIST_RE = re.compile(r"https?://streaming\.cf\.ci\.me/[^\s\"'<>\\]+?/media/hls/([^/]+)/playlist\.m3u8", re.IGNORECASE)
CIME_VIEWER_API = "https://ci.me/api/app/channels/{channel}/video/viewer?videoId={video_id}&record=true&duration=0"


def isCimeVodUrl(url: str) -> bool:
    u = str(url or "").strip().lower()
    return "ci.me/" in u or "streaming.cf.ci.me" in u


def parseCimeVodUrl(url: str):
    m = CIME_VOD_PAGE_RE.search(str(url or ""))
    if not m:
        return None, None
    return m.group(1), m.group(2)


def loadCimeCookies():
    cookies = {}
    try:
        if hasattr(data_manager, "getCimeCookies"):
            cookies = data_manager.getCimeCookies() or {}
    except Exception:
        cookies = {}

    if not cookies:
        try:
            if hasattr(data_manager, "loadCookies"):
                raw = data_manager.loadCookies() or {}
                if isinstance(raw, dict):
                    cookies = raw.get("cime") or raw.get("ci_me") or raw.get("cime_cookies") or {}
        except Exception:
            cookies = {}

    if isinstance(cookies, str):
        out = {}
        for part in cookies.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        cookies = out

    if not isinstance(cookies, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in cookies.items() if str(k).strip() and str(v).strip()}


def hasCimeLoginCookie() -> bool:
    cookies = loadCimeCookies()
    lower_keys = {str(k).strip().lower() for k in cookies.keys()}
    return "mauth-authorization-code" in lower_keys or "authorization" in lower_keys or "access-token" in lower_keys


def getCimeCookieGuide() -> str:
    return (
        "씨미 720p60 이상 VOD는 로그인 쿠키가 필요할 수 있습니다.\n\n"
        "씨미 쿠키 입력/갱신 방법:\n\n"
        "1. 씨미 로그인 후 웹페이지 F12 -> 개발도구 페이지 -> Application 탭 -> 좌측 메뉴 Storage - Cookies - https://ci.me 선택합니다.\n\n"
        "2. session-id, mauth-authorization-code 항목의 Value값을 복사합니다.\n\n"
        "3. WEB/GUI 대시보드 쿠키관리 화면에서 씨미 쿠키값을 입력/수정 후 저장합니다.\n\n"
        "4. 직접 수정할 경우 /json/cookie.json의 cime 항목의 각 쿠키값을 입력/수정 후 파일을 저장합니다.\n\n"
        "5. 쿠키를 입력/갱신 하였다면 VOD다운로더 프로그램 재시작합니다."
    )


def cimeQualityNeedsCookie(quality_item: dict) -> bool:
    h = _to_int((quality_item or {}).get("height") or 0, 0)
    return h >= 720


def appendQualityToFilename(filename: str, quality_label: str) -> str:
    q = str(quality_label or "").strip()
    if not q:
        return filename

    base, ext = os.path.splitext(str(filename or ""))
    if not ext:
        ext = ".mp4"

    if re.search(r"(?:^|\s)\d{3,4}p(?:\d{2,3})?$", base, re.IGNORECASE):
        return base + ext

    return f"{base} {q}{ext}"


def getCimeHeaders(referer: str = "https://ci.me/", accept: str = "*/*", use_cookie: bool = True):
    if os.name == 'nt':
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36"
    else:
        user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36"

    headers = {
        "User-Agent": user_agent,
        "Referer": referer or "https://ci.me/",
        "Accept": accept,
        "Origin": "https://ci.me"
    }

    if use_cookie:
        cookies = loadCimeCookies()
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return headers


def _ffmpegCimeHeaderArgs(referer: str = "https://ci.me/") -> list[str]:
    headers = getCimeHeaders(referer=referer, accept="application/x-mpegURL, application/vnd.apple.mpegurl, */*", use_cookie=True)
    lines = []
    for k in ("Cookie", "User-Agent", "Referer", "Origin"):
        if headers.get(k):
            lines.append(f"{k}: {headers.get(k)}")
    return ["-headers", "\r\n".join(lines) + "\r\n"] if lines else []


def _normalizeCimeText(text: str) -> str:
    text = html_lib.unescape(str(text or ""))
    text = text.replace("\\/", "/")
    try:
        text = text.encode("utf-8", "ignore").decode("unicode_escape", "ignore")
    except Exception:
        pass
    return text


def makeCimeMasterUrl(url: str) -> str:
    u = unquote(str(url or "").strip()).replace("\\/", "/")
    if not u:
        return ""
    m0 = CIME_MASTER_RE.search(u)
    if m0:
        return m0.group(0)
    m = CIME_PLAYLIST_RE.search(u)
    if m:
        return u[:m.start(1)-len('/media/hls/')] + "/media/hls/master.m3u8"
    return ""


def extractCimeM3u8Urls(text: str) -> list[str]:
    body = _normalizeCimeText(text)
    urls = []
    for pat in (CIME_MASTER_RE, CIME_PLAYLIST_RE):
        for m in pat.finditer(body):
            u = m.group(0)
            if u not in urls:
                urls.append(u)
    return urls


def getCimeViewerInfo(vod_url: str) -> dict:
    channel, video_id = parseCimeVodUrl(vod_url)
    info = {
        "platform": "cime",
        "videoTitle": "cime_vod",
        "title": "cime_vod",
        "videoId": video_id or "",
        "channel": {"channelName": channel or "cime"},
        "vodStatus": "CIME_HLS",
        "inKey": None,
        "webpage_url": "https://ci.me/" if "streaming.cf.ci.me" in str(vod_url or "") else vod_url,
    }
    if not channel or not video_id:
        return info

    api = CIME_VIEWER_API.format(channel=channel, video_id=video_id)
    try:
        r = requests.get(api, headers=getCimeHeaders(referer=vod_url, accept="application/json, text/plain, */*"), timeout=10)
        r.raise_for_status()
        obj = r.json()
        data = obj.get("data") if isinstance(obj, dict) else {}
        if isinstance(data, dict):
            title = data.get("title") or info["videoTitle"]
            info.update({
                "videoTitle": title,
                "title": title,
                "isAdult": bool(data.get("isAdult", False)),
                "isLive": bool(data.get("isLive", False)),
            })
    except Exception:
        pass
    return info


def getCimeMasterUrl(vod_url: str) -> str:
    url_s = str(vod_url or "").strip()
    direct = makeCimeMasterUrl(url_s)
    if direct:
        return direct

    channel, video_id = parseCimeVodUrl(url_s)
    if not channel or not video_id:
        raise Exception("씨미 VOD 주소가 올바르지 않습니다.")

    try:
        r = requests.get(url_s, headers=getCimeHeaders(referer="https://ci.me/", accept="text/html,application/xhtml+xml,*/*"), timeout=15)
        r.raise_for_status()
        urls = extractCimeM3u8Urls(r.text)
        if urls:
            master = makeCimeMasterUrl(urls[0])
            if master:
                return master

        html_text = _normalizeCimeText(r.text)
        scripts = re.findall(r"<script[^>]+src=[\"\']([^\"\']+\.js[^\"\']*)[\"\']", html_text, re.IGNORECASE)
        for src in scripts[:20]:
            js_url = urljoin(url_s, src)
            try:
                jr = requests.get(js_url, headers=getCimeHeaders(referer=url_s, accept="*/*"), timeout=10)
                if jr.status_code != 200:
                    continue
                urls = extractCimeM3u8Urls(jr.text)
                if urls:
                    master = makeCimeMasterUrl(urls[0])
                    if master:
                        return master
            except Exception:
                continue
    except Exception as e:
        raise Exception(f"씨미 VOD 페이지 조회 실패: {e}")

    raise Exception("씨미 VOD 재생 URL을 찾지 못했습니다. master.m3u8 주소를 직접 입력해 주세요.")


def parseCimeMasterPlaylist(master_url: str, playlist_text: str) -> list[dict]:
    lines = [ln.strip() for ln in str(playlist_text or "").splitlines() if ln.strip()]
    out = []
    pending = None

    for ln in lines:
        if ln.startswith("#EXT-X-STREAM-INF"):
            pending = ln
            continue
        if ln.startswith("#"):
            continue
        if not pending:
            continue

        playlist_url = urljoin(master_url, ln)
        q_from_url = ""
        m = re.search(r"/media/hls/([^/]+)/playlist\.m3u8", playlist_url)
        if m:
            q_from_url = m.group(1)

        height = 0
        width = 0
        fps = ""
        bw = 0
        rm = re.search(r"RESOLUTION=(\d+)x(\d+)", pending, re.IGNORECASE)
        if rm:
            width = _to_int(rm.group(1), 0)
            height = _to_int(rm.group(2), 0)
        fm = re.search(r"FRAME-RATE=([0-9.]+)", pending, re.IGNORECASE)
        if fm:
            fps = str(_to_int(fm.group(1), 0) or fm.group(1))
        bm = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", pending, re.IGNORECASE)
        if bm:
            bw = _to_int(bm.group(1), 0)

        if not q_from_url:
            q_from_url = f"{height}p{fps}" if height and fps else (f"{height}p" if height else playlist_url.rsplit("/", 2)[-2])

        out.append({
            "id": q_from_url,
            "quality": q_from_url,
            "bandwidth": bw,
            "width": width,
            "height": height,
            "frameRate": fps,
            "playlist_url": playlist_url,
            "baseurl": playlist_url,
            "downloadType": "cime_hls",
        })
        pending = None

    seen = {}
    for item in out:
        key = str(item.get("id") or item.get("quality") or "")
        if key and key not in seen:
            seen[key] = item
    out = list(seen.values())
    out.sort(key=lambda x: (_to_int(x.get("height") or 0, 0), _to_int(x.get("bandwidth") or 0, 0)), reverse=True)
    return out


def getCimeVODQualities(vod_url: str):
    global _LAST_CIME_VOD_ERROR
    _LAST_CIME_VOD_ERROR = ""
    try:
        vod_info = getCimeViewerInfo(vod_url)
        master_url = getCimeMasterUrl(vod_url)
        headers = getCimeHeaders(referer=vod_info.get("webpage_url") or "https://ci.me/", accept="application/x-mpegURL, application/vnd.apple.mpegurl, */*")
        r = requests.get(master_url, headers=headers, timeout=15)
        r.raise_for_status()
        qualities = parseCimeMasterPlaylist(master_url, r.text)
        if not qualities:
            raise Exception("씨미 master.m3u8에서 품질 목록을 찾지 못했습니다.")
        vod_info["master_url"] = master_url
        return qualities, vod_info
    except Exception as e:
        _LAST_CIME_VOD_ERROR = str(e)
        print("[ERROR] 씨미 VOD 품질 조회 오류:", e)
        return None, None


def _pickQualityFromList(qualities: list[dict], quality: str) -> dict | None:
    q = str(quality or "best").strip()
    if not qualities:
        return None
    if q.lower() in ("", "best"):
        return max(qualities, key=lambda x: (_to_int(x.get("height") or 0, 0), _to_int(x.get("bandwidth") or 0, 0)))
    if q.lower() == "worst":
        return min(qualities, key=lambda x: (_to_int(x.get("height") or 0, 0), _to_int(x.get("bandwidth") or 0, 0)))
    for item in qualities:
        if q == str(item.get("id") or "") or q == str(item.get("quality") or ""):
            return item
    m = re.search(r"(\d+)", q)
    if m:
        target = _to_int(m.group(1), 0)
        cand = [x for x in qualities if _to_int(x.get("height") or 0, 0) <= target]
        if cand:
            return max(cand, key=lambda x: (_to_int(x.get("height") or 0, 0), _to_int(x.get("bandwidth") or 0, 0)))
    return qualities[0]


def downloadCimeVOD(vod_url: str, quality: str, output_folder: str, auto_filename: str, download_section: str = None):
    qualities, vod_info = getCimeVODQualities(vod_url)
    if not qualities:
        raise Exception("씨미 VOD 품질 목록을 가져오지 못했습니다." + (f" {_LAST_CIME_VOD_ERROR}" if _LAST_CIME_VOD_ERROR else ""))

    selected = _pickQualityFromList(qualities, quality)
    if not selected:
        raise Exception("선택한 씨미 품질을 찾지 못했습니다.")

    quality_label = str(selected.get("quality") or selected.get("id") or quality or "").strip()

    if cimeQualityNeedsCookie(selected) and not hasCimeLoginCookie():
        raise Exception(getCimeCookieGuide())

    safe_name = sanitize_filename(appendQualityToFilename(auto_filename, quality_label))
    output_file = os.path.join(output_folder, safe_name)

    proceed, _ = checkDuplicateFile(output_file)
    if not proceed:
        return False

    playlist_url = selected.get("playlist_url") or selected.get("baseurl")
    if not playlist_url:
        raise Exception("씨미 playlist.m3u8 주소를 찾지 못했습니다.")

    cmd = [getFFmpeg()]
    if download_section:
        ds = download_section.strip()
        if "~" in ds:
            start_section, end_section = ds.split("~", 1)
        elif "-" in ds:
            start_section, end_section = ds.split("-", 1)
        else:
            raise Exception("download_section 형식이 올바르지 않습니다. (예: 00:10:00~00:20:00)")

        start_section = start_section.strip()
        end_section = end_section.strip()
        h1, m1, s1 = map(int, start_section.split(':'))
        h2, m2, s2 = map(int, end_section.split(':'))
        duration = (h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)
        if duration <= 0:
            raise Exception("구간 다운로드: 종료 시간이 시작 시간보다 작거나 같습니다.")
        cmd += ["-ss", start_section]
        cmd += _ffmpegCimeHeaderArgs(vod_info.get("webpage_url") or "https://ci.me/")
        cmd += ["-i", playlist_url, "-t", str(duration)]
    else:
        cmd += _ffmpegCimeHeaderArgs(vod_info.get("webpage_url") or "https://ci.me/")
        cmd += ["-i", playlist_url]

    cmd += ["-c", "copy", "-y", "-stats", "-loglevel", "info", output_file]

    print("\n[INFO] 씨미 VOD ffmpeg 다운로드")
    print("ffmpeg CMD:", _formatCmdForLog(cmd), "\n")

    p = subprocess.Popen(cmd)
    _set_current_processes([p])
    try:
        rc = p.wait()
    finally:
        _clear_current_processes()

    if rc != 0:
        msg = f"씨미 VOD 다운로드 실패 (ffmpeg returncode={rc})"
        if cimeQualityNeedsCookie(selected):
            msg += "\n\n" + getCimeCookieGuide()
        raise Exception(msg)
    if (not os.path.exists(output_file)) or os.path.getsize(output_file) < 1024:
        raise Exception("씨미 VOD 다운로드 실패: 출력 파일이 생성되지 않았습니다.")

    print("[INFO] 씨미 VOD 다운로드 완료. 파일을 확인해주세요.\n")
    return True

# 치지직 VOD API URL 상수
CHZZK_VOD_INFO_API = "https://api.chzzk.naver.com/service/v2/videos/{videoNo}"
CHZZK_VOD_URI_API = "https://apis.naver.com/neonplayer/vodplay/v2/playback/{videoId}?key={inKey}"

current_download_process = None
current_download_processes = []
_current_process_lock = threading.Lock()

_LAST_YT_DLP_ERROR = ""
_LAST_CHZZK_VOD_ERROR = ""
_LAST_CIME_VOD_ERROR = ""

# 의존성 파일들 상대경로
def getYtDlp():
    return data_manager.getYtDlp()

def getAria2c():
    return data_manager.getAria2c()

def getFFmpeg():
    return data_manager.getFFmpeg()

def getStreamlink():
    return data_manager.getStreamlink()


def _strip_cookies_args(args: list[str]) -> list[str]:
    out = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "--cookies":
            skip_next = True
            continue
        out.append(a)
    return out


def _first_json_line(stdout_text: str) -> str | None:
    for ln in (stdout_text or "").splitlines():
        s = ln.lstrip()
        if s.startswith("{") and s.endswith("}"):
            return ln
    return None


def _set_current_processes(procs):
    global current_download_process, current_download_processes
    with _current_process_lock:
        current_download_processes = [p for p in procs if p is not None]
        current_download_process = current_download_processes[-1] if current_download_processes else None


def _clear_current_processes():
    global current_download_process, current_download_processes
    with _current_process_lock:
        current_download_processes = []
        current_download_process = None

def getLastYtDlpError() -> str:
    return _LAST_YT_DLP_ERROR or ""


def getLastChzzkVodError() -> str:
    return _LAST_CHZZK_VOD_ERROR or ""


def getLastCimeVodError() -> str:
    return _LAST_CIME_VOD_ERROR or ""


# GUI에서 호출하는 표준 중지 함수
def request_stop_current_download():
    global current_download_process

    # (1) 현재 등록된 프로세스 스냅샷
    with _current_process_lock:
        procs = list(current_download_processes)
        if current_download_process and (current_download_process not in procs):
            procs.append(current_download_process)

    # (2) 먼저 terminate 시도
    for p in procs:
        try:
            if p and (p.poll() is None):
                p.terminate()
        except Exception:
            pass

    # (3) 잠깐 기다렸다가 안 죽으면 kill 보조
    for p in procs:
        try:
            if p and (p.poll() is None):
                p.wait(timeout=3)
        except Exception:
            try:
                if p and (p.poll() is None):
                    p.kill()
            except Exception:
                pass

    # (4) 레지스트리 정리
    _clear_current_processes()


def loadCookies():
    try:
        return data_manager.getChzzkCookies() or {}
    except Exception as e:
        print("치지직 쿠키 파일 읽기 오류:", e)
        return {}


def hasChzzkLoginCookie() -> bool:
    cookies = loadCookies() or {}
    return bool(str(cookies.get("NID_SES", "") or "").strip() and str(cookies.get("NID_AUT", "") or "").strip())


def getChzzkCookieGuide() -> str:
    return (
        "치지직 연령제한/프라임/네이버 멤버십 VOD는 로그인 쿠키가 필요할 수 있습니다.\n\n"
        "치지직 쿠키 입력/갱신 방법:\n\n"
        "1. 치지직 로그인 후 웹페이지 F12 -> 개발도구 페이지 -> Application 탭 -> 좌측 메뉴 Storage - Cookies - https://chzzk.naver.com 혹은 https://naver.com을 선택합니다.\n\n"
        "2. NID_SES, NID_AUT 항목의 Value값을 복사합니다.\n\n"
        "3. WEB/GUI 대시보드 쿠키관리 화면에서 치지직 쿠키값을 입력/수정 후 저장합니다.\n\n"
        "4. 직접 수정할 경우 /json/cookie.json의 chzzk 항목의 각 쿠키값을 입력/수정 후 파일을 저장합니다.\n\n"
        "5. 쿠키를 입력/갱신 하였다면 VOD다운로더 프로그램 재시작합니다."
    )


def loadYoutubeCookies():
    try:
        return data_manager.yloadCookies()
    except Exception:
        # 예외 상황에서는 기존 로직으로 폴백
        ycookie_file = os.path.join(base_dir, "json", "ycookie.txt")
        if os.path.exists(ycookie_file):
            return ycookie_file
        else:
            return None


def sanitize_filename(filename: str) -> str:
    text = str(filename or "").replace('\u3000', ' ').replace('\u00a0', ' ')
    text = re.sub(r'[\r\n\t]+', ' ', text)

    # 확장자 분리 전에 경로 구분자를 먼저 제거
    text = text.replace("/", "_").replace("\\", "_")

    base, ext = os.path.splitext(text)
    forbidden_pattern = r'[:*?"<>|\(\)\{\}]'
    base = re.sub(forbidden_pattern, '_', base)
    base = base.replace('.', '_')
    base = re.sub(r"\s+", " ", base).strip(" ._")

    if not base:
        base = "_"
    if ext and not ext.startswith('.'):
        ext = '.' + ext

    return base + ext


def formatLiveDate(live_open_date_raw: str):
    if not live_open_date_raw:
        return None, None
    try:
        date_part, time_part = live_open_date_raw.split(" ")
    except ValueError:
        date_part = live_open_date_raw.strip()
        time_part = "00:00:00"
    y, m, d = date_part.split("-")
    hh, mm, ss = time_part.split(":")
    recording_time = f"{y[2:]}{m}{d}_{hh}{mm}{ss}"
    start_time = date_part
    return recording_time, start_time


def getCookieHeaders():
    if os.name == 'nt':
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            " AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/98.0.4758.102 Safari/537.36"
        )
    else:
        user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64)"
            " AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/98.0.4758.102 Safari/537.36"
        )

    headers = {
        "User-Agent": user_agent,
        "Referer": "https://chzzk.naver.com/",
        "Accept": "application/json, */*",
        "Origin": "https://chzzk.naver.com"
    }

    cookies = loadCookies()
    parts = []

    nid_ses = str(cookies.get("NID_SES", "") or "").strip()
    nid_aut = str(cookies.get("NID_AUT", "") or "").strip()

    if nid_ses:
        parts.append(f"NID_SES={nid_ses}")
    if nid_aut:
        parts.append(f"NID_AUT={nid_aut}")

    if parts:
        headers["Cookie"] = "; ".join(parts)

    return headers



def checkDuplicateFile(output_file: str) -> (bool, str):
    if os.path.exists(output_file):
        print(f"파일 '{output_file}'이(가) 이미 존재합니다.")
        while True:
            print("어떻게 하시겠습니까?")
            print("1. 중복파일 덮어쓰기")
            print("2. 해당파일 이어받기")
            print("3. 해당파일 건너뛰기")
            ans = input("번호를 선택하세요 (1/2/3): ").strip()
            if ans == '3':
                print("완성된 파일이 있으므로 건너뜁니다.")
                return False, ""
            elif ans == '1':
                try:
                    os.remove(output_file)
                    print("기존 파일을 삭제하고 재다운로드합니다.")
                except Exception as e:
                    print("파일 삭제 실패:", e)
                    return False, ""
                return True, ""
            elif ans == '2':
                print("이어받기를 시도합니다.")
                return True, "--continue"
            else:
                print("잘못된 입력입니다. 1, 2, 또는 3을 입력해주세요.")
    return True, ""


def getCommonYtdlpAargs() -> list[str]:
    ytSabrMode = False
    try:
        _cfg = data_manager.loadConfig() or {}
        ytSabrMode = data_manager.toBool(_cfg.get("ytSabrMode", False))
    except Exception:
        ytSabrMode = False

    args: list[str] = []

    # 1) JS runtime(deno) 적용
    deno_path = None
    try:
        deno_path = data_manager.getDeno()
    except SystemExit:
        deno_path = None
    except Exception:
        deno_path = None

    if deno_path and os.path.isfile(deno_path):
        args += ["--js-runtimes", f"deno:{deno_path}"]

    # 2) 유튜브 쿠키 적용
    ycookie_file = loadYoutubeCookies()
    if ycookie_file and os.path.isfile(ycookie_file):
        args += ["--cookies", ycookie_file]

    # 3) 기본 클라이언트 설정만 유지
    extractor_args = ["player-client=default,mweb"]

    if ytSabrMode:
        extractor_args.append("formats=duplicate")

    args += ["--extractor-args", "youtube:" + ";".join(extractor_args)]

    return args


def getYoutubePlaylistInfo(vod_url: str):
    try:
        cmd = [getYtDlp(), '-J', '--flat-playlist']
        cmd += getCommonYtdlpAargs()
        cmd.append(vod_url)

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if result.returncode != 0:
            raise Exception(result.stderr)
        data = json.loads(result.stdout)

        playlist_title = data.get('title')  
        entries = data.get('entries', [])
        return playlist_title, len(entries)
    except Exception as e:
        print("[ERROR] 유튜브 재생목록 정보 조회 오류:", e)
        return None, 0


def getPlaylistItems(playlist_url: str):
    cmd = [getYtDlp(), '-J', '--flat-playlist']
    cmd += getCommonYtdlpAargs()
    cmd.append(playlist_url)

    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if result.returncode != 0:
            raise Exception(result.stderr)

        data = json.loads(result.stdout)
        entries = data.get('entries', [])
        video_urls = []

        for e in entries:
            vid_url = e.get('url')
            if vid_url:
                video_urls.append(vid_url)
        return video_urls
    except Exception as e:
        print("[ERROR] 재생목록 items 조회 오류:", e)
        return []


def getYoutubeQualities(vod_url: str, only_first_in_playlist: bool = False):
    global _LAST_YT_DLP_ERROR
    _LAST_YT_DLP_ERROR = ""

    cmd = [
        data_manager.getYtDlp(),
        vod_url,
        "-j",
        "--no-colors",
    ]

    if only_first_in_playlist:
        cmd += ["--playlist-items", "1"]
    else:
        cmd += ["--no-playlist"]

    common_args = getCommonYtdlpAargs()
    cmd += common_args

    def _run_and_parse(_cmd: list[str]) -> tuple[list[tuple[str, str]] | None, dict | None, str]:
        try:
            result = subprocess.run(
                _cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45
            )
        except Exception as e:
            return None, None, str(e)

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            return None, None, err or f"yt-dlp returncode={result.returncode}"

        json_line = _first_json_line(result.stdout)
        if not json_line:
            err = (result.stderr or "").strip()
            return None, None, err or "yt-dlp stdout에 JSON이 없습니다."

        try:
            meta = json.loads(json_line)
        except Exception as e:
            err = (result.stderr or "").strip()
            return None, None, err or f"JSON 파싱 실패: {e}"

        formats = meta.get("formats", []) or []

        best_by_combo = {}  # (h,fps,vcodec,ext) -> (fmt_id, tbr)

        for f in formats:
            fmt_id = str(f.get("format_id", "") or "")
            vcodec = f.get("vcodec")
            if not vcodec or vcodec == "none":
                continue

            h = f.get("height") or 0
            fps = f.get("fps") or 0
            ext = f.get("ext") or "mp4"

            if isinstance(vcodec, str):
                vc = vcodec.split(".")[0]
            else:
                vc = "unknown"

            key = (int(h), int(round(float(fps) if fps else 0)), vc.lower(), ext.lower())
            tbr = f.get("tbr") or 0
            prev = best_by_combo.get(key)
            if prev is None or (tbr and tbr > prev[1]):
                best_by_combo[key] = (fmt_id, tbr)

        keys_sorted = sorted(best_by_combo.keys(), key=lambda x: (-x[0], -x[1], x[2], x[3]))

        combos = []
        for (h, fps, vc, ext) in keys_sorted:
            fmt_id, _tbr = best_by_combo[(h, fps, vc, ext)]
            label = f"{h}p{fps} {vc.upper()} ({ext})"
            combos.append((label, fmt_id))

        video_info = {
            "title": meta.get("title") or "",
            "uploader": meta.get("uploader") or "",
            "id": meta.get("id") or "",
            "webpage_url": meta.get("webpage_url") or vod_url,
        }
        return combos, video_info, ""

    # 1차 시도
    combos, info, err = _run_and_parse(cmd)

    if (combos is None) and err and ("cookies are no longer valid" in err.lower()):
        cmd2 = _strip_cookies_args(cmd)
        combos, info, err2 = _run_and_parse(cmd2)
        if combos is None:
            _LAST_YT_DLP_ERROR = err2 or err
            print(f"[ERROR] 유튜브 메타데이터 조회 오류(쿠키 제거 재시도 실패): {_LAST_YT_DLP_ERROR}")
            return None, None

        _LAST_YT_DLP_ERROR = err
        return combos, info

    if combos is None:
        _LAST_YT_DLP_ERROR = err
        print(f"[ERROR] 유튜브 메타데이터 조회 오류: {_LAST_YT_DLP_ERROR}")
        return None, None

    return combos, info


def getVODQualities(vod_url: str):
    global _LAST_CHZZK_VOD_ERROR
    _LAST_CHZZK_VOD_ERROR = ""

    vod_info = None
    qualities = None

    if isCimeVodUrl(vod_url):
        return getCimeVODQualities(vod_url)

    if "chzzk.naver.com/video/" in vod_url:
        parts = vod_url.rstrip("/").split("/")
        videoNo = parts[-1]
        info_api_url = CHZZK_VOD_INFO_API.format(videoNo=videoNo)
        headers = getCookieHeaders()
        r = requests.get(info_api_url, headers=headers, timeout=10)
        r.raise_for_status()
        info = r.json()
        if info.get("code") != 200:
            raise Exception("VOD info API 오류: " + str(info.get("message")))
        vod_info = info.get("content", {})
        if not vod_info:
            return None, None

        # HLS 분기
        if vod_info.get("inKey") is None:
            try:
                hls_url = getVODUrl(vod_url)
                sl_cmd = [getStreamlink()]
                sl_cmd += _streamlinkHeaderArgs()
                sl_cmd += ["--json", hls_url]

                sl_res = subprocess.run(
                    sl_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20
                )

                if sl_res.returncode == 0 and sl_res.stdout.strip().startswith("{"):
                    sl_obj = json.loads(sl_res.stdout)
                    streams = (sl_obj or {}).get("streams") or {}
                    q_by_name = {}

                    for name in streams.keys():
                        name_s = str(name or "").strip()
                        normalized_name = normalizeChzzkHlsQuality(name_s, "")
                        if not normalized_name:
                            continue

                        m = re.match(r"^(\d+)p", normalized_name)
                        h = int(m.group(1)) if m else 0
                        q_by_name[normalized_name] = {
                            "id": normalized_name,
                            "quality": normalized_name,
                            "bandwidth": None,
                            "width": None,
                            "height": h,
                            "frameRate": None,
                            "downloadType": "streamlink_hls"
                        }

                    q_list = list(q_by_name.values())

                    def _sort_key(item):
                        q = str(item.get("id") or "")
                        if q == "best":
                            return 99999
                        if q == "worst":
                            return -1
                        return _to_int(item.get("height") or 0, 0)

                    q_list.sort(key=_sort_key, reverse=True)
                    if q_list:
                        qualities = q_list
                        return qualities, vod_info

                    _LAST_CHZZK_VOD_ERROR = "Streamlink가 스트림 목록을 반환했지만 사용할 수 있는 품질명이 없습니다."
                else:
                    _LAST_CHZZK_VOD_ERROR = (
                        f"streamlink --json 실패 (returncode={sl_res.returncode}) "
                        f"{(sl_res.stderr or '').strip()}"
                    )
                    print("[WARN]", _LAST_CHZZK_VOD_ERROR)

            except Exception as e:
                _LAST_CHZZK_VOD_ERROR = f"streamlink 품질 조회 예외: {e}"
                print("[WARN]", _LAST_CHZZK_VOD_ERROR)

            live_rewind_json_str = vod_info.get("liveRewindPlaybackJson")
            if not live_rewind_json_str:
                raise Exception("liveRewindPlaybackJson 정보가 없습니다. " + (_LAST_CHZZK_VOD_ERROR or ""))

            try:
                qualities = collectChzzkHlsQualitiesFromPlaybackJson(live_rewind_json_str)
            except Exception as e:
                raise Exception(f"HLS 품질 목록 파싱 실패: {e}")

            if not qualities:
                raise Exception("HLS 미디어 품질 정보를 찾지 못했습니다. " + (_LAST_CHZZK_VOD_ERROR or ""))

        else:
            videoId = vod_info.get("videoId")
            inKey = vod_info.get("inKey")
            if not videoId or not inKey:
                raise Exception("필수 videoId 또는 inKey 값이 없습니다.")

            mpd_url = CHZZK_VOD_URI_API.format(videoId=videoId, inKey=inKey)
            headers2 = getCookieHeaders()
            headers2["Accept"] = "application/dash+xml, application/xml, */*"
            r2 = requests.get(mpd_url, headers=headers2, timeout=10)
            r2.raise_for_status()
            mpd_content = r2.content.strip()

            try:
                qualities = collectChzzkMpdQualities(mpd_content)
            except Exception as e:
                _LAST_CHZZK_VOD_ERROR = f"MPD 품질 목록 파싱 실패: {e}"
                raise Exception(_LAST_CHZZK_VOD_ERROR)

            if not qualities:
                _LAST_CHZZK_VOD_ERROR = "MPD에서 사용할 수 있는 영상 품질을 찾지 못했습니다."
                raise Exception(_LAST_CHZZK_VOD_ERROR)
    else:
        return None, None

    return qualities, vod_info


def getVODUrl(vod_url: str, quality: str = None) -> str:
    if isCimeVodUrl(vod_url):
        return getCimeMasterUrl(vod_url)

    if "chzzk.naver.com/video/" in vod_url:
        parts = vod_url.rstrip("/").split("/")
        videoNo = parts[-1]
        info_api_url = CHZZK_VOD_INFO_API.format(videoNo=videoNo)
        headers = getCookieHeaders()
        try:
            r = requests.get(info_api_url, headers=headers, timeout=10)
            r.raise_for_status()
        except Exception as e:
            raise Exception(f"VOD 정보 API 호출 실패: {e}")
        info = r.json()
        if info.get("code") != 200:
            raise Exception("VOD info API 오류: " + str(info.get("message")))
        vod_info = info.get("content", {})

        if vod_info.get("inKey") is None:
            live_rewind_json_str = vod_info.get("liveRewindPlaybackJson")
            if not live_rewind_json_str:
                raise Exception("liveRewindPlaybackJson 정보가 없습니다.")

            live_data = json.loads(live_rewind_json_str)
            media_list = live_data.get("media") or []
            if not media_list:
                raise Exception("HLS 미디어 정보가 없습니다.")

            selected_media = None
            for media in media_list:
                if str(media.get("mediaId") or "").upper() == "HLS" and media.get("path"):
                    selected_media = media
                    break

            if selected_media is None:
                for media in media_list:
                    if media.get("path"):
                        selected_media = media
                        break

            if selected_media is None:
                raise Exception("HLS 재생 URL(path)을 찾지 못했습니다.")

            return selected_media.get("path")
        else:
            videoId = vod_info.get("videoId")
            inKey = vod_info.get("inKey")
            if not videoId or not inKey:
                raise Exception("필수 videoId 또는 inKey 값이 없습니다.")

            mpd_url = CHZZK_VOD_URI_API.format(videoId=videoId, inKey=inKey)
            headers2 = getCookieHeaders()
            headers2["Accept"] = "application/dash+xml, application/xml, */*"
            r2 = requests.get(mpd_url, headers=headers2, timeout=10)
            r2.raise_for_status()
            qualities = collectChzzkMpdQualities(r2.content.strip())

            if not quality:
                raise Exception("다운로드할 품질이 지정되지 않았습니다. (예: 1080p, 720p, 144p)")

            quality_s = str(quality or "").strip()
            selected = None

            for rep in qualities:
                if str(rep.get('id') or "").strip() == quality_s:
                    selected = rep
                    break

            if selected is None:
                match = re.search(r'(\d+)', quality_s)
                desired_quality = match.group(1) if match else ""
                if desired_quality:
                    for rep in qualities:
                        rep_height = str(rep.get("height") or "").strip()
                        rep_quality = str(rep.get("quality") or "").strip()
                        if rep_height == desired_quality or desired_quality in rep_quality:
                            selected = rep
                            break

            if not selected or not selected.get("baseurl"):
                raise Exception("원하는 품질의 다운로드 URL을 찾을 수 없습니다.")
            return selected.get("baseurl")
    else:
        return vod_url


def downloadVOD(vod_url: str, quality: str, output_folder: str, auto_filename: str, speed_option: str, download_section: str = None):
    global current_download_process

    if isCimeVodUrl(vod_url):
        return downloadCimeVOD(vod_url, quality, output_folder, auto_filename, download_section)

    safe_name   = sanitize_filename(auto_filename)
    output_file = os.path.join(output_folder, safe_name)

    # 중복 파일 처리
    proceed, _ = checkDuplicateFile(output_file)
    if not proceed:
        return False

    vod_info = None
    if "chzzk.naver.com/video/" in vod_url:
        parts = vod_url.rstrip("/").split("/")
        videoNo = parts[-1]
        info_api_url = CHZZK_VOD_INFO_API.format(videoNo=videoNo)
        headers = getCookieHeaders()
        try:
            r = requests.get(info_api_url, headers=headers, timeout=10)
            r.raise_for_status()
            info = r.json()
            if info.get("code") != 200:
                raise Exception("VOD info API 오류: " + str(info.get("message")))
            vod_info = info.get("content", {})
        except Exception as e:
            raise Exception(f"VOD 정보 API 호출 실패: {e}")

    #HLS 분기
    if vod_info.get("inKey") is None:
        hls_url = getVODUrl(vod_url)
        raw_quality = str(quality or "best").strip()
        hls_quality = resolveChzzkHlsQualityFromVodInfo(vod_info, raw_quality, "best")

        if raw_quality.lower() != hls_quality.lower():
            print(f"[WARN] 치지직 HLS VOD 품질값 보정: {raw_quality!r} -> {hls_quality!r}")

        streamlink_cmd = [getStreamlink()]
        streamlink_cmd += _streamlinkHeaderArgs()
        streamlink_cmd += [
            hls_url,
            hls_quality,
            "--stdout"
        ]

        print("\n[INFO] 치지직 빠른 다시보기 => streamlink+ffmpeg 전체 다운로드")
        print("streamlink CMD:", _formatCmdForLog(streamlink_cmd))
        ffmpeg_cmd = [
            getFFmpeg(),
            "-i", "pipe:0",
            "-c", "copy",
            "-y",
            "-stats",
            "-loglevel", "info",
            output_file
        ]

        print("ffmpeg CMD:", " ".join(ffmpeg_cmd), "\n")
        p_streamlink = subprocess.Popen(streamlink_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        p_ffmpeg = subprocess.Popen(ffmpeg_cmd, stdin=p_streamlink.stdout, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        p_streamlink.stdout.close()

        _set_current_processes([p_streamlink, p_ffmpeg])

        try:
            def read_streamlink_stderr():
                while True:
                    line = p_streamlink.stderr.readline()
                    if not line:
                        break
                    print("[STREAMLINK]", line, end="")

            def read_ffmpeg_stderr():
                while True:
                    line = p_ffmpeg.stderr.readline()
                    if not line:
                        break
                    print("[FFMPEG]", line, end="")

            t_sl = threading.Thread(target=read_streamlink_stderr, daemon=True)
            t_ff = threading.Thread(target=read_ffmpeg_stderr, daemon=True)
            t_sl.start()
            t_ff.start()

            p_ffmpeg.wait()
            p_streamlink.wait()
            time.sleep(0.5)

            rc_sl = p_streamlink.returncode
            rc_ff = p_ffmpeg.returncode

            if rc_sl != 0 or rc_ff != 0:
                raise Exception(f"HLS 다운로드 실패 (streamlink={rc_sl}, ffmpeg={rc_ff})")

            if (not os.path.exists(output_file)) or os.path.getsize(output_file) < 1024:
                raise Exception("HLS 다운로드 실패: 출력 파일이 생성되지 않았습니다.")

            print("[INFO] 치지직 빠른 다시보기 다운로드 완료. 파일을 확인하세요.\n")
            return True

        finally:
            _clear_current_processes()
 
    # DASH MPD 분기
    else:
        qualities, _ = getVODQualities(vod_url)
        if not qualities:
            raise Exception("치지직 DASH 품질 목록을 가져오지 못했습니다.")

        if str(quality).lower() in ("best", "worst"):
            mode = str(quality).lower()
            cand = []
            for rep in qualities:
                rid = str(rep.get("id") or "").strip()
                if not rid:
                    continue
                h = _to_int(rep.get("height") or 0, 0)
                bw = _to_int(rep.get("bandwidth") or rep.get("bitrate") or 0, 0)
                cand.append((h, bw, rid))

            if not cand:
                raise Exception("DASH best/worst 자동 선택 실패: 후보가 없습니다.")

            cand.sort()
            quality = cand[-1][2] if mode == "best" else cand[0][2]
            print(f"[INFO] DASH {mode} 자동선택 → {quality}")

        selected_rep = None
        quality_s = str(quality or "").strip()

        for rep in qualities:
            if str(rep.get('id') or "").strip() == quality_s:
                selected_rep = rep
                break

        if selected_rep is None:
            m = re.search(r"(\d+)", quality_s)
            desired_height = m.group(1) if m else ""

            if desired_height:
                for rep in qualities:
                    rep_height = str(rep.get("height") or "").strip()
                    rep_quality = str(rep.get("quality") or "").strip()
                    if rep_height == desired_height or desired_height in rep_quality:
                        selected_rep = rep
                        print(f"[WARN] DASH 품질값을 height 기준으로 보정: {quality_s!r} -> {rep.get('id')!r}")
                        break

        if not selected_rep or not selected_rep.get('baseurl'):
            raise Exception("선택한 품질의 다운로드 URL(BaseURL)이 없습니다.")

        download_url = selected_rep['baseurl']
        download_type = str(selected_rep.get("downloadType") or "").strip()
        print(f"\n[INFO] 선택한 품질의 다운로드 URL: {download_url}")

        if download_type == "hls_mpd" or ".m3u8" in download_url:
            _runFfmpegHttpCopy(download_url, output_file, download_section)
            print("[INFO] 치지직 VOD 다운로드 완료. 파일을 확인해주세요.\n")
            return True

        # (A) 구간 다운로드: ffmpeg
        if download_section:
            _runFfmpegHttpCopy(download_url, output_file, download_section)

        # (B) 전체 다운로드: aria2c
        else:
            headers = getCookieHeaders()
            cookie_str = headers.get("Cookie", "")
            user_agent_str = headers.get("User-Agent", "")
            referer_str = headers.get("Referer", "")

            downloader_args_mapping = {
                "100%": ["-x", "16", "-s", "16", "-k", "1M"],
                "75%":  ["-x", "12", "-s", "12", "-k", "1M"],
                "50%":  ["-x", "8", "-s", "8", "-k", "1M"],
                "25%":  ["-x", "4", "-s", "4", "-k", "1M"],
                "분할 없음": ["-x", "1", "-s", "1", "-k", "1M"]
            }

            aria_args = downloader_args_mapping.get(speed_option, ["-x", "16", "-s", "16", "-k", "1M"])

            cmd = [
                getAria2c(),
                *aria_args,
                "--file-allocation=none",
                "-d", output_folder,
                "-o", safe_name,
            ]

            if cookie_str:
                cmd += ["--header", f"Cookie: {cookie_str}"]
            if user_agent_str:
                cmd += ["--header", f"User-Agent: {user_agent_str}"]
            if referer_str:
                cmd += ["--header", f"Referer: {referer_str}"]

            cmd.append(download_url)

            print("\n[INFO] 치지직 VOD 전체 다운로드: aria2c 직접 다운로드")
            print(_formatCmdForLog(cmd), "\n")

            p = subprocess.Popen(cmd)
            _set_current_processes([p])

            try:
                rc = p.wait()
            finally:
                _clear_current_processes()

            if rc != 0:
                raise Exception(f"DASH 다운로드 실패 (aria2c returncode={rc})")

            if (not os.path.exists(output_file)) or os.path.getsize(output_file) < 1024:
                raise Exception("DASH 다운로드 실패: 출력 파일이 생성되지 않았습니다.")

        print("[INFO] 치지직 VOD 다운로드 완료. 파일을 확인해주세요.\n")
        return True


def downloadMultiVOD():
    vod_list = []

    # 1) 치지직 VOD URL 여러 개 입력받기
    while True:
        inp = input("치지직 VOD URL 입력 (종료하려면 엔터): ").strip()
        if not inp:
            break
        if "chzzk.naver.com/video/" not in inp:
            print("치지직 VOD 주소만 입력 가능합니다.")
            continue
        vod_list.append(inp)

        # 추가로 입력할지 여부 확인
        more = input("다운로드할 치지직 VOD URL을 추가하시겠습니까? (y 혹은 n 입력): ").strip().lower()
        if more != 'y':
            break

    if not vod_list:
        print("입력된 VOD가 없어 모드를 종료합니다.")
        return

    # 2) 각 VOD 마다 HLS인지 DASH인지 확인
    hls_list = []
    dash_list = []
    for vod_url in vod_list:
        try:
            qualities, vod_info = getVODQualities(vod_url)
            if not qualities or not vod_info:
                print(f"[WARNING] 품질 정보를 가져올 수 없는 URL: {vod_url}")
                continue

            # HLS = inKey가 None / DASH = inKey 존재
            if vod_info.get("inKey") is None:
                hls_list.append(vod_url)
            else:
                dash_list.append(vod_url)
        except Exception as e:
            print(f"[ERROR] getVODQualities() 실패 URL: {vod_url}, 에러: {e}")

    if not hls_list and not dash_list:
        print("다운로드 가능한 VOD가 없습니다.")
        return

    # 3) 사용자에게 '대표 품질' & 분할옵션 받아오기 (HLS / DASH 각기 따로)
    selected_hls_quality = None
    selected_dash_quality = None
    dash_speed_option = None

    # (3-1) HLS용 대표 품질
    if hls_list:
        # HLS용 임의의 VOD 하나를 골라서 품질 리스트를 구해 사용자에게 보여주기
        sample_url = hls_list[0]
        q, vinfo = getVODQualities(sample_url)
        if q:
            print("\n[빠른 다시보기VOD(HLS) 대표 품질 선택] (예: 360p, 720p, 1080p 등)")
            for idx, item in enumerate(q):
                print(f"{idx+1}. {item['quality']} (ID: {item['id']})")
            while True:
                c = input("HLS 대표 품질 번호 선택: ").strip()
                try:
                    c_int = int(c)
                    if 1 <= c_int <= len(q):
                        selected_hls_quality = q[c_int - 1]['id']
                        break
                except:
                    pass
                print("잘못된 입력입니다.")
        else:
            print("[WARNING] HLS 품질 정보를 가져오지 못했습니다. 기본 720p 처리")
            selected_hls_quality = "720p"
        # HLS는 구간다운로드 X, 분할다운로드 없음

    # (3-2) DASH용 대표 품질 + 분할옵션
    if dash_list:
        sample_url = dash_list[0]
        q, vinfo = getVODQualities(sample_url)
        if q:
            print("\n[인코딩 완료 다시보기 VOD(DASH) 대표 품질 선택]")
            for idx, item in enumerate(q):
                # 예: PD_1080P_1920_8000_192 (ID: PD_1080P_1920_8000_192)
                print(f"{idx+1}. {item['quality']} (ID: {item['id']})")
            while True:
                c = input("DASH 대표 품질 번호 선택: ").strip()
                try:
                    c_int = int(c)
                    if 1 <= c_int <= len(q):
                        selected_dash_quality = q[c_int - 1]['id']
                        break
                except:
                    pass
                print("잘못된 입력입니다.")
        else:
            print("[WARNING] DASH 품질 정보를 가져오지 못했습니다. 기본 720p 처리")
            selected_dash_quality = "PD_720P_1280_4000_192"

        # 분할 다운로드(aria2c) 옵션 (예: 100%, 75% 등)
        print("\n[DASH 다운로드 분할 옵션 선택]")
        print("1) 100% (16분할)")
        print("2) 75% (12분할)")
        print("3) 50% (8분할)")
        print("4) 25% (4분할)")
        print("5) 분할 없음 (1)")

        while True:
            c = input("번호 선택: ").strip()
            if c == '1':
                dash_speed_option = "100%"
                break
            elif c == '2':
                dash_speed_option = "75%"
                break
            elif c == '3':
                dash_speed_option = "50%"
                break
            elif c == '4':
                dash_speed_option = "25%"
                break
            elif c == '5':
                dash_speed_option = "분할 없음"
                break
            else:
                print("1~5 중에서 골라주세요.")

    # 4) 파일명 옵션(1/2) + 다운로드 폴더 경로
    print("\n파일명 옵션 선택:")
    print("1. [YYMMDD_hhmmss] 채널명 영상제목.mp4")
    print("2. [YYYY-MM-DD] 채널명 영상제목.mp4")
    file_opt = input("옵션 번호 (1/2): ").strip()
    if file_opt not in ['1', '2']:
        file_opt = '1'

    output_folder = ""
    while True:
        output_folder = input("\n다운로드 받을 폴더 경로: ").strip()
        if not output_folder:
            print("다운로드 경로를 입력하세요.")
            continue
        if not os.path.exists(output_folder):
            try:
                os.makedirs(output_folder)
                print(f"폴더가 생성되었습니다: {output_folder}")
            except Exception as e:
                print("폴더 생성 실패:", e)
                continue
        break

    # 5) 각 URL 다운로드 수행 (순차)
    print("\n===== 입력된 치지직 VOD들을 순서대로 다운로드합니다. =====\n")
    for vod_url in vod_list:
        try:
            qualities, vod_info = getVODQualities(vod_url)
            if not (qualities and vod_info):
                print(f"[SKIP] 품질 정보 없음: {vod_url}")
                continue

            # 파일명 자동 생성
            live_open_date_raw = vod_info.get("liveOpenDate", "")
            recording_time, start_time = formatLiveDate(live_open_date_raw)
            channel_info = vod_info.get("channel", {}) or {}
            channelName = channel_info.get("channelName") or ""
            videoTitle_raw = vod_info.get("videoTitle", "")
            video_title = videoTitle_raw.strip()

            if file_opt == '2':
                raw_name     = f"[{start_time}] {channelName} {video_title}.mp4"
            else:
                raw_name     = f"[{recording_time}] {channelName} {video_title}.mp4"

            auto_filename = sanitize_filename(raw_name)

            # HLS vs DASH 구분
            if vod_info.get("inKey") is None:
                # HLS
                use_quality = selected_hls_quality or "720p"
                speed_option = "nolimit"  # HLS는 분할 X
            else:
                # DASH
                use_quality = selected_dash_quality or "PD_720P_1280_4000_192"
                speed_option = dash_speed_option or "100%"  # 디폴트

            print(f"\n--- 다운로드 시작: {vod_url}")
            print(f"    품질: {use_quality}, 분할옵션: {speed_option}")
            print(f"    파일명: {auto_filename}")

            # 구간 다운로드는 지원 X
            downloadVOD(
                vod_url=vod_url,
                quality=use_quality,
                output_folder=output_folder,
                auto_filename=auto_filename,
                speed_option=speed_option,
                download_section=None  # 구간X
            )
        except Exception as e:
            print(f"[ERROR] 다운로드 실패: {vod_url}, 사유: {e}")

    print("\n=== 모든 VOD 순차 다운로드를 마쳤습니다. ===\n")
    input("계속하려면 엔터를 누르세요.")



def downloadYoutube(vod_url: str, format_id: str, output_folder: str, base_filename: str, speedLimit: str, download_section: str = None):
    if base_filename.lower().endswith(".mp4"):
        base_filename = base_filename[:-4]

    ffmpeg_path = getFFmpeg()
    output_file = os.path.join(output_folder, sanitize_filename(base_filename))

    # 중복 파일 처리
    proceed, resume_option = checkDuplicateFile(output_file)
    if not proceed:
        return

    # 출력 경로
    output_file = os.path.join(output_folder, sanitize_filename(base_filename))

    cmd = [
        getYtDlp(),
    ]

    cmd += getCommonYtdlpAargs()

    cmd += [
        vod_url,
        '-f', f'{format_id}+bestaudio',
        '-o', output_file,
        '--merge-output-format', 'mkv',
        '--ffmpeg-location', os.path.dirname(ffmpeg_path),
        '--postprocessor-args', 'ffmpeg:-loglevel error'
    ]

    if resume_option:
        cmd.append(resume_option)

    if download_section:
        time_arg = download_section.replace("~", "-")
        if not time_arg.startswith("*"):
            time_arg = "*" + time_arg
        cmd.extend(["--download-sections", time_arg])

    if speedLimit:
        cmd.extend(['--limit-rate', speedLimit])

    print("\n[INFO] 유튜브 영상 다운로드: yt-dlp 명령어")
    print(" ".join(cmd), "\n")
    process = subprocess.Popen(cmd)
    _set_current_processes([process])

    try:
        rc = process.wait()
    finally:
        _clear_current_processes()

    if rc != 0:
        raise Exception(f"yt-dlp 실패 (returncode={rc})")
    print("[INFO] 유튜브 영상 다운로드 완료.\n")

    return True


def downloadYoutubePlaylist(playlist_url: str, output_folder: str, speedLimit: str, selected_quality: str = None):
    ffmpeg_path = getFFmpeg()

    # (1) 재생목록 이름 구하기
    playlist_title, _ = getYoutubePlaylistInfo(playlist_url)
    if not playlist_title:
        playlist_title = "재생목록"
    subfolder_name = f"[재생목록] {playlist_title}"
    subfolder_name = sanitize_filename(subfolder_name)
    final_output_dir = os.path.join(output_folder, subfolder_name)
    if not os.path.exists(final_output_dir):
        os.makedirs(final_output_dir, exist_ok=True)

    # (2) 대표 품질 선택 (selected_quality이 None이면 첫 영상 기준으로 선택)
    if selected_quality is None:
        first_qualities, first_info = getYoutubeQualities(playlist_url, only_first_in_playlist=True)
        if first_qualities and len(first_qualities) > 0:
            print("\n사용 가능한 유튜브 (첫 번째 영상) 화질 목록:")
            for idx, q in enumerate(first_qualities):
                print(f"{idx+1}. {q['quality']} (ID: {q['id']})")
            while True:
                choice = input("\n대표로 사용할 품질 번호를 선택하세요(엔터시 best): ").strip()
                if not choice:
                    print("특정 품질을 선택하지 않았습니다. (bestvideo+bestaudio)")
                    selected_quality = None
                    break
                try:
                    c_int = int(choice)
                    if 1 <= c_int <= len(first_qualities):
                        selected_quality = first_qualities[c_int - 1]['id']
                        break
                except:
                    pass
                print("잘못된 입력입니다. 다시 선택해주세요.")
        else:
            print("첫 번째 영상의 화질 정보를 가져올 수 없습니다. (bestvideo+bestaudio로 진행)")
            selected_quality = None

    # (3) 재생목록 모든 영상 URL 추출
    all_items = getPlaylistItems(playlist_url)
    if not all_items:
        print("[ERROR] 재생목록에 영상이 없습니다.")
        return

    # (4) 각 영상 반복 다운로드 (downloadYoutube 내부에서 중복 파일 처리가 적용됨)
    for idx, video_url in enumerate(all_items, start=1):
        print(f"\n=== [재생목록 영상 {idx}/{len(all_items)}] 다운로드 시도 중 ===\n")
        v_qualities, v_info = getYoutubeQualities(video_url, only_first_in_playlist=False)
        if not v_qualities or len(v_qualities) == 0 or not v_info:
            print("[WARN] 이 영상의 품질 정보를 가져오지 못했습니다. 건너뜁니다.")
            continue

        if selected_quality:
            found = any(q['id'] == selected_quality for q in v_qualities)
            if found:
                ch_name = v_info.get("uploader", "")
                vid_title = (v_info.get("title") or "").strip()
                base_name = f"{ch_name} {vid_title}" if ch_name else vid_title
                downloadYoutube(
                    vod_url=video_url,
                    format_id=selected_quality,
                    output_folder=final_output_dir,
                    base_filename=base_name,
                    speedLimit=speedLimit
                )
                continue
            else:
                print("\n[INFO] 이 영상은 대표로 선택한 품질을 지원하지 않습니다.\n")

        # 대표 품질이 없거나 지원하지 않으면, 해당 영상에 대해 다시 사용자에게 품질 선택 안내
        print("이 영상 전용으로, 사용 가능한 품질을 다시 표시합니다.")
        for i2, q2 in enumerate(v_qualities):
            print(f"{i2+1}. {q2['quality']} (ID: {q2['id']})")
        new_format = None
        while True:
            c = input("원하는 품질 번호를 선택하세요(엔터시 best): ").strip()
            if not c:
                print("특정 품질을 선택하지 않았습니다. (bestvideo+bestaudio)")
                new_format = None
                break
            try:
                c_int = int(c)
                if 1 <= c_int <= len(v_qualities):
                    new_format = v_qualities[c_int - 1]['id']
                    break
            except:
                pass
            print("잘못된 입력입니다. 다시 선택해주세요.")

        ch_name = v_info.get("uploader", "")
        vid_title = (v_info.get("title") or "").strip()
        base_name = f"{ch_name} {vid_title}" if ch_name else vid_title

        downloadYoutube(
            vod_url=video_url,
            format_id=(new_format if new_format else "bestvideo"),
            output_folder=final_output_dir,
            base_filename=base_name,
            speedLimit=speedLimit
        )
    print("\n[INFO] 재생목록의 모든 영상 다운로드를 마쳤습니다.\n")


def main():
    print("==== {} ====\n".format(VERSION))

    # 모드 선택
    print("1) 다중 치지직 VOD 순차 다운로드 모드")
    print("   → 다수의 치지직 VOD URL을 한 번에 입력받아 순차적으로 자동 다운로드")
    print()
    print("2) 단일 다운로드 모드")
    print("   → 단일 치지직 다시보기VOD / 유튜브 영상 / 유튜브 재생목록 다운로드")
    print()

    mode_choice = input("원하는 모드를 선택하세요: (1 혹은 2 입력) ").strip()
    if mode_choice == '1':
        downloadMultiVOD()
        return
    else:

        while True:
            print("==== 영상 다운로드 (CLI 버전) ====\n")
            vod_url = input("영상 주소 (예: https://chzzk.naver.com/video/1234567 또는 https://www.youtube.com/watch?v=xxxx): ").strip()
            if not vod_url:
                print("주소를 입력해야 합니다.")
                continue

            is_youtube = ("youtube.com" in vod_url or "youtu.be" in vod_url)
            is_playlist = (is_youtube and "list=" in vod_url)

            # (1) 유튜브 재생목록
            if is_playlist:
                print("\n[감지] 유튜브 재생목록 주소로 확인되었습니다.\n")

                while True:
                    output_folder = input("다운로드 폴더 경로(예: C:/Downloads): ").strip()
                    if not output_folder:
                        print("다운로드 경로를 입력하세요.")
                        continue
                    if not os.path.exists(output_folder):
                        try:
                            os.makedirs(output_folder)
                            print(f"폴더가 생성되었습니다: {output_folder}")
                        except Exception as e:
                            print("폴더 생성 실패:", e)
                            continue
                    break

                speed_limit = input("다운로드 속도 제한(예: 2M, 제한없음: Enter): ").strip()
                if speed_limit == "":
                    speed_limit = None

                confirm = input("재생목록 전체를 다운로드하시겠습니까? (y 혹은 n 입력): ").strip().lower()
                if confirm != "y":
                    print("취소되었습니다.")
                    continue

                # (2) downloadYoutubePlaylist 호출
                try:
                    downloadYoutubePlaylist(
                        playlist_url=vod_url,
                        output_folder=output_folder,
                        speedLimit=speed_limit,
                        selected_quality=None  # None이면 내부에서 품질선택
                    )
                except Exception as e:
                    print("재생목록 다운로드 중 오류 발생:", e)
                    continue

            # (2) 유튜브 단일 영상
            elif is_youtube:
                qualities, video_info = getYoutubeQualities(vod_url)
                if not qualities or len(qualities) == 0:
                    print("유튜브 영상의 품질 정보를 가져올 수 없습니다.")
                    input("계속하려면 Enter를 누르세요.")
                    continue

                print("\n사용 가능한 유튜브 품질:")
                for idx, q in enumerate(qualities):
                    print(f"{idx+1}. {q['quality']} (ID: {q['id']})")
                while True:
                    choice = input("원하는 품질 번호를 선택하세요: ").strip()
                    if not choice:
                        print("값을 입력해야 합니다.")
                        continue
                    try:
                        choice_int = int(choice)
                        if choice_int < 1 or choice_int > len(qualities):
                            print("잘못된 선택입니다.")
                            continue
                    except ValueError:
                        print("숫자를 입력하세요.")
                        continue
                    selected_quality = qualities[choice_int - 1]['id']
                    break

                while True:
                    output_folder = input("다운로드 받을 폴더 경로 (예: C:/Downloads): ").strip()
                    if not output_folder:
                        print("다운로드 경로를 입력하세요.")
                        continue
                    if not os.path.exists(output_folder):
                        try:
                            os.makedirs(output_folder)
                            print(f"폴더가 생성되었습니다: {output_folder}")
                        except Exception as e:
                            print("폴더 생성 실패:", e)
                            continue
                    break

                channel_name = video_info.get("uploader", "")
                video_title = (video_info.get("title") or "").strip()

                if channel_name:
                    combined_name = f"{channel_name} {video_title}"
                else:
                    combined_name = video_title

                auto_filename = sanitize_filename(combined_name)
                print("\n자동 생성 파일명:", auto_filename)

                download_section = input("다운로드 구간 (00:00:00~00:00:00, 전체: Enter): ").strip()
                if download_section == "":
                    download_section = None

                speed_limit = input("다운로드 속도 제한 (예: 2M, 제한없음: Enter): ").strip()
                if speed_limit == "":
                    speed_limit = None

                print("\n최종 정보 확인:")
                print("영상 URL:", vod_url)
                print("품질 ID:", selected_quality)
                print("폴더:", output_folder)
                print("파일명:", auto_filename)
                print("구간:", download_section if download_section else "전체")
                print("속도 제한:", speed_limit if speed_limit else "제한없음")
                confirm = input("다운로드를 시작하시겠습니까? (y 혹은 n 입력): ").strip().lower()
                if confirm != "y":
                    print("취소되었습니다.")
                    input("계속하려면 Enter를 누르세요.")
                    continue

                try:
                    downloadYoutube(
                        vod_url, 
                        selected_quality, 
                        output_folder, 
                        auto_filename, 
                        speed_limit, 
                        download_section
                    )
                except Exception as e:
                    print("유튜브 다운로드 중 오류 발생:", e)
                    input("계속하려면 Enter를 누르세요.")
                    continue

            # 치지직 VOD 분기 (유튜브가 아닌 경우)
            else:
                try:
                    qualities, vod_info = getVODQualities(vod_url)
                except Exception as e:
                    err_msg = str(e)
                    if "cookie" in err_msg:
                        print("쿠키 값이 없거나 만료되었습니다.")
                    else:
                        print("품질 정보를 가져오는 중 오류 발생:", e)
                    input("계속하려면 Enter를 누르세요.")
                    continue

                if not qualities or len(qualities) == 0:
                    print("사용 가능한 품질 정보를 찾지 못했습니다.")
                    input("계속하려면 Enter를 누르세요.")
                    continue

                vod_status = vod_info.get("vodStatus") if vod_info else ""
                live_open_date_raw = vod_info.get("liveOpenDate", "")
                recording_time, start_time = formatLiveDate(live_open_date_raw)
                channel_info = vod_info.get("channel", {}) or {}
                channelName = channel_info.get("channelName") or ""
                videoTitle_raw = vod_info.get("videoTitle", "")
                video_title = videoTitle_raw.strip()

                print("\n파일명 옵션 선택:")
                print("1. [{}] {} {}".format(recording_time, channelName, video_title))
                print("2. [{}] {} {}".format(start_time, channelName, video_title))
                option = input("옵션 번호를 선택하세요 (1 또는 2): ").strip()
                if option == "2":
                    auto_filename = f"[{start_time}] {channelName} {video_title}.mp4"
                else:
                    auto_filename = f"[{recording_time}] {channelName} {video_title}.mp4"
                auto_filename = sanitize_filename(auto_filename)
                print("\n자동 생성 파일명:", auto_filename)

                print("\n사용 가능한 품질:")
                for idx, q in enumerate(qualities):
                    print(f"{idx+1}. {q['quality']} (ID: {q['id']})")
                while True:
                    choice = input("원하는 품질 번호를 선택하세요: ").strip()
                    if not choice:
                        print("값을 입력해야 합니다.")
                        continue
                    try:
                        choice_int = int(choice)
                        if choice_int < 1 or choice_int > len(qualities):
                            print("잘못된 선택입니다.")
                            continue
                    except ValueError:
                        print("숫자를 입력하세요.")
                        continue
                    selected_quality = qualities[choice_int - 1]['id']
                    break

                while True:
                    output_folder = input("다운로드 받을 폴더 경로 (예: C:/Downloads): ").strip()
                    if not output_folder:
                        print("다운로드 경로를 입력하세요.")
                        continue
                    if not os.path.exists(output_folder):
                        try:
                            os.makedirs(output_folder)
                            print(f"폴더가 생성되었습니다: {output_folder}")
                        except Exception as e:
                            print("폴더 생성 실패:", e)
                            continue
                    break

                # HLS 분기: 구간 다운로드 불허용 → 전체 다운로드 진행
                if vod_info.get("inKey") is None:
                    print("\n빠른 다시보기(HLS) 다운로드는 구간 다운로드가 지원되지 않습니다. 전체 다운로드로 진행합니다.")
                    download_section = None
                else:
                    # DASH 분기: 사용자가 입력한 구간 형식을 그대로 사용
                    while True:
                        section_input = input("다운로드 구간 (00:00:00~00:00:00, 전체: Enter): ").strip()
                        if not section_input:
                            print("전체 다운로드로 진행합니다.")
                            download_section = None
                            break
                        match = re.match(r'^(\d{2}:\d{2}:\d{2})~(\d{2}:\d{2}:\d{2})$', section_input)
                        if not match:
                            print("입력 형식이 올바르지 않습니다. 다시 입력해주세요.")
                            continue
                        download_section = section_input
                        break

                if vod_status in ["UPLOAD", "NONE"]:
                    speed_option = "nolimit"
                    print("\n다운로드 속도 옵션: 제한없음(nolimit)")
                else:
                    print("\n다운로드 속도 옵션 선택:")
                    print("1. 100% (16분할)")
                    print("2. 75% (12분할)")
                    print("3. 50% (8분할)")
                    print("4. 25% (4분할)")
                    print("5. 분할 없음")
                    speed_mapping = {1: "100%", 2: "75%", 3: "50%", 4: "25%", 5: "분할 없음"}
                    while True:
                        speed_choice = input("옵션 번호: ").strip()
                        if not speed_choice:
                            print("값을 입력해야 합니다.")
                            continue
                        try:
                            sp = int(speed_choice)
                            if sp not in speed_mapping:
                                print("잘못된 선택입니다.")
                                continue
                            speed_option = speed_mapping[sp]
                            break
                        except ValueError:
                            print("숫자를 입력하세요.")
                            continue

                print("\n최종 정보 확인:")
                print("VOD URL:", vod_url)
                print("품질 ID:", selected_quality)
                print("폴더:", output_folder)
                print("파일명:", auto_filename)
                print("속도 옵션:", speed_option)
                print("구간:", download_section if download_section else "전체")
                confirm = input("다운로드를 시작하시겠습니까? (y 혹은 n 입력): ").strip().lower()
                print("[DEBUG] confirm 입력:", confirm)
                if confirm != "y":
                    print("취소되었습니다.")
                    input("계속하려면 Enter를 누르세요.")
                    continue

                try:
                    downloadVOD(vod_url, selected_quality, output_folder, auto_filename, speed_option, download_section)
                except Exception as e:
                    print("다운로드 중 오류 발생:", e)
                    input("계속하려면 Enter를 누르세요.")
                    continue

            run_again = input("\n다시 실행하시겠습니까? (y 혹은 n 입력): ").strip().lower()
            if run_again != "y":
                break
    input("\n프로그램을 종료하려면 Enter키를 누르세요.")

if __name__ == '__main__':
    main()
