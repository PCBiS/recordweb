import os
from typing import Any, Dict, List

from module.data_manager import (
    loadConfig, loadChannels, getFFmpeg, getStreamlink, CONFIG_PATH,
    CHANNELS_PATH, COOKIE_PATH
)


def _exists_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path)


def validateRuntimeEnvironment(mode: str = "app", *, print_result: bool = True) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    info: List[str] = []

    try:
        cfg = loadConfig() or {}
    except Exception as e:
        cfg = {}
        errors.append(f"config.json 로드 실패: {e}")

    try:
        channels = loadChannels() or []
    except Exception as e:
        channels = []
        errors.append(f"channels.json 로드 실패: {e}")

    for label, path in [
        ("config.json", CONFIG_PATH),
        ("channels.json", CHANNELS_PATH),
    ]:
        if not _exists_file(path):
            warnings.append(f"{label} 파일을 찾을 수 없습니다: {path}")

    try:
        ffmpeg = getFFmpeg()
        streamlink = getStreamlink()
    except Exception as e:
        ffmpeg = streamlink = ""
        errors.append(f"의존성 경로 조회 실패: {e}")

    for label, path in [
        ("ffmpeg", ffmpeg),
        ("streamlink", streamlink),
    ]:
        if not path:
            warnings.append(f"{label} 경로가 비어 있습니다.")
        elif not os.path.exists(path):
            warnings.append(f"{label} 실행 파일을 찾을 수 없습니다: {path}")
        else:
            info.append(f"{label} OK: {path}")

    if not isinstance(channels, list):
        errors.append("channels.json 형식이 list가 아닙니다.")
        channels = []

    seen = set()
    for idx, ch in enumerate(channels):
        if not isinstance(ch, dict):
            warnings.append(f"channels[{idx}] 항목이 dict가 아닙니다.")
            continue

        cid = str(ch.get("id") or "").strip()
        platform = str(ch.get("platform") or "").strip().lower()
        if not cid:
            warnings.append(f"channels[{idx}] id가 비어 있습니다.")
        elif cid in seen:
            warnings.append(f"중복 채널 id 감지: {cid}")
        else:
            seen.add(cid)

        if platform not in ("chzzk", "cime"):
            warnings.append(f"{cid or idx}: platform 값이 예상과 다릅니다: {platform!r}")

        if platform == "cime" and ch.get("extension") != ".mp4":
            warnings.append(f"{cid}: 씨미 채널 확장자가 .mp4가 아닙니다. 현재값={ch.get('extension')!r}")

    report = {
        "mode": mode,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "channel_count": len(channels),
        "config_keys": sorted(list(cfg.keys())) if isinstance(cfg, dict) else [],
    }

    if print_result:
        prefix = f"[VALIDATE][{mode}]"
        print(f"{prefix} 채널 {len(channels)}개, errors={len(errors)}, warnings={len(warnings)}")
        for msg in errors:
            print(f"{prefix}[ERROR] {msg}")
        for msg in warnings[:20]:
            print(f"{prefix}[WARN] {msg}")
        if len(warnings) > 20:
            print(f"{prefix}[WARN] ... 외 {len(warnings) - 20}개")

    return report
