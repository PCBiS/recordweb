import subprocess
import os
import time
import json
import sys
import asyncio
import traceback
import threading
import re
import uvicorn
import requests
from datetime import datetime
from typing import Optional, Any
from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager, suppress

from module.data_manager import (
    RecorderManager, loadAccount, saveAccount, loadCookies, saveCookies,
    getChzzkCookies, getCimeCookies,
    loadChannels, saveChannels, loadConfig, saveConfig,
    saveNotification, loadNotification, notifyEvent, last_notified_state,
    CONFIG_PATH, CHANNELS_PATH, COOKIE_PATH, LOGIN_PATH, getFFmpeg, 
    getStreamlink, toBool
)

from module.meta_cache import (
    ensure as mc_ensure, refreshLoop as mc_refreshLoop, 
    getMetadataCached, getThumbnailsCached, refreshOneChannel
)

from module.recording_adapter import fetchMetadata, startSession
from module.channel_fsm import ChannelFsm
from module.config_validator import validateRuntimeEnvironment
from module.runtime_log import setupAppLogging, recordException
from module.live_recorder import queueBatchLast, queueBatchPattern
from module.cookie_checker import checkChzzkCookie
from module.cime_recorder import getCimeMetadata

# 윈도우면 콘솔 코드페이지도 UTF-8로 맞춰주면 더 안전
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


# 표준 출력/에러를 UTF-8로 고정
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


# 파일 상대경로 기준
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) 
STATIC_DIR = os.path.join(BASE_DIR, "templates", "static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Starlette/FastAPI 최신 버전 TemplateResponse 호출 호환 패치
def patchTemplateResponseCompat(templates_obj):
    original_template_response = templates_obj.TemplateResponse

    def compatibleTemplateResponse(*args, **kwargs):
        if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], dict):
            template_name = args[0]
            context = args[1]
            request = context.get('request')
            if request is not None:
                return original_template_response(request, template_name, context, *args[2:], **kwargs)

        return original_template_response(*args, **kwargs)

    templates_obj.TemplateResponse = compatibleTemplateResponse

patchTemplateResponseCompat(templates)

# 전역 락 선언
thread_lock = threading.Lock()  # 동기 함수에서 사용할 락
channels_lock = asyncio.Lock() # 채널 변수보호를 위한 락

# RecorderManager 클래스 인스턴스 생성
recorder_manager = RecorderManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[INFO] 애플리케이션이 시작됩니다.")
    meta_task = None
    seed_task = None

    app.state.bg_tasks = set()

    try:
        # 1) 필수 경로/실행파일 체크
        try:
            checkRequiredPaths()
        except Exception as e:
            print(f"[FATAL] 필수 경로/실행파일 점검 실패: {e}")
            raise

        # 2) 채널/락/설정 app.state에 주입
        channels = get_channels() or []

        # 기존 유튜브 라이브 녹화 채널이 남아 있으면 씨미로 보정
        changed = False
        for ch in channels:
            if (ch.get("platform") or "").lower() == "youtube":
                ch["platform"] = "cime"
                changed = True
            if (ch.get("platform") or "").lower() == "cime":
                if ch.get("extension") != ".mp4":
                    ch["extension"] = ".mp4"
                    changed = True
                if ch.get("recordWatchParty"):
                    ch["recordWatchParty"] = False
                    changed = True
        if changed:
            await asyncio.to_thread(saveChannels, channels)

        app.state.channels = channels
        app.state.fsm = ChannelFsm()
        app.state.channels_lock = channels_lock
        app.state.config = get_config()
        app.state.cookies = get_cookies()

        # 유튜브 라이브 녹화 기능은 씨미로 대체되어 youtube_cookie_path는 사용하지 않습니다.
        RecorderManager.setChannelsRef(app.state.channels)
        RecorderManager.setChannelsLockRef(app.state.channels_lock)

        try:
            RecorderManager.setChannels(channels)
        except Exception as e:
            print(f"[WARN] RecorderManager.setChannels 실패(계속 진행): {e}")

        # 3) 채널 상태 초기화
        try:
            await initChannelStates(app.state.channels)
        except Exception as e:
            print(f"[ERROR] 채널 상태 초기화 실패: {e}")
            raise

        # 4) 메타 캐시 준비
        mc_ensure(app)

        # 시드 메타 태스크
        try:
            seed_task = asyncio.create_task(_seedMetadataOnce(app))
        except Exception as e:
            print(f"[WARN] seed schedule failed: {e}")

        app.state.meta_fetcher = _fetchMetaWorker
        app.state.save_debounced = DebouncedSaver(app, delay=1.2)

        meta_task = asyncio.create_task(
            mc_refreshLoop(app, app.state.meta_fetcher, app.state.save_debounced, channels_lock)
        )

        # 부팅 시 자동녹화 모드면 딱 한 번만 WATCHING 진입
        try:
            if (get_config() or {}).get("autoRecordingMode", False):
                print("[DEBUG] 자동 녹화 모드: 부팅 시 한 번만 WATCHING 진입")
                await app.state.fsm.startAllWatching()
        except Exception as e:
            print(f"[WARN] 자동녹화 초기 진입 실패(무시): {e}")

        yield

    finally:
        # 1) 메타/시드 태스크 정리
        if meta_task and not meta_task.done():
            meta_task.cancel()
            with suppress(asyncio.CancelledError):
                await meta_task

        if seed_task and not seed_task.done():
            seed_task.cancel()

            with suppress(asyncio.CancelledError):
                await seed_task

        # BG 태스크 일괄 정리
        try:
            pending = list(app.state.bg_tasks)
            for t in pending:
                t.cancel()
            for t in pending:
                with suppress(Exception):
                    await t

        except Exception as e:
            print(f"[WARN] bg_tasks cleanup error: {e}")

        # 종료 직전 채널 저장 강제 플러시
        try:
            if hasattr(app.state, "save_debounced") and app.state.save_debounced:
                await app.state.save_debounced.flush()
        except Exception as e:
            print(f"[WARN] save_debounced.flush failed: {e}")

        # 2) httpx AsyncClient 정리 (핵심)
        try:
            from module.live_recorder import closeHttpxClient
            await closeHttpxClient()
        except Exception as e:
            print(f"[WARN] closeHttpxClient 실패(무시): {e}")

        print("[INFO] 애플리케이션이 종료됩니다.")


# FastAPI 앱 생성
app = FastAPI(lifespan=lifespan)

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    print(f"[WARN] Static dir not found: {STATIC_DIR}")


# 프로그램 첫 실행 시 FFmpeg, Streamlink 경로 확인
def checkRequiredPaths():
    ffmpeg_path = getFFmpeg()
    streamlink_path = getStreamlink()

    if not ffmpeg_path:
        print("[ERROR] FFmpeg 경로가 설정되지 않았습니다. 프로그램을 종료합니다.")
    elif not streamlink_path:
        print("[ERROR] Streamlink 경로가 설정되지 않았습니다. 프로그램을 종료합니다.")
    else:
        print("[INFO] 필수 프로그램 경로 확인 완료.")
        return


# 상태 조회용 파일명 헬퍼(읽기 전용)
def _getRecFilename(cid: str):
    try:
        return RecorderManager.recording_filename.get(cid)
    except Exception:
        return None

def _getRecStartTime(cid: str):
    try:
        ts = RecorderManager.recording_start_time.get(cid)
        return datetime.fromtimestamp(float(ts)) if ts else None
    except Exception:
        return None


# 즉시 로드용 게터
def get_channels():
    try:
        return app.state.channels
    except Exception:
        return loadChannels()


def get_config():
    return loadConfig()


def get_cookies():
    return loadCookies()


# 채널 상태 초기화 함수
async def initChannelStates(channels):
    try:
        print("[DEBUG] initChannelStates 시작")

        # 1) 값이 비어 있을 때만 플레이스홀더 채우기 
        async with channels_lock:
            for channel in channels:
                cid = channel.get('id')
                print(f"[DEBUG] 초기화 중인 채널: {cid}, {channel.get('name')}")

                channel.setdefault('status', '대기 중')
                channel.setdefault('record_enabled', True)

                # 제목/카테고리: 비어 있을 때만 플레이스홀더
                if not str(channel.get('live_title', '')).strip():
                    channel['live_title'] = "불러오는 중..."
                if not str(channel.get('category', '')).strip():
                    channel['category'] = "불러오는 중..."

                # 썸네일 기본값: 기존 값 없을 때만 설정
                if not channel.get('thumbnail_url'):
                    if (channel.get('platform') or '').lower() == 'cime':
                        channel['thumbnail_url'] = '/static/img/cimeclosed_thumbnail.png'
                    else:
                        channel['thumbnail_url'] = '/static/img/default_thumbnail.png'

        # 2) 초기 부팅 직후 메타 프리페치(동시성 제한)로 즉시 값 채우기
        sem = asyncio.Semaphore(6)  # 과도한 동시 요청 방지

        async def _prefetch_one(ch: dict):
            async with sem:
                try:
                    platform = (ch.get('platform') or '').lower()
                    meta = await fetchMetadata(ch, platform) 
                    if not isinstance(meta, dict):
                        return

                    # 메타에서 추출
                    title = meta.get('live_title') or meta.get('liveTitle') or meta.get('title') or ''
                    cate  = meta.get('category') or meta.get('category_name') or meta.get('liveCategoryValue') or ''
                    turl  = meta.get('thumbnail_url') or ''

                    # 채널 딕셔너리 업데이트는 락 내부에서
                    async with channels_lock:
                        if title:
                            ch['live_title'] = title
                        if cate:
                            ch['category'] = cate
                        if turl:
                            ch['thumbnail_url'] = turl
                except Exception as e:
                    print(f"[WARN] prefetch meta failed for {ch.get('id')}: {e}")

        await asyncio.gather(*[asyncio.create_task(_prefetch_one(ch)) for ch in channels])

        print("[DEBUG] initChannelStates 완료")

    except Exception as e:
        print(f"[ERROR] initChannelStates 중 오류 발생: {e}")
        raise e


# 자동 녹화 모드 함수
async def autoRecording():
    cfg = get_config()
    if cfg.get("autoRecordingMode", False):
        print("[DEBUG] 자동 녹화 모드 활성화 → 모든 채널 WATCHING 진입")
        await app.state.fsm.startAllWatching()
    else:
        print("[DEBUG] 자동 녹화 모드가 비활성화되어 있습니다.")


# 특정 채널의 녹화를 시작하는 함수
async def startRecordingForChannel(channel_id: str, is_user_request: bool = False):
    channels = get_channels() or []
    ch = next((c for c in channels if c.get("id") == channel_id), None)

    if not ch:
        return

    # 수동 요청: 예약 표기만 선반영(UX)
    recorder_manager.set_is_user_stopped(channel_id, False)
    if bool(ch.get("record_enabled", True)):
        recorder_manager.set_status_reserved(channel_id, True)

    # 실제 시작은 항상 FSM
    await app.state.fsm.userStart(channel_id, is_user_request=is_user_request)


# 특정 채널의 녹화를 중지하는 함수
async def stopRecordingForChannel(channel_id: str):
    try:
        # 1) 종료되기 전에 패턴 스냅샷 확보
        last = recorder_manager.get_recording_filename(channel_id)

        # 2) 중지 표식 / 예약 해제
        recorder_manager.set_is_user_stopped(channel_id, True)
        recorder_manager.set_status_reserved(channel_id, False)

        # 3) FSM에 실제 종료 위임
        await app.state.fsm.userStop(channel_id)

        # 4) 분할녹화 배치 트리거: 패턴 우선 → 라스트 폴백
        if last:
            asyncio.create_task(queueBatchPattern(channel_id, last))
        else:
            asyncio.create_task(queueBatchLast(channel_id))

    except Exception as e:
        print(f"[WARN] stopRecordingForChannel failed for {channel_id}: {e}")


# 모든 플랫폼 동시에 모두 녹화하기 함수
async def startRecordingForAllChannels(app, is_user_request: bool=False):
    async with app.state.channels_lock:
        channels = [dict(c) for c in app.state.channels]

    results = {}
    tasks = []
    for ch in channels:
        cid = ch.get("id")
        if not cid or not bool(ch.get("record_enabled", True)):
            continue
        recorder_manager.set_is_user_stopped(cid, False)
        recorder_manager.set_status_reserved(cid, True)
        tasks.append(asyncio.create_task(app.state.fsm.userStart(cid)))
        results[cid] = {"state": "예약녹화 중", "recording_duration": ""}

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return results


# 모든 플랫폼 동시에 모두 녹화 중지하기 함수
async def stopRecordingForAllChannels():
    # 1) 중지 전에 각 채널의 패턴 스냅샷 확보
    pre_snap = {}
    async with app.state.channels_lock:
        channels = list(app.state.channels)
    for ch in channels:
        cid = ch.get("id")
        if not cid:
            continue
        last = recorder_manager.get_recording_filename(cid)
        if last:
            pre_snap[cid] = last

    # 1.5) 선플래그: 루프가 즉시 STOP을 감지하도록 먼저 표시
    flagged = 0
    for ch in channels:
        cid = ch.get("id")
        if not cid:
            continue
        recorder_manager.set_is_user_stopped(cid, True)
        recorder_manager.set_status_reserved(cid, False)
        flagged += 1
    print(f"[DEBUG] set stop flag for {flagged} channels (GUI pre-stopAll)")

    # 2) FSM 일괄 종료
    await app.state.fsm.stopAll()
    print("[DEBUG] FSM 일괄 STOP 요청 완료")

    # 3) 스냅샷 기반으로 우선 배치 큐잉
    for cid, last in pre_snap.items():
        asyncio.create_task(queueBatchPattern(cid, last))

    # 4) 스냅샷 없던 채널은 라스트-폴백
    try:
        async with app.state.channels_lock:
            channels = list(app.state.channels)
        for ch in channels:
            cid = ch.get("id")
            if not cid or cid in pre_snap:
                continue
            asyncio.create_task(queueBatchLast(cid))
    except Exception as e:
        print(f"[WARN] stopAll fallback enqueue failed: {e}")



# IP 주소를 가져오는 함수
def getAddresses():

    with thread_lock:  # 전역 락 사용
        internal_ip = "127.0.0.1"
        local_ip = None
        external_ip = None

        if os.name == 'nt':

            try:
                result = subprocess.run(['ipconfig'], capture_output=True, text=True, encoding='cp949')
                ip_address_match = re.search(r'IPv4 주소[^:]*:\s*([\d.]+)', result.stdout)
                if ip_address_match:
                    local_ip = ip_address_match.group(1)
                else:
                    local_ip = "내부 사설 IP 주소를 찾을 수 없습니다."
            except Exception as e:
                local_ip = f"내부 사설 IP 주소를 가져오는 중 오류 발생: {e}"
        else:
            try:
                result = subprocess.run(['ip', 'addr', 'show'], capture_output=True, text=True)
                # 일반적으로 127.0.0.1은 제외하고 첫번째 inet 주소 사용
                ip_address_match = re.search(r'\s+inet\s+(\d+\.\d+\.\d+\.\d+)/', result.stdout)
                if ip_address_match:
                    local_ip = ip_address_match.group(1)
                else:
                    local_ip = "내부 사설 IP 주소를 찾을 수 없습니다."
            except Exception as e:
                local_ip = f"내부 사설 IP 주소를 가져오는 중 오류 발생: {e}"

        try:
            # 외부 공인 IP 주소 가져오기 (httpbin 사용)
            response = requests.get('https://httpbin.org/ip', timeout=5)
            if response.status_code == 200:
                external_ip = response.json()["origin"]
            else:
                external_ip = "공인 IP 주소를 가져오는 데 실패했습니다."
        except Exception as e:
            external_ip = f"공인 IP 주소를 가져오는 중 오류 발생: {e}"

        return internal_ip, local_ip, external_ip


# Persistence helpers: 저장 디바운스/비동기 I/O
class DebouncedSaver:
    def __init__(self, app, delay=1.2):
        self.app = app
        self.delay = delay
        self._task = None
        self._pending = False

    def __call__(self, _unused=None):
        self._pending = True
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        # 변경이 더 없을 때까지 반복 루프
        while True:
            # 최근 트리거부터 delay만큼 모아 받기
            await asyncio.sleep(self.delay)
            self._pending = False
            try:
                async with self.app.state.channels_lock:
                    snap = list(self.app.state.channels)
                # 디스크 I/O는 스레드에서
                await asyncio.to_thread(saveChannels, snap)
            except Exception as e:
                print(f"[WARN] DebouncedSaver failed: {e}")
            # 잠자는 동안 또 트리거가 들어왔으면 한 번 더 돈다
            if not self._pending:
                break

    # 종료 직전 강제 저장
    async def flush(self):
        try:
            async with self.app.state.channels_lock:
                snap = list(self.app.state.channels)
            await asyncio.to_thread(saveChannels, snap)
        except Exception as e:
            print(f"[WARN] DebouncedSaver.flush failed: {e}")


# fetcher 콜백
async def _fetchMetaWorker(channel: dict):
    platform = (channel.get("platform") or "").lower()
    return await fetchMetadata(channel, platform)


def _seedMetaConcurrency() -> int:
    # 1) 환경변수 우선
    env = os.environ.get("SEED_META_CONCURRENCY", "").strip()
    if env.isdigit() and int(env) > 0:
        return max(1, min(12, int(env)))

    # 2) 설정파일 값 재사용 (config.json: seedMetaConcurrency 또는 metaConcurrency)
    cfg = loadConfig() or {}
    val = cfg.get("seedMetaConcurrency", cfg.get("metaConcurrency", "auto"))
    if isinstance(val, int) and val > 0:
        return max(1, min(12, val))

    # 3) AUTO: 코어수 75%, 2~12
    cores = os.cpu_count() or 2
    auto = int(cores * 0.75)
    return max(2, min(12, auto))


# 저사양 보조 메타 채우기 최적화
async def _seedMetadataOnce(app):
    chs = get_channels() or []
    if not chs:
        return

    conc = _seedMetaConcurrency()
    sem = asyncio.Semaphore(conc)
    print(f"[DEBUG] Seed meta concurrency = {conc}")

    async def _one(ch: dict):
        async with sem:
            try:
                payload = await _fetchMetaWorker(ch)
                if isinstance(payload, dict):
                    async with channels_lock:
                        ch['live_title']    = payload.get('live_title',    ch.get('live_title', '정보 없음'))
                        ch['category']      = payload.get('category',      ch.get('category', '정보 없음'))
                        ch['thumbnail_url'] = payload.get('thumbnail_url', ch.get('thumbnail_url', '/static/img/cimeclosed_thumbnail.png' if (ch.get('platform') or '').lower() == 'cime' else '/static/img/default_thumbnail.png'))
                    await asyncio.sleep(0.03)  # 폭주 방지
            except Exception as e:
                print(f"[WARN] seed one failed: {e}")

    await asyncio.gather(*[asyncio.create_task(_one(ch)) for ch in chs])


# 치지직/씨미 메타데이터 단건 조회
@app.get("/api/update_metadata/{channel_id}")
async def api_update_metadata(channel_id: str):
    chs = get_channels()
    ch = next((c for c in chs if str(c.get("id")) == str(channel_id)), None)
    if not ch:
        return {"metadata": None, "from_cache": False, "fresh": False}

    platform = (ch.get("platform") or "").lower()

    # 1차: 캐시에서 읽기
    payload, from_cache, fresh = await getMetadataCached(
        app, channel_id, platform, app.state.meta_fetcher, app.state.save_debounced, app.state.channels_lock
    )

    # 플레이스홀더 판정
    def _blankish(x):
        if x is None: return True
        s = str(x).strip()
        return (s == "") or (s in ("정보 없음", "방송 제목 없음", "카테고리 없음"))

    need_force = True
    if isinstance(payload, dict):
        title = payload.get("live_title") or payload.get("title") or payload.get("video_title")
        cate  = payload.get("category")  or payload.get("category_name")
        # fresh가 False이거나 제목/카테고리가 빈값이면 강제 새김
        need_force = (not bool(fresh)) or _blankish(title) or _blankish(cate)

    if need_force:
        # 2차 즉시 강제 새김
        try:
            await refreshOneChannel(
                app, ch, app.state.meta_fetcher, app.state.save_debounced, app.state.channels_lock,
                need_meta=True, need_thumb=False
            )
        except Exception:
            pass
        # 3차: 다시 읽기
        payload, from_cache, fresh = await getMetadataCached(
            app, channel_id, platform, app.state.meta_fetcher, app.state.save_debounced, app.state.channels_lock
        )

    return {
        "metadata": payload if isinstance(payload, dict) else None,
        "from_cache": bool(from_cache),
        "fresh": bool(fresh),
    }


@app.get("/status")
async def get_status():
    status = {}
    current_channels = get_channels()
    for channel in current_channels:
        cid = channel.get("id")
        rec  = recorder_manager.get_status_recording(cid)
        resv = recorder_manager.get_status_reserved(cid)

        # FSM.WATCHING 도 예약으로 판단(표시만)
        fsm_state = app.state.fsm.getState(cid)
        effective_reserved = bool(resv or (fsm_state == "WATCHING"))

        # 좀비 보정: 녹화 True인데 실제 프로세스가 없으면 정리(유예 0초, worker는 즉시 처리)
        p = recorder_manager.get_tasks_process(cid)
        if rec and (not p or p.returncode is not None):
            recorder_manager.set_status_recording(cid, False)
            recorder_manager.recording_remove_start_time(cid)
            recorder_manager.recording_remove_filename(cid)
            recorder_manager.clear_tasks_process(cid)
            rec = False

        dur = recorder_manager.get_recording_duration(cid) if rec else ""

        status[cid] = {
            "recording": bool(rec),
            "reserved":  bool(resv),
            "recording_duration": dur,
        }

    return status


@app.get("/api/check_status/{channel_id}")
async def api_check_status(channel_id: str, request: Request):
    try:
        # 채널 스냅샷 확보
        async with request.app.state.channels_lock:
            _channel = next((c for c in request.app.state.channels if c['id'] == channel_id), None)
            if not _channel:
                raise HTTPException(status_code=404, detail="Channel not found")
            channel = dict(_channel)

        channel_name = channel.get('name', 'Unknown Channel')
        platform     = (channel.get('platform') or 'unknown').lower()

        recording_status         = recorder_manager.get_status_recording(channel_id)
        reserved_status          = recorder_manager.get_status_reserved(channel_id)
        filename                 = _getRecFilename(channel_id)
        recording_start_time_obj = _getRecStartTime(channel_id)
        recording_duration       = recorder_manager.get_recording_duration(channel_id)
        stop_requested           = recorder_manager.get_is_user_stopped(channel_id)

        # FSM.WATCHING 은 예약으로 간주(표시만)
        fsm_state = request.app.state.fsm.getState(channel_id)
        effective_reserved = bool(reserved_status or (fsm_state == "WATCHING"))

        # 플래그 정합성 보정: 녹화 True인데 실제 프로세스가 없으면 정리
        p = recorder_manager.get_tasks_process(channel_id)
        if recording_status and (not p or p.returncode is not None):
            recorder_manager.set_status_recording(channel_id, False)
            recorder_manager.recording_remove_start_time(channel_id)
            recorder_manager.recording_remove_filename(channel_id)
            recorder_manager.clear_tasks_process(channel_id)
            recording_status = False

        # 표시 상태 결정
        if recording_status:
            channel_status = '녹화 중'
        elif effective_reserved:
            channel_status = '예약녹화 중'
        else:
            channel_status = '대기 중'

        # 예정/시작 시간 문자열화
        scheduled_start_time = channel.get('scheduled_start_time')
        if isinstance(scheduled_start_time, datetime):
            scheduled_start_time_str = scheduled_start_time.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(scheduled_start_time, str) and scheduled_start_time:
            scheduled_start_time_str = scheduled_start_time
        else:
            scheduled_start_time_str = "예정된 라이브 방송이 없습니다."

        if isinstance(recording_start_time_obj, datetime):
            recording_start_time_str = recording_start_time_obj.strftime("%Y-%m-%d %H:%M:%S")
        else:
            recording_start_time_str = '녹화 시작 시간이 설정되지 않았습니다.'

        print(f"[DEBUG] [{channel_name}] ({platform}) {filename} : {channel_status} "
              f"{recording_duration or '00:00:00'} Start: {recording_start_time_str} "
              f"Stop Requested: {stop_requested}")

        return JSONResponse(content={
            'status': 'success',
            'state': channel_status,
            'filename': filename or '녹화 파일이 없습니다.',
            'recording_duration': recording_duration or '00:00:00',
            'recording_start_time': recording_start_time_str,
            'scheduled_start_time': scheduled_start_time_str,
            'stop_requested': stop_requested
        })

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] check_status 오류 발생: {e}")
        import traceback; print(traceback.format_exc())
        return JSONResponse(content={'status': 'error', 'message': str(e)}, status_code=500)


# 캐시전용 메타데이터 스냅샷
@app.get("/api/metadata_snapshot")
async def api_metadata_snapshot():
    chs = get_channels()
    items = []
    for ch in chs:
        items.append({
            "id": ch.get("id"),
            "platform": (ch.get("platform") or "").lower(),
            "live_title": ch.get("live_title") or "",
            "category": ch.get("category") or "",
            "thumbnail_url": ch.get("thumbnail_url") or "",
        })
    return {"channels": items}


# 전체 썸네일 배치 조회
@app.get("/api/thumbnail_status")
async def api_thumbnail_status():
    chs = get_channels()
    meta_fetcher = app.state.meta_fetcher
    results = await getThumbnailsCached(app, chs, meta_fetcher, app.state.save_debounced, app.state.channels_lock)

    # 패스트패스 훅: 라이브 채널만 일괄 트리거
    try:
        for it in results:
            cid = str(it.get("id") or "")
            p   = (it.get("platform") or "").lower()
    except Exception:
        pass

    # fresh/from_cache 정규화만 수행 (
    BAD_TEXT = {"", None, "방송 제목 없음", "정보 없음", "카테고리 없음"}
    for it in results:

        # 서버 측에서 다른 이름을 쓰는 경우를 흡수
        _fresh = bool(it.get("fresh", it.get("is_fresh", False)))
        it["fresh"] = _fresh

        # 캐시 여부를 서버가 명시 안 주면 알 수 없음 취급
        it["from_cache"] = bool(it.get("from_cache", it.get("cached", False)))

        # fresh 키가 아예 없다면, 값의 빈/플레이스홀더 여부로 유추
        if "fresh" not in it or it["fresh"] is False:
            t = it.get("live_title") or it.get("title")
            c = it.get("category") or it.get("category_name")
            if (t in BAD_TEXT) or (c in BAD_TEXT):
                it["fresh"] = False

    return {"channels": results}


# 개별 녹화시작 API 함수
@app.post("/api/start_recording/{channel_id}")
async def api_start_recording(channel_id: str, request: Request):
    try:
        data = await request.json()
        is_user_request = data.get('is_user_request', False)
    except Exception:
        is_user_request = False

    await startRecordingForChannel(channel_id, is_user_request=is_user_request)

    # 짧은 대기 후 현재 상태 계산
    try:
        await asyncio.sleep(0.15)
    except Exception:
        pass

    rec  = recorder_manager.get_status_recording(channel_id)
    rsv  = recorder_manager.get_status_reserved(channel_id)
    file = recorder_manager.get_recording_filename(channel_id) or ""

    state = "녹화 중" if rec and not rsv else ("예약녹화 중" if rsv else "대기 중")
    return JSONResponse(content={
        'status': 'success',
        'message': '시작 요청을 접수했습니다.',
        'state': state,
        'filename': file
    })


# 개별 녹화중지 API 함수
@app.post("/api/stop_recording/{channel_id}")
async def api_stop_recording(channel_id: str):
    await stopRecordingForChannel(channel_id)
    state           = recorder_manager.get_status_recording(channel_id)
    reserved_status = recorder_manager.get_status_reserved(channel_id)
    filename        = _getRecFilename(channel_id)
    return JSONResponse(content={
        'status': 'success',
        'state': '녹화 중' if state and not reserved_status else
                 '예약녹화 중' if reserved_status else
                 '대기 중',
        'filename': filename or '녹화 파일이 없습니다.'
    })


# 모두 녹화시작 API 함수
@app.post("/api/start_all_recording")
async def api_start_all_recording(request: Request):
    try:
        data = await request.json()
        is_user_request = bool(data.get('is_user_request', False))
    except Exception:
        is_user_request = False

    results = await startRecordingForAllChannels(request.app, is_user_request=is_user_request)
    return JSONResponse({'status': 'success', 'message': '일괄 시작 요청 접수', 'channels_status': results})


# 모두 녹화중지 API 함수는 그대로 OK
@app.post("/api/stop_all_recording")
async def api_stop_all_recording(request: Request):
    await stopRecordingForAllChannels()
    return JSONResponse(content={'status': 'success', 'message': '일괄 중지 요청 접수'})


# 채널 목록 조회 API 함수
@app.get("/api/channels")
async def getChannels(request: Request):
    try:
        async with channels_lock:
            # 참조가 흔들리지 않게 스냅샷으로 반환
            snapshot = list(get_channels() or [])
        return JSONResponse(content={"channels": snapshot})
    except Exception as e:
        print(f"[ERROR] 채널 목록 조회 중 오류: {e}")
        raise HTTPException(status_code=500, detail="채널 목록 조회 중 오류")


# 채널 추가 API 함수
@app.post("/api/channels")
async def addChannel(request: Request):
    try:
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="요청 본문이 비어 있습니다.")
        new_channel = await request.json()

        # id 필드/플랫폼별 옵션/불리언 변환
        if "channelId" in new_channel:
            new_channel["id"] = new_channel.pop("channelId")

        platform = (new_channel.get("platform") or "").lower()

        # recordWatchParty → 불리언 변환
        def _to_bool(v):
            if isinstance(v, bool): return v
            return str(v).strip().lower() in {"1","true","on","yes"}

        if "recordWatchParty" in new_channel:
            new_channel["recordWatchParty"] = _to_bool(new_channel.get("recordWatchParty", False))
        else:
            new_channel["recordWatchParty"] = False

        # 확장자 점(.) 보정
        ext = new_channel.get("extension")
        if isinstance(ext, str) and ext and not ext.startswith("."):
            new_channel["extension"] = f".{ext}"

        # 씨미면 강제 규칙 적용
        if platform == "cime":
            new_channel["extension"] = ".mp4"
            new_channel["recordWatchParty"] = False

        # 기본값
        if "record_enabled" not in new_channel:
            new_channel["record_enabled"] = True
        else:
            new_channel["record_enabled"] = _to_bool(new_channel["record_enabled"])

        # 필수 필드 검증
        if platform not in ["chzzk", "cime"]:
            raise HTTPException(status_code=400, detail="잘못된 플랫폼 값입니다.")
        required = ("platform", "id", "name", "output_dir", "quality", "extension")
        if not all(k in new_channel for k in required):
            raise HTTPException(status_code=400, detail="필수 필드가 누락되었습니다.")

        async with channels_lock:
            current_channels = get_channels()
            current_channels.append(new_channel)
            snapshot = list(current_channels)

        app.state.save_debounced(None)
        try:
            RecorderManager.setChannels(snapshot)
        except Exception as _e:
            print(f"[WARN] setChannels 실패(무시): {_e}")

        # 변경 직후 메타 캐시 한 번만 갱신
        try:
            asyncio.create_task(_seedMetadataOnce(app))
        except Exception as _e:
            print(f"[WARN] seedMetadata 트리거 실패(무시): {_e}")

        print(f"[DEBUG] 새 채널 추가 완료: {new_channel.get('name')} ({new_channel.get('id')})")
        return JSONResponse(content={'status': 'success'})

    except HTTPException:
        raise

    except Exception as e:
        print(f"[ERROR] 채널 추가 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="채널 추가 중 오류 발생")


# 채널 수정 API 함수
@app.put("/api/channels/{channel_id}")
async def editChannel(channel_id: str, request: Request):
    try:
        updated_channel = await request.json()

        async with channels_lock:
            current_channels = get_channels()
            target = next((c for c in current_channels if c.get('id') == channel_id), None)
            if not target:
                raise HTTPException(status_code=404, detail="Channel not found")

            effective_platform = (updated_channel.get("platform") or target.get("platform") or "").lower()

            # 보정 규칙
            def _to_bool(v):
                if isinstance(v, bool): return v
                return str(v).strip().lower() in {"1","true","on","yes"}

            if "recordWatchParty" in updated_channel:
                rw = _to_bool(updated_channel["recordWatchParty"])
                if effective_platform == "cime":
                    rw = False
                updated_channel["recordWatchParty"] = rw

            ext = updated_channel.get("extension")
            if isinstance(ext, str) and ext and not ext.startswith("."):
                updated_channel["extension"] = f".{ext}"

            if effective_platform == "cime":
                updated_channel["extension"] = ".mp4"

            updated_channel['id'] = channel_id
            target.update(updated_channel)
            snapshot = list(current_channels)

        app.state.save_debounced(None)
        try:
            RecorderManager.setChannels(snapshot)
        except Exception as _e:
            print(f"[WARN] setChannels 실패(무시): {_e}")

        try:
            asyncio.create_task(_seedMetadataOnce(app))
        except Exception as _e:
            print(f"[WARN] seedMetadata 트리거 실패(무시): {_e}")

        print(f"[DEBUG] 채널 수정 완료: {target.get('name')} ({channel_id})")
        return JSONResponse(content={'status': 'success'})

    except HTTPException:
        raise

    except Exception as e:
        print(f"[ERROR] 채널 수정 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="채널 수정 중 오류 발생")



# 채널 삭제 API 함수
@app.delete("/api/channels/{channel_id}")
async def deleteChannel(channel_id: str, request: Request):
    try:
        # 락 안: in-place 삭제(참조 유지) 후 스냅샷 생성
        async with channels_lock:
            current_channels = get_channels()
            before = len(current_channels)

            # in-place 갱신으로 app.state.channels 참조 유지
            current_channels[:] = [ch for ch in current_channels if ch.get('id') != channel_id]
            if len(current_channels) == before:

                raise HTTPException(status_code=404, detail="Channel not found")
            snapshot = list(current_channels)

        app.state.save_debounced(None)

        try:
            RecorderManager.setChannels(snapshot)
        except Exception as _e:
            print(f"[WARN] setChannels 실패(무시): {_e}")

        try:
            asyncio.create_task(_seedMetadataOnce(app))
        except Exception as _e:
            print(f"[WARN] seedMetadata 트리거 실패(무시): {_e}")

        print(f"[DEBUG] 채널 삭제 완료: {channel_id}")
        return JSONResponse(content={'status': 'success'})

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] 채널 삭제 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="채널 삭제 중 오류 발생")


@app.get("/cookies", response_class=HTMLResponse)
async def getCookies(request: Request):
    cookies = loadCookies()  
    return templates.TemplateResponse('cookies.html', {'request': request, 'cookies': cookies})


@app.post("/cookies")
async def updateCookies(request: Request):
    try:
        new_cookies = await request.json()

        if not new_cookies:
            raise ValueError("수신된 쿠키 데이터가 비어 있습니다.")

        saveCookies(new_cookies)
        app.state.cookies = loadCookies()

        print("[INFO] 쿠키 설정이 저장되었습니다.")
        return JSONResponse(content={'status': 'success'})
    
    except ValueError as ve:
        print(f"쿠키 데이터 오류: {ve}")
        return JSONResponse(content={'status': 'error', 'message': str(ve)}, status_code=400)

    except Exception as e:
        print(f"쿠키 업데이트 중 오류 발생: {e}")
        return JSONResponse(content={'status': 'error', 'message': '쿠키 업데이트 중 오류 발생'}, status_code=500)


@app.get("/api/check_chzzk_cookie")
async def api_check_chzzk_cookie():
    cookies = loadCookies()
    result = await asyncio.to_thread(checkChzzkCookie, cookies)
    return result


@app.get("/api/check_cime_cookie")
async def api_check_cime_cookie():
    cime = getCimeCookies()

    mauth = str(cime.get("mauth-authorization-code", "") or "").strip()
    session_id = str(cime.get("session-id", "") or "").strip()

    if not mauth or not session_id:
        return {
            "ok": False,
            "message": "씨미 쿠키가 비어 있습니다. mauth-authorization-code / session-id 값을 입력하세요."
        }

    test_channel = {
        "platform": "cime",
        "id": "kkaekkeushan_seolrem",
        "name": "씨미 쿠키 테스트",
        "quality": "best",
    }

    try:
        metadata = await getCimeMetadata(test_channel)

        if metadata.get("is_live") and metadata.get("playback_url"):
            return {
                "ok": True,
                "message": (
                    f"씨미 쿠키가 적용되었습니다. "
                    f"adult={metadata.get('adult')}, "
                    f"canWatchUhd={metadata.get('can_watch_uhd')}, "
                    f"selected={metadata.get('selected_playback_source')}"
                )
            }

        return {
            "ok": False,
            "status": "unverified",
            "message": (
                "씨미 쿠키는 저장되어 있지만 실제 playback 권한은 확인하지 못했습니다. "
                "테스트 채널이 오프라인이거나, 쿠키가 만료되었거나, 해당 방송 접근 권한이 없을 수 있습니다."
            )
        }

    except Exception as e:
        return {
            "ok": False,
            "message": f"씨미 쿠키 확인 중 오류: {e}"
        }


# 텔레그램/디스코드 알림 API 테스트 함수
@app.get("/api/test_notification/{target}")
async def testNotification(target: str, request: Request):
    target = (target or "").strip().lower()
    cfg = loadNotification() or {}

    if target == "telegram":
        if not cfg.get("telegram_enabled"):
            return JSONResponse(content={"status": "error", "message": "텔레그램 알림이 OFF입니다."}, status_code=400)

        ok = notifyEvent(
            "notification_test",
            "recordWEB 테스트",
            "텔레그램 테스트 메시지입니다.",
            severity="info",
            target="telegram",
            force=True
        )

        if not ok:
            return JSONResponse(content={"status": "error", "message": "텔레그램 전송에 실패했습니다."}, status_code=500)

        return JSONResponse(content={"status": "success", "message": "텔레그램 테스트 메시지를 전송했습니다."})

    if target == "discord":
        if not cfg.get("discord_enabled"):
            return JSONResponse(content={"status": "error", "message": "디스코드 알림이 OFF입니다."}, status_code=400)

        ok = notifyEvent(
            "notification_test",
            "recordWEB 테스트",
            "디스코드 테스트 메시지입니다.",
            severity="info",
            target="discord",
            force=True
        )

        if not ok:
            return JSONResponse(content={"status": "error", "message": "디스코드 전송에 실패했습니다."}, status_code=500)

        return JSONResponse(content={"status": "success", "message": "디스코드 테스트 메시지를 전송했습니다."})

    return JSONResponse(content={"status": "error", "message": "지원하지 않는 알림 대상입니다."}, status_code=400)


# 설정 페이지 라우트
@app.get("/config", response_class=HTMLResponse)
async def configPage(request: Request):
    config_data = loadConfig()
    account = loadAccount()
    notification = loadNotification()

    # 채널 분배 UI를 위해 channels 전달
    try:
        async with request.app.state.channels_lock:
            channels = [dict(c) for c in request.app.state.channels]
    except Exception:
        channels = loadChannels()

    return templates.TemplateResponse('config.html', {
        'request': request,
        'config': config_data,
        'account': account,
        'notification': notification,
        'channels': channels,  
    })


@app.post("/config")
async def updateConfig(
    request: Request,
    autoRecordingMode: Optional[str] = Form(None),
    enableTray: Optional[str] = Form(None),
    minimizeToTrayOnClose: Optional[str] = Form(None),
    minimizeToTrayOnStart: Optional[str] = Form(None),
    plugin_type: Optional[str] = Form(None),
    timemachine_time_shift: Optional[int] = Form(None),
    autoPostProcessing: Optional[str] = Form(None),
    deleteAfterPostProcessing: Optional[str] = Form(None),
    removeFixedPrefix: Optional[str] = Form(None),
    moveAfterProcessingEnabled: Optional[str] = Form(None),
    moveAfterProcessing: Optional[str] = Form(None),
    postNewWindow: Optional[str] = Form(None),
    recheckInterval: Optional[int] = Form(None),
    filenamePattern: Optional[str] = Form(None),
    splitRecordingMode: Optional[str] = Form(None),
    splitPostProcessing: Optional[str] = Form(None),
    autoStopInterval: Optional[int] = Form(None),
    splitOverlapSec: Optional[int] = Form(None),
    stream_copy: Optional[str] = Form(None),
    video_codec: Optional[str] = Form(None),
    preset: Optional[str] = Form(None),
    postprocess_resolution: Optional[str] = Form(None),
    use_bitrate_mode: Optional[str] = Form(None),
    video_quality: Optional[int] = Form(None),
    video_bitrate: Optional[str] = Form(None),
    vbv_maxrate: Optional[str] = Form(None),
    vbv_bufsize: Optional[str] = Form(None),
    extra_ffmpeg_options: Optional[str] = Form(None),
    audio_codec: Optional[str] = Form(None),
    audio_bitrate: Optional[str] = Form(None),
    gpuCount: Optional[int] = Form(None),
    video_codec_gpu1: Optional[str] = Form(None),
    preset_gpu1: Optional[str] = Form(None),
    postprocess_resolution_gpu1: Optional[str] = Form(None),
    use_bitrate_mode_gpu1: Optional[str] = Form(None),
    video_quality_gpu1: Optional[int] = Form(None),
    video_bitrate_gpu1: Optional[str] = Form(None),
    vbv_maxrate_gpu1: Optional[str] = Form(None),
    vbv_bufsize_gpu1: Optional[str] = Form(None),
    extra_ffmpeg_options_gpu1: Optional[str] = Form(None),
    audio_codec_gpu1: Optional[str] = Form(None),
    audio_bitrate_gpu1: Optional[str] = Form(None),
    gpuAssignmentsJson: Optional[str] = Form(None),    

    telegram_enabled: Optional[str] = Form("off"),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),

    discord_enabled: Optional[str] = Form("off"),
    discord_webhook_url: str = Form(""),

    notify_record_started: Optional[str] = Form("off"),
    notify_record_finished: Optional[str] = Form("off"),
    notify_record_start_failed: Optional[str] = Form("on"),
    notify_record_abnormally_stopped: Optional[str] = Form("on"),
    notify_record_user_stopped: Optional[str] = Form("off"),
    notify_postprocess_finished: Optional[str] = Form("on"),
    notify_postprocess_failed: Optional[str] = Form("on"),
    notify_cookie_auth_failed: Optional[str] = Form("on"),
    notify_watchparty_skipped: Optional[str] = Form("off"),
    notify_disk_space_low: Optional[str] = Form("on"),

    notify_dedupe_seconds: Optional[int] = Form(300),
    notify_disk_space_low_gb: Optional[int] = Form(10)
):
    try:
        # 1) 기존 설정
        current_config = loadConfig() or {}

        # gpuCount 정규화 (1/2만 허용)
        try:
            _gc = int(gpuCount) if gpuCount is not None else int(current_config.get("gpuCount", 1) or 1)
        except Exception:
            _gc = int(current_config.get("gpuCount", 1) or 1)
        _gc = 2 if _gc == 2 else 1

        # 2) 플러그인/시프트
        posted_plugin = (plugin_type or "").strip().lower() if plugin_type is not None else None
        if posted_plugin in ("basic", "timemachine_plus"):
            normalized_plugin = posted_plugin
        else:
            normalized_plugin = (current_config.get("plugin_type") or "basic")

        try:
            if timemachine_time_shift is None:
                _shift = int(current_config.get("timemachine_time_shift", 0) or 0)
            else:
                _shift = int(timemachine_time_shift or 0)
        except Exception:
            _shift = int(current_config.get("timemachine_time_shift", 0) or 0)

        normalized_shift = (
            max(0, min(10, _shift)) if normalized_plugin == "basic"
            else max(0, min(3600, _shift))
        )

        # 3) 분할/오버랩/오토스탑
        _split_on = toBool(splitRecordingMode) if splitRecordingMode is not None else bool(current_config.get("splitRecordingMode", False))
        try:
            if splitOverlapSec is None:
                _ovl = int(current_config.get("splitOverlapSec", 0) or 0)
            else:
                _ovl = int(splitOverlapSec or 0)
        except Exception:
            _ovl = int(current_config.get("splitOverlapSec", 0) or 0)
        _ovl = max(0, min(30, _ovl))
        if not _split_on:
            _ovl = 0

        try:
            if _split_on:
                _auto_stop = int(autoStopInterval) if autoStopInterval is not None else int(current_config.get("autoStopInterval", 0) or 0)
            else:
                _auto_stop = 0
        except Exception:
            _auto_stop = 0 if not _split_on else int(current_config.get("autoStopInterval", 0) or 0)

        # 4) 트레이/텔레그램
        _enable_tray   = toBool(enableTray) if enableTray is not None else bool(current_config.get("enableTray", False))
        _tray_on_close = toBool(minimizeToTrayOnClose) if minimizeToTrayOnClose is not None else bool(current_config.get("minimizeToTrayOnClose", False))
        _tray_on_start = toBool(minimizeToTrayOnStart) if minimizeToTrayOnStart is not None else bool(current_config.get("minimizeToTrayOnStart", False))
        if not _enable_tray:
            _tray_on_close = False
            _tray_on_start = False

        # 5) 재탐색/파일명
        try:
            _recheck = int(recheckInterval) if recheckInterval is not None else int(current_config.get("recheckInterval", 60))
        except Exception:
            _recheck = int(current_config.get("recheckInterval", 60))
        _pattern = filenamePattern if (filenamePattern not in (None, "")) else current_config.get("filenamePattern", "[{start_time}] {safe_live_title}")

        # 6) 이동경로: 누락 시 기존값 유지(빈 문자열→None)
        _move_path = (
            current_config.get("moveAfterProcessing")
            if moveAfterProcessing is None
            else (moveAfterProcessing or None)
        )

        # 후처리 옵션(GPU0)폼 누락(None)일 때 기존값 유지
        _stream_copy0 = toBool(stream_copy) if stream_copy is not None else bool(current_config.get("stream_copy", True))

        # 영상 출력 해상도 정규화
        _allowed_res = ("source", "1080p", "720p", "480p")
        _pp0_raw = postprocess_resolution if postprocess_resolution is not None else current_config.get("postprocess_resolution", "source")
        _pp0 = str(_pp0_raw or "source").strip().lower()
        if _pp0 not in _allowed_res:
            _pp0 = "source"

        _video_codec0 = video_codec if (video_codec not in (None, "")) else current_config.get("video_codec", "libx264")
        _preset0      = preset      if (preset      not in (None, "")) else current_config.get("preset", "medium")

        _use_bitrate_mode0 = toBool(use_bitrate_mode) if use_bitrate_mode is not None else bool(current_config.get("use_bitrate_mode", False))

        try:
            _video_quality0 = int(video_quality) if video_quality is not None else int(current_config.get("video_quality", 23) or 23)
        except Exception:
            _video_quality0 = int(current_config.get("video_quality", 23) or 23)

        _video_bitrate0 = video_bitrate if video_bitrate is not None else current_config.get("video_bitrate", "1000k")
        _vbv_maxrate0   = vbv_maxrate   if vbv_maxrate   is not None else current_config.get("vbv_maxrate", "")
        _vbv_bufsize0   = vbv_bufsize   if vbv_bufsize   is not None else current_config.get("vbv_bufsize", "")
        _extra_opts0    = extra_ffmpeg_options if extra_ffmpeg_options is not None else current_config.get("extra_ffmpeg_options", "")

        _audio_codec0   = audio_codec   if (audio_codec   not in (None, "")) else current_config.get("audio_codec", "aac")
        _audio_bitrate0 = audio_bitrate if audio_bitrate is not None else current_config.get("audio_bitrate", "192k")


        # GPU1 입력값이 비어있으면 기존값/또는 GPU0 값으로 fallback
        _vc1 = video_codec_gpu1 if (video_codec_gpu1 not in (None, "")) else current_config.get("video_codec_gpu1", _video_codec0)
        _pr1 = preset_gpu1      if (preset_gpu1      not in (None, "")) else current_config.get("preset_gpu1", _preset0)

        _pp1_raw = postprocess_resolution_gpu1 if postprocess_resolution_gpu1 is not None else current_config.get("postprocess_resolution_gpu1", _pp0)
        _pp1 = str(_pp1_raw or _pp0).strip().lower()
        if _pp1 not in _allowed_res:
            _pp1 = _pp0

        _ubm1 = (
            toBool(use_bitrate_mode_gpu1) if use_bitrate_mode_gpu1 is not None
            else bool(current_config.get("use_bitrate_mode_gpu1", _use_bitrate_mode0))
        )

        try:
            _vq1 = int(video_quality_gpu1) if video_quality_gpu1 is not None else int(current_config.get("video_quality_gpu1", _video_quality0) or _video_quality0)
        except Exception:
            _vq1 = int(current_config.get("video_quality_gpu1", _video_quality0) or _video_quality0)

        _vb1 = video_bitrate_gpu1 if video_bitrate_gpu1 is not None else current_config.get("video_bitrate_gpu1", _video_bitrate0)

        _vbv_maxrate1 = vbv_maxrate_gpu1 if vbv_maxrate_gpu1 is not None else current_config.get("vbv_maxrate_gpu1", "")
        _vbv_bufsize1 = vbv_bufsize_gpu1 if vbv_bufsize_gpu1 is not None else current_config.get("vbv_bufsize_gpu1", "")
        _extra_opts1  = extra_ffmpeg_options_gpu1 if extra_ffmpeg_options_gpu1 is not None else current_config.get("extra_ffmpeg_options_gpu1", "")

        _ac1 = audio_codec_gpu1   if (audio_codec_gpu1   not in (None, "")) else current_config.get("audio_codec_gpu1", _audio_codec0)
        _ab1 = audio_bitrate_gpu1 if audio_bitrate_gpu1 is not None else current_config.get("audio_bitrate_gpu1", _audio_bitrate0)


        # 7) 새 설정
        new_config = {
            **current_config,
            "autoRecordingMode":           toBool(autoRecordingMode),
            "enableTray":                  _enable_tray,
            "minimizeToTrayOnClose":       _tray_on_close,
            "minimizeToTrayOnStart":       _tray_on_start,
            "plugin_type":                 normalized_plugin,
            "timemachine_time_shift":      normalized_shift,
            "autoPostProcessing":          toBool(autoPostProcessing),
            "deleteAfterPostProcessing":   toBool(deleteAfterPostProcessing),
            "removeFixedPrefix":           toBool(removeFixedPrefix),
            "moveAfterProcessingEnabled":  toBool(moveAfterProcessingEnabled),
            "moveAfterProcessing":         _move_path,
            "postNewWindow":               toBool(postNewWindow),
            "recheckInterval":             _recheck,
            "filenamePattern":             _pattern,
            "splitRecordingMode":          _split_on,
            "splitPostProcessing":         toBool(splitPostProcessing),
            "autoStopInterval":            _auto_stop,
            "splitOverlapSec":             _ovl,
            "stream_copy":                 _stream_copy0,
            "video_codec":                 _video_codec0,
            "preset":                      _preset0,
            "postprocess_resolution":      _pp0,
            "use_bitrate_mode":            _use_bitrate_mode0,
            "video_quality":               _video_quality0,
            "video_bitrate":               _video_bitrate0,
            "vbv_maxrate":                 _vbv_maxrate0,
            "vbv_bufsize":                 _vbv_bufsize0,
            "extra_ffmpeg_options":        _extra_opts0,
            "audio_codec":                 _audio_codec0,
            "audio_bitrate":               _audio_bitrate0,
            "gpuCount":                    _gc,
            "video_codec_gpu1":            _vc1,
            "preset_gpu1":                 _pr1,
            "postprocess_resolution_gpu1": _pp1,
            "use_bitrate_mode_gpu1":       _ubm1,
            "video_quality_gpu1":          _vq1,
            "video_bitrate_gpu1":          _vb1,
            "vbv_maxrate_gpu1":            _vbv_maxrate1,
            "vbv_bufsize_gpu1":            _vbv_bufsize1,
            "extra_ffmpeg_options_gpu1":   _extra_opts1,
            "audio_codec_gpu1":            _ac1,
            "audio_bitrate_gpu1":          _ab1,
        }


        # 8) 알림 필수 체크
        try:
            _notify_dedupe_seconds = int(notify_dedupe_seconds or 300)
        except Exception:
            _notify_dedupe_seconds = 300
        _notify_dedupe_seconds = max(0, min(86400, _notify_dedupe_seconds))

        try:
            _notify_disk_space_low_gb = int(notify_disk_space_low_gb or 10)
        except Exception:
            _notify_disk_space_low_gb = 10
        _notify_disk_space_low_gb = max(1, min(1024, _notify_disk_space_low_gb))

        notification_data = {
            "telegram_enabled": toBool(telegram_enabled),
            "telegram_bot_token": telegram_bot_token.strip(),
            "telegram_chat_id": telegram_chat_id.strip(),

            "discord_enabled": toBool(discord_enabled),
            "discord_webhook_url": discord_webhook_url.strip(),

            "events": {
                "record_started": toBool(notify_record_started),
                "record_finished": toBool(notify_record_finished),
                "record_start_failed": toBool(notify_record_start_failed),
                "record_abnormally_stopped": toBool(notify_record_abnormally_stopped),
                "record_user_stopped": toBool(notify_record_user_stopped),
                "postprocess_finished": toBool(notify_postprocess_finished),
                "postprocess_failed": toBool(notify_postprocess_failed),
                "cookie_auth_failed": toBool(notify_cookie_auth_failed),
                "watchparty_skipped": toBool(notify_watchparty_skipped),
                "disk_space_low": toBool(notify_disk_space_low),
            },

            "limits": {
                "dedupe_seconds": _notify_dedupe_seconds,
                "disk_space_low_gb": _notify_disk_space_low_gb,
            },
        }

        error_message = ""

        if notification_data["telegram_enabled"]:
            if not notification_data["telegram_bot_token"] or not notification_data["telegram_chat_id"]:
                error_message = "텔레그램 알림 사용 시 봇 토큰과 채팅방 ID를 모두 입력해야 합니다."

        if not error_message and notification_data["discord_enabled"]:
            if not notification_data["discord_webhook_url"]:
                error_message = "디스코드 알림 사용 시 웹훅 URL을 입력해야 합니다."

        if error_message:
            account = loadAccount()

            try:
                async with request.app.state.channels_lock:
                    channels = [dict(c) for c in request.app.state.channels]
            except Exception:
                channels = loadChannels()

            return templates.TemplateResponse("config.html", {
                "request": request,
                "config": new_config,
                "account": account,
                "notification": notification_data,
                "channels": channels,
                "error_message": error_message,
            }, status_code=400)

        # 9) 저장 및 앱 상태 갱신
        print("[DEBUG] 설정 저장 중...(GUI worker)")
        saveConfig(new_config)
        saveNotification(notification_data)
        try:
            request.app.state.config = new_config
        except Exception:
            pass
        print("[DEBUG] 설정 저장 완료(GUI worker)")

        # gpuCount==2일 때만 channels.json(gpu_index) 반영
        if _gc == 2 and gpuAssignmentsJson not in (None, ""):
            try:
                mapping = json.loads(gpuAssignmentsJson) 
                if isinstance(mapping, dict):
                    changed = False
                    snap = None

                    async with request.app.state.channels_lock:
                        for ch in request.app.state.channels:
                            plat = str(ch.get("platform", "") or "").strip().lower()
                            cid  = str(ch.get("id", "") or "").strip()

                            if plat != "chzzk" or not cid:
                                continue

                            key = f"{plat}:{cid}"

                            raw = None
                            if key in mapping:
                                raw = mapping[key]
                            elif cid in mapping:
                                raw = mapping[cid]
                            else:
                                continue

                            try:
                                gi = 1 if int(raw) == 1 else 0
                            except Exception:
                                gi = 0

                            if ch.get("gpu_index") != gi:
                                ch["gpu_index"] = gi
                                changed = True

                        # 변경이 있을 경우 락 안에서 dict 단위 복사로 스냅샷 생성
                        if changed:
                            snap = [dict(c) for c in request.app.state.channels]

                    # 락 밖에서 파일 IO 수행
                    if snap is not None:
                        await asyncio.to_thread(saveChannels, snap)


            except Exception as e:
                print(f"[WARN] gpuAssignmentsJson 적용 실패(GUI worker): {e}")

        return JSONResponse(content={"status": "success"})

    except Exception as e:
        print(f"[ERROR] 설정 저장 중 오류 발생(GUI worker): {e}")
        return JSONResponse(
            content={"status": "error", "message": "설정 저장 중 오류 발생: " + str(e)},
            status_code=500
        )


@app.get("/get_config")
async def api_get_config():
    try:
        cfg = loadConfig()
        return {"status": "success", "config": cfg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# runUvicorn 서버실행 함수
async def runUvicorn():
    try:
        config_data = loadConfig()  
        port = config_data.get('port', 5000)  # port 값 불러오고, 없으면 기본값 5000 사용

        # Uvicorn 서버를 비동기적으로 실행
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="debug")
        server = uvicorn.Server(config)
        print(f"[DEBUG] Uvicorn 서버 시작 - 포트 {port}")
        await server.serve()
    except Exception as e:
        print(f"[ERROR] runUvicorn 중 오류 발생: {e}")
        raise e


# 비동기로 서버 실행 함수 
async def runAutomodeServer():
    try:
        await runUvicorn()
    except Exception as e:
        print(f"[ERROR] 서버 실행 중 오류 발생: {e}")
        try:
            recordException("recordWorker.runAutomodeServer", e)
        except Exception:
            pass


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    setupAppLogging("worker")
    try:
        validateRuntimeEnvironment("recordWorker")
    except Exception as e:
        print(f"[WARN] 시작 전 안정성 점검 실패(계속 진행): {e}")
        try:
            recordException("recordWorker.startup", e)
        except Exception:
            pass
    config_data = loadConfig() 
    port = config_data.get('port', 5000)
    internal_ip, local_ip, external_ip = getAddresses()
    checkRequiredPaths()
    print(f"* 로컬 접속: http://127.0.0.1:{port}")
    # 외부 접속 기능은 필요 없으므로 host를 127.0.0.1로 지정
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="debug")