import subprocess
import os
import time
import traceback
import json
import sys
import ctypes
import asyncio
import platform
import threading
import webbrowser
import re
import string
import shutil
import secrets
from datetime import datetime
from typing import Optional, Any, List, Dict

sys.stdout.reconfigure(encoding='utf-8')

# 필요한 모듈을 설치하는 함수
def install_missing_modules():
    # 윈도우 외  pygetwindow를 설치하지 않도록 분기처리
    if os.name == 'nt':
        missing_modules = [
            "requests", "httpx", "fastapi", "uvicorn", "aiohttp",
            "jinja2", "werkzeug", "itsdangerous", "python-multipart",
            "starlette", "psutil", "cryptography", "pystray", "pillow",
            "py-cpuinfo"
            
        ]
    else:
        missing_modules = [
            "requests", "httpx", "fastapi", "uvicorn", "aiohttp",
            "jinja2", "werkzeug", "itsdangerous", "python-multipart",
            "starlette", "psutil", "cryptography", "pystray", "pillow",
            "py-cpuinfo"
        ]

    installed_modules = []

    # 각 모듈이 이미 설치되어 있는지 확인하고, 없는 경우 설치 목록에 추가
    for module in missing_modules:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "show", module],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError:
            installed_modules.append(module)

    # 설치가 필요한 모듈이 있는 경우 설치 진행
    if installed_modules:
        print("필수 모듈을 자동으로 설치합니다...")
        for module in installed_modules:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", module])
                print(f"'{module}' 모듈 설치 완료.")
            except subprocess.CalledProcessError as e:
                print(f"모듈 설치 중 오류 발생: {e}")

        print("필수 모듈 설치가 완료되었습니다.")
    else:
        print("모든 필수 모듈이 이미 설치되어 있습니다.")

install_missing_modules()

import uvicorn
import requests
import httpx
import psutil


try:
    import cpuinfo
    import pystray
    from PIL import Image

except Exception:
    pystray = None
    Image = None
    cpuinfo = None

from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks, Depends, Body, Query, APIRouter
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import asynccontextmanager, suppress

from module.data_manager import (
    RecorderManager, loadAccount, saveAccount, loadCookies, saveCookies,
    getChzzkCookies, getCimeCookies,
    loadChannels, saveChannels, loadConfig, saveConfig,
    saveNotification, loadNotification, notifyEvent, last_notified_state,
    CONFIG_PATH, CHANNELS_PATH, COOKIE_PATH, LOGIN_PATH, getFFmpeg, 
    getStreamlink, getBaseUrl, toBool,
    PROGRAM_NAME, PROGRAM_VERSION, WEB_UI_TITLE, GUI_TITLE
)

from module.meta_cache import (
    ensure as mc_ensure, refreshLoop as mc_refreshLoop, 
    getMetadataCached, getThumbnailsCached
)

from module.file_manager import (
    buildAllowedRoots, ensureInRoots, listDir, diskUsageFor, listDisks,
    makeTrashPath, softDelete, hardDelete, movePath, renamePath, mkdirPath,
    busyFilePaths, isLocked, normPath, listMountRoots, streamCopyFile
)

from module.recording_adapter import fetchMetadata, startSession
from module.channel_fsm import ChannelFsm
from module.live_recorder import queueBatchLast, queueBatchPattern
from module.cookie_checker import checkChzzkCookie
from module.cime_recorder import getCimeMetadata
from module.runtime_guard import RuntimeGuard, RuntimeAlreadyRunning
from module.config_validator import validateRuntimeEnvironment
from module.runtime_log import setupAppLogging, recordException

# 현재 파일의 경로
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 정적 파일 경로와 템플릿 디렉토리 설정
static_directory = os.path.join(BASE_DIR, "templates", "static")
templates_directory = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=templates_directory)
templates.env.globals.update(
    program_name=PROGRAM_NAME,
    program_version=PROGRAM_VERSION,
    web_ui_title=WEB_UI_TITLE,
)

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

# data_manager.py의 RecorderManager 클래스 인스턴스 생성
recorder_manager = RecorderManager()

# 네트워크 속도 계산을 위한 직전 스냅샷 저장소 (프로세스 메모리)
_last_net: Dict[str, float] = {"ts": 0.0, "bytes_sent": 0.0, "bytes_recv": 0.0}

# CPU 명칭 조회
_CPU_NAME = None


# 애플리케이션 생애주기 핸들러
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.channels = loadChannels()
    app.state.fsm = ChannelFsm()
    app.state.channels_lock = asyncio.Lock()
    app.state.config = loadConfig()
    app.state.cookies = loadCookies()
    app.state.bg_tasks = set()

    changed = False
    async with app.state.channels_lock:
        changed = coerceChannelsInplace(app.state.channels)
    if changed:
        await asyncio.to_thread(saveChannels, app.state.channels)
    print("[INFO] 채널 데이터 보정 적용 완료.")

    RecorderManager.setChannels(app.state.channels)
    RecorderManager.setChannelsRef(app.state.channels)
    RecorderManager.setChannelsLockRef(app.state.channels_lock)

    meta_task = None

    try:
        await initChannelStates(app)

        app.state.meta_fetcher = _makeMetaFetcher(app)
        app.state.save_debounced = DebouncedSaver(app, delay=1.2)
        mc_ensure(app)

        # 부팅 직후 1회성 메타 시드
        seed_task = asyncio.create_task(_seedMetadataWEB(app))

        meta_task = asyncio.create_task(
            mc_refreshLoop(app, app.state.meta_fetcher, app.state.save_debounced, app.state.channels_lock)
        )

        # 부팅시 한 번만 자동 시작 (Auto ON일 때)
        if app.state.config.get("autoRecordingMode", False):
            print("[DEBUG] 자동 녹화 모드: 부팅시 한 번만 WATCHING 진입")
            await app.state.fsm.startAllWatching()

        yield

    finally:

        # 메타 루프 정리
        if meta_task and not meta_task.done():
            meta_task.cancel()
            with suppress(Exception):
                await meta_task

        # 시드 태스크 정리
        try:
            seed_task  # 존재하면 NameError 아님
            if seed_task and not seed_task.done():
                seed_task.cancel()
                with suppress(Exception):
                    await seed_task

        except NameError:
            pass

        # 백그라운드 태스크 모두 취소/대기
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

        # httpx AsyncClient 정리 
        try:
            from module.live_recorder import closeHttpxClient
            await closeHttpxClient()
        except Exception as e:
            print(f"[WARN] closeHttpxClient 실패(무시): {e}")


app = FastAPI(lifespan=lifespan)


if os.path.isdir(static_directory):
    app.mount("/static", StaticFiles(directory=static_directory), name="static")
else:
    print(f"[WARN] Static dir not found: {static_directory}")


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


# Windows 콘솔 창 최소화
def minimizeConsole():

    if os.name != "nt":
        return

    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return  # pythonw.exe 등 콘솔이 없으면 무시
        SW_MINIMIZE = 6
        ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)

    except Exception as e:
        print(f"[WARN] 콘솔 최소화 실패: {e}")


# 서버 기준 불리언 보정
def coerceChannelsInplace(chs: list):
    changed = False
    for ch in chs:
        # 기존 유튜브 라이브 녹화 채널은 씨미로 보정
        if (ch.get("platform") or "").lower() == "youtube":
            ch["platform"] = "cime"
            changed = True

        # recordWatchParty 문자열 → 불리언
        if "recordWatchParty" in ch and not isinstance(ch["recordWatchParty"], bool):
            ch["recordWatchParty"] = toBool(ch["recordWatchParty"])
            changed = True

        # 씨미는 recordWatchParty 강제 False
        if ch.get("platform") == "cime" and ch.get("recordWatchParty", False):
            ch["recordWatchParty"] = False
            changed = True

        # 씨미 확장자 강제 .mp4
        if ch.get("platform") == "cime" and ch.get("extension") != ".mp4":
            ch["extension"] = ".mp4"
            changed = True

        # extension 앞에 점 없으면 보정 
        ext = ch.get("extension")
        if isinstance(ext, str) and ext and not ext.startswith("."):
            ch["extension"] = f".{ext}"
            changed = True

        # record_enabled 불리언으로 보정
        if "record_enabled" in ch and not isinstance(ch["record_enabled"], bool):
            ch["record_enabled"] = toBool(ch["record_enabled"])
            changed = True

    return changed


# 상태 조회 헬퍼(읽기 전용)
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

# 전역 락 선언
thread_lock = threading.Lock()  # 동기 함수에서 사용할 락


# 메타데이터 가져오는 fetcher를 app에 바인딩하기 위한 팩토리
def _makeMetaFetcher(app: FastAPI):
    async def _fetch(channel: dict):
        platform = (channel.get('platform') or '').lower()
        return await fetchMetadata(channel, platform)
    return _fetch


# 세션을 사용하기 위한 미들웨어
account_data = loadAccount()
if account_data and 'secret_key' in account_data:
    app.add_middleware(SessionMiddleware, secret_key=account_data['secret_key'])
else:
    # secret_key가 없으면 기본적으로 생성
    new_secret_key = secrets.token_hex(32)
    saveAccount({'secret_key': new_secret_key})
    app.add_middleware(SessionMiddleware, secret_key=new_secret_key)


# 예외 처리기 정의
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse(url='/login')
    else:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )


# 채널 상태 초기화 함수
async def initChannelStates(app: FastAPI):
    try:
        print("[DEBUG] initChannelStates 시작")
        async with app.state.channels_lock:
            for channel in app.state.channels:
                channel.setdefault('status', '대기 중')
                channel.setdefault('record_enabled', True)
                channel['live_title'] = "불러오는 중..."
                channel['category'] = "불러오는 중..."
                channel['thumbnail_url'] = (
                    '/static/img/cimeclosed_thumbnail.png' if (channel.get('platform') or '').lower() == 'cime'
                    else '/static/img/default_thumbnail.png'
                )
        print("[DEBUG] initChannelStates 완료")
    except Exception as e:
        print(f"[ERROR] initChannelStates 중 오류 발생: {e}")
        raise


# 로그인 인증 의존성 함수 정의
async def requireLogin(request: Request):
    if not request.app.state.config.get('loginMode', False):
        return True
    if request.session.get('logged_in'):
        return True
    # API엔 401 JSON
    if request.url.path.startswith('/api/') or 'application/json' in (request.headers.get('accept','')):
        raise HTTPException(status_code=401, detail="Login required")
    return RedirectResponse(url="/login", status_code=302)


# 자동 녹화 모드 함수
async def autoRecording(app: FastAPI):
    cfg = app.state.config
    if app.state.config.get("autoRecordingMode", False):
        print("[DEBUG] 자동 녹화 모드 활성화 → 모든 채널 WATCHING 진입")
        asyncio.create_task(app.state.fsm.startAllWatching())
    else:
        print("[DEBUG] 자동 녹화 모드가 비활성화되어 있습니다.")


# 특정 채널의 녹화를 시작하는 함수
async def startRecordingForChannel(app, channel_id: str, is_user_request: bool = False):
    async with app.state.channels_lock:
        channels = list(app.state.channels)
    ch = next((c for c in channels if c.get("id") == channel_id), None)
    if not ch:
        return {"status": "error", "message": "unknown channel"}

    from module.data_manager import RecorderManager
    rm = RecorderManager()

    # 0) 이전 세션 잔재 선제 정리(이전 파일명/시간이 UI에 비치지 않도록)
    rm.recording_remove_start_time(channel_id)
    rm.recording_remove_filename(channel_id)
    rm.clear_tasks_process(channel_id)
    rm.set_status_recording(channel_id, False)

    # 1) 사용자 시작 의사 표시 및 예약 표기
    rm.set_is_user_stopped(channel_id, False)
    if bool(ch.get("record_enabled", True)):
        rm.set_status_reserved(channel_id, True)

    # 2) FSM에 실제 시작 요청
    try:
        await app.state.fsm.userStart(channel_id, is_user_request=is_user_request)

        try:
            await asyncio.sleep(0.15)
        except Exception:
            pass

        rec  = recorder_manager.get_status_recording(channel_id)
        rsv  = recorder_manager.get_status_reserved(channel_id)
        file = recorder_manager.get_recording_filename(channel_id) or ""

        state = "녹화 중" if rec and not rsv else ("예약녹화 중" if rsv else "대기 중")
        return {"status": "success", "state": state, "filename": file}
    except Exception as e:
        return {"status": "error", "message": str(e)}



# 특정 채널의 녹화를 중지하는 함수
async def stopRecordingForChannel(app: FastAPI, channel_id: str):
    try:
        last = recorder_manager.get_recording_filename(channel_id)

        # 1) 사용자 중지 플래그
        recorder_manager.set_is_user_stopped(channel_id, True)
        recorder_manager.set_status_reserved(channel_id, False)

        # 2) FSM 중지
        await app.state.fsm.userStop(channel_id)

        # 3) ★즉시 UI 혼선 방지: 스테일 상태 정리
        recorder_manager.set_status_recording(channel_id, False)
        recorder_manager.recording_remove_start_time(channel_id)
        recorder_manager.recording_remove_filename(channel_id)
        recorder_manager.clear_tasks_process(channel_id)

        # 4) 후처리 큐잉
        if last:
            asyncio.create_task(queueBatchPattern(channel_id, last))
        else:
            asyncio.create_task(queueBatchLast(channel_id))

        # 5) 마지막 파일명 반환(상위 API 응답용)
        return last

    except Exception as e:
        print(f"[WARN] stopRecordingForChannel failed for {channel_id}: {e}")
        return None


# 모두 녹화하기 함수
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
        # 일단 자리만 만들어 두고 나중에 실제 상태로 교정
        results[cid] = {"state": "예약녹화 중", "recording_duration": ""}

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # 실제 상태로 정정: 녹화 > 예약 > 대기
    for cid in list(results.keys()):
        st  = recorder_manager.get_status_recording(cid)
        rsv = recorder_manager.get_status_reserved(cid)
        fsm = app.state.fsm.getState(cid)
        eff = bool(rsv or (fsm == "WATCHING"))
        results[cid]["state"] = "녹화 중" if st else ("예약녹화 중" if eff else "대기 중")

    return results


# 모든 플랫폼 동시에 모두 녹화 중지하기 함수
async def stopRecordingForAllChannels(app: FastAPI):
    # 1) 스냅샷
    pre_snap = {}
    async with app.state.channels_lock:
        channels = list(app.state.channels)
    for ch in channels:
        cid = ch.get("id")
        if cid:
            last = recorder_manager.get_recording_filename(cid)
            if last: pre_snap[cid] = last

    # 1.5) 선플래그: 루프가 즉시 STOP을 감지하도록 먼저 표시
    flagged = 0
    for ch in channels:
        cid = ch.get("id")
        if not cid:
            continue
        recorder_manager.set_is_user_stopped(cid, True)
        # UI가 잠깐 헷갈리지 않도록 예약 표시는 꺼둠 (루프 finalize에서 토글상태에 따라 다시 세움)
        recorder_manager.set_status_reserved(cid, False)
        flagged += 1
    print(f"[DEBUG] set stop flag for {flagged} channels (pre-stopAll)")

    # 2) STOP
    await app.state.fsm.stopAll()
    print("[DEBUG] FSM에게 일괄 STOPPED 전이를 요청했습니다.")

    # 3) 스냅샷 우선 큐잉 → 폴백
    for cid, last in pre_snap.items():
        asyncio.create_task(queueBatchPattern(cid, last))
    async with app.state.channels_lock:
        channels = list(app.state.channels)
    for ch in channels:
        cid = ch.get("id")
        if cid and cid not in pre_snap:
            asyncio.create_task(queueBatchLast(cid))


# IP 주소를 가져오는 함수
def getAddresses():
    with thread_lock:  # 전역 락 사용
        internal_ip = "127.0.0.1"
        local_ip = None
        external_ip = None

        if os.name == 'nt':
            # Windows: ipconfig 사용
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
            # Linux: ip addr show 사용
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


# CPU 모델명 문자열을 반환 함수
def _getCpuName():
    global _CPU_NAME
    if _CPU_NAME:
        return _CPU_NAME

    name = None

    # 1) py-cpuinfo 우선
    try:
        if cpuinfo is not None:
            info = cpuinfo.get_cpu_info() or {}
            name = info.get('brand_raw') or info.get('brand')  # brand_raw 없을 수 있음
    except Exception:
        name = None

    # 2) Linux 전용 간단 폴백(/proc/cpuinfo)
    if not name:
        try:
            if os.path.exists("/proc/cpuinfo"):
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "model name" in line:
                            name = line.split(":", 1)[1].strip()
                            break
        except Exception:
            pass

    # 3) 최후의 폴백
    if not name:
        name = platform.processor() or platform.machine() or "Unknown CPU"

    _CPU_NAME = name
    return _CPU_NAME

# 전역 표시용(1회 평가 후 캐시)
cpu_name = _getCpuName()

# 시스템 모니터링은 짧은 주기로 반복 호출되므로 측정 결과와 디스크 목록을 재사용합니다.
_SYS_METRICS_LOCK = threading.Lock()
_SYS_METRICS_CACHE = {"at": 0.0, "data": None}
_SYS_METRICS_CACHE_TTL = 2.5
_SYS_DISK_PARTS_CACHE = {"at": 0.0, "parts": []}
_SYS_DISK_PARTS_TTL = 60.0
_SYS_NET_PREV = psutil.net_io_counters(pernic=False)
_SYS_NET_PREV_AT = time.monotonic()
psutil.cpu_percent(interval=None)


# 대시보드 디스크 라벨 정리
def _shortDiskLabel(p):
    raw_mp = (p.mountpoint or "").strip()
    mp = raw_mp if raw_mp == os.sep else raw_mp.rstrip(os.sep)
    fs = (p.fstype or "").upper()

    if os.name == "nt":
        label = mp.upper() if (len(mp) >= 2 and mp[1] == ":") else (p.device or mp)
    else:
        if mp in ("/", "/home", "/boot"):
            label = mp
        elif mp.startswith("/sys/fs/cgroup/"):
            parts = [x for x in mp.split("/") if x]
            label = "cgroup/" + (parts[3] if len(parts) > 3 else "")
        elif len(mp) > 16:
            base = os.path.basename(mp)
            label = base if base else mp
        else:
            label = mp
    return label


def _collectSysMetrics() -> dict:
    global _SYS_NET_PREV, _SYS_NET_PREV_AT

    now = time.monotonic()
    with _SYS_METRICS_LOCK:
        cached = _SYS_METRICS_CACHE.get("data")
        if cached is not None and now - float(_SYS_METRICS_CACHE.get("at") or 0.0) < _SYS_METRICS_CACHE_TTL:
            return cached

        cpu_pct = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()

        net = psutil.net_io_counters(pernic=False)
        dt = max(0.001, now - _SYS_NET_PREV_AT)
        up_bps = max(0.0, float(net.bytes_sent - _SYS_NET_PREV.bytes_sent) / dt)
        down_bps = max(0.0, float(net.bytes_recv - _SYS_NET_PREV.bytes_recv) / dt)
        _SYS_NET_PREV = net
        _SYS_NET_PREV_AT = now

        if now - float(_SYS_DISK_PARTS_CACHE.get("at") or 0.0) >= _SYS_DISK_PARTS_TTL:
            try:
                parts = psutil.disk_partitions(all=True)
            except Exception:
                parts = []
            _SYS_DISK_PARTS_CACHE["at"] = now
            _SYS_DISK_PARTS_CACHE["parts"] = parts
        else:
            parts = _SYS_DISK_PARTS_CACHE.get("parts") or []

        disks = []
        seen = set()
        ephemeral = {"tmpfs", "proc", "sysfs", "cgroup", "cgroup2", "squashfs", "devpts", "overlay"}

        for part in parts:
            mountpoint = (part.mountpoint or "").strip()
            if not mountpoint or mountpoint in seen:
                continue
            if (part.fstype or "").lower() in ephemeral and mountpoint not in ("/", "/home", "/boot"):
                continue

            try:
                usage = psutil.disk_usage(mountpoint)
            except Exception:
                continue

            seen.add(mountpoint)
            disks.append({
                "device": part.device or mountpoint,
                "mountpoint": mountpoint,
                "label": _shortDiskLabel(part),
                "fstype": (part.fstype or "").lower(),
                "total": int(usage.total),
                "used": int(usage.used),
                "free": int(usage.free),
                "percent": float(usage.percent),
            })

        if not disks and os.name == "nt":
            for letter in string.ascii_uppercase:
                root = f"{letter}:\\"
                if not os.path.exists(root):
                    continue
                try:
                    usage = shutil.disk_usage(root)
                except Exception:
                    continue
                disks.append({
                    "device": root,
                    "mountpoint": root,
                    "label": root,
                    "fstype": "",
                    "total": int(usage.total),
                    "used": int(usage.used),
                    "free": int(usage.free),
                    "percent": float((usage.used / usage.total * 100.0) if usage.total else 0.0),
                })

        data = {
            "cpu": {
                "name": cpu_name,
                "percent": cpu_pct,
                "cores": psutil.cpu_count(logical=True),
            },
            "memory": {
                "total": int(vm.total),
                "used": int(vm.used),
                "free": int(vm.available),
                "percent": float(vm.percent),
            },
            "network": {
                "up_bps": up_bps,
                "down_bps": down_bps,
                "bytes_sent": int(net.bytes_sent),
                "bytes_recv": int(net.bytes_recv),
            },
            "disks": disks[:10],
            "sampled_at": time.time(),
        }
        _SYS_METRICS_CACHE["at"] = now
        _SYS_METRICS_CACHE["data"] = data
        return data


# 보안 경로 정규화 헬퍼 함수
def _normalizeAllowedRoots(candidates: List[str]) -> List[str]:
    safe = []
    seen = set()
    for raw in candidates or []:
        if not raw:
            continue
        p = os.path.abspath(os.path.expanduser(raw.strip()))
        # 존재 + 디렉터리만 허용
        if not os.path.isdir(p):
            continue

        # 드라이브 루트/시스템 폴더 차단 (윈도우)
        lower = p.lower().replace('/', '\\')

        if os.name == 'nt':
            # 드라이브 루트(ex: C:\) 차단
            if re.match(r'^[a-z]:\\$', lower):
                continue
            # 대표 시스템 디렉터리 차단
            deny = ['\\windows\\', '\\program files\\', '\\program files (x86)\\', '\\programdata\\', '\\users\\public\\']
            if any(d in lower for d in deny):
                continue

        else:
            # 리눅스/유닉스: 루트(/) 자체, 핵심시스템 경로 차단
            deny = ['/', '/bin', '/sbin', '/etc', '/proc', '/sys', '/dev', '/run', '/var', '/usr']
            if p in deny or any(p.startswith(d + os.sep) for d in deny):
                continue

        if p not in seen:
            safe.append(p)
            seen.add(p)
    return safe


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
        while True:
            await asyncio.sleep(self.delay)
            self._pending = False
            try:
                async with self.app.state.channels_lock:
                    snap = list(self.app.state.channels)
                await asyncio.to_thread(saveChannels, snap)
            except Exception as e:
                print(f"[WARN] DebouncedSaver failed: {e}")
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



# WEB용 메타 시드 동시성 결정
def _seedMetaConcurrency() -> int:
    env = os.environ.get("SEED_META_CONCURRENCY", "").strip()
    if env.isdigit() and int(env) > 0:
        return max(1, min(12, int(env)))
    cfg = loadConfig() or {}
    val = cfg.get("seedMetaConcurrency", cfg.get("metaConcurrency", "auto"))
    if isinstance(val, int) and val > 0:
        return max(1, min(12, val))
    cores = os.cpu_count() or 2
    auto = int(cores * 0.75)
    return max(2, min(12, auto))


# 1회성 메타 시드 태스크
async def _seedMetadataWEB(app):
    chs = list(app.state.channels or [])
    if not chs:
        return
    conc = _seedMetaConcurrency()
    sem = asyncio.Semaphore(conc)
    print(f"[DEBUG] (WEB) Seed meta concurrency = {conc}")

    async def _one(ch: dict):
        async with sem:
            try:
                payload = await app.state.meta_fetcher(ch)  # WEB은 _makeMetaFetcher(app)로 주입
                if isinstance(payload, dict):
                    async with app.state.channels_lock:
                        ch['live_title']    = payload.get('live_title',    ch.get('live_title', '정보 없음'))
                        ch['category']      = payload.get('category',      ch.get('category', '정보 없음'))
                        ch['thumbnail_url'] = payload.get('thumbnail_url', ch.get('thumbnail_url', '/static/img/cimeclosed_thumbnail.png' if (ch.get('platform') or '').lower() == 'cime' else '/static/img/default_thumbnail.png'))
                    await asyncio.sleep(0.03)  # 폭주 방지
            except Exception as e:
                print(f"[WARN] (WEB) seed one failed: {e}")

    await asyncio.gather(*[asyncio.create_task(_one(ch)) for ch in chs])


# 녹화 현황 페이지
@app.get("/recording", response_class=HTMLResponse)
async def recording_page(request: Request, login: Any = Depends(requireLogin)):
    async with request.app.state.channels_lock:
        chs = [dict(c) for c in request.app.state.channels]

    updated = False
    for channel in chs:
        channel_id = channel['id']

        if "status" not in channel:
            channel['status'] = "대기 중"
            updated = True

        # 녹화 상태 확인
        recording_status = recorder_manager.get_status_recording(channel_id)
        reserved_status  = recorder_manager.get_status_reserved(channel_id)
        filename         = _getRecFilename(channel_id)

        # FSM.WATCHING 도 예약으로 취급
        fsm_state = request.app.state.fsm.getState(channel_id)
        effective_reserved = bool(reserved_status or (fsm_state == "WATCHING"))

        if recording_status:
            channel['status'] = "녹화 중"
            channel['filename'] = filename or '파일 없음'
        elif effective_reserved:
            channel['status'] = "예약녹화 중"
            channel['filename'] = "예약녹화 대기 중"
        else:
            channel['status'] = "대기 중"
            channel['filename'] = "녹화 중이 아닙니다."

        # 초기 표시용 필드
        channel['live_title'] = "불러오는 중..."
        channel['category'] = "불러오는 중..."
        channel['thumbnail_url'] = '/static/img/cimeclosed_thumbnail.png' if (channel.get('platform') or '').lower() == 'cime' else '/static/img/default_thumbnail.png'

    return templates.TemplateResponse('recording.html', {
        'request': request,
        'channels': chs,
        'program_version': PROGRAM_VERSION
    })


@app.get("/status")
async def get_status(request: Request):
    status = {}
    async with request.app.state.channels_lock:
        current_channels = list(request.app.state.channels)

    for channel in current_channels:
        cid = channel.get("id")
        rec  = recorder_manager.get_status_recording(cid)
        resv = recorder_manager.get_status_reserved(cid)

        # WATCHING은 예약으로 표시(단, 녹화 중일 땐 덮지 않음)
        fsm_state = request.app.state.fsm.getState(cid)
        if (fsm_state == "WATCHING") and (not rec):
            resv = True
        else:
            if rec:
                resv = False

        # 8초 유예 좀비 보정
        p = recorder_manager.get_tasks_process(cid)
        if rec:
            if (not p) or (p and p.returncode is not None):
                try:
                    ts = RecorderManager.recording_start_time.get(cid)
                    elapsed = (time.time() - float(ts)) if ts else 999
                except Exception:
                    elapsed = 999
                if elapsed >= 8:
                    recorder_manager.set_status_recording(cid, False)
                    recorder_manager.recording_remove_start_time(cid)
                    recorder_manager.recording_remove_filename(cid)
                    recorder_manager.clear_tasks_process(cid)
                    rec = False

        # 녹화 시간 계산
        duration_str = ""
        if rec:
            # 1) start ts(초) 기반
            ts = None
            try:
                ts = RecorderManager.recording_start_time.get(cid)
            except Exception:
                ts = None
            if ts:
                try:
                    elapsed = max(0, int(time.time() - float(ts)))
                    h = elapsed // 3600
                    m = (elapsed % 3600) // 60
                    s = elapsed % 60
                    duration_str = f"{h:02d}:{m:02d}:{s:02d}"
                except Exception:
                    duration_str = recorder_manager.get_recording_duration(cid) or "00:00:00"
            else:
                # 2) 폴백: 기존 매니저 제공값
                duration_str = recorder_manager.get_recording_duration(cid) or "00:00:00"

        # 현재 세션에 저장된 파일 경로를 가져와 파일명으로 변환
        fname = (
            recorder_manager.get_recording_filename(cid)
            or channel.get("output_path")
            or ""
        )

        status[cid] = {
            "recording": bool(rec),
            "reserved":  bool(resv),
            "duration":  duration_str,  
            "filename":  os.path.basename(fname) if fname else ""
        }
    return status


@app.get("/api/check_status/{channel_id}")
async def api_check_status(request: Request, channel_id: str, login: Any = Depends(requireLogin)):
    try:
        async with request.app.state.channels_lock:
            _channel = next((c for c in request.app.state.channels if c['id'] == channel_id), None)
            if not _channel:
                raise HTTPException(status_code=404, detail="Channel not found")
            channel = dict(_channel)

        channel_name = channel.get('name', 'Unknown Channel')
        platform = (channel.get('platform') or 'unknown').lower()

        recording_status         = recorder_manager.get_status_recording(channel_id)
        reserved_status          = recorder_manager.get_status_reserved(channel_id)
        filename                 = _getRecFilename(channel_id)
        recording_start_time_obj = _getRecStartTime(channel_id)
        recording_duration       = recorder_manager.get_recording_duration(channel_id)
        stop_requested           = recorder_manager.get_is_user_stopped(channel_id)

        # 녹화>예약>대기 우선순위 강제
        fsm_state = request.app.state.fsm.getState(channel_id)
        if recording_status:
            reserved_status = False
        elif fsm_state == "WATCHING":
            reserved_status = True

        effective_reserved = bool(reserved_status)
        channel_status = '녹화 중' if recording_status else ('예약녹화 중' if effective_reserved else '대기 중')

        # 8초 유예 좀비 보정 (동일)
        p = recorder_manager.get_tasks_process(channel_id)
        if recording_status:
            if (not p) or (p and p.returncode is not None):
                try:
                    ts = RecorderManager.recording_start_time.get(channel_id)
                    elapsed = (time.time() - float(ts)) if ts else 999
                except Exception:
                    elapsed = 999
                if elapsed >= 8:
                    recorder_manager.set_status_recording(channel_id, False)
                    recorder_manager.recording_remove_start_time(channel_id)
                    recorder_manager.recording_remove_filename(channel_id)
                    recorder_manager.clear_tasks_process(channel_id)
                    recording_status = False
                    filename = _getRecFilename(channel_id)
                    recording_start_time_obj = _getRecStartTime(channel_id)

        # 상태 문자열 교정
        if recording_status:
            channel_status = '녹화 중'
        elif effective_reserved:
            channel_status = '예약녹화 중'
        else:
            channel_status = '대기 중'

        # 예정/시작 시간 문자열
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

        # 녹화시간 계산
        if recording_status:
            try:
                if isinstance(recording_start_time_obj, datetime):
                    elapsed = max(0, int(time.time() - recording_start_time_obj.timestamp()))
                else:
                    ts = RecorderManager.recording_start_time.get(channel_id)
                    elapsed = max(0, int(time.time() - float(ts))) if ts else 0
                h = elapsed // 3600
                m = (elapsed % 3600) // 60
                s = elapsed % 60
                recording_duration = f"{h:02d}:{m:02d}:{s:02d}"
            except Exception:
                recording_duration = recording_duration or "00:00:00"
        else:
            recording_duration = "" 

        print(f"[DEBUG] [{channel_name}] ({platform}) {filename} : {channel_status} "
              f"{recording_duration or '00:00:00'} Start: {recording_start_time_str}")

        return JSONResponse(content={
            'status': 'success',
            'state': channel_status,
            'filename': filename or '녹화 파일이 없습니다.',
            'recording_duration': recording_duration or '00:00:00',
            'recording_start_time': recording_start_time_str,
            'scheduled_start_time': scheduled_start_time_str,
            'stop_requested': recorder_manager.get_is_user_stopped(channel_id)
        })
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] check_status 오류 발생: {e}")
        import traceback; print(traceback.format_exc())
        return JSONResponse(content={'status': 'error', 'message': str(e)}, status_code=500)


# 치지직 및 씨미 메타데이터 통합 API
@app.get("/api/update_metadata/{channel_id}")
async def update_metadata(channel_id: str, request: Request, login: Any = Depends(requireLogin)):
    # 1) channel 스냅샷
    async with request.app.state.channels_lock:
        channel = next((c for c in request.app.state.channels if c['id'] == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    platform = (channel.get('platform') or 'unknown').lower()

    # 2) 캐시-우선 응답 (stale면 백그라운드에서 자동 갱신)
    payload, from_cache, fresh = await getMetadataCached(
        request.app,
        channel_id,
        platform,
        request.app.state.meta_fetcher,
        request.app.state.save_debounced,
        request.app.state.channels_lock,
    )

    # 3) 필요 시 ISO 문자열 보정
    if payload and isinstance(payload, dict):
        dt = payload.get('scheduled_start_time_dt')
        if isinstance(dt, datetime):
            payload['scheduled_start_time_str'] = dt.isoformat()
            payload.pop('scheduled_start_time_dt', None)

    return JSONResponse(content={
        'status': 'success',
        'from_cache': from_cache,
        'fresh': fresh,
        'metadata': payload or {}
    })


# 썸네일 상태갱신 API
@app.get("/api/thumbnail_status")
async def api_thumbnail_status(request: Request, login: Any = Depends(requireLogin)):
    # 캐시-우선 썸네일 목록 (stale이면 백그라운드 갱신 트리거)
    async with request.app.state.channels_lock:
        chs = list(request.app.state.channels)
    items = await getThumbnailsCached(
        request.app, chs, request.app.state.meta_fetcher, request.app.state.save_debounced, request.app.state.channels_lock
    )

    try:
        for it in items:
            cid = str(it.get("id") or "")
            p   = (it.get("platform") or "").lower()

    except Exception:
        pass

    return JSONResponse(content={'channels': items})


# 개별 녹화시작 API 함수
@app.post("/api/start_recording/{channel_id}")
async def api_start_recording(channel_id: str, request: Request, login: Any = Depends(requireLogin)):
    try:
        body = await request.json()
        is_user_request = bool(body.get('is_user_request', False))
    except Exception:
        is_user_request = False

    await startRecordingForChannel(request.app, channel_id, is_user_request=is_user_request)

    # 간격으로 최대 2초 대기: 프로세스 핸들 or recording=True 감지
    for _ in range(20):
        rec  = recorder_manager.get_status_recording(channel_id)
        proc = recorder_manager.get_tasks_process(channel_id)
        if rec or (proc and proc.returncode is None):
            # 실제로 기동됨 → 예약 플래그는 의미없도록 덮어씀
            effective_reserved = False
            break
        await asyncio.sleep(0.1)

    else:
        # 타임아웃 시 마지막 스냅샷으로 계산
        rec  = recorder_manager.get_status_recording(channel_id)
        resv = recorder_manager.get_status_reserved(channel_id)
        fsm_state = request.app.state.fsm.getState(channel_id)
        effective_reserved = bool(resv or (fsm_state == "WATCHING"))

    state = '녹화 중' if rec else ('예약녹화 중' if effective_reserved else '대기 중')
    return JSONResponse({
        'status': 'success',
        'message': '시작 요청을 접수했습니다.',
        'state': state
    })


# 개별 녹화중지 API 함수
@app.post("/api/stop_recording/{channel_id}")
async def api_stop_recording(channel_id: str, request: Request, login: Any = Depends(requireLogin)):
    # 중지 실행 + 마지막 파일명 획득
    last = await stopRecordingForChannel(request.app, channel_id)

    # 표시 우선순위: 녹화 > 예약 > 대기
    state    = recorder_manager.get_status_recording(channel_id)
    reserved = recorder_manager.get_status_reserved(channel_id)

    # FSM WATCHING도 예약으로 취급
    fsm_state = request.app.state.fsm.getState(channel_id)
    effective_reserved = bool(reserved or (fsm_state == "WATCHING"))

    # 녹화가 True면 무조건 '녹화 중'이 우선
    resolved = '녹화 중' if state else ('예약녹화 중' if effective_reserved else '대기 중')

    return JSONResponse({
        'status': 'success',
        'state': resolved,
        'filename': last or '녹화 파일이 없습니다.'
    })



# 모두 녹화시작 API 함수
@app.post("/api/start_all_recording")
async def api_start_all_recording(request: Request, login: Any = Depends(requireLogin)):
    try:
        body = await request.json()
        is_user_request = bool(body.get("is_user_request", False))
    except Exception:
        is_user_request = False

    results = await startRecordingForAllChannels(request.app, is_user_request=is_user_request)
    return JSONResponse({'status': 'success', 'message': '일괄 시작 요청 접수', 'channels_status': results})


# 모두 녹화중지 API 함수
@app.post("/api/stop_all_recording")
async def api_stop_all_recording(request: Request, login: Any = Depends(requireLogin)):
    await stopRecordingForAllChannels(request.app)
    return JSONResponse({'status': 'success', 'message': '일괄 중지 요청 접수'})


# 채널별 녹화 활성/비활성 토글 API 
@app.post("/api/toggle_record_enabled/{channel_id}")
async def toggle_record_enabled(channel_id: str, request: Request, login: Any = Depends(requireLogin)):
    async with request.app.state.channels_lock:
        chs = request.app.state.channels
        channel = next((c for c in chs if c['id'] == channel_id), None)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        before = toBool(channel.get('record_enabled', True))
        # 토글
        channel['record_enabled'] = (not before)
        # 저장 직전 스냅샷
        snapshot = list(chs)

    request.app.state.save_debounced(None)

    if channel['record_enabled'] is False:
        # OFF시 현재 감시/워커만 정리 (녹화 중이면 이번 회차는 유지, 다음 회차부터 반영)
        await request.app.state.fsm.onRecordEnabledChanged(channel_id, enabled=False)
    else:

        try:
            channel['status'] = "대기 중"
        except Exception:
            pass

    print(f"[DEBUG] toggle_record_enabled: {channel['name']}({channel_id}) {before} -> {channel['record_enabled']}")
    return {"status": "success", "channel_id": channel_id, "record_enabled": channel['record_enabled']}


# 웹 대시보드에 매트릭 API
@app.get("/api/sys_metrics")
async def api_sys_metrics():
    try:
        return JSONResponse(content=await asyncio.to_thread(_collectSysMetrics))
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[SYS_METRICS][ERROR] {e}\n{tb}")
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=500
        )


# 메인 페이지 라우트
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    config = request.app.state.config
    loginMode = config.get('loginMode', False)  # 로그인 모드 상태 확인

    # 로그인되지 않은 상태에서 로그인 모드가 활성화된 경우
    if loginMode and not request.session.get('logged_in'):
        return templates.TemplateResponse('index.html', {
            'request': request,
            'config': config,
            'loginMode': loginMode,
            'program_name': PROGRAM_NAME,
            'program_version': PROGRAM_VERSION
        })
    else:
        # 로그인된 상태 또는 로그인 모드가 비활성화된 경우
        return templates.TemplateResponse('index.html', {
            'request': request,
            'config': config,
            'loginMode': loginMode,
            'program_name': PROGRAM_NAME,
            'program_version': PROGRAM_VERSION
        })


# 로그인 라우트
@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        # 계정 정보를 로드합니다.
        account = loadAccount()
        
        # 계정 정보가 존재하고 비밀번호가 일치할 경우
        if account and account['username'] == username and check_password_hash(account['password'], password):
            # 세션에 로그인 상태 저장
            request.session['logged_in'] = True
            return JSONResponse(
                content={"status": "success", "message": "로그인 성공", "redirect_url": "/"},
                status_code=200
            )
        
        else:
            # 로그인 실패 시 JSON 응답을 반환 (리다이렉션 없음)
            return JSONResponse(
                content={"status": "error", "message": "아이디 또는 비밀번호가 올바르지 않습니다."},
                status_code=401,  # 401 Unauthorized 상태 코드
                headers={"Content-Type": "application/json"}
            )
    
    except Exception as e:
        # 예외 처리
        print(f"로그인 중 오류 발생: {e}")
        return JSONResponse(
            content={"status": "error", "message": "로그인 처리 중 오류가 발생했습니다."},
            status_code=500
        )


# 로그아웃 라우트
@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url='/', status_code=302)


# GET 요청을 처리하여 계정 생성 페이지를 렌더링
@app.get("/register")
async def register_page(request: Request):
    account = loadAccount()
    loginMode = request.app.state.config.get('loginMode', False)
    need_account = request.query_params.get('need_account') 

    if account:
        error_message = "이미 계정이 존재합니다. 추가 계정을 만들 수 없습니다."

        return templates.TemplateResponse(
            'register.html',
            {'request': request, 'error_message': error_message, 'loginMode': loginMode, 'program_version': PROGRAM_VERSION},
            status_code=400
        )

    return templates.TemplateResponse(
        'register.html',
        {
            'request': request,
            'loginMode': loginMode,
            'info_message': "로그인 모드를 켰습니다. 먼저 관리자 계정을 생성하세요." if need_account else None,
            'program_version': PROGRAM_VERSION
        }
    )


# 계정 생성 폼을 처리하는 POST 요청
@app.post("/register")
async def register(request: Request, username: str = Form(...), password: str = Form(...), password_confirm: str = Form(...)):
    account = loadAccount()
    
    # 계정이 이미 존재하면 계정 생성 금지
    if account:
        error_message = "이미 계정이 존재합니다. 추가 계정을 만들 수 없습니다."
        return templates.TemplateResponse('register.html', {
            'request': request,
            'error_message': error_message,
            'program_version': PROGRAM_VERSION
        }, status_code=400)

    # 비밀번호 확인
    if password != password_confirm:

        return templates.TemplateResponse('register.html', {
            'request': request,
            'error_message': "비밀번호가 일치하지 않습니다.",
            'program_version': PROGRAM_VERSION
        })

    # 비밀번호 해시화 후 계정 저장
    hashed_password = generate_password_hash(password)
    account = {"username": username, "password": hashed_password}
    saveAccount(account)

    # 계정 생성 후 메인 페이지로 리다이렉트
    return RedirectResponse(url='/', status_code=302)


# 계정 수정/삭제 관련 라우트
@app.post("/updateAccount")
async def updateAccount(
    request: Request, 
    username: str = Form(...), 
    current_password: str = Form(...), 
    new_password: str = Form(None), 
    new_password_confirm: str = Form(None), 
    action: str = Form(...)
):
    try:
        account = loadAccount()  # 계정 정보 로드

        # 로그를 추가하여 디버깅
        print(f"Received request for action: {action}")
        print(f"Username: {username}, Current Password: {current_password}, Action: {action}")

        if action == "update":  # 계정 수정
            if account:
                is_password_valid = check_password_hash(account['password'], current_password)
                if not is_password_valid:
                    return JSONResponse(
                        content={"status": "error", "message": "기존 비밀번호가 일치하지 않습니다."},
                        status_code=400
                    )
                if new_password != new_password_confirm:
                    return JSONResponse(
                        content={"status": "error", "message": "새 비밀번호가 일치하지 않습니다."},
                        status_code=400
                    )

                # 비밀번호 해시 업데이트
                hashed_password = generate_password_hash(new_password)
                account['username'] = username
                account['password'] = hashed_password
                saveAccount(account)

                # 세션 정리 및 로그아웃 처리
                request.session.clear()  # 기존 세션 제거
                return JSONResponse(
                    content={"status": "success", "message": "계정이 성공적으로 수정되었습니다. 로그아웃 후 메인 페이지로 이동합니다.", "redirect_url": "/logout"},
                    status_code=200
                )

        elif action == "delete":  # 계정 삭제
            print(f"Attempting to delete account: {username}")

            # 삭제는 username과 current_password만 필요
            if account and check_password_hash(account['password'], current_password):
                if os.path.exists(LOGIN_PATH):
                    os.remove(LOGIN_PATH)
                    request.session.clear()  # 세션 정리
                    return JSONResponse(
                        content={"status": "success", "message": "계정이 삭제되었습니다.", "redirect_url": "/logout"},
                        status_code=200
                    )
                else:
                    return JSONResponse(
                        content={"status": "error", "message": "삭제할 계정이 없습니다."},
                        status_code=400
                    )
            else:
                return JSONResponse(
                    content={"status": "error", "message": "기존 비밀번호가 일치하지 않습니다."},
                    status_code=400
                )
    except Exception as e:
        print(f"Exception: {str(e)}")  # 예외 발생 시 디버그 메시지 출력
        return JSONResponse(
            content={"status": "error", "message": f"계정 처리 중 오류 발생: {str(e)}"},
            status_code=500
        )


@app.post("/api/save_chzzk_cookies")
async def save_chzzk_cookies(request: Request, body: dict = Body(...), login: Any = Depends(requireLogin)):
    cookies = loadCookies()
    cookies["chzzk"] = {
        "NID_AUT": str(body.get("NID_AUT", "") or "").strip(),
        "NID_SES": str(body.get("NID_SES", "") or "").strip(),
    }

    saveCookies(cookies)
    request.app.state.cookies = cookies

    return {"status": "ok"}


@app.get("/api/check_chzzk_cookie")
async def api_check_chzzk_cookie(request: Request, login: Any = Depends(requireLogin)):
    cookies = loadCookies()
    result = await asyncio.to_thread(checkChzzkCookie, cookies)
    return result


@app.get("/api/check_cime_cookie")
async def api_check_cime_cookie(request: Request, login: Any = Depends(requireLogin)):
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
            adult = metadata.get("adult")
            can_watch_uhd = metadata.get("can_watch_uhd")
            selected = metadata.get("selected_playback_source")

            return {
                "ok": True,
                "message": (
                    f"씨미 쿠키가 적용되었습니다. "
                    f"adult={adult}, canWatchUhd={can_watch_uhd}, selected={selected}"
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


@app.post("/api/save_config")
async def save_config_api(request: Request, body: dict = Body(...), login: Any = Depends(requireLogin)):
    merged = saveConfig(body)                  
    request.app.state.config = merged            
    return {"status": "ok", "config": merged}   


# 계정 사용자 정보 전달 
@app.get("/user_info")
async def user_info(request: Request):
    account = loadAccount()
    config = request.app.state.config or {}
    loginMode  = bool(config.get('loginMode', False))
    enableTray = bool(config.get('enableTray', False))

    payload = {
        "config": {
            "loginMode":  loginMode,
            "enableTray": enableTray,   
        }
    }

    if account and request.session.get('logged_in'):
        username = account.get('username', 'Unknown User')
        payload.update({"logged_in": True, "username": username})
    else:
        payload.update({"logged_in": False, "username": None})

    return JSONResponse(content=payload)


@app.get("/channels", response_class=HTMLResponse)
async def channelsPage(request: Request, login: Any = Depends(requireLogin)):
    async with request.app.state.channels_lock:
        chs = list(request.app.state.channels)

    return templates.TemplateResponse('channels.html', {
        'request': request,
        'channels': chs,
        'program_version': PROGRAM_VERSION
    })


# 채널 추가 API 함수
@app.post("/api/channels")
async def addChannel(request: Request, login: Any = Depends(requireLogin)):
    try:
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="요청 본문이 비어 있습니다.")
        new_channel = await request.json()

        # 플랫폼/필드 보정
        if new_channel["platform"] == "cime":
            new_channel["extension"] = ".mp4"

        # recordWatchParty 정규화
        rw = toBool(new_channel.get("recordWatchParty", False))
        if new_channel.get("platform") == "cime":
            rw = False
        new_channel["recordWatchParty"] = rw

        # watchPartyExcludeTags 정규화(문자열/배열 모두 허용)
        def _norm_exclude(v):
            if isinstance(v, str):
                items = [s.strip() for s in v.split(",")]
            elif isinstance(v, list):
                items = [str(s).strip() for s in v]
            else:
                items = []
            seen = set(); res = []
            for s in items:
                k = s.lower()
                if s and k not in seen:
                    seen.add(k); res.append(s)
            return res

        new_channel["watchPartyExcludeTags"] = _norm_exclude(new_channel.get("watchPartyExcludeTags"))

        # channelId → id 치환
        if "channelId" in new_channel:
            new_channel["id"] = new_channel.pop("channelId")

        # 유효성 검사
        if new_channel["platform"] not in ["chzzk", "cime"]:
            raise HTTPException(status_code=400, detail="잘못된 플랫폼 값입니다.")
        required = ("platform", "id", "name", "output_dir", "quality", "extension")
        if not all(k in new_channel for k in required):
            raise HTTPException(status_code=400, detail="필수 필드가 누락되었습니다.")

        # 기본값
        new_channel.setdefault("record_enabled", True)

        # 락 안: 메모리만 수정
        async with request.app.state.channels_lock:
            chs = request.app.state.channels
            chs.append(new_channel)
            snapshot = list(chs)  # 저장용 스냅샷

        request.app.state.save_debounced(None)

        try:
            RecorderManager.setChannels(snapshot)
        except Exception as _e:
            print(f"[WARN] setChannels 실패(무시): {_e}")

        print("[DEBUG] 새 채널이 추가되었습니다.")
        return JSONResponse(content={'status': 'success'})

    except HTTPException as http_exc:
        print(f"[ERROR] 채널 추가 중 오류 발생: {http_exc.detail}")
        raise
    except Exception as e:
        print(f"[ERROR] 채널 추가 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="채널 추가 중 오류 발생")


# 채널 수정 API 함수
@app.put("/api/channels/{channel_id}")
async def editChannel(channel_id: str, request: Request, login: Any = Depends(requireLogin)):
    try:
        updated_channel = await request.json()

        # 락 안: 대상 찾고 메모리만 수정
        async with request.app.state.channels_lock:
            chs = request.app.state.channels
            target = next((c for c in chs if c.get('id') == channel_id), None)
            if not target:
                raise HTTPException(status_code=404, detail="Channel not found")

            # 유효 플랫폼 결정 (payload 우선)
            effective_platform = updated_channel.get("platform", target.get("platform"))

            # 씨미면 확장자 강제 .mp4
            if effective_platform == "cime":
                updated_channel["extension"] = ".mp4"

            # recordWatchParty 정규화
            if "recordWatchParty" in updated_channel:
                rw = toBool(updated_channel["recordWatchParty"])
                if effective_platform == "cime":
                    rw = False
                updated_channel["recordWatchParty"] = rw

            # watchPartyExcludeTags 정규화
            def _norm_exclude(v):
                if isinstance(v, str):
                    items = [s.strip() for s in v.split(",")]
                elif isinstance(v, list):
                    items = [str(s).strip() for s in v]
                else:
                    items = []
                seen = set(); res = []
                for s in items:
                    k = s.lower()
                    if s and k not in seen:
                        seen.add(k); res.append(s)
                return res

            if "watchPartyExcludeTags" in updated_channel:
                updated_channel["watchPartyExcludeTags"] = _norm_exclude(updated_channel["watchPartyExcludeTags"])

            updated_channel['id'] = channel_id 
            target.update(updated_channel)

            snapshot = list(chs)

        request.app.state.save_debounced(None)

        try:
            RecorderManager.setChannels(snapshot)
        except Exception as _e:
            print(f"[WARN] setChannels 실패(무시): {_e}")

        return JSONResponse(content={'status': 'success'})

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] 채널 수정 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="채널 수정 중 오류 발생")



# 채널 삭제 API 함수
@app.delete("/api/channels/{channel_id}")
async def deleteChannel(channel_id: str, request: Request, login: Any = Depends(requireLogin)):
    try:
        # 락 안: 메모리만 수정
        async with request.app.state.channels_lock:
            chs = request.app.state.channels
            new_list = [c for c in chs if c.get('id') != channel_id]
            if len(new_list) == len(chs):
                raise HTTPException(status_code=404, detail="Channel not found")

            chs[:] = new_list  # 제자리 갱신
            snapshot = list(chs)  # 저장용 스냅샷

        request.app.state.save_debounced(None)

        try:
            RecorderManager.setChannels(snapshot)
        except Exception as _e:
            print(f"[WARN] setChannels 실패(무시): {_e}")

        return JSONResponse(content={'status': 'success'})

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] 채널 삭제 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="채널 삭제 중 오류 발생")


@app.get("/cookies", response_class=HTMLResponse)
async def getCookies(request: Request, login: Any = Depends(requireLogin)):
    cookies = loadCookies()  # 디스크에서 항상 최신본
    cfg = request.app.state.config or {}

    return templates.TemplateResponse('cookies.html', {
        'request': request,
        'cookies': cookies,
        'loginMode': bool(cfg.get("loginMode", False)),
        'program_version': PROGRAM_VERSION
    })


@app.post("/cookies")
async def updateCookies(request: Request, login: Any = Depends(requireLogin)):
    try:
        new_cookies = await request.json()
        if not new_cookies:
            return JSONResponse(content={'status': 'error', 'message': '쿠키 데이터가 비어 있습니다.'}, status_code=400)

        saveCookies(new_cookies)
        request.app.state.cookies = loadCookies()

        print("[INFO] 쿠키 설정이 저장되었습니다.")
        return JSONResponse(content={'status': 'success'})

    except Exception as e:
        print(f"쿠키 업데이트 중 오류 발생: {e}")
        return JSONResponse(content={'status': 'error', 'message': '쿠키 업데이트 중 오류 발생'}, status_code=500)


# 파일관리 페이지
@app.get("/files", response_class=HTMLResponse)
async def filesPage(request: Request, login: Any = Depends(requireLogin)):
    cfg = request.app.state.config
    # roots 존재 여부와 무관하게, 스위치만으로 활성화
    enabled = bool(cfg.get("fileManagerEnabled"))
    roots = cfg.get("fileManagerRoots") or []

    return templates.TemplateResponse(
        "files.html",
        {
            "request": request,
            "loginMode": cfg.get("loginMode", False),
            "fm_enabled": enabled,
            "fm_roots": roots,
            "program_version": PROGRAM_VERSION,
        },
    )


# 사용량/목록 API
@app.get("/api/files/usage")
async def api_files_usage(request: Request, login: Any = Depends(requireLogin)):
    cfg = request.app.state.config
    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(cfg, request.app.state.channels)
    return {"status": "ok", "volumes": listDisks(roots)}


# 파일 목록
@app.get("/api/files/list")
async def api_files_list(
    request: Request,
    path: str,
    show_hidden: bool = Query(False),
    login: Any = Depends(requireLogin)
):
    cfg = request.app.state.config
    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(cfg, request.app.state.channels)
        busy  = busyFilePaths(recorder_manager, request.app.state.channels)

    rp = ensureInRoots(path, roots)
    items = listDir(rp, show_hidden=show_hidden)
    for it in items:
        it["locked"] = isLocked(it["path"], busy)
    return {"status": "ok", "path": rp, "items": items}


# 루트 목록
@app.get("/api/files/roots")
async def api_files_roots(request: Request, login: Any = Depends(requireLogin)):
    cfg = request.app.state.config
    if not cfg.get("fileManagerEnabled", False):
        raise HTTPException(status_code=403, detail="File manager disabled")

    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(cfg, request.app.state.channels)

    # 블랙리스트(오픈) 모드(["*"])일 땐 시스템 마운트 루트들을 반환
    if roots == ["*"]:
        roots_list = listMountRoots()
        default_path = os.path.expanduser("~") if os.path.isdir(os.path.expanduser("~")) else (roots_list[0] if roots_list else None)
    else:
        roots_list = roots
        default_path = roots_list[0] if roots_list else None

    return {"roots": roots_list, "default": default_path}


# 파일 목록 호출(프론트가 /api/files/ls 호출)
@app.get("/api/files/ls")
async def api_files_ls(
    request: Request,
    path: str,
    show_hidden: bool = Query(False),
    login: Any = Depends(requireLogin)
):
    cfg = request.app.state.config
    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(cfg, request.app.state.channels)
        busy  = busyFilePaths(recorder_manager, request.app.state.channels)

    # 1) 허용 루트 검사 → 403
    try:
        rp = ensureInRoots(path, roots)
    except PermissionError:
        # 프론트의 보안 안내문 포맷터가 이 문구/상태코드를 잡아줍니다.
        raise HTTPException(status_code=403, detail="outside allowed roots")
    except Exception:
        # 형식이 이상한 path 등은 400
        raise HTTPException(status_code=400, detail="invalid path")

    # 2) 존재/타입 검사 → 404
    if not os.path.isdir(rp):
        raise HTTPException(status_code=404, detail="path not found")

    # 3) 목록 조회(권한 문제 등) → 403
    try:
        items = listDir(rp, show_hidden=show_hidden)
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="path not found")
    except OSError:
        # 접근 금지/장치 오류 등 기타 OS 에러는 보수적으로 403
        raise HTTPException(status_code=403, detail="access denied")

    for it in items:
        it["locked"] = isLocked(it["path"], busy)

    return {"status": "ok", "path": rp, "items": items}


# 디스크 사용량
@app.get("/api/files/disk-usage")
async def api_files_disk_usage(request: Request, paths: List[str] = Query(default=None), login: Any = Depends(requireLogin)):
    cfg = request.app.state.config
    if paths:
        roots = [p for p in paths if os.path.isdir(p)]
    else:
        # paths 미지정: 마운트 루트 전체 또는 설정 루트 사용
        async with request.app.state.channels_lock:
            built = buildAllowedRoots(cfg, request.app.state.channels)
        roots = listMountRoots() if built == ["*"] else built

    usages = []
    for r in roots:
        try:
            d = diskUsageFor(r)
            # 프론트 표시용 label
            d["label"] = r
            usages.append(d)
        except Exception:
            continue
    return {"status":"ok", "usages": usages}


# 4) 파일 다운로드 
@app.get("/api/files/download")
async def api_files_download(request: Request, path: str, login: Any = Depends(requireLogin)):
    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(request.app.state.config, request.app.state.channels)

    rp = ensureInRoots(path, roots)
    if not os.path.isfile(rp):
        raise HTTPException(status_code=404, detail="File not found")

    filename = os.path.basename(rp)
    return FileResponse(rp, filename=filename, media_type="application/octet-stream")



# 경로 만들기 API
@app.post("/api/files/mkdir")
async def api_files_mkdir(request: Request, body: dict = Body(...), login: Any = Depends(requireLogin)):
    if request.app.state.config.get("fileManagerReadOnly", False):
        raise HTTPException(status_code=403, detail="Read-only mode")
    parent  = body.get("path")
    newName = body.get("new_name")
    if not parent or not newName:
        raise HTTPException(status_code=400, detail="path/new_name required")

    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(request.app.state.config, request.app.state.channels)
    parent = ensureInRoots(parent, roots)
    created = mkdirPath(parent, newName)
    return {"status":"ok", "created": created}


# 경로 수정 API
@app.post("/api/files/rename")
async def api_files_rename(request: Request, body: dict = Body(...), login: Any = Depends(requireLogin)):
    if request.app.state.config.get("fileManagerReadOnly", False):
        raise HTTPException(status_code=403, detail="Read-only mode")
    src = body.get("path"); newName = body.get("new_name")
    if not src or not newName:
        raise HTTPException(status_code=400, detail="path/new_name required")
    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(request.app.state.config, request.app.state.channels)
        busy  = busyFilePaths(recorder_manager, request.app.state.channels)
    src = ensureInRoots(src, roots)
    if isLocked(src, busy): raise HTTPException(status_code=423, detail="Locked (recording)")
    dst = renamePath(src, newName)
    return {"status":"ok", "path": dst}


# 파일 이동 API
@app.post("/api/files/move")
async def api_files_move(request: Request, body: dict = Body(...), login: Any = Depends(requireLogin)):
    if request.app.state.config.get("fileManagerReadOnly", False):
        raise HTTPException(status_code=403, detail="Read-only mode")

    srcs = body.get("srcs") or ([] if not body.get("src") else [body.get("src")])
    dstDir = body.get("dst_dir")
    if not srcs or not dstDir:
        raise HTTPException(status_code=400, detail="src/srcs and dst_dir required")

    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(request.app.state.config, request.app.state.channels)
        busy  = busyFilePaths(recorder_manager, request.app.state.channels)

    dstDir = ensureInRoots(dstDir, roots)
    moved = []
    for s in srcs:
        rp = ensureInRoots(s, roots)
        if isLocked(rp, busy):
            raise HTTPException(status_code=423, detail=f"Locked: {rp}")
        moved.append(movePath(rp, dstDir))
    return {"status":"ok", "moved": moved}


# 파일 삭제 API
@app.post("/api/files/delete")
async def api_files_delete(request: Request, body: dict = Body(...), login: Any = Depends(requireLogin)):
    if request.app.state.config.get("fileManagerReadOnly", False):
        raise HTTPException(status_code=403, detail="Read-only mode")

    paths = body.get("paths") or ([] if not body.get("path") else [body.get("path")])
    hard  = bool(body.get("hard", False))
    if not paths:
        raise HTTPException(status_code=400, detail="paths or path required")

    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(request.app.state.config, request.app.state.channels)
        busy  = busyFilePaths(recorder_manager, request.app.state.channels)

    def pickRootFor(p: str) -> str:
        rp = normPath(p)
        # 와일드카드 모드(["*"])에서는 같은 폴더 아래 .trash 사용
        if roots == ["*"]:
            return os.path.dirname(rp)
        candidates = [r for r in roots if rp.startswith(normPath(r))]
        if not candidates:
            raise PermissionError("Outside roots")
        return max(candidates, key=lambda r: len(normPath(r)))

    deleted = []
    for p in paths:
        rp = ensureInRoots(p, roots)
        if isLocked(rp, busy):
            raise HTTPException(status_code=423, detail=f"Locked: {rp}")
        if hard or not request.app.state.config.get("trashEnabled", True):
            hardDelete(rp)
            deleted.append(rp)
        else:
            rootForTrash = pickRootFor(rp)
            deleted.append(softDelete(rp, rootForTrash))
    return {"status":"ok", "deleted": deleted}


# 파일매니저 스트림복사 API함수
@app.post("/api/files/streamcopy")
async def api_files_streamcopy(
    request: Request,
    body: dict = Body(...),
    login: Any = Depends(requireLogin),
):
    # 읽기전용 모드 차단
    if request.app.state.config.get("fileManagerReadOnly", False):
        raise HTTPException(status_code=403, detail="Read-only mode")

    srcs = body.get("paths") or []
    if not srcs:
        raise HTTPException(status_code=400, detail="paths required")

    async with request.app.state.channels_lock:
        roots = buildAllowedRoots(request.app.state.config, request.app.state.channels)
        busy  = busyFilePaths(recorder_manager, request.app.state.channels)

    results = []

    for s in srcs:
        try:
            rp = ensureInRoots(s, roots)
            if not os.path.isfile(rp):
                raise HTTPException(status_code=400, detail=f"Not a file: {rp}")
            if isLocked(rp, busy):
                raise HTTPException(status_code=423, detail=f"Locked (recording): {rp}")

            # 공용 함수 사용 
            from module.file_manager import streamCopyFile
            dst = await asyncio.to_thread(streamCopyFile, rp)

            results.append({"src": rp, "dst": dst, "ok": True})
        except HTTPException as he:
            results.append({"src": s, "error": he.detail, "ok": False})
        except Exception as e:
            results.append({"src": s, "error": str(e), "ok": False})

    return {"status": "ok", "results": results}


# 텔레그램/디스코드 알림 API 테스트 함수
@app.get("/api/test_notification/{target}")
async def testNotification(target: str, request: Request, login: Any = Depends(requireLogin)):
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
async def configPage(request: Request, login: Any = Depends(requireLogin)):
    config_data = loadConfig()           
    request.app.state.config = config_data   
    account = loadAccount()             
    notification = loadNotification()

    # 채널 분배 UI를 위해 channels 전달
    async with request.app.state.channels_lock:
        channels = [dict(c) for c in request.app.state.channels]

    return templates.TemplateResponse('config.html', {
        'request': request,
        'config': config_data,
        'account': account,
        'notification': notification,
        'channels': channels,           
        'program_version': PROGRAM_VERSION
    })


@app.get("/api/config")
async def get_config_api():
    return loadConfig()


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
    loginMode: Optional[str] = Form(None),
    fileManagerEnabled: Optional[str] = Form(None),
    fileManagerRoots: List[str] = Form([]),
    fileManagerMode: str = Form("blacklist"),
    fileManagerReadOnly: Optional[str] = Form(None),
    trashEnabled: Optional[str] = Form(None),

    telegram_enabled: str = Form("off"),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    discord_enabled: str = Form("off"),
    discord_webhook_url: str = Form(""),

    notify_record_started: str = Form("off"),
    notify_record_finished: str = Form("off"),
    notify_record_start_failed: str = Form("on"),
    notify_record_abnormally_stopped: str = Form("on"),
    notify_record_user_stopped: str = Form("off"),
    notify_postprocess_finished: str = Form("on"),
    notify_postprocess_failed: str = Form("on"),
    notify_cookie_auth_failed: str = Form("on"),
    notify_watchparty_skipped: str = Form("off"),
    notify_disk_space_low: str = Form("on"),
    notify_dedupe_seconds: int = Form(300),
    notify_disk_space_low_gb: int = Form(10),
    login: Any = Depends(requireLogin)
):
    try:
        # 1) 기존 설정 로드
        current_config = loadConfig() or {}

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

        # 3) 파일매니저(웹 전용)
        normalized_roots = _normalizeAllowedRoots(fileManagerRoots)
        effective_fm_enabled = toBool(loginMode) and toBool(fileManagerEnabled)

        # 4) 분할/오버랩/오토스탑
        _split_on = toBool(splitRecordingMode) if splitRecordingMode is not None else bool(current_config.get("splitRecordingMode", False))
        try:
            if splitOverlapSec is None:
                ov = int(current_config.get("splitOverlapSec", 0) or 0)
            else:
                ov = int(splitOverlapSec or 0)
        except Exception:
            ov = int(current_config.get("splitOverlapSec", 0) or 0)
        if ov < 0: ov = 0
        if ov > 30: ov = 30
        if not _split_on:
            ov = 0

        # autoStopInterval: 분할 ON일 때만, 누락 시 기존값 유지
        try:
            if _split_on:
                _auto_stop = int(autoStopInterval) if autoStopInterval is not None else int(current_config.get("autoStopInterval", 0) or 0)
            else:
                _auto_stop = 0
        except Exception:
            _auto_stop = 0 if not _split_on else int(current_config.get("autoStopInterval", 0) or 0)

        # 5) 트레이
        _enable_tray   = toBool(enableTray) if enableTray is not None else bool(current_config.get("enableTray", False))
        _tray_on_close = toBool(minimizeToTrayOnClose) if minimizeToTrayOnClose is not None else bool(current_config.get("minimizeToTrayOnClose", False))
        _tray_on_start = toBool(minimizeToTrayOnStart) if minimizeToTrayOnStart is not None else bool(current_config.get("minimizeToTrayOnStart", False))
        if not _enable_tray:
            _tray_on_close = False
            _tray_on_start = False

        # 6) 재탐색/파일명
        try:
            _recheck = int(recheckInterval) if recheckInterval is not None else int(current_config.get("recheckInterval", 60))
        except Exception:
            _recheck = int(current_config.get("recheckInterval", 60))
        _pattern = filenamePattern if (filenamePattern not in (None, "")) else current_config.get("filenamePattern", "[{start_time}] {safe_live_title}")

        # 7) 이동경로: 누락 시 기존값 유지(빈 문자열은 None)
        _move_path = (
            current_config.get("moveAfterProcessing")
            if moveAfterProcessing is None
            else (moveAfterProcessing or None)
        )

        # gpuCount 정규화
        try:
            _gc = int(gpuCount) if gpuCount is not None else int(current_config.get("gpuCount", 1) or 1)
        except Exception:
            _gc = int(current_config.get("gpuCount", 1) or 1)
        _gc = 2 if _gc == 2 else 1         

        # 후처리 옵션(GPU0): 폼 누락(None)일 때 기존값 유지
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

        # 폼 누락으로 기존값 유지된 경우 확인용 디버그
        if (video_codec is None or preset is None or video_quality is None):
            print(f"[DEBUG][CFG] GPU0 폼 누락 → 기존값 유지: codec={_video_codec0}, preset={_preset0}, q={_video_quality0}")

        # GPU1 옵션 누락(None)이면 기존값/또는 GPU0 값으로 fallback
        _vc1 = video_codec_gpu1 if (video_codec_gpu1 not in (None, "")) else current_config.get("video_codec_gpu1", _video_codec0)
        _pr1 = preset_gpu1      if (preset_gpu1      not in (None, "")) else current_config.get("preset_gpu1", _preset0)

        _pp1_raw = postprocess_resolution_gpu1 if postprocess_resolution_gpu1 is not None else current_config.get("postprocess_resolution_gpu1", _pp0)
        _pp1 = str(_pp1_raw or _pp0).strip().lower()
        if _pp1 not in _allowed_res:
            _pp1 = _pp0

        _ubm1 = toBool(use_bitrate_mode_gpu1) if use_bitrate_mode_gpu1 is not None else bool(current_config.get("use_bitrate_mode_gpu1", _use_bitrate_mode0))

        try:
            _vq1 = int(video_quality_gpu1) if video_quality_gpu1 is not None else int(current_config.get("video_quality_gpu1", _video_quality0) or _video_quality0)
        except Exception:
            _vq1 = int(current_config.get("video_quality_gpu1", _video_quality0) or _video_quality0)

        _vb1 = video_bitrate_gpu1 if video_bitrate_gpu1 is not None else current_config.get("video_bitrate_gpu1", _video_bitrate0)

        # GPU1의 vbv/extra는 None이면 기존값 유지, 누락이면 사용자가 비운 값으로 반영
        _vbv_maxrate1 = vbv_maxrate_gpu1 if vbv_maxrate_gpu1 is not None else current_config.get("vbv_maxrate_gpu1", "")
        _vbv_bufsize1 = vbv_bufsize_gpu1 if vbv_bufsize_gpu1 is not None else current_config.get("vbv_bufsize_gpu1", "")
        _extra_opts1  = extra_ffmpeg_options_gpu1 if extra_ffmpeg_options_gpu1 is not None else current_config.get("extra_ffmpeg_options_gpu1", "")
        _ac1 = audio_codec_gpu1   if (audio_codec_gpu1   not in (None, "")) else current_config.get("audio_codec_gpu1", _audio_codec0)
        _ab1 = audio_bitrate_gpu1 if audio_bitrate_gpu1 is not None else current_config.get("audio_bitrate_gpu1", _audio_bitrate0)


        # 8) 새 설정 구성(없으면 기존값 유지 원칙)
        new_config = {
            **current_config,
            "autoRecordingMode":            toBool(autoRecordingMode),
            "enableTray":                   _enable_tray,
            "minimizeToTrayOnClose":        _tray_on_close,
            "minimizeToTrayOnStart":        _tray_on_start,
            "plugin_type":                  normalized_plugin,
            "timemachine_time_shift":       normalized_shift,
            "autoPostProcessing":           toBool(autoPostProcessing),
            "deleteAfterPostProcessing":    toBool(deleteAfterPostProcessing),
            "removeFixedPrefix":            toBool(removeFixedPrefix),
            "moveAfterProcessingEnabled":   toBool(moveAfterProcessingEnabled),
            "moveAfterProcessing":          _move_path,
            "postNewWindow":                toBool(postNewWindow),
            "recheckInterval":              _recheck,
            "filenamePattern":              _pattern,
            "splitRecordingMode":           _split_on,
            "splitPostProcessing":          toBool(splitPostProcessing),
            "autoStopInterval":             _auto_stop,
            "splitOverlapSec":              ov,
            "stream_copy":                  _stream_copy0,
            "video_codec":                  _video_codec0,
            "preset":                       _preset0,
            "use_bitrate_mode":             _use_bitrate_mode0,
            "postprocess_resolution":      _pp0,
            "video_quality":                _video_quality0,
            "video_bitrate":                _video_bitrate0,
            "vbv_maxrate":                  _vbv_maxrate0,
            "vbv_bufsize":                  _vbv_bufsize0,
            "extra_ffmpeg_options":         _extra_opts0,
            "audio_codec":                  _audio_codec0,
            "audio_bitrate":                _audio_bitrate0,
            "gpuCount":                     _gc,
            "video_codec_gpu1":             _vc1,
            "preset_gpu1":                  _pr1,
            "postprocess_resolution_gpu1": _pp1,
            "use_bitrate_mode_gpu1":        _ubm1,
            "video_quality_gpu1":           _vq1,
            "video_bitrate_gpu1":           _vb1,
            "vbv_maxrate_gpu1":             _vbv_maxrate1,
            "vbv_bufsize_gpu1":             _vbv_bufsize1,
            "extra_ffmpeg_options_gpu1":    _extra_opts1,
            "audio_codec_gpu1":             _ac1,
            "audio_bitrate_gpu1":           _ab1, 
            "loginMode":                    toBool(loginMode),
            "fileManagerEnabled":           effective_fm_enabled,
            "fileManagerRoots":             normalized_roots,
            "fileManagerMode":              fileManagerMode if fileManagerMode in ("blacklist","whitelist") else "blacklist",
            "fileManagerReadOnly":          toBool(fileManagerReadOnly),
            "trashEnabled":                 toBool(trashEnabled),
        }

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

        # 9) 알림 필수값 체크
        error_message = ""

        if notification_data["telegram_enabled"]:
            if not notification_data["telegram_bot_token"] or not notification_data["telegram_chat_id"]:
                error_message = "텔레그램 알림 사용 시 봇 토큰과 채팅방 ID를 모두 입력해야 합니다."

        if not error_message and notification_data["discord_enabled"]:
            if not notification_data["discord_webhook_url"]:
                error_message = "디스코드 알림 사용 시 웹훅 URL을 입력해야 합니다."

        if error_message:
            config_data = new_config
            account = loadAccount()

            async with request.app.state.channels_lock:
                channels = [dict(c) for c in request.app.state.channels]

            return templates.TemplateResponse("config.html", {
                "request": request,
                "config": config_data,
                "account": account,
                "channels": channels,
                "notification": notification_data,
                "error_message": error_message,
            })

        # 10) 설정 저장
        print("[DEBUG] 설정 저장 중...")
        saveConfig(new_config)
        saveNotification(notification_data)
        request.app.state.config = new_config
        print("[DEBUG] 설정 저장 완료")

        # gpuCount==2일 때만 채널 분배 반영
        if _gc == 2 and gpuAssignmentsJson:
            try:
                mapping = json.loads(gpuAssignmentsJson)   
                if isinstance(mapping, dict):
                    changed = False
                    snap = None

                    async with request.app.state.channels_lock:
                        for ch in request.app.state.channels:
                            plat = str(ch.get("platform", "")).strip().lower()
                            cid  = str(ch.get("id", "")).strip()
                            if plat != "chzzk" or not cid:
                                continue

                            key = f"{plat}:{cid}"
                            if key not in mapping and cid not in mapping:
                                continue

                            raw = None
                            if key in mapping:
                                raw = mapping[key]
                            elif cid in mapping:
                                raw = mapping[cid]  # 구버전 호환
                            else:
                                continue

                            try:
                                gi = 1 if int(raw) == 1 else 0
                            except Exception:
                                gi = 0

                            if ch.get("gpu_index") != gi:
                                ch["gpu_index"] = gi
                                changed = True

                        if changed:
                            snap = [dict(c) for c in request.app.state.channels]

                    if snap is not None:
                        await asyncio.to_thread(saveChannels, snap)

            except Exception as e:
                print(f"[WARN] gpuAssignmentsJson parse/apply failed: {e}")

        if new_config.get('loginMode', False):
            account = loadAccount()
            if not account:
                return RedirectResponse(url="/register?need_account=1", status_code=303)

        return RedirectResponse(url="/", status_code=303)

    except Exception as e:
        print(f"[ERROR] 설정 저장 중 오류 발생: {e}")
        return JSONResponse(
            content={"status": "error", "message": "설정 저장 중 오류 발생: " + str(e)},
            status_code=500
        )


@app.get("/get_config")
async def get_config(request: Request):
    try:
        return {"status": "success", "config": request.app.state.config}
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
        # 자동녹화는 lifespan에서 시작하므로 여기서는 서버만 띄움
        print("[DEBUG] runUvicorn 호출")
        await runUvicorn()

    except Exception as e:
        print(f"[ERROR] 서버 실행 중 오류 발생: {e}")
        try:
            recordException("recordWEB.runAutomodeServer", e)
        except Exception:
            pass


# 트레이 아이콘 기동 함수 (수정본)
def startWebTray():
    try:
        cfg = loadConfig() or {}
        enable_tray = bool(cfg.get("enableTray", False))
        if not enable_tray:
            return

        # pystray/Pillow 사용 가능 여부 확인
        if pystray is None or Image is None:
            print("[WARN] 최소화 트레이 기능이 활성화 되었지만 pystray 또는 Pillow가 설치되지 않았습니다.")
            return

        # Windows 권장
        if os.name != "nt":
            print("[WARN] 최소화 트레이 기능이 활성화 되었지만 현재 OS에서 트레이가 보장되지 않습니다.")

        # 모듈 네임스페이스 별칭 (함수 내부 re-import 불필요)
        Menu     = pystray.Menu
        MenuItem = pystray.MenuItem
        Icon     = pystray.Icon

        icon_path = os.path.join(BASE_DIR, "templates", "static", "img", "tray_icon.png")

        try:
            img = Image.open(icon_path)
        except Exception:
            # 못 열면 투명 64x64 placeholder
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            print(f"[WARN] tray icon open failed: {icon_path}")

        def _open_browser():
            import webbrowser
            try:
                webbrowser.open(getBaseUrl())
            except Exception:
                pass

        def _start_all():
            import requests
            try:
                requests.post(f"{getBaseUrl()}/api/start_all_recording",
                              json={"is_user_request": True}, timeout=5)
            except Exception:
                pass

        def _stop_all():
            import requests
            try:
                requests.post(f"{getBaseUrl()}/api/stop_all_recording",
                              json={"is_user_request": True}, timeout=5)
            except Exception:
                pass

        def _on_quit(icon, item):
            icon.visible = False
            os._exit(0)

        menu = Menu(
            MenuItem("브라우저 열기", _open_browser),
            Menu.SEPARATOR,
            MenuItem("모두 녹화 시작", _start_all),
            MenuItem("모두 녹화 중지", _stop_all),
            Menu.SEPARATOR,
            MenuItem("종료", _on_quit),
        )

        # 현재 스레드 블록하지 않음
        Icon("recordWEB", img, "recordWEB", menu).run_detached()
        print("[DEBUG] Web tray icon started (detached).")

    except Exception as e:
        print(f"[WARN] 트레이 초기화 중 예외: {e}")



if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)

    setupAppLogging("web")
    try:
        _runtime_guard = RuntimeGuard("recordWEB").acquire()
        validateRuntimeEnvironment("recordWEB")
    except RuntimeAlreadyRunning as e:
        print(f"[FATAL] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[WARN] 시작 전 안정성 점검 실패(계속 진행): {e}")
        try:
            recordException("recordWEB.startup", e)
        except Exception:
            pass

    config_data = loadConfig() 
    port = config_data.get('port', 5000) 

    internal_ip, local_ip, external_ip = getAddresses()

    # 프로그램 이름과 버전 출력
    print(f"Starting {PROGRAM_NAME} version {PROGRAM_VERSION}")

    # 프로그램 첫 실행 시 FFmpeg와 Streamlink 경로 확인
    checkRequiredPaths()

    if internal_ip and "오류" not in internal_ip:
        print(f"* 로컬호스트 주소로 접속 http://{internal_ip}:{port}")

    if local_ip and "오류" not in local_ip:
        print(f"* 내부 사설 IP 주소로 접속 http://{local_ip}:{port}")

    if external_ip and "오류" not in external_ip:
        print(f"* 공인 IP 주소로 접속 http://{external_ip}:{port}")

    # 먼저 트레이를 비차단 방식으로 띄운 뒤,
    if config_data.get("enableTray", False):
        startWebTray()
        try:
            minimizeConsole()
        except Exception:
            pass

    # 기존과 동일하게 비동기 서버를 실행
    asyncio.run(runAutomodeServer())
