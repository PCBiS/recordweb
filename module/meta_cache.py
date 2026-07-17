import asyncio 
import time 
import random
from typing import Any, Dict, List, Callable, Awaitable, Tuple, Optional

from module.runtime_log import debugThrottle


# 기본 주기
FAST_TTL      = 20       # 라이브(OPEN/is_live=True)일 때 메타 TTL
OFFLINE_TTL   = 90       # 종료 상태일 때 메타 TTL
THUMB_TTL     = 300      # 썸네일 TTL (5분)
BACKOFF_STEPS = [30, 60, 120, 300]  # 에러 시 지수 백오프
JITTER_SEC    = 3


def _now() -> float:
    return time.time()


# app.state에 캐시 관련 상태를 보장
def ensure(app) -> None:
    if not hasattr(app.state, "meta_cache"):
        app.state.meta_cache = {}  # {cid: {"data":dict|None,"ts_meta":float,"ts_thumb":float,"err":int}}
    if not hasattr(app.state, "meta_lock"):
        app.state.meta_lock = asyncio.Lock()
    if not hasattr(app.state, "refreshing"):
        app.state.refreshing = set()


def isOpen(platform: str, data: Optional[Dict[str, Any]]) -> bool:
    if not data:
        return False
    p = (platform or "").lower()
    if p == "chzzk":
        return data.get("status") == "OPEN"
    if p == "cime":
        return bool(data.get("is_live")) or data.get("status") == "OPEN"
    return False


async def getCached(app, cid: str) -> Optional[Dict[str, Any]]:
    async with app.state.meta_lock:
        return app.state.meta_cache.get(cid)


async def setCached(app, cid: str, data: Optional[Dict[str, Any]] = None, touch_meta: bool = False, touch_thumb: bool = False,
                    clear_error: bool = False,) -> None:
    async with app.state.meta_lock:
        ent = app.state.meta_cache.get(cid) or {"data": None, "ts_meta": 0.0, "ts_thumb": 0.0, "err": 0}
        if data is not None:
            ent["data"] = data
        if touch_meta:
            ent["ts_meta"] = _now()
        if touch_thumb:
            ent["ts_thumb"] = _now()
        if clear_error:
            ent["err"] = 0
        app.state.meta_cache[cid] = ent


async def bumpError(app, cid: str) -> None:
    async with app.state.meta_lock:
        ent = app.state.meta_cache.get(cid) or {"data": None, "ts_meta": _now(), "ts_thumb": _now(), "err": 0}
        ent["err"] = min(ent.get("err", 0) + 1, len(BACKOFF_STEPS))
        app.state.meta_cache[cid] = ent


# 채널 dict에 제목/카테고리/썸네일만 반영
def mergeChannelFields(real: Dict[str, Any], data: Dict[str, Any]) -> bool:
    changed = False
    platform = str(real.get("platform") or data.get("platform") or "").lower()
    default_thumb = "/static/img/cimeclosed_thumbnail.png" if platform == "cime" else "/static/img/default_thumbnail.png"
    mapping = [
        ("live_title",    "live_title",    "방송 제목 없음"),
        ("category",      "category",      "카테고리 없음"),
        ("thumbnail_url", "thumbnail_url", default_thumb),
    ]
    for dst, src, default in mapping:
        nv = data.get(src, default)
        if not nv:
            continue
        old = real.get(dst)

        # 기존 값이 있고, 신규 값이 플레이스홀더라면 덮어쓰지 않음
        if old and nv == default:
            continue

        if old != nv:
            real[dst] = nv
            changed = True
    return changed


async def refreshOneChannel(app, channel: Dict[str, Any], fetcher: Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]],
                            saveChannels: Callable[[List[Dict[str, Any]]], None], channels_lock: asyncio.Lock, need_meta: bool = True,
                            need_thumb: bool = True,) -> None:
    cid = channel["id"]
    try:
        data = await fetcher(channel)
        if data:
            await setCached(app, cid, data, touch_meta=need_meta, touch_thumb=need_thumb, clear_error=True)

            # 채널 리스트에도 반영하되, 디스크 저장은 하지 않음(메모리만 갱신)
            async with channels_lock:
                real = next((c for c in app.state.channels if c["id"] == cid), None)
                if real:
                    mergeChannelFields(real, data)  
        else:
            await bumpError(app, cid)

    except Exception:
        await bumpError(app, cid)

    finally:
        app.state.refreshing.discard(cid)


async def refreshLoop(app, fetcher: Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]], saveChannels: Callable[[List[Dict[str, Any]]], None],
                      channels_lock: asyncio.Lock,) -> None:
    # 주기적으로 채널 메타/썸네일 캐시 갱신
    ensure(app)
    while True:
        async with channels_lock:
            chs = list(app.state.channels)
        now = _now()
        for ch in chs:
            cid = ch["id"]
            platform = (ch.get("platform") or "").lower()
            ent = await getCached(app, cid) or {"data": None, "ts_meta": 0.0, "ts_thumb": 0.0, "err": 0}
            data = ent["data"]
            open_state = isOpen(platform, data)
            ttl_meta = FAST_TTL if open_state else OFFLINE_TTL
            ttl_thumb = THUMB_TTL
            backoff = BACKOFF_STEPS[min(ent.get("err", 0), len(BACKOFF_STEPS)-1)] if ent.get("err", 0) > 0 else 0

            need_meta  = (now - ent["ts_meta"]  > max(ttl_meta,  backoff))
            need_thumb = (now - ent["ts_thumb"] > max(ttl_thumb, backoff))

            if not (need_meta or need_thumb):
                continue
            if cid in app.state.refreshing:
                continue
            app.state.refreshing.add(cid)
            asyncio.create_task(
                refreshOneChannel(app, ch, fetcher, saveChannels, channels_lock, need_meta, need_thumb)
            )
        await asyncio.sleep(3 + random.uniform(-JITTER_SEC, JITTER_SEC))


# meta_cache.py
async def getMetadataCached(app, channel_id: str, platform: str, fetcher: Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]],
                            saveChannels: Callable[[List[Dict[str, Any]]], None], channels_lock: asyncio.Lock,) -> Tuple[Dict[str, Any], bool, bool]:

    ensure(app)
    ent = await getCached(app, channel_id)
    data = ent["data"] if ent else None
    now = _now()
    open_state = isOpen(platform, data)
    ttl = FAST_TTL if open_state else OFFLINE_TTL
    fresh = bool(ent and (now - ent["ts_meta"] <= ttl))

    # 캐시 미스면 "즉시 1회" 동기 수집
    if data is None:
        async with channels_lock:
            ch = next((c for c in app.state.channels if c["id"] == channel_id), None)
        if ch:
            if channel_id not in app.state.refreshing:
                app.state.refreshing.add(channel_id)
                try:
                    res = await fetcher(ch)
                    if res:
                        await setCached(
                            app,
                            channel_id,
                            res,
                            touch_meta=True,
                            touch_thumb=True,
                            clear_error=True,
                        )

                        # 채널 필드 병합 (런타임 메모리만)
                        async with channels_lock:
                            real = next((c for c in app.state.channels if c["id"] == channel_id), None)
                            if real:
                                mergeChannelFields(real, res)
                        return res, False, True

                finally:
                    app.state.refreshing.discard(channel_id)

        # 즉시 수집 실패 → 백그라운드 예약만 걸고 기본값 반환
        if ch and (channel_id not in app.state.refreshing):
            app.state.refreshing.add(channel_id)
            asyncio.create_task(
                refreshOneChannel(app, ch, fetcher, saveChannels, channels_lock, need_meta=True, need_thumb=False)
            )

        fallback_thumb = "/static/img/cimeclosed_thumbnail.png" if platform == "cime" else "/static/img/default_thumbnail.png"
        default = {
            "live_title": "방송 제목 없음",
            "category": "카테고리 없음",
            "thumbnail_url": fallback_thumb,
            "is_live": False if platform == "cime" else None,
        }
        return default, False, False

    # (2) 데이터는 있으나 stale이면 백그라운드 갱신만
    if not fresh and (channel_id not in app.state.refreshing):
        async with channels_lock:
            ch = next((c for c in app.state.channels if c["id"] == channel_id), None)
        if ch:
            app.state.refreshing.add(channel_id)
            asyncio.create_task(
                refreshOneChannel(app, ch, fetcher, saveChannels, channels_lock, need_meta=True, need_thumb=False)
            )

    # (3) 캐시값 반환
    return data, True, fresh



# 각 채널 썸네일 URL 리스트 반환
async def getThumbnailsCached(app, channels: List[Dict[str, Any]], fetcher: Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]],
                              saveChannels: Callable[[List[Dict[str, Any]]], None], channels_lock: asyncio.Lock,) -> List[Dict[str, Any]]:

    ensure(app)
    out = []
    now = _now()
    for ch in channels:
        cid = ch["id"]
        platform = (ch.get("platform") or "").lower()
        ent = await getCached(app, cid)
        data = ent["data"] if ent else None
        fresh_thumb = bool(ent and (now - ent["ts_thumb"] <= THUMB_TTL))

        if not fresh_thumb and (cid not in app.state.refreshing):
            app.state.refreshing.add(cid)
            asyncio.create_task(
                refreshOneChannel(app, ch, fetcher, saveChannels, channels_lock, need_meta=False, need_thumb=True)
            )

        if data and isinstance(data, dict):
            thumb = data.get("thumbnail_url")
        else:
            thumb = "/static/img/cimeclosed_thumbnail.png" if platform == "cime" else "/static/img/default_thumbnail.png"

        # 썸네일 URL 로그 (실제 URL일 때만, 채널/플랫폼별 60초에 1번)
        if isinstance(thumb, str) and thumb.startswith("http"):
            debugThrottle(
                f"thumb:{platform}:{cid}",
                f"Generated thumbnail URL: {thumb}",
                min_secs=60.0
            )

        out.append({"id": cid, "thumbnail_url": thumb})

    # 채널 객체에도 반영
    async with channels_lock:
        changed = False
        for item in out:
            real = next((c for c in app.state.channels if c["id"] == item["id"]), None)
            if real and real.get("thumbnail_url") != item["thumbnail_url"]:
                real["thumbnail_url"] = item["thumbnail_url"]
                changed = True
        if changed:
            saveChannels(app.state.channels)

    return out