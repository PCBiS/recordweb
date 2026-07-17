import os
import shutil 
import time
import subprocess
import psutil
import mimetypes
from pathlib import Path
from typing import Dict, Any, List

from module.data_manager import getFFmpeg


# 파일 경로에서 숨김처리할 목록
HIDDEN_NAME_SET = {
    'system volume information',
    '$recycle.bin',
    'recycler',
    'pagefile.sys',
    'hiberfil.sys',
    'swapfile.sys',
    'config.msi',
    'msocache',
    'recovery',
    'desktop.ini',
    'thumbs.db',
}

def isHiddenName(name: str) -> bool:
    if not name:
        return False
    # 리눅스/맥: dot-hidden
    if name.startswith('.'):
        return True
    # 윈도우/공통: 사전 정의 집합
    return name.lower() in HIDDEN_NAME_SET


def normPath(p: str) -> str:
    """실경로 + OS 케이스 규칙으로 정규화"""
    return os.path.normcase(os.path.realpath(p))


# 파일관리 루트 산정
def buildAllowedRoots(config: dict, channels: List[dict]) -> List[str]:
    mode = (config or {}).get("fileManagerMode", "blacklist")
    if mode == "blacklist":
        return ["*"]  # 와일드카드: ensureInRoots 가 블랙리스트 판정을 수행

    roots = set()
    for c in channels:
        out = c.get("output_dir")
        if out:
            roots.add(normPath(out))
    if config.get("moveAfterProcessingEnabled") and config.get("moveAfterProcessing"):
        roots.add(normPath(config["moveAfterProcessing"]))
    for extra in config.get("fileManagerRoots", []):
        if extra:
            roots.add(normPath(extra))
    return sorted(roots)


# 경로 검증
def ensureInRoots(path: str, allowedRoots: List[str]) -> str:
    rp = normPath(path)

    # 블랙리스트 모드: 시스템 주요 경로만 거부
    if allowedRoots == ["*"]:
        if isDeniedPath(rp):
            raise PermissionError("Path is denied by system blacklist")
        return rp

    # 화이트리스트 모드: 기존 로직
    for root in allowedRoots:
        r = normPath(root)
        try:
            if os.path.commonpath([rp, r]) == r:
                return rp
        except Exception:
            pass
    raise PermissionError("Path is outside allowed roots")


# 시스템 주요 경로 블랙리스트
def isDeniedPath(p: str) -> bool:
    rp = os.path.abspath(os.path.expanduser(p))
    if os.name == 'nt':
        lower = rp.lower().replace('/', '\\')
        denies = [
            r'c:\windows', r'c:\windows\system32', r'c:\program files',
            r'c:\program files (x86)', r'c:\programdata', r'c:\users\public'
        ]
        return any(lower == d or lower.startswith(d + '\\') for d in denies)
    else:
        denies = ['/', '/bin', '/sbin', '/etc', '/proc', '/sys', '/dev', '/run', '/var', '/usr']
        return rp in denies or any(rp.startswith(d + os.sep) for d in denies)


# 플랫폼별 마운트 지점/드라이브 루트 목록
def listMountRoots() -> List[str]:
    roots = []
    try:
        for part in psutil.disk_partitions(all=False):
            mp = part.mountpoint
            # 시스템 블랙리스트 제외
            if not isDeniedPath(mp) and os.path.isdir(mp):
                roots.append(mp)
    except Exception:
        pass
    # 중복 제거 및 정렬
    roots = sorted(set(roots))
    return roots


# 디렉터리 목록
def listDir(path: str, show_hidden: bool = False) -> List[Dict]:
    items: List[Dict] = []
    with os.scandir(path) as it:
        for e in it:
            name = e.name
            # 숨김/시스템 항목 필터
            if not show_hidden and isHiddenName(name):
                continue

            try:
                st = e.stat(follow_symlinks=False)
            except Exception:
                # 권한 문제 등은 스킵
                continue

            is_dir = e.is_dir(follow_symlinks=False)
            size   = 0 if is_dir else st.st_size
            # mtime 문자열 포맷
            try:
                mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))
            except Exception:
                mtime_str = ''

            # 확장자(파일) 또는 '폴더'
            if is_dir:
                ext = '폴더'
            else:
                _, ext_raw = os.path.splitext(name)
                ext = ext_raw.lstrip('.').lower() or '파일'

            items.append({
                "name":   name,
                "path":   os.path.join(path, name),
                "is_dir": is_dir,
                "size":   size,
                "mtime":  mtime_str,  # ← 문자열로 반환
                "ext":    ext,
            })

    # 폴더 우선, 이름 오름차순
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return items



def diskUsageFor(path: str) -> Dict:
    total, used, free = shutil.disk_usage(path)
    pct = 0 if total == 0 else int(used * 100 / total)
    return {"path": path, "total": total, "used": used, "free": free, "percent": pct}


# 각 루트 기준 사용량
def listDisks(roots: List[str]) -> List[Dict]:
    result = []
    for r in roots:
        try:
            result.append(diskUsageFor(r))
        except Exception:
            continue
    return result


def makeTrashPath(root: str) -> str:
    return os.path.join(root, ".trash")


def softDelete(path: str, root: str) -> str:
    trashDir = makeTrashPath(root)
    os.makedirs(trashDir, exist_ok=True)
    base = os.path.basename(path)
    ts   = time.strftime("%Y%m%d_%H%M%S")
    dst  = os.path.join(trashDir, f"{base}.{ts}")
    return shutil.move(path, dst)


def hardDelete(path: str):
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def movePath(src: str, dstDir: str) -> str:
    os.makedirs(dstDir, exist_ok=True)
    return shutil.move(src, dstDir)


def streamCopyFile(src: str) -> str:
    ffmpeg = getFFmpeg()
    if not ffmpeg:
        raise RuntimeError("[스트림복사] FFmpeg 경로를 찾을 수 없습니다.")

    if not os.path.isfile(src):
        raise FileNotFoundError(f"[스트림복사] 파일을 찾을 수 없습니다: {src}")

    dirname   = os.path.dirname(src)
    basename  = os.path.basename(src)
    dst       = os.path.join(dirname, f"fixed_{basename}")

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-c", "copy", "-map", "0", dst
    ]

    print(f"[스트림복사] 시작: {basename}")
    print(f"[스트림복사] CMD : {' '.join(cmd)}")
    t0 = time.time()

    completed = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.time() - t0

    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        print(f"[스트림복사] 실패: {basename} ({dt:.2f}s) → {err}")
        raise RuntimeError(f"ffmpeg failed: {err}")

    try:
        size = os.path.getsize(dst)
    except Exception:
        size = -1

    print(f"[스트림복사] 완료: {basename} → {os.path.basename(dst)} "
          f"({size if size>=0 else '?'} bytes, {dt:.2f}s)")
    return dst


def renamePath(src: str, newName: str) -> str:
    dst = os.path.join(os.path.dirname(src), newName)
    os.replace(src, dst)
    return dst


def mkdirPath(parent: str, name: str) -> str:
    p = os.path.join(parent, name)
    os.makedirs(p, exist_ok=True)
    return p


# 녹화 중인 파일(풀 경로) 목록
def busyFilePaths(recorderManager, channels: List[dict]) -> List[str]:
    busy = []
    for c in channels:
        cid = c.get("id")
        if recorderManager.get_recording_status(cid):
            fn = recorderManager.get_recording_filename(cid)
            if fn:
                busy.append(normPath(fn))
    return busy


# 녹화중 파일 잠금
def isLocked(path: str, busyPaths: List[str]) -> bool:
    rp = normPath(path)
    for bp in busyPaths:
        if rp == bp:
            return True
        # 파일이 들어있는 폴더 자체를 옮기거나 지우는 것도 잠금
        if rp.startswith(os.path.dirname(bp) + os.sep):
            return True
    return False


def isPreviewable(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in {".mp4", ".webm", ".mkv", ".ts", ".m4v", ".mov", ".mp3", ".m4a", ".aac", ".wav", ".flac"}


def parseRangeHeader(rangeHeader: str, fileSize: int):
    try:
        if not rangeHeader or not rangeHeader.startswith("bytes="):
            return None
        rng = rangeHeader.split("=", 1)[1].strip()
        start_s, end_s = (rng.split("-", 1) + [""])[:2]
        if start_s == "":
            # bytes=-500  (끝에서 500바이트)
            length = int(end_s)
            if length <= 0: return None
            start = max(0, fileSize - length)
            end = fileSize - 1
        else:
            start = int(start_s)
            end = fileSize - 1 if end_s == "" else int(end_s)
            if start > end or start < 0: return None
        end = min(end, fileSize - 1)
        return (start, end)
    except Exception:
        return None

def guessMime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"