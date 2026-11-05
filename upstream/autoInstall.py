import os
import time
import sys
import subprocess
import shutil
import zipfile


# 7-Zip url
SEVEN_ZR_URL = "https://www.7-zip.org/a/7zr.exe"


def install_and_check_requests():
    required_modules = ["requests"]

    for module_name in required_modules:
        try:
            __import__(module_name)
            print(f"'{module_name}' 모듈이 이미 설치되어 있습니다.")
        except ImportError:
            print(f"'{module_name}' 모듈이 설치되지 않았습니다. 설치를 진행합니다...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", module_name])
                print(f"'{module_name}' 모듈 설치 완료.")

                __import__(module_name)
                print(f"'{module_name}' 모듈이 정상적으로 설치되었습니다.")
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"모듈 설치 중 오류 발생: {e}") from e
            except ImportError as e:
                raise RuntimeError(f"'{module_name}' 모듈이 설치되었으나, 정상적으로 불러올 수 없습니다.") from e


def ensure_directory(path):
    if not os.path.exists(path):
        os.makedirs(path)
        print(f"디렉토리 생성: {path}")
    else:
        print(f"디렉토리 존재: {path}")


def safe_remove(path, tries=10, delay=0.5):
    for _ in range(tries):
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except PermissionError:
            time.sleep(delay)
        except OSError:
            time.sleep(delay)
    return False


def safe_rmtree(path, tries=10, delay=0.5):
    for _ in range(tries):
        try:
            if os.path.exists(path):
                shutil.rmtree(path)
            return True
        except PermissionError:
            time.sleep(delay)
        except OSError:
            time.sleep(delay)
    return False


def download_file(requests, url, dest_path):
    def _fmt_bytes(n: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB"]
        x = float(max(0, n))
        i = 0
        while x >= 1024.0 and i < len(units) - 1:
            x /= 1024.0
            i += 1
        if i == 0:
            return f"{int(x)}{units[i]}"
        return f"{x:.2f}{units[i]}"

    def _fmt_hms(sec: float) -> str:
        if sec is None or sec <= 0 or sec != sec:  # NaN 방지
            return "--:--"
        s = int(sec)
        h = s // 3600
        m = (s % 3600) // 60
        s = s % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    filename = os.path.basename(dest_path) or dest_path
    print(f"다운로드 시작: {filename}")
    print(f"URL: {url}")

    # stream=True로 큰 파일도 메모리 폭발 없이 청크 단위로 받음
    r = requests.get(url, stream=True, timeout=(10, 60))
    if r.status_code != 200:
        raise Exception(f"다운로드 실패: HTTP {r.status_code}")

    total = 0
    try:
        total = int(r.headers.get("Content-Length") or 0)
    except Exception:
        total = 0

    downloaded = 0
    t0 = time.time()
    last_print = t0

    # 출력이 너무 자주 갱신되지 않게(콘솔 스팸 방지)
    PRINT_INTERVAL = 0.2  # 초

    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):  # 256KB
            if not chunk:
                continue

            f.write(chunk)
            downloaded += len(chunk)

            now = time.time()
            if now - last_print < PRINT_INTERVAL:
                continue
            last_print = now

            elapsed = max(0.001, now - t0)
            speed = downloaded / elapsed  # bytes/sec
            speed_str = f"{_fmt_bytes(int(speed))}/s"

            if total > 0:
                pct = (downloaded / total) * 100.0
                eta = (total - downloaded) / max(1.0, speed)
                msg = (
                    f"\r[{pct:6.2f}%] "
                    f"{_fmt_bytes(downloaded)} / {_fmt_bytes(total)} | "
                    f"{speed_str} | ETA {_fmt_hms(eta)}"
                )
            else:
                # 서버가 Content-Length를 안 주는 경우(진행률 대신 받은 용량/속도만 표시)
                msg = (
                    f"\r[ ---- ] "
                    f"{_fmt_bytes(downloaded)} 다운로드 중 | "
                    f"{speed_str} | 경과 {_fmt_hms(elapsed)}"
                )

            sys.stdout.write(msg)
            sys.stdout.flush()


def get_7z_extractor(requests, base_dir):
    for exe_name in ("7z.exe", "7zr.exe", "7za.exe"):
        found = shutil.which(exe_name)
        if found:
            return found, None 

    tmp_dir = os.path.join(base_dir, "_tmp_7zip")
    ensure_directory(tmp_dir)
    seven_exe = os.path.join(tmp_dir, "7zr.exe")

    if not os.path.exists(seven_exe):
        download_file(requests, SEVEN_ZR_URL, seven_exe)

    return seven_exe, tmp_dir


def extract_and_rename(requests, archive_path, dependent_dir, desired_name, seven_exe=None):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    temp_extract_dir = os.path.join(base_dir, "temp_extract")

    # 이전 실행 찌꺼기 방지
    if os.path.exists(temp_extract_dir):
        ok = safe_rmtree(temp_extract_dir)

        if (not ok) or os.path.exists(temp_extract_dir):
            raise Exception(
                f"임시 압축해제 폴더를 삭제하지 못했습니다: {temp_extract_dir}\n"
                f"autoInstall을 다시 실행하기 전에 해당 폴더가 사용 중인지 확인하세요."
            )

    ensure_directory(temp_extract_dir)

    ext = os.path.splitext(archive_path)[1].lower()
    print(f"압축 해제 중: {archive_path} -> {temp_extract_dir}")

    if ext == ".zip":
        with zipfile.ZipFile(archive_path, "r") as z:
            z.extractall(path=temp_extract_dir)

    elif ext == ".7z":
        if not seven_exe:
            raise Exception("7z 해제용 실행 파일 경로가 없습니다(seven_exe).")

        cmd = [seven_exe, "x", archive_path, f"-o{temp_extract_dir}", "-y"]
        subprocess.check_call(cmd)

    else:
        raise Exception(f"지원하지 않는 압축 포맷: {ext}")

    # 압축 풀린 최상위 엔트리 확인
    entries = [e for e in os.listdir(temp_extract_dir) if e and e not in (".", "..")]
    if not entries:
        raise Exception("압축 해제 결과가 비었습니다.")

    final_path = os.path.join(dependent_dir, desired_name)
    if os.path.exists(final_path):
        ok = safe_rmtree(final_path)

        if (not ok) or os.path.exists(final_path):
            raise Exception(
                f"기존 설치 폴더를 삭제하지 못했습니다: {final_path}\n"
                f"recordWEB/recordGUI, ffmpeg.exe, ffprobe.exe, streamlink.exe가 실행 중이면 모두 종료한 뒤 다시 실행하세요."
            )

    # 보통 배포 ZIP/7Z는 최상위 폴더 1개를 가짐
    if len(entries) == 1 and os.path.isdir(os.path.join(temp_extract_dir, entries[0])):
        extracted_folder = os.path.join(temp_extract_dir, entries[0])
        shutil.move(extracted_folder, final_path)

    else:
        # 최상위 폴더가 없거나 여러 개면, desired_name 폴더를 만들고 그 안으로 모두 이동
        ensure_directory(final_path)
        for e in entries:
            shutil.move(os.path.join(temp_extract_dir, e), os.path.join(final_path, e))

    print(f"압축 해제 및 폴더명 변경 완료: {final_path}")

    safe_rmtree(temp_extract_dir)


def extract_single_file_to_dir(archive_path, final_dir, target_filename, final_filename=None, seven_exe=None):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    temp_extract_dir = os.path.join(base_dir, f"temp_extract_{os.path.splitext(target_filename)[0]}")

    if os.path.exists(temp_extract_dir):
        ok = safe_rmtree(temp_extract_dir)

        if (not ok) or os.path.exists(temp_extract_dir):
            raise Exception(
                f"임시 압축해제 폴더를 삭제하지 못했습니다: {temp_extract_dir}\n"
                f"autoInstall을 다시 실행하기 전에 해당 폴더가 사용 중인지 확인하세요."
            )

    ensure_directory(temp_extract_dir)

    try:
        ext = os.path.splitext(archive_path)[1].lower()
        print(f"압축 해제 중: {archive_path} -> {temp_extract_dir}")

        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as z:
                z.extractall(path=temp_extract_dir)

        elif ext == ".7z":
            if not seven_exe:
                raise Exception("7z 해제용 실행 파일 경로가 없습니다(seven_exe).")

            cmd = [seven_exe, "x", archive_path, f"-o{temp_extract_dir}", "-y"]
            subprocess.check_call(cmd)

        else:
            raise Exception(f"지원하지 않는 압축 포맷: {ext}")

        found_path = None
        target_lower = target_filename.lower()

        for root, _, files in os.walk(temp_extract_dir):
            for f in files:
                if f.lower() == target_lower:
                    found_path = os.path.join(root, f)
                    break

            if found_path:
                break

        if not found_path:
            raise Exception(f"{target_filename} 파일을 찾을 수 없습니다.")

        ensure_directory(final_dir)

        final_filename = final_filename or target_filename
        final_path = os.path.join(final_dir, final_filename)

        if os.path.exists(final_path):
            ok = safe_remove(final_path)

            if (not ok) or os.path.exists(final_path):
                raise Exception(
                    f"기존 파일을 삭제하지 못했습니다: {final_path}\n"
                    f"{final_filename}이 실행 중이면 종료한 뒤 다시 실행하세요."
                )

        shutil.move(found_path, final_path)
        print(f"{target_filename} 이동 완료 → {final_path}")
        return final_path

    finally:
        safe_rmtree(temp_extract_dir)


def main():
    install_and_check_requests()
    import requests  # noqa: E402  (설치 이후 import)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    dependent_dir = os.path.join(base_dir, "dependent")
    ensure_directory(dependent_dir)

    downloads = {
        "ffmpeg": {
            "url": "https://github.com/GyanD/codexffmpeg/releases/download/8.0.1/ffmpeg-8.0.1-full_build.7z",
            "desired_name": "ffmpeg"
        },

        "streamlink": {
            "url": "https://github.com/streamlink/windows-builds/releases/download/7.5.0-1/streamlink-7.5.0-1-py313-x86_64.zip",
            "desired_name": "streamlink"
        },

        "aria2c": {
            "url": "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip",
            "desired_name": "aria2c"
        },

        "yt-dlp": {
            "url": "https://github.com/coletdjnz/yt-dlp-dev/releases/download/sabr/yt-dlp_win.zip",
            "desired_name": "yt-dlp"
        },

        "deno": {
            "url": "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
            "desired_name": "yt-dlp",
            "filename": "deno.exe"
        }
    }

    seven_exe = None
    seven_cleanup_dir = None

    try:
        for tool, info in downloads.items():
            tool_ok = True

            print(f"==== {tool} 설치 시작 ====")
            print(f"※ 해외서버 다운로드 속도가 느려 시간이 오래 걸릴 수 있습니다.")                    

            if tool == "yt-dlp":
                url = info["url"]
                ext = os.path.splitext(url.split("?", 1)[0])[1].lower()

                if ext in (".zip", ".7z"):
                    tmp_archive = os.path.join(base_dir, f"yt-dlp{ext}")
                    try:
                        download_file(requests, url, tmp_archive)

                        if ext == ".7z" and not seven_exe:
                            seven_exe, seven_cleanup_dir = get_7z_extractor(requests, base_dir)

                        final_dir = os.path.join(dependent_dir, info["desired_name"])

                        extract_single_file_to_dir(
                            tmp_archive,
                            final_dir,
                            "yt-dlp.exe",
                            "yt-dlp.exe",
                            seven_exe=seven_exe
                        )

                        print(f"yt-dlp 설치 완료 → {final_dir}")

                    except Exception as e:
                        tool_ok = False
                        print(f"{tool} 설치 중 오류: {e}")
                    finally:
                        safe_remove(tmp_archive)

                else:
                    # exe 단일 배포일 경우
                    tmp_exe = os.path.join(base_dir, "yt-dlp.exe")
                    try:
                        download_file(requests, url, tmp_exe)

                        final_dir = os.path.join(dependent_dir, info["desired_name"])
                        ensure_directory(final_dir)
                        final_exe = os.path.join(final_dir, "yt-dlp.exe")

                        if os.path.exists(final_exe):
                            ok = safe_remove(final_exe)

                            if (not ok) or os.path.exists(final_exe):
                                raise Exception(
                                    f"기존 yt-dlp.exe를 삭제하지 못했습니다: {final_exe}\n"
                                    f"yt-dlp.exe가 실행 중이면 종료한 뒤 다시 실행하세요."
                                )

                        shutil.move(tmp_exe, final_exe)
                        print(f"yt-dlp 이동 완료 → {final_exe}")

                    except Exception as e:
                        tool_ok = False
                        print(f"{tool} 설치 중 오류: {e}")
                    finally:
                        safe_remove(tmp_exe)

            elif tool == "deno":
                if os.name != "nt":
                    print("[INFO] deno 자동 설치는 현재 Windows(nt)만 대상으로 처리합니다. (다른 OS는 패키지 매니저/PATH 사용)")
                    continue

                tmp_zip = os.path.join(base_dir, "deno.zip")
                temp_dir = os.path.join(base_dir, "temp_extract_deno")
                try:
                    download_file(requests, info["url"], tmp_zip)

                    if os.path.exists(temp_dir):
                        ok = safe_rmtree(temp_dir)

                        if (not ok) or os.path.exists(temp_dir):
                            raise Exception(
                                f"임시 deno 압축해제 폴더를 삭제하지 못했습니다: {temp_dir}\n"
                                f"autoInstall을 다시 실행하기 전에 해당 폴더가 사용 중인지 확인하세요."
                            )

                    ensure_directory(temp_dir)

                    with zipfile.ZipFile(tmp_zip, "r") as zf:
                        zf.extractall(temp_dir)

                    exe_path = None
                    for root, _, files in os.walk(temp_dir):
                        for f in files:
                            if f.lower() == "deno.exe":
                                exe_path = os.path.join(root, f)
                                break
                        if exe_path:
                            break
                    if not exe_path:
                        raise Exception("deno.exe 를 찾을 수 없습니다.")

                    # yt-dlp 폴더에 같이 넣기
                    final_dir = os.path.join(dependent_dir, info["desired_name"]) 
                    ensure_directory(final_dir)

                    final_exe = os.path.join(final_dir, "deno.exe")
                    if os.path.exists(final_exe):
                        ok = safe_remove(final_exe)

                        if (not ok) or os.path.exists(final_exe):
                            raise Exception(
                                f"기존 deno.exe를 삭제하지 못했습니다: {final_exe}\n"
                                f"deno.exe가 실행 중이면 종료한 뒤 다시 실행하세요."
                            )

                    shutil.move(exe_path, final_exe)
                    print(f"deno 이동 완료 → {final_exe}")

                except Exception as e:
                    tool_ok = False
                    print(f"{tool} 설치 중 오류: {e}")

                finally:
                    safe_remove(tmp_zip)
                    safe_remove(os.path.join(base_dir, "deno-x86_64-pc-windows-msvc.zip"))
                    safe_rmtree(temp_dir)

            else:
                url_no_query = info["url"].split("?", 1)[0]
                ext = os.path.splitext(url_no_query)[1].lower()
                if ext not in [".zip", ".7z"]:
                    ext = ".zip"

                tmp_archive = os.path.join(base_dir, f"{tool}{ext}")
                try:
                    download_file(requests, info["url"], tmp_archive)

                    if ext == ".7z":
                        if not seven_exe:
                            seven_exe, seven_cleanup_dir = get_7z_extractor(requests, base_dir)

                        extract_and_rename(requests, tmp_archive, dependent_dir, info["desired_name"], seven_exe=seven_exe)
                    else:
                        extract_and_rename(requests, tmp_archive, dependent_dir, info["desired_name"], seven_exe=None)

                except Exception as e:
                    tool_ok = False
                    print(f"{tool} 설치 중 오류: {e}")
                finally:
                    # WinError 32 대비 재시도 삭제
                    safe_remove(tmp_archive)

            if tool_ok:
                print(f"==== {tool} 설치 완료 ====\n")
            else:
                print(f"==== {tool} 설치 실패 ====\n")

        # streamlink 패키지 내부 중복 ffmpeg 제거
        streamlink_ffmpeg = os.path.join(dependent_dir, "streamlink", "ffmpeg")
        if os.path.exists(streamlink_ffmpeg):
            safe_rmtree(streamlink_ffmpeg)
            print(f"중복된 ffmpeg 폴더 삭제 완료: {streamlink_ffmpeg}")

    finally:
        # 7zr.exe를 임시로 다운로드한 경우에만 끝나고 삭제
        if seven_cleanup_dir:
            safe_rmtree(seven_cleanup_dir)


if __name__ == "__main__":
    main()
