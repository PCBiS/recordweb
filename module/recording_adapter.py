import asyncio
import os
from typing import Optional, Dict, Any

from module.data_manager import RecorderManager, loadCookies
from module.runtime_log import debugThrottle
from module.live_recorder import chzzkStartRecording, getLiveMetadata
from module.cime_recorder import cimeStartRecording, getCimeMetadata

recorder_manager = RecorderManager()

# 외부에서 필요한 심볼만 노출
__all__ = ["fetchMetadata", "startSession"]


async def fetchMetadata(channel: dict, platform: str) -> Optional[Dict[str, Any]]:
    p = (platform or "").lower()
    cid = (channel.get("id") or "unknown")

    if p == "chzzk":
        # 캐시 미스/실제 조회 직전에만 (채널별 30초에 1번) 디버그
        debugThrottle(f"meta:chzzk:{cid}",
                      f"[DEBUG] fetchMetadata(chzzk:{cid}) : 채널정보 업데이트 중",
                      min_secs=30.0)
        cookies = loadCookies() or {}
        return await getLiveMetadata(channel, cookies)

    if p == "cime":
        debugThrottle(f"meta:cime:{cid}",
                      f"[DEBUG] fetchMetadata(cime:{cid}) : 채널정보 업데이트 중",
                      min_secs=30.0)
        return await getCimeMetadata(channel)

    return None


async def startSession(channel, platform, cfg, is_user_request: bool = False):
    p = (platform or "").lower()

    if p == "chzzk":
        raw_plugin = (channel.get("plugin_type") or cfg.get("plugin_type", "basic")).lower()
        plugin = raw_plugin if raw_plugin in ("basic", "timemachine_plus") else "basic"

        try:
            shift = int(cfg.get("timemachine_time_shift", 0) or 0)
        except Exception:
            shift = 0
        if plugin == "basic":
            shift = max(0, min(10, shift))
        else:
            shift = max(0, min(3600, shift))

        cookies = loadCookies() or {}

        post_cfg = {
            "dscMinimize":                cfg.get("dscMinimize", False),
            "stream_copy":                cfg.get("stream_copy", True),
            "preset":                     cfg.get("preset", "medium"),
            "use_bitrate_mode":           cfg.get("use_bitrate_mode", False),
            "video_bitrate":              cfg.get("video_bitrate", "1000k"),
            "video_codec":                cfg.get("video_codec", "libx264"),
            "video_quality":              cfg.get("video_quality", "23"),
            "audio_codec":                cfg.get("audio_codec", "aac"),
            "audio_bitrate":              cfg.get("audio_bitrate", "192k"),
            "extra_ffmpeg_options":       cfg.get("extra_ffmpeg_options", ""),
            "moveAfterProcessingEnabled": cfg.get("moveAfterProcessingEnabled", False),
            "moveAfterProcessing":        cfg.get("moveAfterProcessing", ""),
            "postNewWindow":              cfg.get("postNewWindow", False),
        }

        await chzzkStartRecording(
            channel=channel,
            cookies=cookies,
            recheckInterval=cfg.get("recheckInterval", 60),
            autoStopInterval=cfg.get("autoStopInterval", 0),
            autoPostProcessing=cfg.get("autoPostProcessing", False),
            filenamePattern=cfg.get("filenamePattern", "[{start_time}] {safe_live_title}"),
            plugin_type=plugin,
            timemachine_time_shift=shift,
            is_user_request=is_user_request,          
            splitRecordingMode=cfg.get("splitRecordingMode", False),
            post_cfg=post_cfg                   
        )
        return

    if p == "cime":
        await cimeStartRecording(
            channel=channel,
            recheckInterval=cfg.get("recheckInterval", 60),
            filenamePattern=cfg.get("filenamePattern", "[{start_time}] {channel_name} {safe_live_title} {record_quality}{file_extension}"),
            moveAfterProcessingEnabled=cfg.get("moveAfterProcessingEnabled", False),
            moveAfterProcessing=cfg.get("moveAfterProcessing", ""),
            is_user_request=is_user_request,
        )
        return
