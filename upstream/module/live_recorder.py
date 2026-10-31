import os
import shlex
import asyncio
import re
import subprocess
import httpx
import contextlib
import signal
import locale
import json 
import shutil
import time
import hashlib
import math 

from datetime import datetime

from module.data_manager import (
    RecorderManager, loadAccount, saveAccount, loadCookies, saveCookies, toBool,
    loadChannels, saveChannels, loadConfig, saveConfig, uniqueFilename, moveDirectory,
    notifyEvent, checkDiskSpaceLow, last_notified_state, base_directory, CONFIG_PATH, CHANNELS_PATH,
    COOKIE_PATH, LOGIN_PATH, getFFmpeg, getFFprobe, getStreamlink
)


# RecorderManager 인스턴스 생성
recorder_manager = RecorderManager()

# 후처리 백그라운드 태스크 참조 보관용
_postprocess_background_tasks: set[asyncio.Task] = set()

def trackPostprocessTask(task: asyncio.Task) -> asyncio.Task:
    _postprocess_background_tasks.add(task)
    task.add_done_callback(_postprocess_background_tasks.discard)
    return task

# 기본 언어인코딩 설정
default_encoding = locale.getpreferredencoding()

_httpxClient = None
_httpxLock = None 

# 분할녹화시 꼬리파일 방지 쿨다운
TAIL_GUARD_COOLDOWN_SEC = 60 

CHZZK_STREAM_QUALITY_RE = re.compile(
    r"^(best|worst|(?:144|240|360|480|720|1080|1440|2160)p)$",
    re.IGNORECASE
)

# 파일명에 들어가는 텍스트 길이 제한
FILENAME_PART_LIMIT = 60
FILENAME_LIVE_TITLE_LIMIT = 45
FILENAME_CHANNEL_NAME_LIMIT = 40

def sanitizeChzzkStreamQuality(value, fallback: str = "best") -> str:
    q = str(value or "").strip().lower()

    if CHZZK_STREAM_QUALITY_RE.fullmatch(q):
        return q

    m = re.match(r"^(144|240|360|480|720|1080|1440|2160)(?:p)?(?:\d{2,3})?$", q)
    if m:
        return f"{m.group(1)}p"

    return fallback


def sanitizeFilenamePart(value, fallback="recording", limit=FILENAME_PART_LIMIT):
    text = str(value or fallback).replace("\r", " ").replace("\n", " ")
    text = re.sub(r'[\\/*?:"<>|+]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return (text or fallback)[:limit]


def sanitizeGeneratedFilename(filename, fallback_ext=".ts"):
    text = str(filename or "").replace("\r", " ").replace("\n", " ")
    text = text.replace("/", "_").replace("\\", "_")
    base, ext = os.path.splitext(text)
    base = re.sub(r'[:*?"<>|]+', "_", base)
    base = re.sub(r"\s+", " ", base).strip(" ._")

    if not ext:
        ext = fallback_ext or ""
    if ext and not ext.startswith("."):
        ext = "." + ext

    return (base or "recording") + ext


def makeChzzkQualityLabel(video_height) -> str:
    try:
        h = int(float(video_height))
    except Exception:
        return ""

    if h <= 0:
        return ""

    return f"{h}p"


def makeChzzkFrameLabel(video_frame_rate) -> str:
    try:
        fps = int(round(float(video_frame_rate)))
    except Exception:
        return "알 수 없는 프레임 레이트"

    if fps <= 0:
        return "알 수 없는 프레임 레이트"

    return str(fps)



# 재탐색 지터 & PROBE 동시성 상한 전역
PROBE_MAX_CONCURRENCY = int(os.environ.get("PROBE_MAX_CONCURRENCY", "4"))   # 기본 4
_probe_sem = asyncio.Semaphore(max(1, PROBE_MAX_CONCURRENCY))               # HLS probe 동시 상한

START_MONO = time.monotonic()                                               # 절대시간 앵커(페이즈 기준)
JITTER_RATIO = float(os.environ.get("RECHECK_JITTER_RATIO", "0.15"))        # 지터 비율(기본 15%)
_JITTER_PHASE = {}                                                          # {channel_id: phase(sec)} 채널별 초기 위상 캐시


# 헤더에 세션 정보를 추가하는 함수
def getChzzkCookieData(cookies: dict | None) -> dict:
    root = cookies if isinstance(cookies, dict) else {}
    chzzk = root.get("chzzk") if isinstance(root.get("chzzk"), dict) else {}

    return {
        "NID_AUT": str(chzzk.get("NID_AUT") or "").strip(),
        "NID_SES": str(chzzk.get("NID_SES") or "").strip(),
    }


def buildChzzkCookieHeader(cookies: dict | None) -> str:
    chzzk = getChzzkCookieData(cookies)

    parts = []
    if chzzk.get("NID_SES"):
        parts.append(f"NID_SES={chzzk['NID_SES']}")
    if chzzk.get("NID_AUT"):
        parts.append(f"NID_AUT={chzzk['NID_AUT']}")

    return "; ".join(parts)


# 헤더에 세션 정보를 추가하는 함수
def getAuthtoHeaders(cookies):
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' if os.name == 'nt' else 'Mozilla/5.0 (X11; Linux x86_64)'
    headers = {
        'User-Agent': ua,
        'Accept': 'application/json, text/plain, */*',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }

    # chzzk 쿠키만 꺼내 Cookie 헤더로 구성
    cookie_header = buildChzzkCookieHeader(cookies)
    if cookie_header:
        headers['Cookie'] = cookie_header

    return headers


async def getHttpxClient() -> httpx.AsyncClient:
    global _httpxClient, _httpxLock
    if _httpxLock is None:
        _httpxLock = asyncio.Lock()

    async with _httpxLock:
        if _httpxClient is None:
            # httpx 버전 호환: Limits 파라미터 이름
            try:
                limits = httpx.Limits(max_keepalive_connections=30, max_connections=30)
            except TypeError:
                limits = httpx.Limits(max_keepalive=30, max_connections=30)

            # Timeout: default + 4개 모두 명시 (0.27에서도 안전)
            timeout = httpx.Timeout(10.0, connect=3.0, read=8.0, write=8.0, pool=3.0)

            _httpxClient = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                headers={"User-Agent": "recordWEB/1.0"},
                follow_redirects=True,
            )
        return _httpxClient


async def closeHttpxClient():
    global _httpxClient
    if _httpxClient is not None:
        await _httpxClient.aclose()
        _httpxClient = None


async def getJsonWithRetry(url: str, *, headers: dict | None = None,
                           retries: int = 3,
                           connect_timeout: float = 3.0,
                           read_timeout: float = 8.0) -> tuple[int | None, dict | None]:

    client = await getHttpxClient()
    backoff = 0.7
    for attempt in range(retries + 1):
        try:
            req_timeout = httpx.Timeout(10.0, connect=connect_timeout, read=read_timeout, write=8.0, pool=3.0)

            resp = await client.get(url, headers=headers, timeout=req_timeout)
            if resp.status_code == 200:
                try:
                    return 200, resp.json()
                except Exception:
                    return 200, None
            if resp.status_code in (404, 410):
                return resp.status_code, None
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(backoff); backoff *= 1.8
                continue
            return resp.status_code, None
        except httpx.RequestError:
            if attempt < retries:
                await asyncio.sleep(backoff); backoff *= 1.8
                continue
            return None, None


async def checkUrlOkWithRetry(url: str, *, headers: dict | None = None,
                              retries: int = 2) -> bool:

    client = await getHttpxClient()
    backoff = 0.5
    for attempt in range(retries + 1):
        try:
            req_timeout = httpx.Timeout(6.0, connect=2.0, read=3.5, write=3.0, pool=2.0)

            r = await client.get(url, headers=headers, timeout=req_timeout)
            if r.status_code == 200:
                return True
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(backoff); backoff *= 1.8
                continue
            return False
        except httpx.RequestError:
            if attempt < retries:
                await asyncio.sleep(backoff); backoff *= 1.8
                continue
            return False


# 라이브 메타데이터를 가져오는 함수
async def getLiveMetadata(channel, cookies):
    try:
        headers = getAuthtoHeaders(cookies)
        url = f"https://api.chzzk.naver.com/service/v3/channels/{channel['id']}/live-detail?_={int(time.time() * 1000)}"

        # 재시도/백오프 + 커넥션 재사용
        status, body = await getJsonWithRetry(url, headers=headers)

        if status is None:
            # 네트워크 레벨 최종 실패
            print(f"요청 오류 발생: {url}")
            return {"thumbnail_url": "/static/img/default_thumbnail.png", "name": channel.get('name') or ""}

        if status in (404, 410):
            print(f"[INFO] {channel['name']} 현재 방송이 종료된 상태(CLOSE) 이므로 세부정보가 없습니다.")
            return {
                "status": "CLOSE",
                "thumbnail_url": "/static/img/liveclosed_thumbnail.png",
                "name": channel.get("name") or "",
                "live_title": "방송 제목 없음",
                "category": "카테고리 없음",
            }

        if status != 200 or not isinstance(body, dict):
            print(f"[WARN] {channel['name']} 메타데이터 조회 실패 http={status}")
            return None

        metadata_content = (body or {}).get("content")
        if not metadata_content:
            print(f"{channel['name']} 채널의 메타데이터가 없습니다 (content가 None).")
            return None

        # 방송 기본 정보
        live_title = metadata_content.get("liveTitle", "방송 제목 없음")
        category   = metadata_content.get("liveCategoryValue", "카테고리 없음")
        metadata_content["watchPartyNo"]  = metadata_content.get("watchPartyNo")
        metadata_content["watchPartyTag"] = metadata_content.get("watchPartyTag") or ""


        # 썸네일
        live_image_url = metadata_content.get("liveImageUrl")
        if live_image_url:
            thumbnail_url = live_image_url.format(type="360")
            print(f"Generated thumbnail URL: {thumbnail_url}")
        else:
            thumbnail_url = "/static/img/default_thumbnail.png"
            print("Using default thumbnail as no liveImageUrl provided.")

        # 성인 채널이면 쿠키 검증
        if metadata_content.get("adult", False) and thumbnail_url.startswith(("http://","https://")):
            ok = await checkUrlOkWithRetry(thumbnail_url, headers=headers, retries=2)
            if not ok:
                print("쿠키값이 유효하지 않습니다. 기본 썸네일을 사용합니다.")
                thumbnail_url = "/static/img/default_thumbnail.png"

        # 방송 상태에 따라 닫힘 썸네일
        if metadata_content.get("status") != "OPEN":
            thumbnail_url = "/static/img/liveclosed_thumbnail.png"
            print(f"Final thumbnail URL for {channel['name']}: {thumbnail_url}")
        elif not thumbnail_url:
            thumbnail_url = "/static/img/default_thumbnail.png"
            print("Thumbnail URL is empty, using default thumbnail.")

        # 이름 보존
        if channel.get('name'):
            metadata_content["name"] = channel["name"]
        else:
            print(f"채널 이름이 None이거나 빈 값입니다: {channel['id']}")

        metadata_content["thumbnail_url"] = thumbnail_url
        metadata_content["liveTitle"]    = live_title
        metadata_content["category"]      = category

        playback_json = {}
        try:
            playback_json = json.loads(metadata_content.get("livePlaybackJson") or "{}")
        except Exception:
            playback_json = {}

        playback_meta = playback_json.get("meta") or {}
        playback_live = playback_json.get("live") or {}

        metadata_content["playback_live_id"] = str(playback_meta.get("liveId") or "")
        metadata_content["playback_open"] = playback_live.get("open")
        metadata_content["playback_start"] = playback_live.get("start")

        def _parse_chzzk_dt(value):
            if not value:
                return None
            text = str(value).strip().replace("T", " ")
            text = text.split("+", 1)[0].replace("Z", "")
            text = text.split(".", 1)[0]
            try:
                return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None

        broadcast_dt = (
            _parse_chzzk_dt(metadata_content.get("playback_open"))
            or _parse_chzzk_dt(metadata_content.get("playback_start"))
            or _parse_chzzk_dt(metadata_content.get("openDate"))
            or datetime.now()
        )

        metadata_content["start_time"] = broadcast_dt.strftime("%Y-%m-%d")
        metadata_content["broadcast_time"] = broadcast_dt.strftime("%y%m%d_%H%M%S")

        # 품질 선택
        user_selected = (channel.get('quality') or 'best').strip().lower()
        frame_rate = "알 수 없는 프레임 레이트"
        record_quality_for_name = "알 수 없는 품질"
        resolved_quality_for_cmd = user_selected

        if metadata_content.get("livePlaybackJson") is None:
            metadata_content["status"] = metadata_content.get("status", "CLOSE")
            metadata_content["record_quality"] = "알 수 없는 품질"
            metadata_content["frame_rate"] = "알 수 없는 프레임 레이트"

            # 방송중인데 playback 정보만 늦게 내려오는 경우에만 경고로 판단
            if metadata_content.get("status") == "OPEN":
                print(f"[WARNING] {channel['name']} livePlaybackJson 없음. HLS 프로브로 재확인합니다.")
                metadata_content["_playback_missing"] = True

            return metadata_content

        playback_json = playback_json or json.loads(metadata_content["livePlaybackJson"])
        encoding_tracks = playback_json.get("media", [])
        variants_by_label = {}

        for track in encoding_tracks:
            for enc in track.get("encodingTrack", []):
                try:
                    if enc.get("audioOnly"):
                        continue

                    h = int(float(enc.get("videoHeight")))
                    label = makeChzzkQualityLabel(h)
                    if not label:
                        continue

                    fps = makeChzzkFrameLabel(enc.get("videoFrameRate"))
                    raw_track_id = str(enc.get("encodingTrackId") or "").strip().lower()

                    try:
                        bitrate = int(enc.get("videoBitRate") or 0)
                    except Exception:
                        bitrate = 0

                    item = {
                        "height": h,
                        "label": label,
                        "fps": fps,
                        "track_id": raw_track_id,
                        "bitrate": bitrate,
                    }

                    # HLS/LLHLS에 같은 품질이 중복으로 들어올 수 있으므로 품질명 기준으로 중복 제거합니다.
                    old = variants_by_label.get(label)
                    if old is None:
                        variants_by_label[label] = item
                    else:
                        old_fps = int(old["fps"]) if str(old.get("fps", "")).isdigit() else 0
                        new_fps = int(fps) if str(fps).isdigit() else 0

                        if (h, new_fps, bitrate) > (old["height"], old_fps, old.get("bitrate", 0)):
                            variants_by_label[label] = item

                except Exception:
                    continue

        variants = sorted(
            variants_by_label.values(),
            key=lambda x: (x["height"], int(x["fps"]) if str(x["fps"]).isdigit() else 0)
        )

        if not variants:
            metadata_content["record_quality"] = "알 수 없는 품질"
            metadata_content["frame_rate"] = frame_rate
            metadata_content["available_qualities"] = []
            metadata_content["resolved_quality"] = sanitizeChzzkStreamQuality(user_selected, "best")
            return metadata_content

        available_labels = [
            v["label"]
            for v in sorted(variants, key=lambda x: x["height"], reverse=True)
        ]

        user_selected_raw = str(channel.get("quality") or "best").strip().lower()
        user_selected = sanitizeChzzkStreamQuality(user_selected_raw, "best")

        def _fps_num(v):
            return int(v["fps"]) if str(v.get("fps", "")).isdigit() else 0

        def _choose_best():
            return max(variants, key=lambda x: (x["height"], _fps_num(x), x.get("bitrate", 0)))

        def _choose_worst():
            return min(variants, key=lambda x: (x["height"], _fps_num(x), x.get("bitrate", 0)))

        def _find_by_label(label: str):
            label = str(label or "").strip().lower()
            for v in variants:
                if v["label"].lower() == label:
                    return v
            return None

        def _find_by_track_id(track_id: str):
            track_id = str(track_id or "").strip().lower()
            if not track_id:
                return None

            for v in variants:
                if v.get("track_id") == track_id:
                    return v
            return None

        chosen = None
        resolved_quality_for_cmd = "best"
        chosen = _find_by_track_id(user_selected_raw)

        if chosen is not None:
            resolved_quality_for_cmd = chosen["label"]

        elif user_selected == "best":
            chosen = _choose_best()
            resolved_quality_for_cmd = "best"

        elif user_selected == "worst":
            chosen = _choose_worst()
            resolved_quality_for_cmd = "worst"

        else:
            chosen = _find_by_label(user_selected)

            if chosen is None:
                m = re.match(r"(\d+)", user_selected)
                target = int(m.group(1)) if m else None

                if target is not None:
                    candidates = [v for v in variants if v["height"] <= target]
                    if candidates:
                        chosen = max(candidates, key=lambda x: (x["height"], _fps_num(x), x.get("bitrate", 0)))

            if chosen is not None:
                resolved_quality_for_cmd = chosen["label"]
            else:
                chosen = _choose_best()
                resolved_quality_for_cmd = "best"

        record_quality_for_name = chosen["label"]
        frame_rate = chosen["fps"]

        if user_selected_raw and user_selected_raw != resolved_quality_for_cmd and not CHZZK_STREAM_QUALITY_RE.fullmatch(user_selected_raw):
            print(
                f"[WARN] {channel.get('name')} 비표준 치지직 품질값 보정: "
                f"{user_selected_raw!r} -> {resolved_quality_for_cmd!r}"
            )

        metadata_content["record_quality"]      = record_quality_for_name
        metadata_content["frame_rate"]          = frame_rate
        metadata_content["available_qualities"] = available_labels
        metadata_content["resolved_quality"]    = sanitizeChzzkStreamQuality(resolved_quality_for_cmd, "best")
        metadata_content["is_live"]             = (metadata_content.get("status") == "OPEN")

        return metadata_content

    except httpx.HTTPStatusError as e:
        print(f"HTTP 오류 발생: {e.response.status_code}")
    except httpx.RequestError as e:
        print(f"요청 오류 발생: {getattr(e, 'request', None) and e.request.url}")

    return {"thumbnail_url": "/static/img/default_thumbnail.png", "name": channel.get('name') or ""}


def isUnresolvedQualityMetadata(metadata: dict | None) -> bool:
    if not metadata:
        return True

    if metadata.get("status") != "OPEN":
        return False

    record_quality = str(metadata.get("record_quality") or "").strip()
    frame_rate = str(metadata.get("frame_rate") or "").strip()

    if not metadata.get("livePlaybackJson"):
        return True

    if not record_quality or "알 수 없는" in record_quality or record_quality.startswith("Unknown"):
        return True

    if not frame_rate or "알 수 없는" in frame_rate or frame_rate.startswith("Unknown"):
        return True

    return False


def normalizeQualityNameParts(channel: dict, metadata: dict | None) -> tuple[str, str]:
    metadata = metadata or {}

    raw_quality = str(metadata.get("record_quality") or "").strip()
    raw_frame = str(metadata.get("frame_rate") or "").strip()

    selected_quality = str(
        metadata.get("resolved_quality")
        or channel.get("quality")
        or "best"
    ).strip()

    def is_unknown(value: str) -> bool:
        return (
            not value
            or "알 수 없는" in value
            or value.startswith("Unknown")
        )

    if is_unknown(raw_quality):
        if selected_quality and selected_quality.lower() != "best":
            m = re.match(r"(\d+)", selected_quality)
            raw_quality = f"{m.group(1)}p" if m else selected_quality
        else:
            raw_quality = "best"

    if is_unknown(raw_frame):
        raw_frame = ""

    return raw_quality, raw_frame


def isUnstableChzzkIdentityMetadata(metadata: dict | None) -> bool:
    if not isinstance(metadata, dict):
        return True

    if metadata.get("status") != "OPEN":
        return True

    if metadata.get("_playback_missing"):
        return True

    if not metadata.get("livePlaybackJson"):
        return True

    live_title = str(metadata.get("liveTitle") or "").strip()
    if not live_title or live_title in ("방송 제목 없음", "녹화"):
        return True

    start_time = str(metadata.get("start_time") or "").strip()
    broadcast_time = str(metadata.get("broadcast_time") or "").strip()

    if not start_time or start_time == "UnknownTime":
        return True

    if not broadcast_time:
        return True

    return False


def applySafeChzzkFilenameMetadata(channel: dict, metadata: dict | None, recording_time: str) -> dict:
    if not isinstance(metadata, dict):
        metadata = {}

    if isUnstableChzzkIdentityMetadata(metadata):
        now_dt = datetime.now()
        metadata["status"] = "OPEN"
        metadata["liveTitle"] = channel.get("name") or "녹화"
        metadata["start_time"] = now_dt.strftime("%Y-%m-%d")
        metadata["broadcast_time"] = recording_time
    else:
        metadata["liveTitle"] = str(metadata.get("liveTitle") or channel.get("name") or "녹화").strip()
        metadata["start_time"] = str(metadata.get("start_time") or datetime.now().strftime("%Y-%m-%d")).strip()
        metadata["broadcast_time"] = str(metadata.get("broadcast_time") or recording_time).strip()

    record_quality, frame_rate = normalizeQualityNameParts(channel, metadata)
    metadata["record_quality"] = record_quality
    metadata["frame_rate"] = frame_rate

    return metadata


async def waitStableChzzkMetadata(channel: dict, cookies: dict,
                                  first_metadata: dict | None = None,
                                  retries: int = 4,
                                  delay: float = 1.5) -> dict | None:
    metadata = first_metadata
    last_signature = None

    for attempt in range(retries + 1):
        if metadata and metadata.get("status") != "OPEN":
            return metadata

        if metadata:
            signature = (
                str(metadata.get("liveId") or ""),
                str(metadata.get("playback_live_id") or ""),
                str(metadata.get("liveTitle") or ""),
                str(metadata.get("broadcast_time") or ""),
            )

            quality_ready = not isUnresolvedQualityMetadata(metadata)
            identity_ready = not isUnstableChzzkIdentityMetadata(metadata)

            if quality_ready and identity_ready and signature == last_signature:
                return metadata

            last_signature = signature

        if attempt >= retries:
            break

        print(f"[META][WAIT] {channel.get('name')} 라이브 메타데이터 안정화 대기 중... ({attempt + 1}/{retries})")
        await asyncio.sleep(delay)
        metadata = await getLiveMetadata(channel, cookies)

    if metadata:
        record_quality, frame_rate = normalizeQualityNameParts(channel, metadata)
        metadata["record_quality"] = record_quality
        metadata["frame_rate"] = frame_rate

    return metadata


def buildCommand(channel, metadata, recording_time, cookies, filenamePattern, plugin_type, timemachine_time_shift):
    if channel is None:
        print(f"[ERROR] 전달된 채널이 None입니다. 명령을 생성할 수 없습니다.")
        return []

    # base path 확정
    output_dir_abs_path = os.path.join(base_directory, channel.get('output_dir', './output'))

    metadata = applySafeChzzkFilenameMetadata(channel, metadata, recording_time)

    # 1) 플러그인/시프트 정규화
    normalized_plugin = (plugin_type or "basic").lower()
    if normalized_plugin not in ("basic", "timemachine_plus"):
        normalized_plugin = "basic"

    try:
        _shift = int(timemachine_time_shift or 0)
    except Exception:
        _shift = 0

    normalized_shift = max(0, min(10, _shift)) if normalized_plugin == "basic" else max(0, min(3600, _shift))

    # 2) 공통 값 계산
    stream_url = f"https://chzzk.naver.com/live/{channel['id']}"
    ffmpeg_path = getFFmpeg()
    streamlink_path = getStreamlink()

    raw_resolved_quality = (metadata or {}).get("resolved_quality") or channel.get('quality', 'best')
    resolved_quality = sanitizeChzzkStreamQuality(raw_resolved_quality, "")

    if not resolved_quality:
        resolved_quality = sanitizeChzzkStreamQuality(channel.get('quality', 'best'), "best")

    if str(raw_resolved_quality or "").strip().lower() != resolved_quality:
        print(
            f"[WARN] Streamlink에 넘길 수 없는 치지직 품질값 보정: "
            f"{raw_resolved_quality!r} -> {resolved_quality!r}"
        )

    file_extension = channel.get('extension', '.ts')

    # 3) 파일명 생성
    live_title = metadata.get("liveTitle") or "녹화"
    safe_live_title = sanitizeFilenamePart(live_title, "녹화", FILENAME_LIVE_TITLE_LIMIT)
    safe_channel_name = sanitizeFilenamePart(
        channel.get("name") or channel.get("id") or "chzzk", "chzzk", FILENAME_CHANNEL_NAME_LIMIT)

    start_time = metadata.get('start_time', 'UnknownTime')
    broadcast_time = metadata.get("broadcast_time") or recording_time

    record_quality_for_name, frame_rate_for_name = normalizeQualityNameParts(channel, metadata)

    filename = filenamePattern.format(
        recording_time=recording_time,
        start_time=start_time,
        broadcast_time=broadcast_time,
        safe_live_title=safe_live_title,
        channel_name=safe_channel_name,
        record_quality=sanitizeFilenamePart(record_quality_for_name, "best", 20),
        frame_rate=sanitizeFilenamePart(frame_rate_for_name, "", 10),
        file_extension=file_extension
    )

    filename = sanitizeGeneratedFilename(filename, file_extension)

    checkDiskSpaceLow(
        output_dir_abs_path,
        channel_id=channel.get("id"),
        channel_name=channel.get("name") or channel.get("id")
    )

    unique_filename = uniqueFilename(output_dir_abs_path, filename, add_suffix=True)
    output_path = os.path.join(output_dir_abs_path, unique_filename)
    channel["output_path"] = output_path


    # 4) Streamlink 커맨드 생성
    cmd_list = [
        streamlink_path,
        "--ffmpeg-copyts",
        "--ffmpeg-ffmpeg", ffmpeg_path,
        "--progress=force",
        "--stream-segment-timeout", "15",
        "--stream-segment-attempts", "12",
        "--hls-segment-queue-threshold", "0",
        "--stream-timeout", "60",
    ]
    # 공통 옵션
    cmd_list.extend(buildCommonPluginArgs(channel, cookies, normalized_plugin=normalized_plugin))

    if normalized_plugin in ("timemachine_plus",):
        cmd_list.append("--hls-live-restart")
        if normalized_shift > 0:
            cmd_list.extend(["--hls-start-offset", str(normalized_shift)])

    # URL/품질/출력
    cmd_list.extend([stream_url, resolved_quality, "-o", output_path])

    print(f"[DEBUG] plugin_type={normalized_plugin}, hls-live-restart=ON, hls-start-offset={normalized_shift}")
    return cmd_list


# 공용 헬퍼
def buildStreamlinkEnv(plugin_type, timemachine_time_shift):
    normalized_plugin = (plugin_type or "basic").lower()
    if normalized_plugin not in ("basic", "timemachine_plus"):
        normalized_plugin = "basic"
    try:
        _shift = int(timemachine_time_shift or 0)
    except Exception:
        _shift = 0
    normalized_shift = max(0, min(10, _shift)) if normalized_plugin == "basic" else max(0, min(3600, _shift))

    env = os.environ.copy()
    env["RWEB_TM_SHIFT"] = str(normalized_shift)
    return env


# 실제 하위 프로세스가 살아 있는지 확인
def procAlive(channel_id: str) -> bool:
    try:
        p = recorder_manager.get_tasks_process(channel_id)
        return (p is not None) and (p.returncode is None)
    except Exception:
        return False


# stdout을 읽는 함수
async def read_stdout(proc, channel_id, stream_ended_flag):
    try:
        while True:
            stdout_line = await proc.stdout.readline()
            if not stdout_line:
                break

            # 시스템의 기본 인코딩으로 디코딩
            decoded_stdout = stdout_line.decode(default_encoding, errors='replace').strip()

            # 출력 내용을 콘솔에 출력
            print(decoded_stdout)

            # "Stream ended" 메시지를 감지하면 플래그 설정 및 루프 종료
            if "Stream ended" in decoded_stdout:
                print(f"[DEBUG] {channel_id} 채널에서 'Stream ended' 메시지가 감지되었습니다.")
                stream_ended_flag.set()  # Stream ended 플래그 설정
                break

    except Exception as e:
        print(f"[ERROR] read_stdout 중 오류 발생: {str(e)}")


# stderr를 읽는 함수
async def read_stderr(proc, channel_id, transient_end_flag: asyncio.Event | None = None):
    try:
        while True:
            stderr_chunk = await proc.stderr.read(1024)
            if not stderr_chunk:
                break

            try:
                decoded_stderr = stderr_chunk.decode(default_encoding, errors='replace')
            except UnicodeDecodeError:
                decoded_stderr = stderr_chunk.decode('utf-8', errors='replace')

            # 콘솔로 그대로 출력
            print(decoded_stderr, end='')

            # ★ 패턴 매칭: 404/403 세그먼트 실패, 세그먼트 중단 경고 등 → 일시 장애로 간주
            if transient_end_flag is not None:
                s = decoded_stderr
                if (
                    ("Failed to fetch segment" in s and ("404" in s or "403" in s or "416" in s))
                    or ("No new segments in playlist" in s)     
                    or ("Read timeout" in s)                         
                ):
                    try:
                        transient_end_flag.set()
                    except Exception:
                        pass

    except Exception as e:
        print(f"[ERROR] read_stderr 중 오류 발생: {str(e)}")


def buildChannelUrl(channel):
    # 녹화 명령과 동일한 URL 규칙 사용
    return f"https://chzzk.naver.com/live/{channel['id']}"


def buildCommonPluginArgs(channel, cookies=None, *, normalized_plugin: str = "basic"):
    args = []

    # chzzk 쿠키만 꺼내 Streamlink Cookie 헤더로 구성
    cookie_value = buildChzzkCookieHeader(cookies)
    if cookie_value:
        args.extend(["--http-header", f"Cookie={cookie_value}"])

    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' if os.name == 'nt' else 'Mozilla/5.0 (X11; Linux x86_64)'
    args.extend(["--http-header", "Referer=https://chzzk.naver.com/"])
    args.extend(["--http-header", "Origin=https://chzzk.naver.com"])
    args.extend(["--http-header", f"User-Agent={ua}"])

    # plugin-dir
    plugin_dir = os.path.join(
        base_directory, "dependent", "plugin",
        "timemachine_plus" if normalized_plugin == "timemachine_plus" else "basic"
    )
    if os.path.isdir(plugin_dir):
        args.extend(["--plugin-dir", plugin_dir])

    return args



# 세그먼트 중지시 일시적인 타임아웃인지 확인 함수
async def probeStream(channel, cookies=None, timeout_sec=10):
    streamlink_path = getStreamlink()
    url = buildChannelUrl(channel)
    quality = "best"

    # plugin만 정규화 
    cfg = loadConfig() or {}
    normalized_plugin = (cfg.get("plugin_type") or "basic").lower()
    if normalized_plugin not in ("basic", "timemachine_plus"):
        normalized_plugin = "basic"

    # 옵션(플러그인/쿠키) → URL → 기타 옵션 순서
    cmd = [streamlink_path]
    cmd.extend(buildCommonPluginArgs(channel, cookies, normalized_plugin=normalized_plugin))
    cmd.extend([
        url, quality,
        "--stdout",
        "--stream-segment-timeout", "3",
        "--stream-segment-attempts", "1",
        "--stream-timeout", str(timeout_sec),
        "--hls-playlist-reload-attempts", "1",
    ])

    # probeStream 하이라이트 
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )

    try:
        # 첫 바이트만 빨리 확인
        try:
            first = await asyncio.wait_for(proc.stdout.read(1), timeout=timeout_sec)
        except asyncio.TimeoutError:
            first = b""

        return bool(first)

    finally:
        # 항상 깔끔하게 종료 + 파이프 드레인
        with contextlib.suppress(Exception):
            if proc.returncode is None:
                proc.kill()  
            if proc.stdout or proc.stderr:
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=1.5)
                except asyncio.TimeoutError:
                    # 종료 대기
                    await proc.wait()
            else:
                await proc.wait()



# PROBE 세마포어 논블로킹 래퍼: 바쁘면 이번 라운드 스킵
async def probeStreamBounded(channel, cookies=None, timeout_sec=10):
    channel["_last_probe_reason"] = None
    try:
        await asyncio.wait_for(_probe_sem.acquire(), timeout=0.001)  # 대기 0
    except asyncio.TimeoutError:
        channel["_last_probe_reason"] = "skip"
        print(f"[INFO] PROBE 슬롯 바쁨 → 이번 라운드 스킵: {channel.get('name')}")
        return False
    try:
        ok = await asyncio.wait_for(
            probeStream(channel, cookies, timeout_sec=timeout_sec),
            timeout=timeout_sec + 2
        )
        channel["_last_probe_reason"] = "ok" if ok else "fail"
        return ok
    except asyncio.TimeoutError:
        channel["_last_probe_reason"] = "timeout"
        print(f"[WARN] HLS probe timeout: {channel.get('name')} ({timeout_sec}s)")
        return False
    finally:
        _probe_sem.release()



async def gracefulTerminate(proc: asyncio.subprocess.Process, timeout: float = 5.0):
    if not proc or proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            # 1) CTRL_BREAK → 2) terminate → 3) kill 순
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except Exception:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    proc.kill()
                    await proc.wait()
        else:
            # POSIX: TERM → KILL
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                await proc.wait()
    finally:
        # 파이프 정리 짧게 드레인 시도
        with contextlib.suppress(Exception):
            if (getattr(proc, "stdout", None) is not None) or (getattr(proc, "stderr", None) is not None):
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=1.5)
                except asyncio.TimeoutError:
                    pass


async def _cancel_task_safely(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _file_ready(path: str) -> bool:
    try:
        # 존재 + stat 가능하면 일단 OK
        if not os.path.exists(path):
            return False
        os.stat(path)
        # Windows에서 공유락에 막히는 케이스 최소화: r 모드 열기 시도
        with open(path, "rb"):
            return True
    except Exception:
        return False


async def quiesce_last_segment(session_dir: str, base_noext: str, ext: str, attempts: int = 10, interval: float = 0.3):
    last_path = None
    last_size = -1
    for _ in range(attempts):
        segs = listNumericSegments(session_dir, base_noext, ext)
        if not segs:
            await asyncio.sleep(interval); continue
        _, last_path = segs[-1]
        try:
            size = os.path.getsize(last_path)
        except Exception:
            size = -1
        if size == last_size and _file_ready(last_path):
            return
        last_size = size
        await asyncio.sleep(interval)
    # 시도 끝, 그냥 진행


def resolveFFprobe(ffmpeg_path: str) -> str:
    if not ffmpeg_path:
        # 마지막 안전장치: PATH
        return shutil.which("ffprobe") or "ffprobe"

    d = os.path.dirname(ffmpeg_path)
    cand = []
    if os.name == "nt":
        cand = [os.path.join(d, "ffprobe.exe"), os.path.join(d, "ffprobe")]
    else:
        cand = [os.path.join(d, "ffprobe"), os.path.join(d, "ffprobe.exe")]
    for c in cand:
        if os.path.exists(c):
            return c

    # PATH에서도 시도
    return shutil.which("ffprobe") or "ffprobe"


def get_media_duration(ffprobe_path: str, path: str) -> float:
    if not path or not os.path.exists(path):
        return 0.0
    try:
        out = subprocess.check_output(
            [ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            creationflags=0, shell=False, text=True
        ).strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def probeVcodec(ffprobe_path: str, path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.check_output(
            [
                ffprobe_path, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nw=1:nk=1",
                path
            ],
            stderr=subprocess.STDOUT,
            timeout=3
        )
        return (out.decode("utf-8", "ignore").strip() or "").lower()
    except Exception:
        return ""


def probeVideoHeight(ffprobe_path: str, path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    try:
        out = subprocess.check_output(
            [
                ffprobe_path, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=height",
                "-of", "default=nw=1:nk=1",
                path
            ],
            stderr=subprocess.STDOUT,
            timeout=3
        )
        s = (out.decode("utf-8", "ignore").strip() or "")
        return int(s) if s.isdigit() else 0
    except Exception:
        return 0


def probeVideoSize(ffprobe_path: str, path: str) -> tuple[int, int]:
    if not path or not os.path.exists(path):
        return (0, 0)
    try:
        out = subprocess.check_output(
            [
                ffprobe_path, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x",
                path
            ],
            stderr=subprocess.STDOUT,
            timeout=3
        )
        s = (out.decode("utf-8", "ignore").strip() or "")
        if "x" in s:
            w, h = s.split("x", 1)
            if w.isdigit() and h.isdigit():
                return (int(w), int(h))
    except Exception:
        pass
    return (0, 0)


def _clamp_gpu_index(v) -> int:
    try:
        return 1 if int(v) == 1 else 0
    except Exception:
        return 0


def _clamp_gpu_count(v) -> int:
    try:
        return 2 if int(v) == 2 else 1
    except Exception:
        return 1


def buildPostCfgForChannel(channel: dict) -> dict:
    cfg = loadConfig() or {}
    gpu_count = _clamp_gpu_count(cfg.get("gpuCount", 1))

    ch_gpu = 0 if gpu_count == 1 else _clamp_gpu_index(channel.get("gpu_index", 0))
    suffix = "" if ch_gpu == 0 else "_gpu1"

    used_keys = {}

    def pick(key: str):
        k = (key + suffix) if (suffix and (key + suffix) in cfg) else key
        used_keys[key] = k
        return cfg.get(k)

    post_cfg = {
        "autoPostProcessing": bool(cfg.get("autoPostProcessing", True)),
        "deleteAfterPostProcessing": bool(cfg.get("deleteAfterPostProcessing", True)),
        "removeFixedPrefix": bool(cfg.get("removeFixedPrefix", True)),
        "moveAfterProcessingEnabled": bool(cfg.get("moveAfterProcessingEnabled", False)),
        "moveAfterProcessing": cfg.get("moveAfterProcessing", "") or "",
        "postNewWindow": bool(cfg.get("postNewWindow", False)),
        "splitPostProcessing": bool(cfg.get("splitPostProcessing", True)),

        # 키가 없으면 기본값으로 진행하고 최소한 로그로 추적 가능
        "stream_copy": bool(cfg.get("stream_copy", True)),
        "video_codec": pick("video_codec"),
        "preset": pick("preset"),
        "postprocess_resolution": pick("postprocess_resolution") or "source",
        "use_bitrate_mode": bool(pick("use_bitrate_mode")),
        "video_quality": pick("video_quality"),
        "video_bitrate": pick("video_bitrate"),
        "vbv_maxrate": pick("vbv_maxrate") or "",
        "vbv_bufsize": pick("vbv_bufsize") or "",
        "audio_codec": pick("audio_codec"),
        "audio_bitrate": pick("audio_bitrate"),
        "extra_ffmpeg_options": pick("extra_ffmpeg_options") or "",

        "gpu_index": ch_gpu if gpu_count == 2 else None,

        # 디버그용 메타
        "_gpu_count": gpu_count,
        "_profile": ("gpu0" if ch_gpu == 0 else "gpu1"),
        "_used_keys": used_keys,
    }

    return post_cfg


# 후처리 작업 함수
async def handlePostProcessing(input_path: str, channel_id: str, channel_name: str, post_cfg: dict | None):
    job_id = None

    try:
        cfg = post_cfg or {}

        used = cfg.get("_used_keys", {})
        prof = cfg.get("_profile", "?")
        gidx = cfg.get("gpu_index", None)

        job_id = recorder_manager.postprocess_register_job(
            channel_id=channel_id,
            channel_name=channel_name,
            input_path=input_path,
            post_cfg=cfg,
        )

        print(
            f"[POST][JOB-START] job={job_id} ch={channel_name} cid={channel_id} "
            f"profile={prof} gpu_index={gidx} src={os.path.basename(input_path or '')}"
        )

        # 1) 설정 검증
        errors = []

        stream_copy = cfg.get("stream_copy", None)
        if stream_copy is None:
            errors.append("stream_copy 설정 키가 누락됨")

        # stream_copy=False(인코딩) 인데 필수 설정이 비면 진행중지
        if stream_copy is False:
            vc = cfg.get("video_codec", None)
            pr = cfg.get("preset", None)
            ac = cfg.get("audio_codec", None)
            ab = cfg.get("audio_bitrate", None)

            if not vc:
                errors.append(f"video_codec 값이 비어있음(사용키={used.get('video_codec')})")
            if not pr:
                errors.append(f"preset 값이 비어있음(사용키={used.get('preset')})")
            if not ac:
                errors.append(f"audio_codec 값이 비어있음(사용키={used.get('audio_codec')})")
            if not ab:
                errors.append(f"audio_bitrate 값이 비어있음(사용키={used.get('audio_bitrate')})")

        if errors:
            reason = " / ".join(errors)
            print(f"[POST][CFG-ERROR] job={job_id} {channel_name} profile={prof} gpu_index={gidx} src={os.path.basename(input_path)}")
            for e in errors:
                print(f"[POST][CFG-ERROR] - {e}")
            print(f"[POST][CFG-ERROR] used_keys={used}")
            print(f"[POST][ABORT] 설정 오류로 후처리 중단(자동이동/원본삭제/파일명 변경 진행 안 함)")

            if job_id:
                recorder_manager.postprocess_fail_job(job_id, reason)

            notifyEvent(
                "postprocess_failed",
                "후처리 실패",
                "후처리 설정 오류로 작업을 중단했습니다.",
                channel_id=channel_id,
                channel_name=channel_name,
                detail=reason,
                severity="error"
            )

            return

        # 2) 어떤 프로필/키/값으로 실행되는지 로그로 남김
        print(
            f"[POST][CFG] job={job_id} ch={channel_name} cid={channel_id} profile={prof} gpu_index={gidx} "
            f"res={cfg.get('postprocess_resolution','source')} "
            f"stream_copy={cfg.get('stream_copy')} vcodec={cfg.get('video_codec')} preset={cfg.get('preset')} "
            f"mode={'bitrate' if cfg.get('use_bitrate_mode') else 'quality'} q={cfg.get('video_quality')} br={cfg.get('video_bitrate')} "
            f"vbv_maxrate={cfg.get('vbv_maxrate')} vbv_bufsize={cfg.get('vbv_bufsize')} "
            f"acodec={cfg.get('audio_codec')} abr={cfg.get('audio_bitrate')} extra={cfg.get('extra_ffmpeg_options')} "
            f"keys={cfg.get('_used_keys')}"
        )

        # 3) copyStream은 성공/실패를 bool로 반환하여 실패시 중지
        ok = await copyStream(
            input_path=input_path,
            deleteAfterPostProcessing=cfg.get('deleteAfterPostProcessing', False),
            removeFixedPrefix=cfg.get('removeFixedPrefix', False),
            stream_copy=stream_copy,
            preset=cfg.get('preset'),
            postprocess_resolution=cfg.get('postprocess_resolution', 'source'),
            use_bitrate_mode=cfg.get('use_bitrate_mode', False),
            video_bitrate=cfg.get('video_bitrate'),
            video_codec=cfg.get('video_codec'),
            video_quality=cfg.get('video_quality'),
            audio_codec=cfg.get('audio_codec'),
            audio_bitrate=cfg.get('audio_bitrate'),
            vbv_maxrate=cfg.get('vbv_maxrate', ''),
            vbv_bufsize=cfg.get('vbv_bufsize', ''),
            moveAfterProcessingEnabled=cfg.get('moveAfterProcessingEnabled', False),
            moveAfterProcessing=cfg.get('moveAfterProcessing', ''),
            extra_ffmpeg_options=cfg.get('extra_ffmpeg_options', ''),
            gpu_index=cfg.get("gpu_index", None),
            ffmpeg_console_mode=("new" if bool(cfg.get("postNewWindow", False)) else "inherit"),
            job_id=job_id,
        )

        if not ok:
            reason = "copyStream returned False"
            print(f"[POST][FAIL] job={job_id} {channel_name} 후처리 실패 → 자동이동/원본삭제/파일명 변경 진행 안 함")
            if job_id:
                recorder_manager.postprocess_fail_job(job_id, reason)

            notifyEvent(
                "postprocess_failed",
                "후처리 실패",
                "후처리 작업이 실패했습니다.",
                channel_id=channel_id,
                channel_name=channel_name,
                detail=f"{reason}: {os.path.basename(input_path or '')}",
                severity="error"
            )

            return

        print(f"[POST][OK] job={job_id} {channel_name} 후처리 완료")
        if job_id:
            recorder_manager.postprocess_finish_job(job_id)

        notifyEvent(
            "postprocess_finished",
            "후처리 완료",
            "후처리 작업이 완료되었습니다.",
            channel_id=channel_id,
            channel_name=channel_name,
            detail=os.path.basename(input_path or ""),
            severity="info"
        )

    except Exception as e:
        reason = str(e)
        print(f"[POST][ERROR] job={job_id} {channel_name} {reason}")
        if job_id:
            recorder_manager.postprocess_fail_job(job_id, reason)

        notifyEvent(
            "postprocess_failed",
            "후처리 실패",
            "후처리 중 예외가 발생했습니다.",
            channel_id=channel_id,
            channel_name=channel_name,
            detail=reason,
            severity="error"
        )

    finally:
        try:
            recorder_manager.recording_remove_postproc(channel_id)
        except Exception:
            pass


# 파일 작업을 처리하는 함수
async def fileOperations(input_path, output_path, deleteAfterPostProcessing, removeFixedPrefix,
                         moveAfterProcessingEnabled, moveAfterProcessing) -> bool:

    # 1) 원본 삭제 (옵션 ON인데 실패하면 다음 단계로 내려가지 않음)
    if deleteAfterPostProcessing:
        try:
            if os.path.exists(input_path):
                os.remove(input_path)
                print(f"원본 파일 {input_path}가 삭제되었습니다.")
            else:
                # 이미 삭제된 경우는 경고만 하고 진행 가능
                print(f"[WARN] 원본 파일이 이미 존재하지 않습니다: {input_path}")
        except OSError as e:
            print(f"[ERROR] 원본 파일 삭제 실패: {input_path} / {e}")
            print(f"[ERROR] 삭제 실패로 인해 파일명 변경/자동이동 단계 진행을 중단합니다.")
            return False
    else:
        print(f"파일 삭제 옵션이 OFF 상태입니다. {input_path} 파일이 삭제되지 않습니다.")

    # 2) fixed_ 접두사 제거 rename (실패하면 move로 내려가지 않음)
    if removeFixedPrefix:
        final_output_path = os.path.join(os.path.dirname(output_path), os.path.basename(output_path).replace("fixed_", ""))
        try:
            if os.path.exists(output_path):
                os.rename(output_path, final_output_path)
                print(f"{output_path}가 {final_output_path}로 이름이 변경되었습니다.")
                output_path = final_output_path
            else:
                print(f"[ERROR] 파일명 변경 대상 파일이 존재하지 않습니다: {output_path}")
                return False
        except OSError as e:
            print(f"[ERROR] 파일명 변경 실패: {output_path} -> {final_output_path} / {e}")
            return False

    # 3) 이동 (실패하면 False 반환)
    if moveAfterProcessingEnabled and moveAfterProcessing:
        try:
            await asyncio.sleep(5)
            await moveDirectory(output_path, moveAfterProcessing)
        except Exception as e:
            print(f"[ERROR] moveDirectory 실패: {output_path} -> {moveAfterProcessing} / {e}")
            return False

    return True


# 후처리 파일을 복사하거나 인코딩하는 함수
async def copyStream(input_path, deleteAfterPostProcessing, removeFixedPrefix, stream_copy, preset,
                     use_bitrate_mode, video_bitrate, video_codec, video_quality, audio_codec, audio_bitrate,
                     vbv_maxrate="", vbv_bufsize="", postprocess_resolution: str = "source",
                     moveAfterProcessingEnabled=False, moveAfterProcessing="",
                     extra_ffmpeg_options="", gpu_index: int | None = None,
                     ffmpeg_console_mode: str = "new", job_id: str | None = None):

    if not input_path:
        print(f"[ERROR] 전달된 input_path가 None입니다. 스트림 복사 작업을 수행할 수 없습니다.")
        return False

    in_dir  = os.path.dirname(input_path) or "."
    in_base = os.path.basename(input_path)

    if stream_copy is False:
        _res = str(postprocess_resolution or "source").strip().lower()
        if _res not in ("source", "1080p", "720p", "480p"):
            _res = "source"

        _extra_raw = str(extra_ffmpeg_options or "").strip()
        _extra_tokens = shlex.split(_extra_raw) if _extra_raw else []
        _has_vf = any(
            (t == "-vf") or t.startswith("-vf=") or (t == "-filter:v") or t.startswith("-filter:v=")
            for t in _extra_tokens
        )

        if (not _has_vf) and (_res != "source"):
            th = 1080 if _res == "1080p" else (720 if _res == "720p" else 480)

            ffprobe_path = getFFprobe()
            ih = probeVideoHeight(ffprobe_path, input_path)

            # 실제로 downscale 되는 경우(입력 height > 목표 height)만 파일명 태그 교체
            if ih > th:
                stem, ext = os.path.splitext(in_base)

                # 1순위: 파일명 끝의 "1080p60" / "1080p" 형태를 교체
                m = re.search(r"(?P<res>(?:2160|1440|1080|720|480)p)(?P<fps>\d{2,3})?$", stem)
                if m:
                    fps = m.group("fps") or ""
                    stem = stem[:m.start("res")] + _res + fps
                else:
                    # 2순위: 파일명 전체에서 마지막 해상도 태그를 교체
                    ms = list(re.finditer(r"(?P<res>(?:2160|1440|1080|720|480)p)(?P<fps>\d{2,3})?", stem))
                    if ms:
                        lm = ms[-1]
                        fps = lm.group("fps") or ""
                        stem = stem[:lm.start("res")] + _res + fps + stem[lm.end():]

                in_base = stem + ext

    if in_base.startswith("fixed_"):
        out_base = in_base
    else:
        out_base = "fixed_" + in_base

    out_base = uniqueFilename(in_dir, out_base)
    output_path = os.path.join(in_dir, out_base)


    if not os.path.exists(in_dir):
        print(f"[ERROR] 출력 경로가 잘못되었습니다: {output_path}")
        return False

    print(
        f"[POST][COPY-START] job={job_id} input={input_path} output={output_path} "
        f"stream_copy={stream_copy} codec={video_codec} gpu_index={gpu_index}"
    )

    # 인코딩(stream_copy=False)인데 핵심 옵션이 비어있으면 여기서도 차단
    if stream_copy is False:
        if not video_codec or not preset or not audio_codec or not audio_bitrate:
            print("[ERROR] 인코딩 설정 누락으로 후처리 중단")
            print(f"[ERROR] video_codec={video_codec}, preset={preset}, audio_codec={audio_codec}, audio_bitrate={audio_bitrate}")
            return False

    try:
        await copySpecificFile(
            input_path=input_path,
            output_path=output_path,
            stream_copy=stream_copy,
            preset=preset,
            postprocess_resolution=postprocess_resolution,
            use_bitrate_mode=use_bitrate_mode,
            video_bitrate=video_bitrate,
            video_codec=video_codec,
            video_quality=video_quality,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            vbv_maxrate=vbv_maxrate,
            vbv_bufsize=vbv_bufsize,
            extra_ffmpeg_options=extra_ffmpeg_options,
            gpu_index=gpu_index,
            ffmpeg_console_mode=ffmpeg_console_mode,
            job_id=job_id,
        )
    except Exception as e:
        print(f"[ERROR] 후처리 작업 중 FFmpeg 처리 오류 발생: {str(e)}")
        return False

    if not os.path.exists(output_path):
        print(f"[ERROR] FFmpeg 처리 후 출력 파일이 존재하지 않습니다: {output_path}. 원본 파일 유지합니다.")
        return False

    if os.path.getsize(output_path) == 0:
        print(f"[ERROR] FFmpeg 처리 후 출력 파일의 크기가 0KB입니다: {output_path}. 원본 파일 유지합니다.")
        return False

    print(f"[DEBUG] FFmpeg 처리 완료 및 출력 파일 검증 성공: {output_path}")

    # fileOperations도 성공/실패를 반환받아 실패면 즉시 중단
    ok_ops = await fileOperations(
        input_path,
        output_path,
        deleteAfterPostProcessing,
        removeFixedPrefix,
        moveAfterProcessingEnabled,
        moveAfterProcessing,
    )
    if not ok_ops:
        print(f"[ERROR] fileOperations 실패 → 후처리 파이프라인 중단되어 파일명 변경/자동이동/원본삭제 중 일부 실패)")
        return False

    print(f"[DEBUG] 후처리가 완료되었습니다.")
    return True


# 후처리시 GPU HW 디코딩 가속
def pickHwaccel(ffmpeg_path: str, ffprobe_path: str, input_path: str,
                video_codec: str, stream_copy: bool, gpu_index: int | None = None) -> list[str]:

    if stream_copy:
        return []

    vc = (video_codec or "").lower()
    pre: list[str] = []

    in_codec = probeVcodec(ffprobe_path, input_path)

    # Intel QSV
    if vc in ("h264_qsv", "hevc_qsv"):
        # 멀티 GPU 선택
        dev = "qsv=qsv:hw"
        if gpu_index is not None:
            if os.name == "nt":
                dev = f"qsv=qsv:hw,child_device={int(gpu_index)},child_device_type=d3d11va"
            else:
                dev = f"qsv=qsv:hw,child_device={int(gpu_index)}"

        pre += ["-extra_hw_frames", "32"]

        pre += ["-init_hw_device", dev,
                "-filter_hw_device", "qsv",
                "-hwaccel", "qsv",
                "-hwaccel_output_format", "qsv"]

        if in_codec in ("h264", "avc1"):
            pre += ["-c:v:0", "h264_qsv"]
        elif in_codec in ("hevc", "h265"):
            pre += ["-c:v:0", "hevc_qsv"]

    elif vc in ("h264_nvenc", "hevc_nvenc"):
        dev = "cuda=cuda:0"
        if gpu_index is not None:
            dev = f"cuda=cuda:{int(gpu_index)}"

        pre += ["-init_hw_device", dev,
                "-filter_hw_device", "cuda",
                "-hwaccel", "cuda",
                "-hwaccel_output_format", "cuda"]


        # NVDEC(CUVID) 디코더 고정
        if in_codec in ("h264", "avc1"):
            pre += ["-c:v:0", "h264_cuvid"]
        elif in_codec in ("hevc", "h265", "hev1", "hvc1"):
            pre += ["-c:v:0", "hevc_cuvid"]

    elif vc in ("h264_amf", "hevc_amf"):
        if os.name == "nt":
            dev = "d3d11va=d3d11"
            if gpu_index is not None:
                dev = f"d3d11va=d3d11:{int(gpu_index)}"
            pre += ["-init_hw_device", dev,
                    "-filter_hw_device", "d3d11",
                    "-hwaccel", "d3d11va",
                    "-hwaccel_output_format", "d3d11"]

            # D3D11VA 디코더로 고정 
            if in_codec in ("h264", "avc1"):
                pre += ["-c:v:0", "h264_d3d11va"]
            elif in_codec in ("hevc", "h265", "hev1", "hvc1"):
                pre += ["-c:v:0", "hevc_d3d11va"]
        else:
            pre += ["-init_hw_device", "vaapi=vaapi",
                    "-filter_hw_device", "vaapi",
                    "-hwaccel", "vaapi",
                    "-hwaccel_output_format", "vaapi"]

    return pre


async def copySpecificFile(input_path, output_path, stream_copy, preset, use_bitrate_mode, video_bitrate,
                           video_codec, video_quality, audio_codec, audio_bitrate, vbv_maxrate="", vbv_bufsize="",
                           postprocess_resolution: str = "source",
                           extra_ffmpeg_options="", gpu_index: int | None = None,
                           ffmpeg_console_mode: str = "auto", job_id: str | None = None):

    ffmpeg_path = getFFmpeg()
    ffprobe_path = getFFprobe()

    def _creationflags(mode: str) -> int:
        if os.name != "nt": return 0
        m = (mode or "auto").lower()
        if m == "inherit": return 0
        if m == "new":     return subprocess.CREATE_NEW_CONSOLE
        return subprocess.CREATE_NEW_CONSOLE

    # 입력 컨테이너/코덱 간단 프로빙 
    def _probe_video_codec():
        try:
            r = subprocess.run(
                [ffprobe_path, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", input_path],
                capture_output=True, text=True, timeout=3
            )
            return (r.stdout or "").strip().lower()
        except Exception:
            return ""

    def _probe_format_name():
        try:
            r = subprocess.run(
                [ffprobe_path, "-v", "error", "-show_entries", "format=format_name",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True, text=True, timeout=3
            )
            return (r.stdout or "").strip().lower()
        except Exception:
            return ""

    in_ext  = os.path.splitext(input_path)[1].lower()
    out_ext = os.path.splitext(output_path)[1].lower()
    in_vc   = _probe_video_codec()    
    in_fmt  = _probe_format_name()     
    pre     = pickHwaccel(ffmpeg_path, ffprobe_path, input_path, video_codec, stream_copy, gpu_index=gpu_index)

    if not stream_copy:
        print(
            f"[POST][HWACCEL] job={job_id} codec={video_codec} gpu_index={gpu_index} "
            f"in_codec={in_vc} in_fmt={in_fmt} args={' '.join(shlex.quote(x) for x in pre)}"
        )

    # 입력 안정화 옵션 보강: genpts + discardcorrupt / err_detect
    cmd = [ffmpeg_path, "-y", "-nostdin",
           "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err"] + pre + ["-i", input_path]

    # 데이터 스트림 등 노이즈 제거
    cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]

    if stream_copy:
        cmd += ["-c:v", "copy", "-c:a", "copy"]

        if out_ext in (".ts", ".m2ts", ".mts"):
            # 입력이 mp4 계열이거나, 코덱이 h264/hevc로 판정되면 적용
            if ("mp4" in in_fmt or "mov" in in_fmt or "ism" in in_fmt or "m4a" in in_fmt
                or in_vc in ("h264", "avc1", "hev1", "hvc1", "hevc", "h265")):
                if in_vc in ("h264", "avc1", ""):
                    cmd += ["-bsf:v", "h264_mp4toannexb"]
                elif in_vc in ("hevc", "h265", "hev1", "hvc1"):
                    cmd += ["-bsf:v", "hevc_mp4toannexb"]

        # TS → MP4 리멕스 시에는 기존대로 ADTS→ASC (오디오)만 적용
        if out_ext == ".mp4" and in_ext in (".ts", ".m2ts", ".mts"):
            cmd += ["-bsf:a", "aac_adtstoasc"]

    else:
        cmd += ["-c:v", video_codec, "-preset", str(preset)]

        # NVENC: 멀티 GPU 환경에서 인코더 GPU를 명시적으로 고정
        if video_codec in ("h264_nvenc", "hevc_nvenc") and gpu_index is not None:
            cmd += ["-gpu", str(int(gpu_index))]

        # source / 1080p / 720p / 480p
        _res = str(postprocess_resolution or "source").strip().lower()
        if _res not in ("source", "1080p", "720p", "480p"):
            _res = "source"

        _extra_tokens = shlex.split(extra_ffmpeg_options) if extra_ffmpeg_options.strip() else []
        _has_vf = any(
            (t == "-vf") or t.startswith("-vf=") or (t == "-filter:v") or t.startswith("-filter:v=")
            for t in _extra_tokens
        )

        # QSV에서 원본 해상도인 경우 사용자가 -vf를 안 줬으면 NV12 포맷 정규화
        _is_qsv = (str(video_codec or "").lower() in ("h264_qsv", "hevc_qsv"))
        _res = str(postprocess_resolution or "source").strip().lower()
        if _is_qsv and (_res == "source") and (not _has_vf):
            cmd += ["-vf", "vpp_qsv=format=nv12"]
            _has_vf = True

        # -vf 위치(재시도 시 교체용)
        _vf_idx: int | None = None
        _vf_fallback: str | None = None

        def _infer_size_from_name(_p: str) -> tuple[int, int]:
            try:
                base = os.path.splitext(os.path.basename(_p))[0]
                m = re.findall(r"\b(2160|1440|1080|720|480)p\b", base)
                if not m:
                    return 0, 0
                h = int(m[-1])
                w_map = {2160: 3840, 1440: 2560, 1080: 1920, 720: 1280, 480: 854}
                w = int(w_map.get(h, 0) or 0)
                return (w, h) if w and h else (0, 0)
            except Exception:
                return 0, 0

        if (not _has_vf) and (_res != "source"):
            # 목표 해상도 (업스케일은 하지 않음)
            if _res == "1080p":
                tw, th = 1920, 1080
            elif _res == "720p":
                tw, th = 1280, 720
            else:  # 480p
                tw, th = 854, 480

            # hwaccel_output_format에 따라 가능한 스케일 필터로 자동 선택
            _hwfmt = ""
            try:
                if "-hwaccel_output_format" in pre:
                    _hwfmt = str(pre[pre.index("-hwaccel_output_format") + 1]).lower()
            except Exception:
                _hwfmt = ""

            # 입력 해상도 프로브 (동일 해상도면 -vf 자체를 안 붙이기 위해 필요)
            iw, ih = probeVideoSize(ffprobe_path, input_path)
            if iw <= 0 or ih <= 0:
                iw2, ih2 = _infer_size_from_name(input_path)
                if iw2 > 0 and ih2 > 0:
                    iw, ih = iw2, ih2

            # 다운스케일 필요 여부 판단 (동일/더 작으면 필터 자체를 생략)
            _need_scale = True
            ow = oh = 0
            if iw > 0 and ih > 0:
                s = min(1.0, min(tw / float(iw), th / float(ih)))
                if s >= 0.999999:
                    _need_scale = False
                else:
                    ow = max(2, int(iw * s) // 2 * 2)
                    oh = max(2, int(ih * s) // 2 * 2)

            if _need_scale:
                # 해상도 미확인(iw/ih=0)인 경우: 안정성 우선 (업스케일 방지 표현식을 사용)
                if _hwfmt == "qsv":
                    if iw > 0 and ih > 0:
                        # QSV: scale_qsv 우선 → 실패 시 vpp_qsv 폴백
                        _scale = f"scale_qsv=w={ow}:h={oh}:format=nv12"
                        _vf_fallback = f"vpp_qsv=w={ow}:h={oh}:format=nv12"
                    else:
                        # QSV는 표현식으로 업스케일 방지 가능(프로브 불가 시에도 동작)
                        _sf = f"min(1\\,min({tw}/iw\\,{th}/ih))"
                        _w  = f"trunc(iw*{_sf}/2)*2"
                        _h  = f"trunc(ih*{_sf}/2)*2"
                        _scale = f"scale_qsv=w='{_w}':h='{_h}':format=nv12"
                        _vf_fallback = f"vpp_qsv=w='{_w}':h='{_h}':format=nv12"

                elif _hwfmt == "cuda":
                    if iw > 0 and ih > 0:
                        _scale = f"scale_cuda=w={ow}:h={oh}:format=nv12"
                    else:
                        # 프로브 실패 시에도 GPU 스케일 유지 
                        _sf = f"min(1\\,min({tw}/iw\\,{th}/ih))"
                        _w  = f"trunc(iw*{_sf}/2)*2"
                        _h  = f"trunc(ih*{_sf}/2)*2"
                        _scale = f"scale_cuda=w='{_w}':h='{_h}':format=nv12"

                elif _hwfmt == "d3d11":
                    if iw > 0 and ih > 0:
                        _scale = f"vpp_amf=w={ow}:h={oh}:format=nv12"
                    else:
                        # 프로브 실패 시에도 GPU 스케일 유지 
                        _sf = f"min(1\\,min({tw}/iw\\,{th}/ih))"
                        _w  = f"trunc(iw*{_sf}/2)*2"
                        _h  = f"trunc(ih*{_sf}/2)*2"
                        _scale = f"vpp_amf=w='{_w}':h='{_h}':format=nv12"

                else:
                    # SW : 소프트 스케일
                    if iw > 0 and ih > 0:
                        _scale = f"scale=w={ow}:h={oh}"
                    else:
                        _scale = f"scale=w='min(iw\\,{tw})':h='min(ih\\,{th})':force_original_aspect_ratio=decrease"

                cmd += ["-vf", _scale]
                _vf_idx = len(cmd) - 1

        elif _has_vf and (_res != "source"):
            print(f"[WARN] extra_ffmpeg_options에 -vf/-filter:v가 포함되어 있어 후처리 해상도({_res}) 스케일은 적용하지 않습니다.")


        # NVENC 멀티 GPU 선택(-gpu) 
        vc = (video_codec or "").lower()
        if gpu_index is not None and vc in ("h264_nvenc", "hevc_nvenc"):
            cmd += ["-gpu", str(int(gpu_index))]

        if use_bitrate_mode:
            cmd += ["-b:v", str(video_bitrate)]
            if vbv_maxrate: cmd += ["-maxrate", str(vbv_maxrate)]
            if vbv_bufsize: cmd += ["-bufsize", str(vbv_bufsize)]
        else:
            if video_codec in ("libx264","libx265"):
                cmd += ["-crf", str(video_quality)]
            elif video_codec in ("h264_nvenc","hevc_nvenc"):
                cmd += ["-cq", str(video_quality)]
            elif video_codec in ("h264_qsv","hevc_qsv"):
                cmd += ["-global_quality", str(video_quality)]
            elif video_codec == "h264_amf":
                cmd += ["-rc","qvbr","-qvbr_quality_level", str(video_quality)]
            elif video_codec == "hevc_amf":
                cmd += ["-rc","qvbr","-quality","quality","-qvbr_quality", str(video_quality)]
            else:
                cmd += ["-crf", str(video_quality)]

        if str(audio_bitrate).lower() == "copy":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", audio_codec, "-b:a", str(audio_bitrate)]

        # extra는 위에서 split한 토큰 그대로 1회만 append
        if _extra_tokens:
            cmd += _extra_tokens

    # faststart는 MP4에만
    if out_ext == ".mp4":
        cmd += ["-movflags", "+faststart"]

    cmd += [output_path]

    print(f"[POST][FFMPEG-CMD] job={job_id} {' '.join(shlex.quote(x) for x in cmd)}")

    flags = _creationflags(ffmpeg_console_mode)

    async def _run_once(_cmd):
        if flags != 0:
            p = await asyncio.create_subprocess_exec(*_cmd, creationflags=flags)
            print(f"[POST][FFMPEG-PID] job={job_id} pid={getattr(p, 'pid', None)} console=new")
            ret = await p.wait()
            return ret, ""

        p = await asyncio.create_subprocess_exec(
            *_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=0,
        )
        print(f"[POST][FFMPEG-PID] job={job_id} pid={getattr(p, 'pid', None)} console=inherit")
        out_b, err_b = await p.communicate()
        err_txt = (err_b or b"").decode(errors="replace")
        return p.returncode, err_txt

    def _print_tail(_err: str, _n: int = 40):
        if not _err:
            return
        tail_lines = [x for x in _err.splitlines() if x.strip()][-_n:]
        for line in tail_lines:
            print("[FFMPEG]", line)

    ret, err_txt = await _run_once(cmd)

    # scale_qsv 실패 시 vpp_qsv로 자동 폴백 
    if (ret != 0) and _vf_fallback and (_vf_idx is not None):
        e = (err_txt or "").lower()
        _hint = (
            ("scale_qsv" in e)
            or ("no such filter" in e)
            or ("error while filtering" in e)
            or ("error reinitializing filters" in e)
            or ("failed to configure output pad" in e)
        )
        # 콘솔 상속 모드(flags!=0)에서는 stderr를 못 받으니 폴백 시도
        if (flags != 0) or (not err_txt) or _hint:
            print("[WARN] scale_qsv 처리 중 오류가 발생하여 vpp_qsv로 재시도합니다.")
            cmd2 = list(cmd)
            cmd2[_vf_idx] = _vf_fallback
            ret2, err2 = await _run_once(cmd2)
            if ret2 != 0:
                _print_tail(err2 or err_txt)
                raise subprocess.CalledProcessError(ret2, cmd2)
            # 성공 시 cmd 교체 (디버그/로그 일관성)
            cmd = cmd2
            ret = 0
            err_txt = err2

    if ret != 0:
        _print_tail(err_txt)
        raise subprocess.CalledProcessError(ret, cmd)

    print(f"{input_path} → {output_path} 완료")



# 재시작 없이 설정 업데이트 헬퍼 함수
def resolveRecordingPrefs():
    cfg = loadConfig() or {}

    # plugin_type 정규화
    plugin = (cfg.get("plugin_type") or "basic").lower()
    normalized_plugin = plugin if plugin in ("basic", "timemachine_plus") else "basic"

    # timemachine shift 정규화
    try:
        _shift = int(cfg.get("timemachine_time_shift") or 0)
    except Exception:
        _shift = 0
    if normalized_plugin == "basic":
        normalized_shift = max(0, min(10, _shift))
    else:
        normalized_shift = max(0, min(3600, _shift))

    # 분할/오버랩/최대시간
    split_mode = toBool(cfg.get("splitRecordingMode", False))
    try:
        auto_stop = int(cfg.get("autoStopInterval") or 0)
    except Exception:
        auto_stop = 0
    if not split_mode:
        auto_stop = 0

    try:
        ov = int(cfg.get("splitOverlapSec") or 0)
    except Exception:
        ov = 0
    ov = max(0, min(30, ov))
    if not split_mode:
        ov = 0

    # 숫자형 옵션 안전 파싱
    try:
        recheck = int(cfg.get("recheckInterval") or 60)
    except Exception:
        recheck = 60
    try:
        vq = int(cfg.get("video_quality") if cfg.get("video_quality") not in (None, "") else 23)
    except Exception:
        vq = 23

    # 최종 prefs
    prefs = {
        "plugin_type": normalized_plugin,
        "timemachine_time_shift": normalized_shift,
        "splitRecordingMode": split_mode,
        "splitPostProcessing":       toBool(cfg.get("splitPostProcessing", True)),
        "autoStopInterval": auto_stop,
        "splitOverlapSec": ov,

        "filenamePattern": cfg.get("filenamePattern") or "[{start_time}] {safe_live_title}",
        "recheckInterval": recheck,

        "autoPostProcessing":        toBool(cfg.get("autoPostProcessing", False)),
        "deleteAfterPostProcessing": toBool(cfg.get("deleteAfterPostProcessing", False)),
        "removeFixedPrefix":         toBool(cfg.get("removeFixedPrefix", False)),
        "moveAfterProcessingEnabled":toBool(cfg.get("moveAfterProcessingEnabled", False)),
        "moveAfterProcessing":       (cfg.get("moveAfterProcessing") or None),
        "postNewWindow":             toBool(cfg.get("postNewWindow", False)),
        "stream_copy":               toBool(cfg.get("stream_copy", True)),

        "video_codec":               cfg.get("video_codec") or "libx264",
        "preset":                    cfg.get("preset") or "medium",
        "use_bitrate_mode":          toBool(cfg.get("use_bitrate_mode", False)),
        "video_quality":             vq,
        "video_bitrate":             cfg.get("video_bitrate") or "1000k",
        "vbv_maxrate":               cfg.get("vbv_maxrate") or "",
        "vbv_bufsize":               cfg.get("vbv_bufsize") or "",
        "audio_codec":               cfg.get("audio_codec") or "aac",
        "audio_bitrate":             cfg.get("audio_bitrate") or "192k",
        "extra_ffmpeg_options":      cfg.get("extra_ffmpeg_options") or "",
    }

    return prefs


# 공통 설저 헬퍼
def postCfg() -> dict:
    prefs = resolveRecordingPrefs()
    return {
        "stream_copy":                prefs["stream_copy"],
        "video_codec":                prefs["video_codec"],
        "preset":                     prefs["preset"],
        "use_bitrate_mode":           prefs["use_bitrate_mode"],
        "video_quality":              prefs["video_quality"],
        "video_bitrate":              prefs["video_bitrate"],
        "vbv_maxrate":                prefs["vbv_maxrate"],
        "vbv_bufsize":                prefs["vbv_bufsize"],
        "extra_ffmpeg_options":       prefs["extra_ffmpeg_options"],
        "audio_codec":                prefs["audio_codec"],
        "audio_bitrate":              prefs["audio_bitrate"],
        "deleteAfterPostProcessing":  prefs["deleteAfterPostProcessing"],
        "removeFixedPrefix":          prefs["removeFixedPrefix"],
        "moveAfterProcessingEnabled": prefs["moveAfterProcessingEnabled"],
        "moveAfterProcessing":        prefs["moveAfterProcessing"] or "",
        "postNewWindow":              prefs["postNewWindow"],
    }


def splitGate() -> tuple[bool, int, int]:
    prefs = resolveRecordingPrefs()
    split_on = bool(prefs["splitRecordingMode"] and prefs["autoStopInterval"] > 0)
    return split_on, int(prefs["autoStopInterval"] or 0), int(prefs["splitOverlapSec"] or 0)


# 세그먼트 패턴 추적
def parseSegPattern(path: str) -> tuple[str, str, str] | None:
    if not path:
        return None
    out_dir, base = os.path.dirname(path), os.path.basename(path)
    m = re.match(r"(.+)_%0\d+d(\.[^.]+)$", base) or re.match(r"(.+)_\d{3,}(\.[^.]+)$", base)
    if not m:
        return None
    if not os.path.isdir(out_dir):
        return None
    base_noext, seg_ext = m.group(1), m.group(2)
    return out_dir, base_noext, seg_ext



def buildPipeCmds(channel: dict, cookies: dict, metadata: dict | None, *,
                  segmentPattern: str, segmentSec: int):
    cfg = loadConfig() or {}
    normalized_plugin = (cfg.get("plugin_type") or "basic").lower()
    if normalized_plugin not in ("basic", "timemachine_plus"):
        normalized_plugin = "basic"

    try:
        _shift = int(cfg.get("timemachine_time_shift") or 0)
    except Exception:
        _shift = 0
    normalized_shift = max(0, min(10, _shift)) if normalized_plugin == "basic" else max(0, min(3600, _shift))

    streamlink_path = getStreamlink()
    ffmpeg_path = getFFmpeg()

    sl_cmd = [streamlink_path]
    sl_cmd.extend(buildCommonPluginArgs(channel, cookies, normalized_plugin=normalized_plugin))

    # timemachine 플러그인일 때만 live-restart + 양수 오프셋
    if normalized_plugin in ("timemachine_plus",):
        sl_cmd.append("--hls-live-restart")
        if normalized_shift > 0:
            sl_cmd.extend(["--hls-start-offset", str(normalized_shift)])

    quality = (metadata or {}).get("resolved_quality") or channel.get("quality", "best")

    sl_cmd.extend([
        buildChannelUrl(channel),
        quality, 
        "--stdout",
        "--stream-segment-timeout", "15",
        "--stream-segment-attempts", "12",
    ])

    ff_cmd = [
        ffmpeg_path,
        "-y", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+genpts",
        "-analyzeduration", "100M", "-probesize", "100M",
        "-i", "-",
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c:v", "copy", "-c:a", "copy",
        "-flush_packets", "1",
        "-f", "segment",
        "-segment_format", "mpegts",      
        "-segment_time", str(int(segmentSec)),
        "-segment_time_delta", "0.5",     
        "-reset_timestamps", "1",
        segmentPattern
    ]

    try:
        print("[FF-CMD]", " ".join(shlex.quote(x) for x in ff_cmd))
    except Exception:
        print("[FF-CMD]", ff_cmd)

    return sl_cmd, ff_cmd


async def pumpPipe(src_proc, dst_proc, chunk: int = 256*1024):
    total = 0
    last_report = time.time()
    try:
        while True:
            buf = await src_proc.stdout.read(chunk)
            if not buf:
                break
            dst_proc.stdin.write(buf)
            await dst_proc.stdin.drain()
            total += len(buf)
            now = time.time()
            if now - last_report >= 5:
                print(f"[PUMP] wrote ~{total/1024/1024:.1f} MiB so far")
                last_report = now
    except Exception as _e:
        print(f"[PUMP][WARN] {type(_e).__name__}: {_e}")
    finally:
        with contextlib.suppress(Exception):
            if dst_proc.stdin:
                dst_proc.stdin.close()


def idxName(i: int, width: int = 3) -> str:

    try:
        return f"{int(i):0{width}d}"
    except Exception:
        return str(i)


def encodeOpts(cfg: dict) -> tuple[list[str], list[str]]:
    vc  = (cfg.get("video_codec") or "libx264").lower()
    pr  = str(cfg.get("preset") or "medium")
    use_br = bool(cfg.get("use_bitrate_mode", False))
    vb  = str(cfg.get("video_bitrate") or "2000k")
    vq  = str(cfg.get("video_quality") or "23")
    ac  = (cfg.get("audio_codec") or "aac").lower()
    ab  = str(cfg.get("audio_bitrate") or "192k")
    vbv_max = str(cfg.get("vbv_maxrate") or "")
    vbv_buf = str(cfg.get("vbv_bufsize") or "")
    extra   = (cfg.get("extra_ffmpeg_options") or "").strip()

    v = ["-c:v", vc, "-preset", pr]
    if use_br:
        v += ["-b:v", vb]
        if vbv_max: v += ["-maxrate", vbv_max]
        if vbv_buf: v += ["-bufsize", vbv_buf]
    else:
        if vc in ("libx264","libx265"):
            v += ["-crf", vq]
        elif vc in ("h264_nvenc","hevc_nvenc"):
            v += ["-cq", vq]
        elif vc in ("h264_qsv","hevc_qsv"):
            v += ["-global_quality", vq]
        elif vc in ("h264_amf",):
            v += ["-rc","qvbr","-qvbr_quality_level", vq]
        elif vc in ("hevc_amf",):
            v += ["-rc","qvbr","-quality","quality","-qvbr_quality", vq]
        else:
            v += ["-crf", vq]

    if ab.strip().lower() == "copy":
        a = ["-c:a", "copy"]
    else:
        a = ["-c:a", ac, "-b:a", ab]

    if extra:
        v += shlex.split(extra)
    return v, a


async def ffAsync(cmd: list[str], *, console_mode="auto"):
    def _creationflags(mode: str) -> int:
        if os.name != "nt": return 0
        m = (mode or "auto").lower()
        if m == "inherit": return 0
        if m == "new":     return subprocess.CREATE_NEW_CONSOLE
        return subprocess.CREATE_NEW_CONSOLE

    p = await asyncio.create_subprocess_exec(*cmd, creationflags=_creationflags(console_mode))
    await p.wait()
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd)


def listNumericSegments(session_dir: str, base_noext: str, ext: str = ".mp4") -> list[tuple[int,str]]:
    pref = f"{base_noext}_"
    out = []
    for n in os.listdir(session_dir):
        if not (n.startswith(pref) and n.endswith(ext)): continue
        s = n[len(pref):-len(ext)]
        if s.isdigit():
            out.append((int(s), os.path.join(session_dir, n)))
    out.sort(key=lambda x: x[0])
    return out


# 분할녹화용 후처리 함수
async def batchEncodeSegments(session_dir: str, base_noext: str, overlap_sec: int, post_cfg: dict, input_ext: str = ".ts"):
    mode = "new" if toBool(post_cfg.get("postNewWindow", False)) else "inherit"
    ff = getFFmpeg()
    ffprobe = resolveFFprobe(ff)
    segs = listNumericSegments(session_dir, base_noext, input_ext)
    if not segs:
        print(f"[BATCH] no segments in {session_dir}")
        return

    do_copy = toBool(post_cfg.get("stream_copy", True))
    vopts, _aopts = encodeOpts(post_cfg)
    aopts = ["-c:a", "aac", "-b:a", "192k"]  # 항상 aac 192k
    out_base_noext = base_noext
    scale_vf: list[str] = []
    overlap_sec = max(0, int(overlap_sec or 0))

    def _concat_file_line(path: str) -> str:
        safe = os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")
        return f"file '{safe}'\n"

    async def _make_overlap_input(prev_path: str, cur_path: str, idx: int) -> tuple[str, list[str]]:
        if overlap_sec <= 0 or not prev_path or not os.path.exists(prev_path):
            return cur_path, []

        stem = f".overlap_{idxName(idx)}"
        tail_path = os.path.join(session_dir, f"{stem}_tail.ts")
        list_path = os.path.join(session_dir, f"{stem}.ffconcat")
        merged_path = os.path.join(session_dir, f"{stem}_merged.ts")
        temp_paths = [tail_path, list_path, merged_path]

        try:
            await ffAsync([
                ff, "-y", "-nostdin",
                "-sseof", f"-{overlap_sec}",
                "-i", prev_path,
                "-map", "0:v:0?", "-map", "0:a:0?",
                "-c", "copy",
                "-f", "mpegts",
                tail_path,
            ], console_mode=mode)

            with open(list_path, "w", encoding="utf-8") as f:
                f.write(_concat_file_line(tail_path))
                f.write(_concat_file_line(cur_path))

            await ffAsync([
                ff, "-y", "-nostdin",
                "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c", "copy",
                "-f", "mpegts",
                merged_path,
            ], console_mode=mode)

            print(f"[BATCH][OVERLAP] {os.path.basename(cur_path)} 앞에 이전 세그먼트 {overlap_sec}s를 붙였습니다.")
            return merged_path, temp_paths

        except Exception as e:
            print(f"[BATCH][OVERLAP][WARN] {os.path.basename(cur_path)} 오버랩 생성 실패 → 원본 세그먼트로 진행: {e}")
            for p in temp_paths:
                with contextlib.suppress(Exception):
                    if os.path.exists(p):
                        os.remove(p)
            return cur_path, []

    def _probe_height(path: str) -> int:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=height", "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=3
            )
            s = (r.stdout or "").strip()
            return int(s) if s.isdigit() else 0
        except Exception:
            return 0

    def _replace_last_res_tag(name: str, new_res: str) -> str:
        # 1순위: 끝이 같은 형태면 suffix만 교체
        m = re.search(r"(?P<res>(?:2160|1440|1080|720|480)p)(?P<fps>\d{2,3})?$", name)
        if m:
            fps = m.group("fps") or ""
            return name[:m.start("res")] + new_res + fps

        # 2순위: 문자열 전체에서 마지막 해상도 토큰(2160/1440/1080/720/480p)을 찾아 교체
        ms = list(re.finditer(r"(?P<res>(?:2160|1440|1080|720|480)p)(?P<fps>\d{2,3})?", name))
        if ms:
            lm = ms[-1]
            fps = lm.group("fps") or ""
            return name[:lm.start("res")] + new_res + fps + name[lm.end():]

        return name

    # 사용자가 이미 -vf/-filter를 줬으면 자동 scale 및 파일명 교체를 하지 않음
    _has_vf = any(
        (t in ("-vf", "-filter:v", "-filter_complex")) or str(t).startswith("-vf=") or str(t).startswith("-filter:v=")
        for t in vopts
    )

    _req_res = str(post_cfg.get("postprocess_resolution", "source") or "source").strip().lower()
    if _req_res not in ("source", "1080p", "720p", "480p"):
        _req_res = "source"

    # stream_copy면 파일명도 바꾸지 않음
    if (not do_copy) and (not _has_vf) and (_req_res != "source"):
        tw, th = (1920, 1080) if _req_res == "1080p" else ((1280, 720) if _req_res == "720p" else (854, 480))

        ih = 0
        for _idx, p in segs:
            try:
                if os.path.getsize(p) < 32 * 1024:
                    continue
            except Exception:
                continue
            ih = _probe_height(p)
            if ih:
                break

        # ffprobe가 높이를 못 뽑는 경우(ih=0) → 파일명 끝의 "1080p60"/"1080p" 태그로 보정
        if not ih:
            m = re.search(r"(?P<res>(?:2160|1440|1080|720|480)p)(?P<fps>\d{2,3})?$", base_noext)
            if m:
                try:
                    ih = int(m.group("res")[:-1])  # '1080p' -> 1080
                except Exception:
                    ih = 0

        # 실제 다운스케일이 적용되는 경우에만 scale + 파일명 태그 교체
        if ih > th:
            scale_vf = [
                "-vf",
                f"scale=w='min(iw\\,{tw})':h='min(ih\\,{th})':force_original_aspect_ratio=decrease:force_divisible_by=2"
            ]
            out_base_noext = _replace_last_res_tag(base_noext, _req_res)
            if out_base_noext != base_noext:
                print(f"[BATCH] downscale active: {base_noext} -> {out_base_noext} (h={ih} > {th})")

    for i, (idx, cur) in enumerate(segs):
        # 너무 작은/깨진 세그먼트 스킵
        try:
            if os.path.getsize(cur) < 32 * 1024:
                print(f"[BATCH] skip tiny/corrupt segment: {os.path.basename(cur)}")
                continue
        except Exception:
            pass

        out = os.path.join(session_dir, f"fixed_{out_base_noext}_{idxName(idx)}.mp4")
        print(f"[BATCH] -> {os.path.basename(out)}")

        temp_overlap_paths: list[str] = []
        effective_cur = cur
        if i > 0 and overlap_sec > 0:
            effective_cur, temp_overlap_paths = await _make_overlap_input(segs[i - 1][1], cur, idx)

        # 마지막 세그먼트 길이(트림 폴백용)
        dur = get_media_duration(ffprobe, effective_cur) if i == len(segs) - 1 else 0.0

        # (A) 1차 시도: 기존 경로 → .ts 입력이면 demux 강제
        def cmd_first_pass():
            is_ts = effective_cur.lower().endswith(".ts")
            base = [
                ff, "-y", "-nostdin",
                "-fflags", "+genpts+discardcorrupt", "-analyzeduration", "100M", "-probesize", "100M",
                "-err_detect", "ignore_err", "-ignore_unknown",
            ]
            if is_ts:
                base += ["-f", "mpegts"]
            base += ["-i", effective_cur, "-map", "0:v:0?", "-map", "0:a:0?"]

            ab = str(post_cfg.get("audio_bitrate", "192k")).lower()
            if do_copy:
                cmd = base + (["-c:v", "copy", "-c:a", "copy"] if ab == "copy"
                              else ["-c:v", "copy", "-c:a", "aac", "-b:a", ab])
                if out.lower().endswith(".mp4") and is_ts:
                    cmd += ["-bsf:a", "aac_adtstoasc"]
                return cmd + ["-movflags", "+faststart", out]
            else:
                return base + scale_vf + vopts + aopts + ["-movflags", "+faststart", out]


        # (B) 2차 시도: 강제 디멀티플렉서 + 재인코드(비디오만이라도 살리기)
        def cmd_second_pass():
            # do_copy여도 실패 시엔 비디오 재인코드로 강제
            vopts_force = vopts[:] if not do_copy else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24"]
            return [
                ff, "-y", "-nostdin",
                "-fflags", "+genpts+discardcorrupt", "-analyzeduration", "200M", "-probesize", "200M",
                "-err_detect", "ignore_err", "-ignore_unknown",
                "-f", "mpegts", "-i", effective_cur,        # 컨테이너 강제
                "-map", "0:v:0?", "-map", "0:a:0?",
                "-shortest",
            ] + scale_vf + vopts_force + aopts + ["-movflags", "+faststart", out]

        # (C) 3차 시도: 마지막 세그먼트면 살짝 트림하여 재시도
        def cmd_third_pass_trim():
            # dur이 신뢰 가능하고 충분히 길 때만(>1.2s)
            cut = max(0.0, dur - 0.7)
            vopts_force = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24"]
            return [
                ff, "-y", "-nostdin",
                "-fflags", "+genpts+discardcorrupt", "-analyzeduration", "200M", "-probesize", "200M",
                "-err_detect", "ignore_err", "-ignore_unknown",
                "-f", "mpegts", "-i", effective_cur,
                "-map", "0:v:0?", "-map", "0:a:0?",
                "-t", f"{cut:.2f}",
                "-shortest",
            ] + scale_vf + vopts_force + aopts + ["-movflags", "+faststart", out]

        try:
            try:
                await ffAsync(cmd_first_pass(), console_mode=mode)
            except subprocess.CalledProcessError:
                print(f"[BATCH][WARN] 1st pass failed: {os.path.basename(cur)} → retry (force demux/reencode)")

                try:
                    await ffAsync(cmd_second_pass(), console_mode=mode)

                except subprocess.CalledProcessError:
                    if i == len(segs) - 1 and dur > 1.2:
                        print(f"[BATCH][WARN] 2nd pass failed: trimming tail and retry ({dur:.2f}s)")

                        try:
                            await ffAsync(cmd_third_pass_trim(), console_mode=mode)

                        except subprocess.CalledProcessError as e3:
                            print(f"[BATCH][WARN] 3rd pass failed on {os.path.basename(cur)} rc={e3.returncode} → skip")
                            continue

                    else:
                        print(f"[BATCH][WARN] 2nd pass failed on {os.path.basename(cur)} → skip")
                        continue
        finally:
            for p in temp_overlap_paths:
                with contextlib.suppress(Exception):
                    if os.path.exists(p):
                        os.remove(p)

    # 후처리 옵션: 원본 세그먼트(ts) 삭제/이동 유지
    if post_cfg.get("deleteAfterPostProcessing", False):
        for _, p in segs:
            with contextlib.suppress(Exception):
                os.remove(p)
        print(f"[BATCH] 후처리 전 원본 파일을 삭제했습니다.")

    if post_cfg.get("removeFixedPrefix", False):
        for i, (idx, _) in enumerate(segs):
            src = os.path.join(session_dir, f"fixed_{out_base_noext}_{idxName(idx)}.mp4")
            dst = os.path.join(session_dir, f"{out_base_noext}_{idxName(idx)}.mp4")
            if os.path.exists(src):
                with contextlib.suppress(Exception):
                    os.replace(src, dst)

    if post_cfg.get("moveAfterProcessingEnabled", False) and post_cfg.get("moveAfterProcessing"):
        target = post_cfg["moveAfterProcessing"]
        os.makedirs(target, exist_ok=True)
        for i, (idx, _) in enumerate(segs):
            name = f"{out_base_noext}_{idxName(idx)}.mp4"
            src_fixed = os.path.join(session_dir, f"fixed_{name}")
            src_plain = os.path.join(session_dir, name)
            src = src_plain if os.path.exists(src_plain) else (src_fixed if os.path.exists(src_fixed) else None)
            if src:
                await moveDirectory(src, target)


# 분할 파이프라인 루프
async def runSegLoop(updated_channel: dict, channel_id: str, channel_name: str, metadata: dict, cookies: dict,
                     *, segmentSec: int, splitOverlapSec: int, autoPostProcessing: bool, post_cfg: dict,
                     base_noext: str, out_dir: str):
    # (1) 세그먼트 패턴/디렉토리
    seg_ext = ".ts"
    INDEX_WIDTH = 3
    INDEX_PATTERN = f"%0{INDEX_WIDTH}d"
    segmentPattern = os.path.join(out_dir, f"{base_noext}_{INDEX_PATTERN}{seg_ext}")
    os.makedirs(out_dir, exist_ok=True)
    print("[SEGCFG] out_dir=", out_dir)
    print("[SEGCFG] pattern=", segmentPattern)

    # 상태 승격
    recorder_manager.set_status_recording(channel_id, True)
    recorder_manager.set_status_reserved(channel_id, False)
    recorder_manager.recording_set_start_time(channel_id)

    streamlink = ffmpeg = None
    slErrTask = ffErrTask = pump_task = None

    # (2) 커맨드 생성
    sl_cmd, ff_cmd = buildPipeCmds(
        updated_channel, cookies, metadata,
        segmentPattern=segmentPattern,
        segmentSec=int(segmentSec),
    )
    try:
        print("[SL-CMD]", " ".join(shlex.quote(x) for x in sl_cmd))
    except Exception:
        print("[SL-CMD]", sl_cmd)

    # (3) 환경/프로세스 스폰
    prefs_for_env = resolveRecordingPrefs()
    env = buildStreamlinkEnv(
        prefs_for_env["plugin_type"],
        prefs_for_env["timemachine_time_shift"]
    )
    print(f"[DEBUG] export RWEB_TM_SHIFT={env.get('RWEB_TM_SHIFT')} (seg path)")

    kwargs_sl, kwargs_ff = {}, {}
    if os.name == "nt":
        kwargs_sl["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs_ff["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        if hasattr(os, "setsid"):
            kwargs_sl["preexec_fn"] = os.setsid
            kwargs_ff["preexec_fn"] = os.setsid

    try:
        streamlink = await asyncio.create_subprocess_exec(
            *sl_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs_sl,
            env=env
        )

        ffmpeg = await asyncio.create_subprocess_exec(
            *ff_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **kwargs_ff
        )

        # 프로세스 등록
        recorder_manager.set_tasks_process(channel_id, ffmpeg)
        recorder_manager.recording_set_filename(channel_id, segmentPattern)

        # 리더/펌프
        slErrTask = asyncio.create_task(read_stderr(streamlink, channel_id))
        ffErrTask = asyncio.create_task(read_stderr(ffmpeg,     channel_id))
        pump_task = asyncio.create_task(pumpPipe(streamlink, ffmpeg))

        # (4) 세그먼트 감시 + 진행상태
        def listSegments():
            patt = f"{base_noext}_"
            files = []
            try:
                for name in os.listdir(out_dir):
                    if name.startswith(patt) and name.endswith(seg_ext):
                        try:
                            idx = int(name[len(patt):-len(seg_ext)])
                            files.append((idx, os.path.join(out_dir, name)))
                        except ValueError:
                            pass
            except Exception:
                pass
            files.sort(key=lambda x: x[0])
            return files

        paths_by_idx: dict[int, str] = {}
        seen_names: set[str] = set()
        last_progress_ts = 0.0

        while True:
            # ffmpeg 종료 시 탈출
            if ffmpeg and ffmpeg.returncode is not None:
                break

            # 사용자 중지 → 부드럽게 종료
            if recorder_manager.get_is_user_stopped(channel_id):
                print(f"[SEG] {channel_name} user stop detected → graceful finalize")

                # (1) ffmpeg EOF/종료 유도(먼저)
                with contextlib.suppress(Exception):
                    if ffmpeg and ffmpeg.returncode is None and ffmpeg.stdin:
                        try:
                            if hasattr(ffmpeg.stdin, "write_eof"):
                                ffmpeg.stdin.write_eof()
                        except Exception:
                            pass
                        ffmpeg.stdin.close()

                # (2) streamlink 종료(출력 EOF 전파)
                with contextlib.suppress(Exception):
                    if streamlink and streamlink.returncode is None:
                        streamlink.terminate()

                # (3) ffmpeg 마무리 대기
                try:
                    await asyncio.sleep(0.3)
                    if ffmpeg and ffmpeg.returncode is None:
                        await asyncio.wait_for(ffmpeg.wait(), timeout=9.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(Exception):
                        ffmpeg.terminate()
                    try:
                        await asyncio.wait_for(ffmpeg.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(Exception):
                            ffmpeg.kill()
                            await ffmpeg.wait()

                # (4) 펌프는 EOF 받아 자연 종료 유도 → 미종료면 그때 취소
                if pump_task and not pump_task.done():
                    try:
                        await asyncio.wait_for(pump_task, timeout=2.0)
                    except asyncio.TimeoutError:
                        pump_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pump_task

                break

            # 신규 세그먼트 로그/트래킹
            segs = listSegments()
            for idx, path in segs:
                if path in seen_names:
                    continue
                seen_names.add(path)
                paths_by_idx[idx] = path
                print(f"[SEG] detected: {os.path.basename(path)}")

            # 진행 상태 (5초 주기)
            now = time.time()
            if segs and now - last_progress_ts >= 5.0:
                active_idx, active_path = segs[-1]
                try:
                    mib = os.path.getsize(active_path) / (1024 * 1024)
                    print(f"[SEG-PROG] {base_noext}_{idxName(active_idx)}{seg_ext} ~{mib:.1f} MiB")
                except Exception:
                    pass
                last_progress_ts = now

            await asyncio.sleep(0.5)

    finally:
        print(f"[FINALIZE] {channel_name} segment finalize begin")

        # 최신 설정 재평가
        prefs = resolveRecordingPrefs()
        auto_pp = bool(prefs.get("splitPostProcessing", prefs.get("autoPostProcessing", True)))
        ov = int(prefs.get("splitOverlapSec") or 0)
        ov = max(0, min(30, ov))

        seg_path_dbg = "N/A"
        try:
            seg_path_dbg = paths_by_idx[max(paths_by_idx)] if paths_by_idx else "N/A"
        except Exception:
            pass
        print(
            f"[POST][DBG] seg_autoPostProcessing={auto_pp} " 
            f"stream_copy={prefs.get('stream_copy')} video_codec={prefs.get('video_codec')} "
            f"audio_codec={prefs.get('audio_codec')} audio_bitrate={prefs.get('audio_bitrate')} "
            f"splitRecordingMode={prefs.get('splitRecordingMode')} autoStopInterval={prefs.get('autoStopInterval')} "
            f"seg_path={seg_path_dbg} out_dir={out_dir}"
        )

        # (1) ffmpeg에 EOF 신호 전달(가능 시) → stdin 닫기
        with contextlib.suppress(Exception):
            if ffmpeg and ffmpeg.returncode is None and ffmpeg.stdin:
                try:
                    if hasattr(ffmpeg.stdin, "write_eof"):
                        ffmpeg.stdin.write_eof()
                except Exception:
                    pass
                ffmpeg.stdin.close()
                if hasattr(ffmpeg.stdin, "wait_closed"):
                    try:
                        await asyncio.wait_for(ffmpeg.stdin.wait_closed(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

        # (2) 프로세스 종료(자연 종료 우선 → 타임아웃 시 terminate/kill)
        for p in (ffmpeg, streamlink):
            if p and p.returncode is None:
                with contextlib.suppress(Exception):
                    p.terminate()
            try:
                await asyncio.wait_for(p.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    p.kill()
                    await p.wait()

        # (3) 펌프는 EOF 받았으면 자연 종료 대기 → 미종료면 취소
        if pump_task and not pump_task.done():
            try:
                await asyncio.wait_for(pump_task, timeout=2.0)
            except asyncio.TimeoutError:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task

        # (4) Windows(Proactor) 완화용: 짧은 드레인(남은 버퍼 비우기)
        with contextlib.suppress(Exception):
            if streamlink and streamlink.stdout:
                # 아주 짧게 몇 번만 읽어 EOF 인지 시도 (없으면 바로 빠짐)
                for _ in range(3):
                    if streamlink.stdout.at_eof():
                        break
                    chunk = await asyncio.wait_for(streamlink.stdout.read(4096), timeout=0.1)
                    if not chunk:
                        break

        # 배치 진입 전 세그먼트 개수 로그
        probe_cnt = len(listNumericSegments(out_dir, base_noext, seg_ext))
        print(f"[BATCH] found {probe_cnt} segment(s) before encode")

        # 배치 트리거 (간소화: 항상 비동기 패턴 큐)
        try:
            # 세그먼트 완전 종료/해제 대기(핸들/사이즈 안정화)
            await asyncio.sleep(0.15)
            await quiesce_last_segment(out_dir, base_noext, seg_ext, attempts=15, interval=0.4)

            if auto_pp and probe_cnt > 0:
                patt = os.path.join(out_dir, f"{base_noext}_%03d{seg_ext}")
                print(f"[BATCH] enqueue pattern: {patt}")
                ok = await queueBatchPattern(channel_id, patt)
                print(f"[BATCH] queued via pattern: {ok}")
            elif not auto_pp:
                print("[BATCH] autoPostProcessing=False → 원본 세그먼트만 보존합니다.")
            else:
                print("[BATCH] no segments → skip queue.")

        except Exception as e:
            print(f"[BATCH][ERROR] trigger failed: {e}")
            # 안전 폴백: 중복은 내부 가드에서 무시
            if auto_pp:
                with contextlib.suppress(Exception):
                    patt = os.path.join(out_dir, f"{base_noext}_%03d{seg_ext}")
                    ok = await queueBatchPattern(channel_id, patt)
                    print(f"[BATCH][FALLBACK] queued via pattern: {ok}")

        # stderr 태스크 마무리
        for t in (slErrTask, ffErrTask):
            if t:
                try:
                    await asyncio.wait_for(t, timeout=1.5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(Exception):
                        t.cancel()
                        await t

        # 빈 세션 폴더 정리(잠금파일만 남은 경우 포함)
        try:
            names = os.listdir(out_dir)
            keep = {".batch.lock", "Thumbs.db"}
            useful = [n for n in names if (n.endswith(seg_ext)
                                           or n.startswith("fixed_")
                                           or n.lower().endswith(".mp4"))]
            if not useful and set(names).issubset(keep):
                for k in list(keep):
                    p = os.path.join(out_dir, k)
                    if os.path.exists(p):
                        with contextlib.suppress(Exception):
                            os.remove(p)
                os.rmdir(out_dir)
                print(f"[FINALIZE] empty session dir removed: {out_dir}")
        except Exception:
            pass


        try:
            # 세그먼트/결과가 전혀 없고, 잠금파일 외엔 아무것도 없으면 폴더 삭제
            names = os.listdir(out_dir)
            keep = {".batch.lock", "Thumbs.db"}   # 무시 목록
            useful = [n for n in names if (n.endswith(seg_ext)
                                           or n.startswith("fixed_")
                                           or n.lower().endswith(".mp4"))]
            if not useful and set(names).issubset(keep):
                with contextlib.suppress(Exception):
                    for k in keep:
                        p = os.path.join(out_dir, k)
                        if os.path.exists(p): os.remove(p)
                os.rmdir(out_dir)
                print(f"[FINALIZE] empty session dir removed: {out_dir}")
        except Exception:
            pass


        # 세션 상태 정리
        try:
            recorder_manager.set_status_recording(channel_id, False)
            recorder_manager.recording_remove_start_time(channel_id)
            recorder_manager.recording_remove_filename(channel_id)
            recorder_manager.clear_tasks_process(channel_id)
        except Exception:
            pass

        # 종료 직후 예약/대기 재마킹 
        try:
            stop_req = recorder_manager.get_is_user_stopped(channel_id)
            latest = RecorderManager.getChannels() or []
            latest_ch = next((c for c in latest if c.get("id") == channel_id), None) or updated_channel
            rec_enabled_now = bool(latest_ch.get("record_enabled", True))
            if rec_enabled_now and not stop_req:
                recorder_manager.set_status_reserved(channel_id, True)
            else:
                recorder_manager.set_status_reserved(channel_id, False)
        except Exception:
            pass


async def queueBatchPattern(channel_id: str, pattern_path: str) -> bool:
    try:
        # 분할 플래그/오토스탑 확인
        split_on, auto_stop, overlap = splitGate()
        if not (split_on and auto_stop > 0):
            return False

        # 분할 전용 자동후처리 플래그 OFF면 즉시 중단
        prefs = resolveRecordingPrefs()
        auto_pp = bool(prefs.get("splitPostProcessing", prefs.get("autoPostProcessing", True)))
        if not auto_pp:
            return False

        parsed = parseSegPattern(pattern_path)
        if not parsed:
            return False
        out_dir, base_noext, seg_ext = parsed

        # 채널 단위 중복 방지
        if not recorder_manager.recording_add_postproc(channel_id):
            return False

        cfg = postCfg()

        # 채널명 조회: 실패해도 channel_id로 대체
        try:
            channels = RecorderManager.getChannels() or []
            ch = next((c for c in channels if c.get("id") == channel_id), None)
            channel_name = (ch or {}).get("name") or channel_id
        except Exception:
            channel_name = channel_id

        job_id = recorder_manager.postprocess_register_job(
            channel_id=channel_id,
            channel_name=channel_name,
            input_path=pattern_path,
            post_cfg={
                **(cfg or {}),
                "_profile": (cfg or {}).get("_profile") or "batch",
            },
        )

        print(
            f"[BATCH][JOB-START] job={job_id} ch={channel_name} "
            f"pattern={pattern_path} out_dir={out_dir}"
        )

        t = asyncio.create_task(batchEncodeSegments(
            session_dir=out_dir,
            base_noext=base_noext,
            overlap_sec=overlap,
            post_cfg=cfg,
            input_ext=seg_ext,
        ))

        def _on_batch_done(task: asyncio.Task):
            try:
                task.result()
                recorder_manager.postprocess_finish_job(job_id)
                print(f"[BATCH][JOB-OK] job={job_id} ch={channel_name} pattern={pattern_path}")
            except Exception as e:
                reason = f"batchEncodeSegments failed: {e}"
                recorder_manager.postprocess_fail_job(job_id, reason)
                print(f"[BATCH][JOB-FAIL] job={job_id} ch={channel_name} reason={reason}")
            finally:
                recorder_manager.recording_remove_postproc(channel_id)

        t.add_done_callback(_on_batch_done)
        trackPostprocessTask(t)

        print(f"[BATCH][FALLBACK][PATTERN] start: {out_dir}")
        return True

    except Exception as e:
        print(f"[BATCH][FALLBACK][PATTERN][ERROR] {channel_id} {e}")
        try:
            recorder_manager.recording_remove_postproc(channel_id)
        except Exception:
            pass
        return False


# 분할녹화 외부 폴백 트리거
async def queueBatchLast(channel_id: str) -> bool:
    try:
        # 분할/오토스탑/분할자동후처리 모두 만족할 때만 시도
        split_on, auto_stop, _ = splitGate()
        prefs = resolveRecordingPrefs()
        auto_pp = bool(prefs.get("splitPostProcessing", prefs.get("autoPostProcessing", True)))
        if not (split_on and auto_stop > 0 and auto_pp):
            return False

        last = recorder_manager.get_recording_filename(channel_id)
        if not last:
            return False
        return await queueBatchPattern(channel_id, last)
    except Exception as e:
        print(f"[BATCH][FALLBACK][ERROR] {channel_id} {e}")
        return False


# 분할녹화 저장 폴더 덮어쓰기 방지
def uniqueSessionDir(base_dir: str, base_noext: str) -> str:
    p = os.path.join(base_dir, base_noext)
    if not os.path.exists(p):
        return p
    i = 2
    while True:
        cand = os.path.join(base_dir, f"{base_noext} ({i})")
        if not os.path.exists(cand):
            return cand
        i += 1


def formatChzzkCmdForLog(cmd: list) -> str:
    safe = []

    for item in cmd:
        text = str(item)

        if text.startswith("Cookie="):
            text = "Cookie=<hidden>"

        safe.append(shlex.quote(text))

    return " ".join(safe)


# 치지직용 녹화시작 함수
async def chzzkStartRecording(channel, cookies, recheckInterval, autoStopInterval, autoPostProcessing, filenamePattern,
                              plugin_type, timemachine_time_shift, is_user_request=False, splitRecordingMode=False, post_cfg=None):


    recent_live_block = {"live_id": None, "until": 0.0}

    # 0) 인자로 id 또는 dict 모두 허용
    if isinstance(channel, dict):
        channel_id = channel.get("id")
    else:
        channel_id = str(channel) if channel is not None else None
    if not channel_id:
        print("[ERROR] chzzkStartRecording: 유효하지 않은 채널 인자")
        return


    # 1) 현재 상태에서 dict 재조회 (메모리 동기화 보장 + 디스크 폴백)
    state_channels = RecorderManager.getChannels() or []
    channel_obj = next((c for c in state_channels if c.get("id") == channel_id), None)
    if not channel_obj:
        try:
            disk_channels = loadChannels() or []
            channel_obj = next((c for c in disk_channels if c.get("id") == channel_id), None)
        except Exception:
            channel_obj = None
    if not channel_obj:
        print(f"[ERROR] chzzkStartRecording: '{channel_id}' 채널을 찾을 수 없습니다.")
        return

    channel = channel_obj
    channel_id = channel["id"]
    channel_name = channel["name"]

    # 하드 가드: 사용자 중지면 어떤 경로로도 시작하지 않음
    if recorder_manager.get_is_user_stopped(channel_id):
        print(f"[DEBUG] {channel_name} 중지 요청 상태(하드 가드). 시작하지 않습니다.")
        return


    # 2) 녹화 플래그가 True인데 실제 프로세스가 없으면 상태 치유(힐링)
    if recorder_manager.get_status_recording(channel_id) and not procAlive(channel_id):
        print(f"[WARN] {channel_name} recording flag True but no live process. Healing state.")
        recorder_manager.set_status_recording(channel_id, False)
        recorder_manager.recording_remove_start_time(channel_id)
        recorder_manager.recording_remove_filename(channel_id)
        recorder_manager.clear_tasks_process(channel_id)

    # 3) 사용자 시작이면 stop 해제
    if is_user_request:
        print(f"[DEBUG] 사용자 요청으로 녹화를 시작합니다. 채널: {channel_name}")
        recorder_manager.set_is_user_stopped(channel_id, False)

    # 4) 중지 요청 상태면 시작 거절
    if recorder_manager.get_is_user_stopped(channel_id):
        print(f"[DEBUG] {channel_name} 채널은 중지 요청 상태. 시작하지 않음.")
        return

    # 5) 이미 '진짜' 녹화 중이면 거절
    if recorder_manager.get_status_recording(channel_id) and procAlive(channel_id):
        print(f"[DEBUG] {channel_name} 채널은 이미 녹화 중입니다.")
        return

    # 6) 예약 상태 선표시: 토글 ON & stop 아님 → 예약 표기, 그 외는 예약 해제
    if channel.get("record_enabled", True) and not recorder_manager.get_is_user_stopped(channel_id):
        recorder_manager.set_status_reserved(channel_id, True)
    else:
        recorder_manager.set_status_reserved(channel_id, False)

    # 7) 스트림 재시작 루프 (감시/녹화/종료/재탐색)
    while True:
        proc = None
        stdout_task = None
        stderr_task = None
        stream_ended_flag = asyncio.Event()

        try:
            # 7-1) 루프마다 채널 최신화(토글 변경 반영)
            updated_channels = RecorderManager.getChannels() or []
            if not updated_channels:
                try:
                    updated_channels = loadChannels() or []
                    if updated_channels:
                        RecorderManager.setChannels(updated_channels)
                except Exception as _e:
                    print(f"[WARN] 메모리 복구 실패(무시): {_e}")
                    updated_channels = []

            updated_channel = next((c for c in updated_channels if c.get("id") == channel_id), None) or channel
            rec_enabled = bool(updated_channel.get("record_enabled", True))

            # 채널별로 post_cfg 생성
            post_cfg = buildPostCfgForChannel(updated_channel)

            post_queued = False  # 이번 세션에서 후처리 큐잉 여부

            # 7-2) 자동-재탐색 경로: 사용자 트리거가 아니고 토글 OFF면 감시 루프 종료
            if not is_user_request and not rec_enabled:
                print(f"[DEBUG] {channel_name} 자동녹화 OFF → 루프 종료(감시 안 함).")
                break

            # 7-3) 중지 버튼 감지 시 종료
            if recorder_manager.get_is_user_stopped(channel_id):
                print(f"[DEBUG] {channel_name} 중지 요청 감지. 루프 종료.")
                break

            # 7-4) 메타데이터 조회 (OPEN 여부)
            metadata = await getLiveMetadata(updated_channel, cookies)
            metadata = await waitStableChzzkMetadata(updated_channel, cookies, metadata)
            is_open = bool(metadata and (metadata.get("status") == "OPEN"))
            is_open_meta = bool(metadata and (metadata.get("status") == "OPEN")) # 힐트리거용 별도 변수

            live_id = (metadata or {}).get("liveId")
            now_ts  = time.time()

            # tail-guard: 분할녹화에서 방종 직후 같은 라이브로 되감기 재접속 금지
            tail_guard_active = False
            if splitRecordingMode and (autoStopInterval or 0) > 0:
                if (recent_live_block["live_id"]
                    and live_id == recent_live_block["live_id"]
                    and now_ts < recent_live_block["until"]):
                    tail_guard_active = True
                    is_open = False
                    remain = int(recent_live_block['until'] - now_ts)
                    print(f"[DEBUG] {channel_name} tail-guard ON for liveId={live_id} ({remain}s left) → treat as CLOSED")
                elif recent_live_block["live_id"] and live_id and live_id != recent_live_block["live_id"]:
                    # 새 라이브 시작 → 쿨다운 즉시 해제
                    recent_live_block["until"] = 0.0

            # 메타가 애매할 때만 프로브, tail-guard 중엔 프로브 금지
            if not is_open and not tail_guard_active:
                ok = await probeStreamBounded(updated_channel, cookies, timeout_sec=6)
                _reason = updated_channel.get("_last_probe_reason")

                if ok:
                    print(f"[DEBUG] {channel_name} OPEN fallback: HLS probe 통과 → 진행")

                    metadata = await waitStableChzzkMetadata(
                        updated_channel,
                        cookies,
                        None,
                        retries=5,
                        delay=1.5
                    )

                    if not metadata or metadata.get("status") != "OPEN" or isUnstableChzzkIdentityMetadata(metadata):
                        now_dt = datetime.now()
                        metadata = {
                            "status": "OPEN",
                            "liveTitle": updated_channel.get("name") or "녹화",
                            "start_time": now_dt.strftime("%Y-%m-%d"),
                            "broadcast_time": now_dt.strftime("%y%m%d_%H%M%S"),
                            "_probe_fallback": True,
                        }

                    if metadata:
                        record_quality_for_name, frame_rate_for_name = normalizeQualityNameParts(updated_channel, metadata)
                        metadata["record_quality"] = record_quality_for_name
                        metadata["frame_rate"] = frame_rate_for_name

                    is_open = True
                else:
                    if _reason == "skip":
                        print(f"[INFO] {channel_name} HLS probe 스킵(슬롯 바쁨) → CLOSED 유지")
                    elif _reason == "timeout":
                        print(f"[WARN] {channel_name} HLS probe 타임아웃 → CLOSED 유지")
                    else:
                        print(f"[DEBUG] {channel_name} HLS probe 실패 → CLOSED 유지")             

            # 예약+OPEN인데 실제 녹화 프로세스가 없으면 해제
            if is_open_meta and not tail_guard_active and recorder_manager.get_status_reserved(channel_id):
                p = recorder_manager.get_tasks_process(channel_id)
                age = recorder_manager.guard_get_start_age(channel_id)  # None이면 가드가 없음

                stalled = float(age) if age is not None else 0.0

                # 가드가 존재할 때만 오래된 잠금 해제
                if (not p) and (age is not None) and (stalled > 45.0):
                    print(f"[HEAL] {channel_name} reserved+OPEN(meta) but no process for {int(stalled)}s → guard_release_start() & retry")
                    recorder_manager.guard_release_start(channel_id)

            if not is_open:

                print(f"[DEBUG] {channel_name} 방송 종료/미오픈 상태.")
                recorder_manager.set_status_recording(channel_id, False)

                if rec_enabled:
                    # 토글 ON → 예약 유지(감시)
                    recorder_manager.set_status_reserved(channel_id, True)
                    new_state = "예약녹화 중"
                    if last_notified_state.get(channel_id) != new_state:
                        last_notified_state[channel_id] = new_state

                    _base = max(10, int(recheckInterval))
                    _jit  = max(1, int(_base * JITTER_RATIO))
                    _seed = int(hashlib.blake2b(str(channel_id).encode(), digest_size=4).hexdigest(), 16)

                    _phase = _JITTER_PHASE.get(channel_id)
                    if _phase is None:
                        _phase = (_seed % (2 * _jit + 1)) - _jit   # [-_jit, +_jit]
                        _JITTER_PHASE[channel_id] = _phase

                    now = time.monotonic()
                    period = _base
                    k = math.floor((now - (START_MONO + _phase)) / period) + 1
                    next_time = START_MONO + _phase + k * period
                    _sleep = max(1, int(next_time - now))

                    print(f"[DEBUG] {channel_name} 예약 유지. {_sleep}s 후 재탐색(초기위상={_phase:+d}s, 주기={period}s).")
                    await asyncio.sleep(_sleep)

                    # 재탐색 직전 OFF/STOP 재확인
                    updated_channels = RecorderManager.getChannels() or []
                    updated_channel = next((c for c in updated_channels if c.get("id") == channel_id), None) or channel
                    if not updated_channel.get("record_enabled", True):
                        print(f"[DEBUG] {channel_name} 토글 OFF 감지. 루프 종료.")
                        break
                    if recorder_manager.get_is_user_stopped(channel_id):
                        print(f"[DEBUG] {channel_name} 중지 요청. 루프 종료.")
                        break
                    continue
                else:
                    # 토글 OFF → 1회성. 감시 유지 안 함.
                    recorder_manager.set_status_reserved(channel_id, False)
                    try:
                        updated_channel["status"] = "대기 중"
                    except Exception:
                        pass
                    print(f"[DEBUG] {channel_name} 토글 OFF이므로 '대기 중' 전환 후 루프 종료.")
                    break

            # 7-5) OPEN 상태 → '같이보기만 녹화' 옵션 + 태그 제외 필터
            if updated_channel.get("recordWatchParty", False):
                # 7-5-1) 같이보기 미활성 → 스킵
                if metadata.get("watchPartyNo") is None:
                    print(f"[DEBUG] {channel_name} 같이보기 미활성 → 녹화 스킵.")
                    recorder_manager.set_status_recording(channel_id, False)

                    if rec_enabled:
                        recorder_manager.set_status_reserved(channel_id, True)
                        await asyncio.sleep(recheckInterval)
                        continue
                    else:
                        recorder_manager.set_status_reserved(channel_id, False)
                        print(f"[DEBUG] {channel_name} 토글 OFF(1회성). '대기 중' 전환 후 루프 종료.")
                        break

                # 7-5-2) 같이보기 활성 + 녹화 제외할 태그 매칭 시 스킵
                exclude_list = (updated_channel.get("watchPartyExcludeTags") or [])
                if exclude_list:
                    tag = (metadata.get("watchPartyTag") or "")
                    lt = tag.lower()
                    hit = next((kw for kw in exclude_list
                                if (kw or "").strip() and (kw.lower() in lt)), None)
                    if hit is not None:
                        print(f"[DEBUG] {channel_name} [같이보기만 녹화에서 제외 태그 확인] 태그에 '{hit}' 포함 → 녹화 스킵.")
                        recorder_manager.set_status_recording(channel_id, False)
                        if rec_enabled:
                            recorder_manager.set_status_reserved(channel_id, True)
                            await asyncio.sleep(recheckInterval)
                            continue
                        else:
                            recorder_manager.set_status_reserved(channel_id, False)
                            print(f"[DEBUG] {channel_name} 토글 OFF(1회성). '대기 중' 전환 후 루프 종료.")
                            break

            # 7-6) 후처리 플래그 초기화 & 같이보기 알림 플래그 리셋
            recorder_manager.recording_remove_postproc(channel_id)
            if metadata.get("watchPartyNo") is not None:
                recorder_manager.reset_watch_party_off_notified(channel_id)

            # 7-7) 파일명 구성
            recording_time = datetime.now().strftime("%y%m%d_%H%M%S")

            if isUnstableChzzkIdentityMetadata(metadata) or isUnresolvedQualityMetadata(metadata):
                fresh_metadata = await waitStableChzzkMetadata(
                    updated_channel,
                    cookies,
                    None,
                    retries=5,
                    delay=1.0
                )

                if fresh_metadata and fresh_metadata.get("status") == "OPEN":
                    metadata = fresh_metadata

            metadata = applySafeChzzkFilenameMetadata(updated_channel, metadata, recording_time)

            record_quality_for_name, frame_rate_for_name = normalizeQualityNameParts(
                updated_channel,
                metadata
            )

            live_title = metadata.get("liveTitle", "녹화")
            safe_live_title = sanitizeFilenamePart(live_title, "녹화", FILENAME_LIVE_TITLE_LIMIT)
            safe_channel_name = sanitizeFilenamePart(channel_name, channel_id, FILENAME_CHANNEL_NAME_LIMIT)
            start_time = metadata.get("start_time", "UnknownTime")
            broadcast_time = metadata.get("broadcast_time") or recording_time
            file_extension = updated_channel.get("extension", ".ts")

            filename = filenamePattern.format(
                recording_time=recording_time,
                start_time=start_time,
                broadcast_time=broadcast_time,
                safe_live_title=safe_live_title,
                channel_name=safe_channel_name,
                record_quality=sanitizeFilenamePart(record_quality_for_name, "best", 20),
                frame_rate=sanitizeFilenamePart(frame_rate_for_name, "", 10),
                file_extension=file_extension,
            )

            filename = sanitizeGeneratedFilename(filename, file_extension)

            output_dir_abs_path = os.path.join(base_directory, updated_channel.get("output_dir", "./output"))

            checkDiskSpaceLow(
                output_dir_abs_path,
                channel_id=channel_id,
                channel_name=channel_name
            )

            unique_filename_ = uniqueFilename(output_dir_abs_path, filename, add_suffix=True)
            output_path = os.path.join(output_dir_abs_path, unique_filename_)
            updated_channel["output_path"] = output_path

            try:
                splitOverlapSec_cfg = int((loadConfig() or {}).get("splitOverlapSec", 0) or 0)
            except Exception:
                splitOverlapSec_cfg = 0
            splitOverlapSec_cfg = max(0, min(30, splitOverlapSec_cfg))

            # 분할 ON이면 일반 경로 스킵하고 파이프라인으로 직행
            if splitRecordingMode and (autoStopInterval or 0) > 0:
                base_dir  = os.path.dirname(output_path)
                base_name = os.path.basename(output_path)
                base_noext, ext = os.path.splitext(base_name)

                # 세션 폴더 경로만 미리 산출 (폴더 생성은 runSegLoop 내부에서 수행)
                session_dir = uniqueSessionDir(base_dir, base_noext)

                print(f"[PIPE] split path engaged: interval={autoStopInterval}s overlap={splitOverlapSec_cfg}s")
                print(f"[PIPE] session_dir={session_dir}")

                seg_cancelled = False
                inner_done = False      

                try:
                    await asyncio.shield(runSegLoop(
                        updated_channel, channel_id, channel_name, metadata, cookies,
                        segmentSec=autoStopInterval,
                        splitOverlapSec=splitOverlapSec_cfg,
                        autoPostProcessing=autoPostProcessing,
                        post_cfg=post_cfg or {},
                        base_noext=base_noext,
                        out_dir=session_dir
                    ))

                    inner_done = True 

                except asyncio.CancelledError:
                    # 세그먼트 롤오버(자동 분할 전환) 상황 → tail-guard 금지
                    seg_cancelled = True
                    print(f"[DEBUG] {channel_name} split loop shielded; outer cancel occurred (rollover).")
                    await asyncio.sleep(0.2)

                finally:

                    had_segments = False
                    try:
                        if os.path.isdir(session_dir):
                            # 분할 세그먼트는 언제나 .ts
                            ts_ok = bool(listNumericSegments(session_dir, base_noext, ".ts"))
                            names = os.listdir(session_dir)
                            fixed_ok = any(
                                n.startswith(f"fixed_{base_noext}") and n.lower().endswith(".mp4")
                                for n in names
                            )
                            had_segments = ts_ok or fixed_ok
                    except Exception:
                        pass

                    # 분할 전용 자동후처리 플래그를 확인해서 OFF면 큐잉하지 않음
                    prefs_outer = resolveRecordingPrefs()
                    effective_auto_pp = bool(prefs_outer.get("splitPostProcessing", prefs_outer.get("autoPostProcessing", True)))

                    if had_segments and effective_auto_pp:
                        try:
                            patt = os.path.join(session_dir, f"{base_noext}_%03d.ts")
                            ok = await queueBatchPattern(channel_id, patt)
                            print(f"[BATCH][OUTER] queued via pattern: {ok}")
                        except Exception:
                            pass
                    else:
                        if had_segments:
                            print("[BATCH][OUTER] splitPostProcessing=False → 큐잉 생략")

                    # 롤오버/방종 분기 재정의
                    cooldown = TAIL_GUARD_COOLDOWN_SEC
                    recent_live_block["live_id"] = (metadata or {}).get("liveId")
                    recent_live_block["until"]   = time.time() + cooldown
                    print(f"[DEBUG] {channel_name} tail-guard ARMED (split finalize, unconditional) {cooldown}s")

                    # 빈 폴더 정리(공통)
                    with contextlib.suppress(Exception):
                        if os.path.isdir(session_dir) and not os.listdir(session_dir):
                            os.rmdir(session_dir)

                # 분할 경로는 다음 감시 루프로
                continue


            # 7-8) 녹화 명령 생성
            cmd = buildCommand(
                updated_channel, metadata, recording_time, cookies, filenamePattern, plugin_type, timemachine_time_shift
            )

            if not cmd or not isinstance(cmd, (list, tuple)):
                print(f"[ERROR] {channel_name} 명령 생성 실패: {cmd}")

                notifyEvent(
                    "record_start_failed",
                    "녹화 시작 실패",
                    "녹화 명령을 생성하지 못했습니다.",
                    channel_id=channel_id,
                    channel_name=channel_name,
                    detail=str(cmd),
                    severity="error"
                )

                recorder_manager.set_status_recording(channel_id, False)
                recorder_manager.recording_remove_start_time(channel_id)
                recorder_manager.recording_remove_filename(channel_id)

                # 토글 ON이면 예약 유지하고 재시도, OFF면 종료
                if rec_enabled:
                    recorder_manager.set_status_reserved(channel_id, True)
                    await asyncio.sleep(recheckInterval)
                    continue
                else:
                    recorder_manager.set_status_reserved(channel_id, False)
                    print(f"[DEBUG] {channel_name} 토글 OFF이므로 명령 실패 후 루프 종료.")
                    break

            try:
                print(f"[DEBUG] {channel_name} cmd: " + formatChzzkCmdForLog(cmd))
            except Exception:
                print(f"[DEBUG] {channel_name} cmd: <명령 출력 중 오류>")

            # 7-9) 프로세스 실행
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                if hasattr(os, "setsid"):
                    kwargs["preexec_fn"] = os.setsid

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )

            recorder_manager.recording_set_filename(channel_id, output_path)
            recorder_manager.set_tasks_process(channel_id, proc)

            # 7-10) 프로세스 핸들/파일명 세팅 완료 후 상태 승격
            recorder_manager.set_status_recording(channel_id, True)
            recorder_manager.set_status_reserved(channel_id, False)
            recorder_manager.recording_set_start_time(channel_id)

            # 7-11) 알림
            new_state = "녹화 중"
            if last_notified_state.get(channel_id) != new_state:
                notifyEvent(
                    "record_started",
                    "녹화 시작",
                    "녹화가 시작되었습니다.",
                    channel_id=channel_id,
                    channel_name=channel_name,
                    severity="info"
                )

                last_notified_state[channel_id] = new_state

            # 7-12) 출력 리더 태스크
            stream_ended_flag = asyncio.Event()
            transient_end_flag = asyncio.Event()  
            stdout_task = asyncio.create_task(read_stdout(proc, channel_id, stream_ended_flag))
            stderr_task = asyncio.create_task(read_stderr(proc, channel_id, transient_end_flag))

            # 7-12) 일반 대기
            await proc.wait()

            # 끝난 즉시 후처리
            current_output_path = updated_channel.get("output_path")
            if (autoPostProcessing and current_output_path and os.path.exists(current_output_path)
                    and not post_queued):

                # 파일 지문 기반 중복 가드 (같은 파일이면 재큐잉 금지)
                if not recorder_manager.postproc_try_acquire_source(current_output_path):
                    print(f"[POST][SKIP] 동일 파일 지문 중복 방지: {os.path.basename(current_output_path)}")
                else:
                    # 인플라이트 가드 확인
                    ok = recorder_manager.recording_add_postproc(channel_id)
                    post_queued = True  # 동일 프레임의 다른 분기에서 재시도 방지

                    if ok:
                        print(f"[POST] start  {channel_name} src={os.path.basename(current_output_path)}")
                        task = asyncio.create_task(
                            handlePostProcessing(current_output_path, channel_id, channel_name, post_cfg)
                        )
                        trackPostprocessTask(task)
                    else:
                        print(f"[POST] skip   {channel_name} (already inflight)")

            # 종료 즉시 알림
            try:
                if not recorder_manager.get_is_user_stopped(channel_id):
                    dur = recorder_manager.get_recording_duration(channel_id)  # "HH:MM:SS"
                    msg = (
                        f"<b>{channel_name}</b> 녹화가 <b>종료</b>되었습니다. 후처리를 시작합니다."
                        + (f" (녹화시간 {dur})" if dur else "")
                        + f"\n<code>{os.path.basename(current_output_path)}</code>"
                    )
                    if last_notified_state.get(channel_id) != "녹화종료":
                        notifyEvent(
                            "record_finished",
                            "녹화 종료",
                            f"녹화가 종료되었습니다." + (f" 녹화시간: {dur}" if dur else ""),
                            channel_id=channel_id,
                            channel_name=channel_name,
                            detail=os.path.basename(current_output_path or ""),
                            severity="info"
                        )

                        last_notified_state[channel_id] = "녹화종료"

            except Exception as _e:
                print(f"[WARN] finalize telegram failed: {_e}")

            # 무조건 stale 경로 제거
            with contextlib.suppress(Exception):
                updated_channel.pop("output_path", None)

            # 방종 꼬리 쿨다운
            if stream_ended_flag.is_set() and not recorder_manager.get_is_user_stopped(channel_id):
                # 1) stderr에서 일시장애로 표기되었으면 메타/HLS 재확인 후 쿨다운 없이 재시작
                if 'transient_end_flag' in locals() and transient_end_flag.is_set():
                    try:
                        meta2 = await getLiveMetadata(updated_channel, cookies)
                        if meta2 and meta2.get("status") == "OPEN":
                            # 실제 HLS도 열려있는지 빠른 프로브
                            ok = await probeStreamBounded(updated_channel, cookies, timeout_sec=6)
                            if ok:
                                # 상태만 정리하고 즉시 다음 루프로(명령 재생성 → 재연결)
                                recorder_manager.set_status_recording(channel_id, False)
                                recorder_manager.recording_remove_start_time(channel_id)
                                recorder_manager.recording_remove_filename(channel_id)
                                print(f"[DEBUG] {channel_name} 일시적 HLS 에러 감지 → 쿨다운 없이 즉시 재시작.")
                                await asyncio.sleep(1.0)
                                continue
                    except Exception as _e:
                        print(f"[WARN] transient restart check failed: {_e}")

                # 2) 여기로 오면 '진짜 종료'로 간주 → 기존 로직 수행(쿨다운/예약 유지)
                recorder_manager.set_status_recording(channel_id, False)

                # 최신 채널 토글 재확인
                latest = RecorderManager.getChannels() or []
                latest_ch = next((c for c in latest if c.get("id") == channel_id), None) or updated_channel
                rec_enabled_now = bool(latest_ch.get("record_enabled", True))

                if not rec_enabled_now:
                    # 즉시 예약 해제 + 상태 문자열 힌트
                    recorder_manager.set_status_reserved(channel_id, False)
                    try:
                        latest_ch["status"] = "대기 중"
                    except Exception:
                        pass
                    stream_ended_flag.clear()
                    break  # OFF이므로 다음 회차 감시로 가지 않고 즉시 루프 종료

                # ON이면 기존처럼 예약 유지 + 쿨다운
                recorder_manager.set_status_reserved(channel_id, True)
                print("[DEBUG] Stream ended detected. Mark reserved immediately; cooldown before re-scan.")
                stream_ended_flag.clear()
                await asyncio.sleep(TAIL_GUARD_COOLDOWN_SEC)


            # 리더 태스크 마무리
            await stdout_task
            if stderr_task:
                await stderr_task

            # 7-13) 종료 후 분기: 사용자 Stop / 토글 ON / 토글 OFF
            updated_channels = RecorderManager.getChannels() or []
            updated_channel = next((c for c in updated_channels if c.get("id") == channel_id), None) or channel
            rec_enabled = bool(updated_channel.get("record_enabled", True))

            if recorder_manager.get_is_user_stopped(channel_id):
                recorder_manager.set_status_reserved(channel_id, False)
                print(f"[DEBUG] {channel_name} 사용자 중지로 루프 종료.")
                break
            else:
                if rec_enabled:
                    recorder_manager.set_status_reserved(channel_id, True)

                    _base = max(10, int(recheckInterval))
                    _jit  = max(1, int(_base * JITTER_RATIO))
                    _seed = int(hashlib.blake2b(str(channel_id).encode(), digest_size=4).hexdigest(), 16)

                    _phase = _JITTER_PHASE.get(channel_id)
                    if _phase is None:
                        _phase = (_seed % (2 * _jit + 1)) - _jit
                        _JITTER_PHASE[channel_id] = _phase

                    now = time.monotonic()
                    period = _base
                    k = math.floor((now - (START_MONO + _phase)) / period) + 1
                    next_time = START_MONO + _phase + k * period
                    _sleep = max(1, int(next_time - now))

                    print(f"[DEBUG] {channel_name} 종료 → 예약 유지. {_sleep}s 후 재탐색(초기위상={_phase:+d}s, 주기={period}s).")
                    await asyncio.sleep(_sleep)

                    # 재탐색 직전 OFF/STOP 재확인
                    updated_channels = RecorderManager.getChannels() or []
                    updated_channel = next((c for c in updated_channels if c.get("id") == channel_id), None) or channel
                    if not updated_channel.get("record_enabled", True):
                        print(f"[DEBUG] {channel_name} OFF 감지. 루프 종료.")
                        break
                    if recorder_manager.get_is_user_stopped(channel_id):
                        print(f"[DEBUG] {channel_name} 사용자 중지 요청. 루프 종료.")
                        break

                    # 다음 루프
                    continue
                else:
                    # 1회성: 예약 없이 종료
                    recorder_manager.set_status_reserved(channel_id, False)
                    print(f"[DEBUG] {channel_name} 토글 OFF(1회성). 루프 종료.")
                    break

        except asyncio.CancelledError:
            print(f"[DEBUG] {channel_name} 녹화 태스크 중지")

            # 프로세스 소프트 종료
            if proc and proc.returncode is None:
                await gracefulTerminate(proc)

            current_output_path = None
            try:
                current_output_path = output_path  # noqa: F821
            except Exception:
                pass
            if not current_output_path:
                try:
                    current_output_path = updated_channel.get("output_path")
                except Exception:
                    current_output_path = None

            # 분할 모드라면 CancelledError도 세그먼트 전환으로 간주
            if splitRecordingMode and (autoStopInterval or 0) > 0:
                print("[SPLIT] skip single-file postprocess on cancel; batch will run in finalize.")
                # 세션 상태만 정리
                try:
                    recorder_manager.set_status_recording(channel_id, False)
                    recorder_manager.recording_remove_start_time(channel_id)
                    recorder_manager.recording_remove_filename(channel_id)
                    recorder_manager.clear_tasks_process(channel_id)
                    recorder_manager.set_status_reserved(channel_id, True)
                except Exception:
                    pass

                proc = None; stdout_task = None; stderr_task = None

                if recorder_manager.get_is_user_stopped(channel_id):
                    print(f"[DEBUG] {channel_name} 사용자 중지 감지 → 분할 경로 즉시 종료.")
                    break
                if not bool(updated_channel.get("record_enabled", True)):
                    print(f"[DEBUG] {channel_name} 토글 OFF 감지 → 종료.")
                    break

                meta2 = await getLiveMetadata(updated_channel, cookies)
                still_open = bool(meta2 and meta2.get("status") == "OPEN")

                if still_open:
                    # API가 OPEN이어도 즉시 HLS 프로브로 실제 상태 확인
                    ok = await probeStreamBounded(updated_channel, cookies, timeout_sec=6)
                    if not ok:
                        # HLS는 닫힘 → 재세션 금지(테일가드 무장)
                        cooldown = TAIL_GUARD_COOLDOWN_SEC
                        recent_live_block["live_id"] = (meta2 or {}).get("liveId") or (metadata or {}).get("liveId")
                        recent_live_block["until"]   = time.time() + cooldown
                        print(f"[DEBUG] {channel_name} cancel→meta OPEN but HLS CLOSED → tail-guard {cooldown}s, stop roll-over.")
                        break
                else:
                    # meta가 CLOSED거나 조회 실패여도 테일가드 
                    cooldown = TAIL_GUARD_COOLDOWN_SEC
                    recent_live_block["live_id"] = (meta2 or {}).get("liveId") or (metadata or {}).get("liveId")
                    recent_live_block["until"]   = time.time() + cooldown
                    print(f"[DEBUG] {channel_name} cancel→meta CLOSED → tail-guard {cooldown}s, stop roll-over.")
                    break


                # 여기까지 왔으면 '진짜 OPEN' → 다음 세그먼트로 진행
                print(f"[DEBUG] {channel_name} cancel을 분할 롤오버로 간주하고 다음 세그먼트로 계속.")
                await asyncio.sleep(0.5)
                continue

            # 분할 모드가 아닐 때: 실제 녹화 세션이었던 경우에만 후처리 큐잉
            else:
                if (autoPostProcessing and proc is not None and recorder_manager.get_status_recording(channel_id) 
                    and current_output_path and os.path.exists(current_output_path) and not post_queued):

                    # ▼ 파일 지문 기반 중복 가드
                    if not recorder_manager.postproc_try_acquire_source(current_output_path):
                        print(f"[POST][SKIP] 동일 파일 지문 중복 방지: {os.path.basename(current_output_path)} (cancelled)")
                    else:
                        ok = False
                        try:
                            ok = recorder_manager.recording_add_postproc(channel_id)
                        except Exception:
                            ok = False
                        post_queued = True  # 동일 프레임 재시도 방지

                        if ok:
                            print(f"[POST] start  {channel_name} src={os.path.basename(current_output_path)} (cancelled)")
                            task = asyncio.create_task(
                                handlePostProcessing(current_output_path, channel_id, channel_name, post_cfg)
                            )
                            trackPostprocessTask(task)

                        else:
                            print(f"[POST] skip   {channel_name} (already inflight; cancelled)")

                # 어떤 경우든(큐잉 여부와 무관) stale 경로는 즉시 정리
                with contextlib.suppress(Exception):
                    updated_channel.pop("output_path", None)

            for t in (stdout_task, stderr_task):
                if t:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(t, timeout=1.5)
            for t in (stdout_task, stderr_task):
                if t and not t.done():
                    t.cancel()
                    with contextlib.suppress(Exception):
                        await t

            stop_req = recorder_manager.get_is_user_stopped(channel_id)
            auto_cfg = (loadConfig() or {}).get("autoRecordingMode", False)
            rec_enabled_dbg = bool(channel.get("record_enabled", True))

            print(
                f"[REC-END] {channel_name} auto={auto_cfg} "
                f"enabled={rec_enabled_dbg} stop_requested={stop_req}"
            )

            try:
                recorder_manager.set_status_recording(channel_id, False)
                recorder_manager.recording_remove_start_time(channel_id)
                recorder_manager.recording_remove_filename(channel_id)
                recorder_manager.clear_tasks_process(channel_id)
            except Exception as _e:
                print(f"[WARN] end-of-session cleanup failed: {_e}")

            if not stop_req:
                recorder_manager.set_is_user_stopped(channel_id, False)

            print(f"[DEBUG] {channel_name} 세션 종료. FSM 후속 전이.")

            try:
                # 최신 토글/사용자중지 상태 재조회
                stop_req = recorder_manager.get_is_user_stopped(channel_id)
                latest = RecorderManager.getChannels() or []
                latest_ch = next((c for c in latest if c.get("id") == channel_id), None) or channel
                rec_enabled_now = bool(latest_ch.get("record_enabled", True))

                if stop_req:
                    # 사용자 중지면 예약 해제하고 루프 종료
                    recorder_manager.set_status_reserved(channel_id, False)
                    print(f"[DEBUG] {channel_name} 사용자 중지 감지 → 루프 종료.")
                    break

                if not rec_enabled_now:
                    # 토글 OFF면 예약 해제하고 루프 종료
                    recorder_manager.set_status_reserved(channel_id, False)
                    print(f"[DEBUG] {channel_name} 토글 OFF → 루프 종료.")
                    break

                # 이 외에는 예약 유지 후 다음 회차를 위해 재탐색 진입
                recorder_manager.set_status_reserved(channel_id, True)

                _base = max(10, int(recheckInterval))
                _jit  = max(1, int(_base * JITTER_RATIO))
                _seed = int(hashlib.blake2b(str(channel_id).encode(), digest_size=4).hexdigest(), 16)

                _phase = _JITTER_PHASE.get(channel_id)
                if _phase is None:
                    _phase = (_seed % (2 * _jit + 1)) - _jit
                    _JITTER_PHASE[channel_id] = _phase

                now = time.monotonic()
                period = _base
                k = math.floor((now - (START_MONO + _phase)) / period) + 1
                next_time = START_MONO + _phase + k * period
                _sleep = max(1, int(next_time - now))

                print(f"[DEBUG] {channel_name} 종료 → 예약 유지. {_sleep}s 후 재탐색(초기위상={_phase:+d}s, 주기={period}s).")
                await asyncio.sleep(_sleep)

                continue  # 다음 루프로 진입 

            except Exception as _e:
                # 예약 유지 후 재탐색 재시도
                print(f"[WARN] {channel_name} 종료 분기 처리 실패({_e}) → {recheckInterval}s 후 재탐색 재시도")
                try:
                    recorder_manager.set_status_reserved(channel_id, True)
                except Exception:
                    pass
                await asyncio.sleep(recheckInterval)
                continue

        finally:

            if proc and proc.returncode is None:
                await gracefulTerminate(proc, timeout=3.0)

            for t in (stdout_task, stderr_task):
                if t and not t.done():
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(t, timeout=1.0)
                if t and not t.done():
                    t.cancel()
                    with contextlib.suppress(Exception):
                        await t

    print(f"[DEBUG] {channel_name} 녹화 루프가 종료됩니다.")
    recorder_manager.set_status_recording(channel_id, False)
    recorder_manager.clear_tasks_process(channel_id)
    recorder_manager.recording_remove_start_time(channel_id)
    recorder_manager.recording_remove_filename(channel_id)

    if recorder_manager.get_is_user_stopped(channel_id):
        # 사용자 요청 녹화중지시 재탐색하지 않고 완전 대기
        recorder_manager.set_status_reserved(channel_id, False)
        try:
            channel["status"] = "대기 중"   
        except Exception:
            pass
        print(f"[DEBUG] {channel_name} 사용자 요청으로 완전히 중지.")

    else:
        # 자연 종료 케이스: 다음 방송을 대비해 예약 상태로 복귀
        recorder_manager.set_is_user_stopped(channel_id, False)

        latest = RecorderManager.getChannels() or []
        latest_ch = next((c for c in latest if c.get("id") == channel_id), None) or channel
        rec_enabled_now = bool(latest_ch.get("record_enabled", True))

        if rec_enabled_now:
            recorder_manager.set_status_reserved(channel_id, True)
            try:
                latest_ch["status"] = "예약녹화 중"
            except Exception:
                pass
        else:
            recorder_manager.set_status_reserved(channel_id, False)
            try:
                latest_ch["status"] = "대기 중"
            except Exception:
                pass


# 치지직용 녹화중지 함수
async def chzzkStopRecording(channel_id):
    print(f"[DEBUG] chzzkStopRecording 시작 - 채널 ID: {channel_id}")

    # 이미 STOP이면 재진입 금지
    if recorder_manager.get_is_user_stopped(channel_id):
        print(f"[DEBUG] {channel_id} 중복 stop 요청 → 무시")
        return

    recorder_manager.set_is_user_stopped(channel_id, True)
