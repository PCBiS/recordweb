from __future__ import annotations

import asyncio
import contextlib
import html
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin

import aiohttp

try:
    import certifi
except Exception:
    certifi = None

from module.data_manager import ( 
    RecorderManager, base_directory, getFFmpeg, loadChannels, loadConfig, notifyEvent,
    checkDiskSpaceLow,getCimeCookieHeader,
)

try:
    from module.runtime_log import debugThrottle
except Exception:
    def debugThrottle(_key: str, msg: str, min_secs: float = 30.0):
        print(msg)


recorder_manager = RecorderManager()

CIME_REFERER = "https://ci.me/"
CIME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
CIME_QUALITIES = {"best", "worst", "2160p", "1440p", "1080p", "720p", "480p", "360p"}

# 씨미 파일명 생성시 텍스트 길이 제한
CIME_FILENAME_PART_LIMIT = 60
CIME_LIVE_TITLE_LIMIT = 45
CIME_CHANNEL_NAME_LIMIT = 40

def _headers(accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") -> Dict[str, str]:
    headers = {
        "User-Agent": CIME_USER_AGENT,
        "Referer": CIME_REFERER,
        "Origin": "https://ci.me",
        "Accept": accept,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    cookie_header = getCimeCookieHeader()
    if cookie_header:
        headers["Cookie"] = cookie_header

    return headers


def _jsonHeaders() -> Dict[str, str]:
    h = _headers("application/json, text/plain, */*")
    h["X-Requested-With"] = "XMLHttpRequest"
    return h


def _createCimeSslContext():
    try:
        if certifi:
            return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    try:
        return ssl.create_default_context()
    except Exception:
        return None


# 씨미 URL 생성
def buildCimeLiveUrl(channel: dict) -> str:
    raw = str(channel.get("url") or channel.get("stream_url") or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    cid = str(channel.get("id") or "").strip()
    if not cid:
        return "https://ci.me/"
    if cid.startswith("http://") or cid.startswith("https://"):
        return cid
    if not cid.startswith("@"):
        cid = "@" + cid
    return f"https://ci.me/{cid}/live"


def sanitizeFilename(text: Any, fallback: str = "recording", limit: int = CIME_FILENAME_PART_LIMIT) -> str:
    s = str(text or fallback).replace("\n", " ").replace("\r", " ")
    s = re.sub(r'[\\/*?:"<>|+]', "_", s)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    if not s:
        s = fallback
    return s[:limit]


def uniquePath(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    idx = 1
    while True:
        cand = f"{base} ({idx}){ext}"
        if not os.path.exists(cand):
            return cand
        idx += 1


# 씨미 m3u8 추출
def extractCimeM3u8Url(text: str) -> Optional[str]:
    if not text:
        return None

    decoded = text
    for _ in range(2):
        decoded = html.unescape(decoded)
        decoded = decoded.replace(r"\/", "/")
        try:
            decoded = bytes(decoded, "utf-8").decode("unicode_escape")
        except Exception:
            pass

    patterns = [
        r"https?://[^'\"\s<>]+\.m3u8[^'\"\s<>]*",
        r"https?%3A%2F%2F[^'\"\s<>]+?\.m3u8[^'\"\s<>]*",
    ]
    for pattern in patterns:
        m = re.search(pattern, decoded, re.IGNORECASE)
        if m:
            return unquote(m.group(0)).replace("\\/", "/")
    return None


def _walkDicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walkDicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walkDicts(item)


def _decodeCimeText(text: str) -> str:
    decoded = html.unescape(text or "")
    decoded = decoded.replace(r"\/", "/")
    decoded = decoded.replace("\\u0026", "&")
    return decoded


def extractCimeBodyDataJson(text: str) -> dict:
    if not text:
        return {}

    decoded = _decodeCimeText(text).strip()

    # 1) 응답 자체가 JSON인 경우
    try:
        data = json.loads(decoded)
        if isinstance(data, dict) and "bodyData" in data:
            return data
        if isinstance(data, dict):
            for d in _walkDicts(data):
                if "bodyData" in d:
                    return d
    except Exception:
        pass

    # 2) HTML/script 안에 JSON 조각으로 들어간 경우
    if '"bodyData"' not in decoded and "&quot;bodyData&quot;" not in decoded:
        return {}

    decoder = json.JSONDecoder()
    for m in re.finditer(r'\{\s*"bodyData"\s*:', decoded):
        try:
            data, _end = decoder.raw_decode(decoded[m.start():])
            if isinstance(data, dict) and "bodyData" in data:
                return data
        except Exception:
            pass

    idx = decoded.find('"bodyData"')
    for pos in range(idx, max(-1, idx - 20000), -1):
        if decoded[pos] != "{":
            continue
        try:
            data, _end = decoder.raw_decode(decoded[pos:])
            if isinstance(data, dict) and "bodyData" in data:
                return data
        except Exception:
            continue

    return {}


def extractCimeLiveFromBodyData(text: str) -> dict:
    data = extractCimeBodyDataJson(text)
    if not data:
        return {}

    body = data.get("bodyData") or {}
    home = body.get("homeData") or {}
    live = home.get("live")
    if isinstance(live, dict):
        return live

    # 구조 변경 대비 fallback
    for d in _walkDicts(data):
        candidate = d.get("live")
        if isinstance(candidate, dict) and (
            candidate.get("playbackUrl")
            or (isinstance(candidate.get("playback"), dict) and candidate["playback"].get("url"))
            or candidate.get("state") == "ACTIVE"
        ):
            return candidate

    return {}


def parseCimeOpenedAt(value: Any) -> datetime:
    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(
            timezone(timedelta(hours=9))
        )
    except Exception:
        return datetime.now(
            timezone(timedelta(hours=9))
        )


def formatCimeStartDate(value: Any) -> str:
    return parseCimeOpenedAt(value).strftime("%Y-%m-%d")


def formatCimeBroadcastTime(value: Any) -> str:
    return parseCimeOpenedAt(value).strftime("%y%m%d_%H%M%S")


def normalizeCimeLiveMetadata(channel: dict, live: dict, fallback_page: str = "") -> Dict[str, Any]:
    playback = live.get("playback") if isinstance(live.get("playback"), dict) else {}
    category_obj = live.get("category") if isinstance(live.get("category"), dict) else {}
    live_channel = live.get("channel") if isinstance(live.get("channel"), dict) else {}

    quality = str(channel.get("quality") or "best").strip().lower()
    can_watch_uhd = bool(playback.get("canWatchUhd"))
    url_uhd = playback.get("urlUhd")

    # best 또는 2160p 요청일 때, 계정 권한이 있고 urlUhd를 가져올 수 있으면 4K URL을 우선 사용합니다.
    if quality in ("best", "2160p") and can_watch_uhd and url_uhd:
        playback_url = url_uhd
        selected_source = "uhd"
    else:
        playback_url = (
            live.get("playbackUrl")
            or playback.get("url")
            or extractCimeM3u8Url(fallback_page)
        )
        selected_source = "normal"

    title = live.get("title") or extractCimeTitle(fallback_page, channel)
    thumb = live.get("imageUrl") or extractCimeThumbnail(fallback_page)
    category = str(category_obj.get("name") or "씨미").strip() or "씨미"
    state = str(live.get("state") or "").upper()

    is_live = bool(playback_url) and state in {"ACTIVE", "OPEN", "LIVE"}

    return {
        "status": "OPEN" if is_live else "CLOSE",
        "is_live": is_live,
        "live_title": title or "방송 제목 없음",
        "liveTitle": title or "방송 제목 없음",
        "category": category,
        "thumbnail_url": thumb or ("/static/img/cime_thumbnail.png" if is_live else "/static/img/cimeclosed_thumbnail.png"),
        "playback_url": playback_url,
        "master_url": playback_url,
        "start_time": formatCimeStartDate(live.get("openedAt")),
        "broadcast_time": formatCimeBroadcastTime(live.get("openedAt")),
        "adult": bool(live.get("isAdult")),
        "viewer_count": live.get("curViewerCnt"),
        "channel_name": live_channel.get("name") or "",
        "channel_slug": live_channel.get("slug") or "",
        "cime_channel_id": live_channel.get("id") or "",

        # 디버그/상태 확인용
        "uhd_active": bool(playback.get("uhdActive")),
        "can_watch_uhd": can_watch_uhd,
        "url_uhd": url_uhd,
        "selected_playback_source": selected_source,
        "is_multitrack": bool(playback.get("isMultitrack")),
    }


def extractCimeSlugFromChannel(channel_or_url: Any) -> str:
    if isinstance(channel_or_url, dict):
        raw = str(
            channel_or_url.get("id")
            or channel_or_url.get("url")
            or channel_or_url.get("stream_url")
            or ""
        ).strip()
    else:
        raw = str(channel_or_url or "").strip()

    if not raw:
        return ""

    m = re.search(r"ci\.me/@?([A-Za-z0-9_]+)", raw)
    if m:
        return m.group(1)

    raw = raw.strip().strip("/")
    raw = raw.replace("https://ci.me/", "").replace("http://ci.me/", "")
    raw = raw.replace("live", "").strip("/")
    raw = raw.lstrip("@").strip()

    if "/" in raw:
        raw = raw.split("/")[0]

    return raw


async def fetchCimeBodyDataCandidates(
    watch_url: str,
    channel: Optional[dict] = None,
    timeout: float = 12.0,
    verbose: bool = False,
) -> Tuple[str, str]:
    candidates: List[str] = []

    slug = extractCimeSlugFromChannel(channel or {}) or extractCimeSlugFromChannel(watch_url)

    if slug:
        # 실제 라우터 데이터 경로
        candidates.extend([
            f"https://ci.me/json/@{slug}/live",
            f"https://ci.me/json/@{slug}",
            f"https://ci.me/json/@{slug}/vods",
            f"https://ci.me/json/@{slug}/clips",
        ])

    if watch_url:
        candidates.append(watch_url)
        sep = "&" if "?" in watch_url else "?"
        candidates.extend([
            f"{watch_url}{sep}__data=1",
            f"{watch_url}{sep}_data=1",
            f"{watch_url}{sep}index",
        ])

    seen = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            status, final_url, body = await _fetchText(url, timeout=timeout, headers=_jsonHeaders())
            has_live = bool(extractCimeLiveFromBodyData(body))

            if verbose:
                print(
                    f"[CIME][JSON] try={url} status={status} "
                    f"final={final_url} len={len(body or '')} has_live={has_live}",
                    flush=True,
                )

            if status < 400 and has_live:
                return final_url, body

        except Exception as e:
            if verbose:
                print(f"[CIME][JSON][ERROR] try={url} err={e}", flush=True)
            continue

    return "", ""


def _stripHtmlTags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extractMetaContent(decoded: str, key_name: str) -> Optional[str]:
    for m in re.finditer(r"<meta\b[^>]*>", decoded, re.IGNORECASE | re.DOTALL):
        tag = m.group(0)

        has_key = re.search(
            rf'(?:property|name)=["\']{re.escape(key_name)}["\']',
            tag,
            re.IGNORECASE,
        )
        if not has_key:
            continue

        content = re.search(
            r'content=["\']([^"\']+)["\']',
            tag,
            re.IGNORECASE | re.DOTALL,
        )
        if content:
            return html.unescape(content.group(1)).strip()

    return None


def extractCimeTitle(text: str, channel: dict) -> str:
    decoded = html.unescape((text or "").replace(r"\/", "/"))

    candidates = []

    # 1) meta 기반
    for key in ("og:title", "twitter:title", "title"):
        v = _extractMetaContent(decoded, key)
        if v:
            candidates.append(v)

    # 2) title 태그
    m = re.search(r"<title[^>]*>(.*?)</title>", decoded, re.IGNORECASE | re.DOTALL)
    if m:
        candidates.append(_stripHtmlTags(m.group(1)))

    # 3) 실제 본문 heading 기반
    for tag in ("h1", "h2"):
        for m in re.finditer(
            rf"<{tag}[^>]*>(.*?)</{tag}>",
            decoded,
            re.IGNORECASE | re.DOTALL,
        ):
            candidates.append(_stripHtmlTags(m.group(1)))

    # 4) JSON/스크립트 조각 기반
    json_patterns = [
        r'"liveTitle"\s*:\s*"([^"]+)"',
        r'"live_title"\s*:\s*"([^"]+)"',
        r'"title"\s*:\s*"([^"]+)"',
    ]
    for pattern in json_patterns:
        for m in re.finditer(pattern, decoded, re.IGNORECASE):
            candidates.append(m.group(1))

    channel_name = str(channel.get("name") or "").strip()

    for raw in candidates:
        title = html.unescape(str(raw or ""))
        title = title.replace("\\u0026", "&")
        title = title.replace("\\/", "/")
        title = _stripHtmlTags(title)
        title = re.sub(r"\s*[-|]\s*씨미\s*$", "", title, flags=re.IGNORECASE).strip()
        title = re.sub(r"\s*[-|]\s*ci\.me\s*$", "", title, flags=re.IGNORECASE).strip()

        # 사이트명/채널명만 나온 경우는 방송 제목으로 쓰지 않음
        if not title:
            continue
        if title in ("씨미", "CIME", "ci.me"):
            continue
        if channel_name and title == channel_name:
            continue

        return title

    return channel_name or "씨미 녹화"


def extractCimeThumbnail(text: str) -> str:
    decoded = html.unescape((text or "").replace(r"\/", "/"))
    patterns = [
        r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, decoded, re.IGNORECASE | re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


async def _fetchText(url: str, timeout: float = 15.0, headers: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=min(5.0, timeout))
    req_headers = headers or _headers()
    ssl_context = _createCimeSslContext()

    async def _request_with_ssl(ssl_value):
        async with aiohttp.ClientSession(timeout=client_timeout, headers=req_headers) as session:
            async with session.get(url, allow_redirects=True, ssl=ssl_value) as resp:
                return resp.status, str(resp.url), await resp.text(errors="replace")

    try:
        return await _request_with_ssl(ssl_context if ssl_context is not None else True)

    except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError, ssl.SSLError) as e:
        print(
            f"[CIME][SSL][WARN] 인증서 검증 실패. 해당 요청에 한해 ssl=False로 재시도: {url} / {e}",
            flush=True,
        )
        return await _request_with_ssl(False)


def _isLivePlaylistText(playlist: str) -> bool:
    if not playlist or "#EXTM3U" not in playlist.upper():
        return False
    if "#EXT-X-ENDLIST" in playlist.upper():
        return False
    up = playlist.upper()
    return "#EXT-X-STREAM-INF" in up or "#EXTINF" in up


async def isLivePlaylist(stream_url: str) -> bool:
    try:
        status, _, playlist = await _fetchText(stream_url, timeout=10.0)
        if status >= 400:
            return False
        return _isLivePlaylistText(playlist)
    except Exception:
        return False


def parseCimeVariants(master_url: str, playlist: str) -> List[Dict[str, Any]]:
    variants: List[Dict[str, Any]] = []
    lines = playlist.splitlines()
    for i, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue

        next_url = None
        for nxt in lines[i + 1:]:
            nxt = nxt.strip()
            if not nxt or nxt.startswith("#"):
                continue
            next_url = nxt
            break

        if not next_url:
            continue

        bw = 0
        width = 0
        height = 0
        frame_rate = ""

        m_bw = re.search(r"BANDWIDTH=(\d+)", line, re.IGNORECASE)
        if m_bw:
            bw = int(m_bw.group(1))

        m_res = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.IGNORECASE)
        if m_res:
            width = int(m_res.group(1))
            height = int(m_res.group(2))

        m_fps = re.search(r"FRAME-RATE=([0-9.]+)", line, re.IGNORECASE)
        if m_fps:
            try:
                fps_float = float(m_fps.group(1))
                frame_rate = str(int(round(fps_float)))
            except Exception:
                frame_rate = ""

        variants.append({
            "url": urljoin(master_url, next_url),
            "bandwidth": bw,
            "width": width,
            "height": height,
            "frame_rate": frame_rate,
        })

    return variants


def formatCimeVariantLabel(variant: Dict[str, Any], fallback: str = "best") -> str:
    height = variant.get("height") or 0
    frame_rate = str(variant.get("frame_rate") or "").strip()

    if height:
        return f"{height}p{frame_rate}" if frame_rate else f"{height}p"

    return fallback


async def selectCimeQuality(master_url: str, quality: str) -> Tuple[str, str]:
    q = (quality or "best").strip().lower()
    if q not in CIME_QUALITIES:
        q = "best"

    try:
        status, _, playlist = await _fetchText(master_url, timeout=10.0)
        if status >= 400:
            return master_url, "best"
    except Exception:
        return master_url, "best"

    variants = parseCimeVariants(master_url, playlist)
    if not variants:
        return master_url, "best"

    if q == "best":
        v = sorted(
            variants,
            key=lambda x: (x.get("height") or 0, x.get("bandwidth") or 0),
            reverse=True,
        )[0]
        label = formatCimeVariantLabel(v, "best")
        return v["url"], label

    if q == "worst":
        v = sorted(variants, key=lambda x: (x.get("bandwidth") or 0, x.get("height") or 0))[0]
        label = formatCimeVariantLabel(v, "worst")
        return v["url"], label

    if q.endswith("p"):
        target = int(q[:-1])
        variants_sorted = sorted(
            variants,
            key=lambda v: (
                0 if (v.get("height") or 0) <= target else 1,
                abs(target - (v.get("height") or 0)),
                -(v.get("bandwidth") or 0),
            ),
        )
        v = variants_sorted[0]
        label = formatCimeVariantLabel(v, q)
        return v["url"], label

    return master_url, "best"


#FSM 메타 캐시와 씨미 메타데이터 반환
async def getCimeMetadata(channel: dict, verbose: bool = False) -> Dict[str, Any]:
    cid = str(channel.get("id") or "unknown")

    default = {
        "platform": "cime",
        "id": cid,
        "status": "CLOSE",
        "is_live": False,
        "live_title": "방송 제목 없음",
        "liveTitle": "방송 제목 없음",
        "category": "씨미",
        "thumbnail_url": "/static/img/cimeclosed_thumbnail.png",
        "watch_url": buildCimeLiveUrl(channel),
        "playback_url": None,
        "master_url": None,
        "record_quality": channel.get("quality", "best") or "best",
        "resolved_quality": channel.get("quality", "best") or "best",
        "frame_rate": "",
        "start_time": datetime.now().strftime("%Y-%m-%d"),
        "broadcast_time": datetime.now().strftime("%y%m%d_%H%M%S"),
        "adult": False,
        "viewer_count": None,
    }

    def finish(result: Dict[str, Any], reason: str) -> Dict[str, Any]:
        if verbose:
            print(
                f"[CIME][META][RESULT] {cid} "
                f"reason={reason} "
                f"is_live={result.get('is_live')} "
                f"status={result.get('status')} "
                f"title={result.get('live_title')!r} "
                f"category={result.get('category')!r} "
                f"playback={'Y' if result.get('playback_url') else 'N'} "
                f"uhdActive={result.get('uhd_active')} "
                f"canWatchUhd={result.get('can_watch_uhd')} "
                f"selected={result.get('selected_playback_source')} "
                f"watch_url={result.get('watch_url')}",
                flush=True,
            )

        return result

    try:
        page = ""
        final_url = default["watch_url"]

        # 1) 씨미 JSON 라우터를 먼저 조회
        json_final_url, json_body = await fetchCimeBodyDataCandidates(default["watch_url"],
            channel, verbose=verbose,)
        live = extractCimeLiveFromBodyData(json_body) if json_body else {}

        if json_final_url:
            default["watch_url"] = json_final_url

        # 2) JSON에서 못 찾았을 때만 HTML fallback
        if not live:
            try:
                status, final_url, page = await _fetchText(default["watch_url"], timeout=15.0)
                if verbose:
                    print(
                        f"[CIME][HTML] {cid} status={status} "
                        f"final_url={final_url} len={len(page or '')}",
                        flush=True,
                    )

                if status >= 400:
                    return finish(default, f"html_status_{status}")

                default["watch_url"] = final_url
                live = extractCimeLiveFromBodyData(page)

            except Exception as e:
                print(f"[CIME][HTML][ERROR] {cid}: {e}", flush=True)
                return finish(default, "html_fetch_error")

        # 3) 그래도 안되면 bodyData.homeData.live 기반 메타데이터 사용
        if live:
            meta = normalizeCimeLiveMetadata(channel, live, page)
            default.update(meta)

            stream_url = default.get("playback_url")

            if not stream_url:
                return finish(default, "live_found_but_no_playback_url")

            try:
                chosen, label = await selectCimeQuality(stream_url, channel.get("quality", "best"))
            except Exception as e:
                if verbose:
                    print(f"[CIME][QUALITY][WARN] {cid}: {e}", flush=True)
                chosen, label = stream_url, "best"

            playback_candidate = chosen or stream_url
            playlist_ok = await isLivePlaylist(playback_candidate)

            if not playlist_ok:
                default.update({
                    "status": "CLOSE",
                    "is_live": False,
                    "playback_url": None,
                    "master_url": None,
                    "record_quality": channel.get("quality", "best") or "best",
                    "resolved_quality": channel.get("quality", "best") or "best",
                    "frame_rate": "",
                    "thumbnail_url": default.get("thumbnail_url") or "/static/img/cimeclosed_thumbnail.png",
                })
                return finish(default, "live_json_but_playlist_closed")

            default.update({
                "status": "OPEN",
                "is_live": True,
                "playback_url": playback_candidate,
                "master_url": stream_url,
                "record_quality": label or "best",
                "resolved_quality": label or "best",
                "frame_rate": "",
            })

            return finish(default, "live_json_ok")

        # 4) 정말 안되면 기존 HTML 정규식 방식 fallback
        stream_url = extractCimeM3u8Url(page)
        title = extractCimeTitle(page, channel)
        thumb = extractCimeThumbnail(page)

        default.update({
            "live_title": title,
            "liveTitle": title,
            "watch_url": final_url,
        })

        if thumb:
            default["thumbnail_url"] = thumb

        if not stream_url:
            return finish(default, "no_live_json_no_m3u8")

        try:
            chosen, label = await selectCimeQuality(stream_url, channel.get("quality", "best"))
        except Exception as e:
            if verbose:
                print(f"[CIME][QUALITY][WARN] {cid}: {e}", flush=True)
            chosen, label = stream_url, "best"

        playback_candidate = chosen or stream_url
        playlist_ok = await isLivePlaylist(playback_candidate)

        if not playlist_ok:
            default.update({
                "status": "CLOSE",
                "is_live": False,
                "playback_url": None,
                "master_url": None,
                "record_quality": channel.get("quality", "best") or "best",
                "resolved_quality": channel.get("quality", "best") or "best",
                "frame_rate": "",
            })
            return finish(default, "html_m3u8_but_playlist_closed")

        if not thumb:
            default["thumbnail_url"] = "/static/img/cime_thumbnail.png"

        default.update({
            "status": "OPEN",
            "is_live": True,
            "playback_url": playback_candidate,
            "master_url": stream_url,
            "record_quality": label or "best",
            "resolved_quality": label or "best",
            "frame_rate": "",
        })

        return finish(default, "html_m3u8_fallback_ok")

    except Exception as e:
        debugThrottle(
            f"cime:meta:error:{cid}",
            f"[CIME][META][ERROR] {cid}: {e}",
            min_secs=30.0,
        )
        print(f"[CIME][META][EXCEPTION] {cid}: {e}", flush=True)
        return finish(default, "exception")


def buildCimeFilename(channel: dict, metadata: dict, filenamePattern: str) -> Tuple[str, str]:
    output_dir = os.path.join(base_directory, channel.get("output_dir", "./output"))
    os.makedirs(output_dir, exist_ok=True)

    live_title = metadata.get("liveTitle") or metadata.get("live_title") or "씨미 녹화"
    safe_live_title = sanitizeFilename(live_title, "씨미 녹화", CIME_LIVE_TITLE_LIMIT)
    channel_name = sanitizeFilename(
        channel.get("name") or channel.get("id") or "씨미",
        "씨미",
        CIME_CHANNEL_NAME_LIMIT
    )
    start_time = (metadata.get("start_time") or datetime.now().strftime("%Y-%m-%d"))
    recording_time = datetime.now().strftime("%y%m%d_%H%M%S")
    broadcast_time = (metadata.get("broadcast_time") or recording_time)
    record_quality = sanitizeFilename(metadata.get("record_quality") or channel.get("quality") or "best", "best", 20)
    frame_rate = sanitizeFilename(metadata.get("frame_rate") or "", "", 10)
    file_extension = ".mp4"

    pattern = filenamePattern or "[{start_time}] {channel_name} {safe_live_title} {record_quality}{file_extension}"
    try:
        filename = pattern.format(
            start_time=start_time,
            broadcast_time=broadcast_time,
            recording_time=recording_time,
            safe_live_title=safe_live_title,
            channel_name=channel_name,
            record_quality=record_quality,
            frame_rate=frame_rate,
            file_extension=file_extension,
        )
    except Exception:
        filename = f"[{start_time}] {channel_name} {safe_live_title} {record_quality}{file_extension}"

    base, ext = os.path.splitext(filename)
    if not ext:
        filename += ".mp4"
    elif ext.lower() != ".mp4":
        filename = base + ".mp4"

    final_path = uniquePath(os.path.join(output_dir, filename))

    base_without_ext = os.path.splitext(final_path)[0]
    temp_path = uniquePath(base_without_ext + ".part.ts")

    return temp_path, final_path


def _buildCimeFfmpegHeaders() -> str:
    headers = (
        "Referer: https://ci.me/\r\n"
        "Accept: application/x-mpegURL, application/vnd.apple.mpegurl, application/json, text/plain\r\n"
    )

    cookie_header = getCimeCookieHeader()
    if cookie_header:
        headers += f"Cookie: {cookie_header}\r\n"

    return headers


def buildCimeFfmpegCommand(stream_url: str, temp_path: str) -> List[str]:
    return [
        getFFmpeg(),
        "-hide_banner",
        "-y",
        "-loglevel", "warning",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_on_http_error", "4xx,5xx",
        "-reconnect_delay_max", "30",
        "-reconnect_max_retries", "10",

        "-extension_picky", "0",
        "-user_agent", CIME_USER_AGENT,
        "-headers", _buildCimeFfmpegHeaders(),

        "-i", stream_url,

        # 씨미 multitrack/master HLS 안정화
        "-map", "0:v:0",
        "-map", "0:a:0?",

        "-c", "copy",
        "-f", "mpegts",
        temp_path,
    ]


def _formatCimeCmdForLog(cmd: List[str]) -> str:
    safe_parts = []

    for item in cmd:
        text = str(item)

        if "Cookie:" in text:
            text = re.sub(
                r"(Cookie:\s*)[^\r\n]+",
                r"\1<hidden>",
                text,
                flags=re.IGNORECASE,
            )

        safe_parts.append(text)

    return " ".join(safe_parts)


async def _readStream(prefix: str, stream):
    if not stream:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "ignore").strip()
            if text:
                print(f"{prefix} {text}", flush=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


def _formatBytes(size: int) -> str:
    try:
        size = int(size or 0)
    except Exception:
        size = 0

    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024

    return f"{size}B"


def _formatElapsed(seconds: float) -> str:
    try:
        seconds = int(seconds or 0)
    except Exception:
        seconds = 0

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


async def _watchCimeProgress(channel_name: str, temp_path: str, interval: int = 10):
    start_ts = time.time()
    last_size = -1

    try:
        while True:
            await asyncio.sleep(max(3, int(interval or 10)))

            try:
                size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
            except Exception:
                size = 0

            # 파일크기 변동이 없어도 로그 출력
            elapsed = _formatElapsed(time.time() - start_ts)
            size_text = _formatBytes(size)
            delta = size - last_size if last_size >= 0 else size
            delta_text = _formatBytes(delta) if delta > 0 else "0B"

            print(
                f"[CIME][PROG] {channel_name}: 녹화 중 {elapsed} / {size_text} "
                f"(+{delta_text}, {os.path.basename(temp_path)})",
                flush=True,
            )

            last_size = size

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[CIME][PROG][WARN] 진행상태 출력 오류: {e}", flush=True)


async def _stopProcessGracefully(proc: asyncio.subprocess.Process, timeout: float = 8.0):
    if not proc or proc.returncode is not None:
        return
    try:
        if proc.stdin:
            proc.stdin.write(b"q\n")
            await proc.stdin.drain()
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return
    except Exception:
        pass

    if proc.returncode is None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.terminate()
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5.0)


async def remuxCimeTempToMp4(temp_path: str, final_path: str) -> bool:
    if not os.path.exists(temp_path) or os.path.getsize(temp_path) <= 0:
        return False
    cmd = [
        getFFmpeg(),
        "-hide_banner",
        "-y",
        "-i", temp_path,
        "-c", "copy",
        "-movflags", "+faststart",
        final_path,
    ]
    print("[CIME][REMUX]", " ".join(cmd), flush=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )
    stderr_task = asyncio.create_task(_readStream("[CIME][REMUX][FFMPEG]", proc.stderr))
    rc = await proc.wait()
    with contextlib.suppress(Exception):
        await stderr_task
    if rc == 0 and os.path.exists(final_path) and os.path.getsize(final_path) > 0:
        with contextlib.suppress(Exception):
            os.remove(temp_path)
        return True
    return False


def resolveCimeFFprobe() -> str:
    ffmpeg_path = getFFmpeg()
    ffmpeg_dir = os.path.dirname(ffmpeg_path or "")

    candidates = []
    if os.name == "nt":
        candidates = [
            os.path.join(ffmpeg_dir, "ffprobe.exe"),
            os.path.join(ffmpeg_dir, "ffprobe"),
        ]
    else:
        candidates = [
            os.path.join(ffmpeg_dir, "ffprobe"),
            os.path.join(ffmpeg_dir, "ffprobe.exe"),
        ]

    for path in candidates:
        if path and os.path.exists(path):
            return path

    return shutil.which("ffprobe") or "ffprobe"


def parseFpsValue(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw in ("0/0", "N/A"):
        return ""

    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            num_f = float(num)
            den_f = float(den)
            if den_f == 0:
                return ""
            fps = num_f / den_f
        else:
            fps = float(raw)

        rounded = round(fps)
        if abs(fps - rounded) < 0.25:
            return str(int(rounded))

        return f"{fps:.2f}".rstrip("0").rstrip(".")

    except Exception:
        return ""


def probeCimeRecordedQuality(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""

    ffprobe = resolveCimeFFprobe()

    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
        "-of", "json",
        path,
    ]

    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )

        data = json.loads(out or "{}")
        streams = data.get("streams") or []
        if not streams:
            return ""

        stream = streams[0]
        height = int(stream.get("height") or 0)

        fps = (
            parseFpsValue(stream.get("avg_frame_rate"))
            or parseFpsValue(stream.get("r_frame_rate"))
        )

        if height <= 0:
            return ""

        return f"{height}p{fps}" if fps else f"{height}p"

    except Exception as e:
        print(f"[CIME][QUALITY][WARN] ffprobe 품질 확인 실패: {e}", flush=True)
        return ""


def renameCimeFinalByActualQuality(final_path: str, metadata: dict) -> str:
    actual_quality = probeCimeRecordedQuality(final_path)
    if not actual_quality:
        return final_path

    old_quality = str(
        metadata.get("record_quality")
        or metadata.get("resolved_quality")
        or ""
    ).strip()

    if old_quality == actual_quality:
        return final_path

    base, ext = os.path.splitext(final_path)
    
    quality_pattern = re.compile(
        r"(?P<prefix>.*?)(?P<sep>[\s_\-\[\]\(\)]*)"
        r"(?P<quality>best|worst|(?:2160|1440|1080|720|480|360)p(?:\d{2,3})?)$",
        re.IGNORECASE,
    )

    m = quality_pattern.match(base)
    if m:
        new_base = f"{m.group('prefix')}{m.group('sep')}{actual_quality}"
    else:
        new_base = f"{base} {actual_quality}"

    new_path = uniquePath(new_base + ext)

    try:
        os.replace(final_path, new_path)
        metadata["record_quality"] = actual_quality
        metadata["resolved_quality"] = actual_quality
        metadata["frame_rate"] = re.sub(r"^\d+p", "", actual_quality)

        print(
            f"[CIME][RENAME] 실제 품질 기준 파일명 변경: "
            f"{os.path.basename(final_path)} -> {os.path.basename(new_path)}",
            flush=True,
        )

        return new_path

    except Exception as e:
        print(f"[CIME][RENAME][WARN] 파일명 품질 교정 실패: {e}", flush=True)
        return final_path


def _moveAfterProcessing(path: str, target_dir: str) -> str:
    if not path or not target_dir or not os.path.exists(path):
        return path
    os.makedirs(target_dir, exist_ok=True)
    dst = uniquePath(os.path.join(target_dir, os.path.basename(path)))
    shutil.move(path, dst)
    return dst


def _findChannel(channel_id: str) -> Optional[dict]:
    state_channels = RecorderManager.getChannels() or []
    ch = next((c for c in state_channels if c.get("id") == channel_id), None)
    if ch:
        return ch
    try:
        disk_channels = loadChannels() or []
        return next((c for c in disk_channels if c.get("id") == channel_id), None)
    except Exception:
        return None


async def cimeStartRecording(channel, recheckInterval: int, filenamePattern: str,
                             moveAfterProcessingEnabled: bool = False,
                             moveAfterProcessing: str = "",
                             is_user_request: bool = False,
                             **_ignored):
    channel_id = channel.get("id") if isinstance(channel, dict) else str(channel or "")
    if not channel_id:
        print("[CIME][ERROR] 유효하지 않은 채널 인자")
        return

    if is_user_request:
        recorder_manager.set_is_user_stopped(channel_id, False)

    while True:
        curr = _findChannel(channel_id)
        if not curr:
            print(f"[CIME][ERROR] 채널을 찾을 수 없습니다: {channel_id}")
            return

        channel_name = curr.get("name") or channel_id
        if recorder_manager.get_is_user_stopped(channel_id):
            recorder_manager.set_status_recording(channel_id, False)
            recorder_manager.set_status_reserved(channel_id, False)
            return

        if curr.get("platform") != "cime":
            print(f"[CIME][ERROR] platform이 cime이 아닙니다: {channel_name}")
            return

        recorder_manager.set_status_reserved(channel_id, True)
        recorder_manager.set_status_recording(channel_id, False)

        metadata = await getCimeMetadata(curr)
        if not metadata.get("is_live"):
            debugThrottle(f"cime:offline:{channel_id}", f"[CIME] {channel_name}: 방송 오프라인, 재탐색 대기", min_secs=60.0)
            await asyncio.sleep(max(5, int(recheckInterval or 60)))
            continue

        if not metadata.get("playback_url"):
            debugThrottle(
                f"cime:playback-missing:{channel_id}",
                f"[CIME] {channel_name}: 라이브 감지됨, 하지만 m3u8/playback URL 미검출. 재탐색 대기",
                min_secs=60.0,
            )
            await asyncio.sleep(max(5, int(recheckInterval or 60)))
            continue

        playlist_ok = await isLivePlaylist(metadata["playback_url"])
        if not playlist_ok:
            debugThrottle(
                f"cime:playlist-closed:{channel_id}",
                f"[CIME] {channel_name}: playback URL이 이미 닫혔거나 404입니다. 재탐색 대기",
                min_secs=60.0,
            )
            recorder_manager.set_status_recording(channel_id, False)
            recorder_manager.set_status_reserved(channel_id, True)
            await asyncio.sleep(max(5, int(recheckInterval or 60)))
            continue

        temp_path, final_path = buildCimeFilename(curr, metadata, filenamePattern)
        curr["output_path"] = final_path

        checkDiskSpaceLow(
            os.path.dirname(final_path),
            channel_id=channel_id,
            channel_name=channel_name
        )

        recorder_manager.recording_set_start_time(channel_id)
        recorder_manager.recording_set_filename(channel_id, final_path)
        recorder_manager.set_status_recording(channel_id, True)
        recorder_manager.set_status_reserved(channel_id, False)

        cmd = buildCimeFfmpegCommand(metadata["playback_url"], temp_path)
        print(f"[CIME][START] {channel_name} → {temp_path}", flush=True)
        print("[CIME][CMD]", _formatCimeCmdForLog(cmd), flush=True)

        proc = None
        stderr_task = None
        progress_task = None
        rc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0),
            )
            recorder_manager.set_tasks_process(channel_id, proc)

            notifyEvent(
                "record_started",
                "녹화 시작",
                "씨미 녹화가 시작되었습니다.",
                channel_id=channel_id,
                channel_name=channel_name,
                detail=os.path.basename(temp_path),
                severity="info"
            )

            stderr_task = asyncio.create_task(_readStream("[CIME][FFMPEG]", proc.stderr))
            progress_task = asyncio.create_task(
                _watchCimeProgress(channel_name, temp_path, interval=10)
            )

            rc = await proc.wait()
            print(f"[CIME][END] {channel_name}: ffmpeg exit={rc}", flush=True)

        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                await _stopProcessGracefully(proc)
            raise

        except Exception as e:
            print(f"[CIME][ERROR] {channel_name}: 녹화 프로세스 오류: {e}", flush=True)

            notifyEvent(
                "record_start_failed",
                "녹화 시작 실패",
                "씨미 녹화 프로세스를 시작하지 못했습니다.",
                channel_id=channel_id,
                channel_name=channel_name,
                detail=str(e),
                severity="error"
            )

            await asyncio.sleep(max(5, int(recheckInterval or 60)))
            continue

        finally:
            if progress_task:
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await progress_task

            if stderr_task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stderr_task

            recorder_manager.clear_tasks_process(channel_id)
            recorder_manager.set_status_recording(channel_id, False)
            recorder_manager.recording_remove_start_time(channel_id)
            recorder_manager.recording_remove_filename(channel_id)

        ok = False
        try:
            temp_exists = os.path.exists(temp_path)
            temp_size = os.path.getsize(temp_path) if temp_exists else 0

            # ffmpeg가 404 등으로 입력을 열지 못한 경우에는 remux를 시도하지 않습니다.
            if rc not in (0, None) and temp_size <= 0:
                print(
                    f"[CIME][WARN] 녹화 데이터가 없어 mp4 마무리를 건너뜁니다. "
                    f"ffmpeg exit={rc}, temp_size={temp_size}, temp={temp_path}",
                    flush=True,
                )

                with contextlib.suppress(Exception):
                    if temp_exists:
                        os.remove(temp_path)

                ok = False

            else:
                ok = await remuxCimeTempToMp4(temp_path, final_path)

            if ok:
                # 최종 mp4 기준으로 실제 품질명을 확인하여 파일명을 교정합니다.
                final_path = renameCimeFinalByActualQuality(final_path, metadata)
                curr["output_path"] = final_path

                moved_path = final_path
                if moveAfterProcessingEnabled and moveAfterProcessing:
                    moved_path = _moveAfterProcessing(final_path, moveAfterProcessing)

                notifyEvent(
                    "record_finished",
                    "녹화 완료",
                    "씨미 녹화가 완료되었습니다.",
                    channel_id=channel_id,
                    channel_name=channel_name,
                    detail=os.path.basename(moved_path),
                    severity="info"
                )
            else:
                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    print(f"[CIME][WARN] mp4 마무리 실패. 임시파일 유지: {temp_path}", flush=True)

                notifyEvent(
                    "record_abnormally_stopped",
                    "녹화 비정상 종료",
                    "씨미 녹화 파일 마무리에 실패했습니다.",
                    channel_id=channel_id,
                    channel_name=channel_name,
                    detail=f"ffmpeg exit={rc}, temp={os.path.basename(temp_path)}",
                    severity="error"
                )

        except Exception as e:
            print(f"[CIME][ERROR] remux/move 실패: {e}", flush=True)

            notifyEvent(
                "record_abnormally_stopped",
                "녹화 비정상 종료",
                "씨미 remux 또는 파일 이동 중 오류가 발생했습니다.",
                channel_id=channel_id,
                channel_name=channel_name,
                detail=str(e),
                severity="error"
            )

        if recorder_manager.get_is_user_stopped(channel_id) or not curr.get("record_enabled", True):
            recorder_manager.set_status_reserved(channel_id, False)
            return

        recorder_manager.set_status_reserved(channel_id, True)
        await asyncio.sleep(max(5, int(recheckInterval or 60)))


async def cimeStopRecording(channel_id: str):
    recorder_manager.set_is_user_stopped(channel_id, True)
    proc = recorder_manager.get_tasks_process(channel_id)
    if proc and proc.returncode is None:
        await _stopProcessGracefully(proc)
    recorder_manager.set_status_recording(channel_id, False)
    recorder_manager.set_status_reserved(channel_id, False)
    recorder_manager.recording_remove_start_time(channel_id)
    recorder_manager.recording_remove_filename(channel_id)
    recorder_manager.clear_tasks_process(channel_id)
