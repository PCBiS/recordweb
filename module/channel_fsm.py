import asyncio
import os
import time
import signal
import random
import subprocess
import contextlib
from typing import Dict, Optional

from module.data_manager import RecorderManager, loadConfig, notifyEvent, last_notified_state
from module.recording_adapter import startSession

try:
    from module.runtime_log import NotLiveError
except Exception:
    class NotLiveError(Exception):
        pass


class ChannelFsm:
    def __init__(self):
        self.rm = RecorderManager()
        self.state: Dict[str, str] = {}                 # cid -> "STOPPED|WATCHING|RECORDING|ERROR"
        self.locks: Dict[str, asyncio.Lock] = {}        # cid -> lock
        self.watchTask: Dict[str, asyncio.Task] = {}    # cid -> task
        self.recordTask: Dict[str, asyncio.Task] = {}   # cid -> task
        self.backoffUntil: Dict[str, float] = {}        # cid -> monotonic timestamp
        self.restartAttempts: Dict[str, int] = {}       # cid -> 연속 실패 횟수(백오프 계산용)


    # 공용 API
    def getState(self, channelId: str) -> str:
        return self.state.get(channelId, "STOPPED")


    # 실제 녹화 프로세스 생존 확인 유틸
    def _procAlive(self, cid: str) -> bool:
        proc = self.rm.get_tasks_process(cid)
        return bool(proc and proc.returncode is None)  # None이면 아직 종료되지 않음


    async def userStart(self, channelId: str, is_user_request: bool = False):
        async with self._lock(channelId):
            ch = self._findChannel(channelId)
            if not ch:
                self._setStopped(channelId)
                return

            # 자동/대량 시작은 record_enabled 필요, 수동은 1회 시작 허용
            if (not ch.get("record_enabled", True)) and (not is_user_request):
                self._setStopped(channelId)
                return

            self._resetBackoff(channelId)

            # 1) 이미 실제 녹화 프로세스가 살아있으면: 상태만 동기화
            if self.rm.get_status_recording(channelId) or self._procAlive(channelId):
                self._setRecording(channelId)
                return

            # 2) 아이돔포턴트 가드
            if not self.rm.guard_try_acquire_start(channelId):
                return
            try:
                cur = self.getState(channelId)

                if cur == "RECORDING":
                    return

                if cur == "WATCHING":
                    wt = self.watchTask.get(channelId)
                    if wt and not wt.done():
                        return
                    self._setWatching(channelId)
                    self._spawnWatch(channelId, is_user_request=is_user_request)
                    return

                self._setWatching(channelId)
                self._spawnWatch(channelId, is_user_request=is_user_request)
            finally:
                self.rm.guard_release_start(channelId)


    async def userStop(self, channelId: str):
        async with self._lock(channelId):
            ch = self._findChannel(channelId)
            name = (ch.get("name") if ch else channelId)

            # 1) 루프가 즉시 감지할 수 있도록 먼저 STOP 플래그
            self.rm.set_is_user_stopped(channelId, True)
            self.rm.set_status_reserved(channelId, False)

            # 2) 워커/프로세스 정지
            await self._stopAllWorkers(channelId)

            # 3) 상태 마킹 (필요 시)
            self._setStopped(channelId)

            # 4) 사용자 중지 알림 (중복 방지)
            try:
                new_state = "사용자중지"
                if last_notified_state.get(channelId) != new_state:
                    notifyEvent(
                        "record_user_stopped",
                        "녹화 사용자 중지",
                        f"{name} 녹화를 사용자 요청으로 중지했습니다.",
                        channel_id=channelId,
                        channel_name=name,
                        severity="info"
                    )
                    last_notified_state[channelId] = new_state
            except Exception as _e:
                print(f"[WARN] userStop notification failed: {_e}")



    async def startAllWatching(self):
        tasks = []
        for ch in (self.rm.getChannels() or []):
            if ch.get("record_enabled", True):
                tasks.append(asyncio.create_task(self.userStart(ch.get("id"))))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


    async def stopAll(self):
        for ch in (self.rm.getChannels() or []):
            await self.userStop(ch.get("id"))


    async def _killProcessTree(self, cid: str, timeout: float = 3.0):
        proc = self.rm.get_tasks_process(cid)
        if not proc or proc.returncode is not None:
            return

        try:
            if os.name == "nt":
                # 1차: CTRL_BREAK (CREATE_NEW_PROCESS_GROUP 필요)
                with contextlib.suppress(Exception):
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                    await asyncio.wait_for(proc.wait(), timeout=2.0)

                # 2차: 트리 강제 종료
                if proc.returncode is None:
                    with contextlib.suppress(Exception):
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)

                # 3차: 최종 강제
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

            else:
                # POSIX: 세션/그룹 단위 종료
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=timeout)

                if proc.returncode is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        proc.kill()
                    await proc.wait()
        finally:
            # 프로세스 핸들 정리
            with contextlib.suppress(Exception):
                self.rm.clear_tasks_process(cid)

    # WEB/Worker에서 상태 전환 반응성 상승
    async def onRecordEnabledChanged(self, channelId, enabled: bool):
        async with self._lock(channelId):
            cur = self.getState(channelId)

            if enabled:
                if cur == "STOPPED":
                    self._setWatching(channelId)
                    self._spawnWatch(channelId) 
                return

            # 녹화 중이면 현재 회차는 녹화유지
            if self.rm.get_status_recording(channelId) or self._procAlive(channelId):
                self.rm.set_status_reserved(channelId, False) 
                return

            # 녹화 중이 아니면 감시/워커만 정리
            await self._stopAllWorkers(channelId)
            self._setStopped(channelId)


    # 내부 유틸
    def _lock(self, cid: str) -> asyncio.Lock:
        self.locks.setdefault(cid, asyncio.Lock())
        return self.locks[cid]


    def _findChannel(self, cid: str) -> Optional[dict]:
        return next((c for c in (self.rm.getChannels() or []) if c.get("id") == cid), None)


    def _setStopped(self, cid: str):
        self.state[cid] = "STOPPED"
        self.rm.set_status_reserved(cid, False)
        self.rm.set_status_recording(cid, False)
        self.rm.set_is_user_stopped(cid, True)
        # 사용자 의도 정지 → 백오프 해제
        self.restartAttempts[cid] = 0
        self.backoffUntil[cid] = 0


    def _setWatching(self, cid: str):
        self.state[cid] = "WATCHING"
        self.rm.set_is_user_stopped(cid, False)
        self.rm.set_status_reserved(cid, True)
        self.rm.set_status_recording(cid, False)


    def _setRecording(self, cid: str):
        self.state[cid] = "RECORDING"
        self.rm.set_status_reserved(cid, False)
        self.rm.set_status_recording(cid, True)
        # 실제 녹화 시작 시점엔 더 이상 '사용자중지' 아님
        self.rm.set_is_user_stopped(cid, False)


    async def _stopAllWorkers(self, cid: str):
        # watch/record 태스크 종료
        await self._cancelTask(self.watchTask.pop(cid, None))
        await self._cancelTask(self.recordTask.pop(cid, None))

        # ★ 실제 녹화 프로세스 트리 종료 (핵심 수정)
        await self._killProcessTree(cid)

        # 기존 워커(코루틴) 취소 루틴은 유지
        with contextlib.suppress(Exception):
            await self.rm.force_terminate_worker(cid)


    async def _cancelTask(self, task: Optional[asyncio.Task]):
        if not task:
            return
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


    def _inBackoff(self, cid: str) -> bool:
        return self.backoffUntil.get(cid, 0) > time.monotonic()


    def _resetBackoff(self, cid: str):
        self.restartAttempts[cid] = 0
        self.backoffUntil[cid] = 0


    def _applyBackoff(self, cid: str):
        attempts = self.restartAttempts.get(cid, 0) + 1
        self.restartAttempts[cid] = attempts
        delay = min(600, 10 * attempts * attempts)  # 기본 정책

        # 자동녹화모드에서는 과도한 지연 방지
        cfg = loadConfig() or {}
        if cfg.get("autoRecordingMode", False):
            base = int(cfg.get("recheckInterval", 60))
            delay = min(delay, max(10, base))  # 최소 10초, 최대 폴링주기

        self.backoffUntil[cid] = time.monotonic() + delay


    # 폴링 간격에 15% 지터, 최소 5초 보장
    async def _sleepWithJitter(self, base_seconds: int):
        jitter = random.uniform(0.85, 1.15)
        await asyncio.sleep(max(5, int(base_seconds * jitter)))


    # Watch Loop
    def _spawnWatch(self, cid: str, is_user_request: bool = False):
        # 이미 실행 중이면 재진입 방지
        if self.watchTask.get(cid) and not self.watchTask[cid].done():
            return

        async def _run():
            ch = self._findChannel(cid)
            if not ch:

                await self.userStop(cid)
                return

            if self.rm.get_is_user_stopped(cid):
                await self.userStop(cid)
                return

            # 수동 요청이면 record_enabled=False라도 1회만 허용
            if (not ch.get("record_enabled", True)) and (not is_user_request):
                await self.userStop(cid)
                return

            # WATCHING 표기만 유지 — 실제 “재탐색/녹화/후처리/재진입”은 startSession 안에서 모두 수행
            self._setWatching(cid)

            try:
                cfg = loadConfig() or {}
                await startSession(ch, (ch.get("platform") or "").lower(), cfg, is_user_request=is_user_request)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                # 세션 에러도 플랫폼 루프에서 대부분 흡수하므로 최소 처리
                print(f"[FSM][ERROR] watch task crashed for {cid}: {e}")
                self._setWatching(cid)

            finally:
                # 토글이 켜져 있고 사용자 중지가 아니면 다시 WATCHING 유지
                ch2 = self._findChannel(cid)
                if not ch2 or not ch2.get("record_enabled", True) or self.rm.get_is_user_stopped(cid):
                    self._setStopped(cid)

                else:
                    # 세션이 내부 루프를 다 돌고 반환된 경우: 다시 WATCHING
                    self._setWatching(cid)

                    # 조기반환/예외로 루프가 끊겨도 지연 후 재스폰
                    try:
                        cfg = loadConfig() or {}
                        base = int(cfg.get("recheckInterval", 60))
                    except Exception:
                        base = 60

                    async def _respawn():
                        await self._sleepWithJitter(base)  # 15% 지터 포함 대기
                        ch3 = self._findChannel(cid)
                        if ch3 and ch3.get("record_enabled", True) and not self.rm.get_is_user_stopped(cid):
                            self._spawnWatch(cid)  # 동일 채널 watch 루프 재스폰

                    asyncio.create_task(_respawn())

        self.watchTask[cid] = asyncio.create_task(_run())
