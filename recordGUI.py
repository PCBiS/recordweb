import os
import subprocess
import sys
import time

def install_missing_modules():
    if os.name == 'nt':
        missing_modules = [
            "requests", "httpx", "fastapi", "uvicorn", "aiohttp",
            "jinja2", "pygetwindow", "werkzeug", "itsdangerous",
            "python-multipart", "starlette", "psutil", "cryptography",
            "PyQt6", "qasync", "pystray", "pillow", "py-cpuinfo"
        ]
    else:
        missing_modules = [
            "requests", "httpx", "fastapi", "uvicorn", "aiohttp",
            "jinja2", "werkzeug", "itsdangerous", "python-multipart",
            "starlette", "psutil", "cryptography", "PyQt6", "qasync",
            "pystray", "pillow", "py-cpuinfo"
        ]

    installed_modules = []

    for module in missing_modules:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "show", module],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError:
            installed_modules.append(module)

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


if __name__ == "__main__":
    install_missing_modules()

    import os, sys, subprocess, time 
    from module.runtime_guard import RuntimeGuard, RuntimeAlreadyRunning
    from module.config_validator import validateRuntimeEnvironment
    from module.runtime_log import setupAppLogging, recordException

    setupAppLogging("gui")
    try:
        _runtime_guard = RuntimeGuard("recordGUI").acquire()
        validateRuntimeEnvironment("recordGUI")
    except RuntimeAlreadyRunning as e:
        print(f"[FATAL] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[WARN] 시작 전 안정성 점검 실패(계속 진행): {e}")
        try:
            recordException("recordGUI.startup", e)
        except Exception:
            pass

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    SENTINEL = os.path.join(BASE_DIR, ".shutdown")

    backend_module = "gui.recordWorker"
    gui_module     = "gui.mainGUI"

    # ② 동일 인터프리터로 서브 프로세스 실행
    backend_cmd = [sys.executable, "-m", backend_module]
    gui_cmd     = [sys.executable, "-m", gui_module]

    def start_process(cmd):
        print("Starting process:", " ".join(cmd))
        return subprocess.Popen(cmd, cwd=BASE_DIR)

    worker = start_process(backend_cmd)
    time.sleep(2)
    gui = start_process(gui_cmd)

    try:
        while True:
            time.sleep(1.0)

            # 사용자가 트레이에서 '완전 종료'를 누른 경우 (mainGUI가 .shutdown 생성)
            if os.path.exists(SENTINEL):
                print("Shutdown sentinel detected. Stopping both GUI and worker...")

                if gui.poll() is None:
                    gui.terminate()
                    try: gui.wait(5)
                    except Exception: gui.kill()

                if worker.poll() is None:
                    worker.terminate()
                    try: worker.wait(5)
                    except Exception: worker.kill()

                try: os.remove(SENTINEL)
                except Exception: pass
                break

            # 평소와 동일한 자동 재시작 로직
            if gui.poll() is not None:
                print("GUI exited. Restarting...")
                gui = start_process(gui_cmd)
            if worker.poll() is not None:
                print("Worker exited. Restarting...")
                worker = start_process(backend_cmd)
    finally:
        # 안전 정리
        for p in (gui, worker):
            if p and p.poll() is None:
                p.terminate()
                try: p.wait(3)
                except Exception: p.kill()
