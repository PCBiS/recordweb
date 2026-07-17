import asyncio
import contextlib
from typing import Any, Dict, List

from module.data_manager import RecorderManager, loadConfig, loadChannels, saveChannels
from module.channel_fsm import ChannelFsm


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "y", "on"):
            return True
        if v in ("0", "false", "no", "n", "off"):
            return False
    return default


class RecordingCore:
    def __init__(self, mode: str = "web"):
        self.mode = mode
        self.fsm = ChannelFsm()
        self.channels_lock = asyncio.Lock()
        self.started = False

    async def prepare(self):
        config = loadConfig() or {}
        channels = loadChannels() or []

        channels = self.normalizeChannels(channels)

        RecorderManager.setChannels(channels)
        RecorderManager.setChannelsRef(channels)
        RecorderManager.setChannelsLockRef(self.channels_lock)

        self.config = config
        self.channels = channels
        return self

    def normalizeChannels(self, channels: List[Dict[str, Any]]):
        changed = False

        for ch in channels:
            if not isinstance(ch, dict):
                continue

            plat = str(ch.get("platform") or "").strip().lower()

            if plat == "youtube":
                plat = "cime"
                changed = True

            if plat:
                ch["platform"] = plat

            if "auto_record" in ch and "record_enabled" not in ch:
                ch["record_enabled"] = _to_bool(ch.get("auto_record"), True)
                del ch["auto_record"]
                changed = True

            if "record_enabled" not in ch:
                ch["record_enabled"] = True
                changed = True
            else:
                fixed_enabled = _to_bool(ch.get("record_enabled"), True)
                if ch.get("record_enabled") is not fixed_enabled:
                    ch["record_enabled"] = fixed_enabled
                    changed = True

            if "recordWatchParty" in ch:
                fixed_watch_party = _to_bool(ch.get("recordWatchParty"), False)
                if ch.get("recordWatchParty") is not fixed_watch_party:
                    ch["recordWatchParty"] = fixed_watch_party
                    changed = True

            if plat == "cime":
                cid = str(ch.get("id") or "").strip()

                if cid.startswith("https://ci.me/") or cid.startswith("http://ci.me/"):
                    cid = cid.replace("https://ci.me/", "").replace("http://ci.me/", "")
                    cid = cid.replace("/live", "").strip("/")

                cid = cid.lstrip("@").strip()

                if cid:
                    fixed_cid = "@" + cid
                    if ch.get("id") != fixed_cid:
                        ch["id"] = fixed_cid
                        changed = True

                if ch.get("extension") != ".mp4":
                    ch["extension"] = ".mp4"
                    changed = True

                if ch.get("recordWatchParty"):
                    ch["recordWatchParty"] = False
                    changed = True

            ext = str(ch.get("extension") or "").strip()
            if ext and not ext.startswith("."):
                ch["extension"] = "." + ext
                changed = True

        if changed:
            with contextlib.suppress(Exception):
                saveChannels(channels)

        return channels

    async def startWatching(self, *, respect_auto_mode: bool = False):
        if self.started:
            return

        auto_mode = bool((self.config or {}).get("autoRecordingMode", False))
        if respect_auto_mode and not auto_mode:
            print("[CORE] autoRecordingMode=False → 자동 감시 시작하지 않음")
            return

        await self.fsm.startAllWatching()
        self.started = True
        print(f"[CORE] recording core started mode={self.mode}")

    async def stop(self):
        if not self.started:
            return

        try:
            await self.fsm.stopAll()
        finally:
            self.started = False
            print(f"[CORE] recording core stopped mode={self.mode}")