import argparse
import asyncio
import contextlib
import signal

from module.app_bootstrap import RecordingCore
from module.data_manager import RecorderManager
from module.runtime_guard import RuntimeGuard, RuntimeAlreadyRunning
from module.config_validator import validateRuntimeEnvironment
from module.runtime_log import setupAppLogging, recordException


async def statusPing(core: RecordingCore, interval: int = 30):
    mgr = RecorderManager()

    while True:
        rec = 0
        rsv = 0
        idle = 0

        for ch in RecorderManager.getChannels() or []:
            cid = ch.get("id")
            if not cid:
                continue

            if mgr.get_status_recording(cid):
                rec += 1
            elif mgr.get_status_reserved(cid):
                rsv += 1
            else:
                idle += 1

        print(f"[LITE] status rec={rec} reserved={rsv} idle={idle}")
        await asyncio.sleep(interval)


def installSignalHandlers(stop_event: asyncio.Event):
    loop = asyncio.get_running_loop()

    def requestStop():
        if not stop_event.is_set():
            print("[LITE] 종료 요청 감지")
            stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, requestStop)
        loop.add_signal_handler(signal.SIGTERM, requestStop)
    except Exception:
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(requestStop))


async def mainAsync(verbose: bool = False):
    stop_event = asyncio.Event()
    installSignalHandlers(stop_event)

    core = await RecordingCore(mode="lite").prepare()
    await core.startWatching(respect_auto_mode=False)

    tasks = set()

    if verbose:
        t = asyncio.create_task(statusPing(core))
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    try:
        await stop_event.wait()
    finally:
        await core.stop()

        for t in list(tasks):
            t.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    setupAppLogging("lite")
    try:
        _runtime_guard = RuntimeGuard("recordLITE").acquire()
        validateRuntimeEnvironment("recordLITE")
    except RuntimeAlreadyRunning as e:
        print(f"[FATAL] {e}")
        return
    except Exception as e:
        print(f"[WARN] 시작 전 안정성 점검 실패(계속 진행): {e}")
        try:
            recordException("recordLITE.startup", e)
        except Exception:
            pass

    try:
        asyncio.run(mainAsync(verbose=args.verbose))
    except Exception as e:
        try:
            recordException("recordLITE.main", e)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()