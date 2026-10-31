from __future__ import annotations

import os
import sys
import re
import traceback
import webbrowser
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QComboBox, QFileDialog,
    QMessageBox, QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressBar, QSizePolicy, QAbstractItemView
)

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 헬퍼
def safe_get(d: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip() != "":
            return str(v)
    return default


def fallback_sanitize_filename(name: str) -> str:
    # 코어에 sanitize_filename이 없을 때 대비
    bad = '\\/:*?"<>|'
    out = "".join("_" if c in bad else c for c in name)
    out = out.strip().rstrip(".")
    return out or "untitled"


@dataclass
class DownloadTask:
    row_index: int
    url: str
    platform: str  # "chzzk" | "youtube" | "cime"
    is_playlist: bool = False

    output_dir: str = ""

    # youtube
    yt_format_id: str = "bestvideo"
    yt_speed_limit: str = ""

    chzzk_quality: str = ""         # 예: "1080p"
    chzzk_speed_option: str = "1"   # 코어는 str 기대
    chzzk_section: str = ""         # "HH:MM:SS-HH:MM:SS" or ""



# Worker Threads
class AnalyzeThread(QThread):
    analyzed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, core: Any, url: str):
        super().__init__()
        self.core = core
        self.url = url

    def run(self):
        try:
            url = self.url.strip()
            if not url:
                raise ValueError("URL이 비어있습니다.")

            # 코어 main 로직과 동일한 판별 흐름을 최대한 따라갑니다.
            if "youtube.com" in url or "youtu.be" in url:
                # playlist 판별(코어에 isYoutubePlaylistURL이 없으면 fallback)
                is_playlist = False
                playlist_videos = []
                if hasattr(self.core, "isYoutubePlaylistURL"):
                    is_playlist = bool(self.core.isYoutubePlaylistURL(url))
                else:
                    is_playlist = ("list=" in url and ("playlist" in url or "watch" in url))

                if is_playlist and hasattr(self.core, "getPlaylistItems"):
                    playlist_videos = self.core.getPlaylistItems(url) or []
                    if not playlist_videos:
                        raise RuntimeError("유튜브 재생목록 영상 목록을 가져오지 못했습니다.")
                    rep_url = playlist_videos[0]
                    yt_qualities, video_info = self.core.getYoutubeQualities(rep_url)
                else:
                    yt_qualities, video_info = self.core.getYoutubeQualities(url)

                if not yt_qualities:
                    err = ""
                    if hasattr(self.core, "getLastYtDlpError"):
                        err = (self.core.getLastYtDlpError() or "").strip()

                    if err:
                        raise RuntimeError("유튜브 품질 목록을 가져오지 못했습니다.\n\n[yt-dlp 로그]\n" + err)
                    raise RuntimeError("유튜브 품질 목록을 가져오지 못했습니다.")

                title = safe_get(video_info or {}, "title", default="unknown")

                self.analyzed.emit({
                    "platform": "youtube",
                    "is_playlist": bool(is_playlist),
                    "playlist_count": len(playlist_videos),
                    "qualities": yt_qualities,
                    "video_info": video_info or {},
                    "title": title,
                })
                return


            # cime VOD
            if ("ci.me/" in url) or ("streaming.cf.ci.me" in url):
                if hasattr(self.core, "getCimeVODQualities"):
                    vod_qualities, vod_info = self.core.getCimeVODQualities(url)
                else:
                    vod_qualities, vod_info = self.core.getVODQualities(url)

                if not vod_qualities:
                    err = ""
                    if hasattr(self.core, "getLastCimeVodError"):
                        err = (self.core.getLastCimeVodError() or "").strip()

                    if err:
                        raise RuntimeError("씨미 품질 목록을 가져오지 못했습니다.\n\n[씨미 VOD 로그]\n" + err)
                    raise RuntimeError("씨미 품질 목록을 가져오지 못했습니다.")

                self.analyzed.emit({
                    "platform": "cime",
                    "vod_info": vod_info or {},
                    "qualities": vod_qualities,
                    "title": safe_get(vod_info or {}, "videoTitle", "title", default="unknown"),
                })
                return

            # chzzk VOD
            if "chzzk.naver.com/video/" in url:
                vod_qualities, vod_info = self.core.getVODQualities(url)
                if not vod_qualities:
                    err = ""
                    if hasattr(self.core, "getLastChzzkVodError"):
                        err = (self.core.getLastChzzkVodError() or "").strip()

                    if err:
                        raise RuntimeError("치지직 품질 목록을 가져오지 못했습니다.\n\n[치지직 VOD 로그]\n" + err)
                    raise RuntimeError("치지직 품질 목록을 가져오지 못했습니다.")
                self.analyzed.emit({
                    "platform": "chzzk",
                    "vod_info": vod_info or {},
                    "qualities": vod_qualities,
                    "title": safe_get(vod_info or {}, "videoTitle", "title", default="unknown"),
                })
                return

            # fallback: 치지직 시도 → 실패하면 유튜브 시도
            try:
                vod_qualities, vod_info = self.core.getVODQualities(url)
                if vod_qualities:
                    self.analyzed.emit({
                        "platform": "chzzk",
                        "vod_info": vod_info or {},
                        "qualities": vod_qualities,
                        "title": safe_get(vod_info or {}, "videoTitle", "title", default="unknown"),
                    })
                    return
            except Exception:
                pass

            yt_qualities, video_info = self.core.getYoutubeQualities(url)
            if yt_qualities:
                title = safe_get(video_info or {}, "title", default="unknown")
                self.analyzed.emit({
                    "platform": "youtube",
                    "is_playlist": False,
                    "playlist_count": 0,
                    "qualities": yt_qualities,
                    "video_info": video_info or {},
                    "title": title,
                })
                return

            raise RuntimeError("플랫폼 판별/품질 조회에 실패했습니다.")

        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


class DownloadThread(QThread):
    log = pyqtSignal(str)
    status = pyqtSignal(int, str)   
    finished_all = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, core: Any, tasks: list[DownloadTask], ui_selected_quality: dict):
        super().__init__()
        self.core = core
        self.tasks = tasks
        self._stop_requested = False

        # 분석 탭에서 선택된 "대표 품질" (치지직: quality/type 매칭용)
        self.ui_selected_quality = ui_selected_quality or {}

    def request_stop(self):
        self._stop_requested = True

        # 코어가 제공하는 표준 중지 함수 우선 사용
        try:
            if hasattr(self.core, "request_stop_current_download"):
                self.core.request_stop_current_download()
                return
        except Exception:
            pass

        # fallback(구버전 코어 호환)
        try:
            p = getattr(self.core, "current_download_process", None)
            if p is not None:
                p.terminate()
        except Exception:
            pass


    def run(self):
        try:
            for task in self.tasks:
                row = task.row_index  

                if self._stop_requested:
                    self.status.emit(row, "중지됨")
                    continue

                self.status.emit(row, "진행중")
                self.log.emit(f"[START] {task.platform.upper()}  {task.url}")

                ok = False
                if task.platform == "youtube":
                    ok = self._run_youtube_task(task)
                elif task.platform == "cime":
                    ok = self._run_cime_task(task)
                else:
                    ok = self._run_chzzk_task(task)

                if self._stop_requested:
                    self.status.emit(row, "중지됨")
                else:
                    self.status.emit(row, "완료" if ok else "실패")

                self.log.emit(f"[END] {'OK' if ok else 'FAIL'}\n")

            self.finished_all.emit()

        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


    def _run_youtube_task(self, task: DownloadTask) -> bool:
        # 1) 기본 fmt 결정
        fmt_req = (task.yt_format_id or "").strip()
        if not fmt_req:
            fmt_req = "bestvideo"

        # 2) 다운로드할 URL 목록 만들기
        videos: list[str] = []
        if task.is_playlist:
            if hasattr(self.core, "getPlaylistItems"):
                videos = self.core.getPlaylistItems(task.url) or []
            elif hasattr(self.core, "getYoutubePlaylistVideos"):
                videos = self.core.getYoutubePlaylistVideos(task.url) or []

            if not videos:
                self.log.emit("[ERROR] 유튜브 재생목록 영상 목록을 가져오지 못했습니다.")
                return False
        else:
            videos = [task.url]

        # 3) 실제 다운로드
        all_ok = True
        total = len(videos)

        for idx, vurl in enumerate(videos, start=1):
            if self._stop_requested:
                return False

            # 3-1) 품질 목록/메타 가져오기
            qualities = []
            vinfo = {}
            try:
                got = self.core.getYoutubeQualities(vurl)
                # (qualities, video_info) 형태 우선 대응
                if isinstance(got, tuple) and len(got) >= 2:
                    qualities, vinfo = got[0] or [], got[1] or {}
                else:
                    qualities = got or []
                    vinfo = {}
            except Exception:
                qualities, vinfo = [], {}

            # 3-2) fmt가 실제 존재하는지 확인(없으면 bestvideo로)
            fmt_use = fmt_req
            try:
                ids = set()
                for q in (qualities or []):
                    if isinstance(q, dict):
                        fid = str(q.get("format_id") or q.get("id") or "").strip()
                    elif isinstance(q, (tuple, list)) and len(q) >= 2:
                        fid = str(q[1] or "").strip()
                    else:
                        continue

                    if fid:
                        ids.add(fid)

                if fmt_use and ids and fmt_use not in ids:
                    fmt_use = "bestvideo"
            except Exception:
                fmt_use = "bestvideo"

            # 3-3) 파일명 결정: video_info의 title을 우선 사용
            base_title = safe_get(vinfo or {}, "title", default="")
            if not base_title:
                base_title = "youtube_video"

            # playlist면 충돌 방지용 접두어
            if task.is_playlist:
                base_title = f"{idx:03d}_{base_title}"

            base_title = self._sanitize(base_title)

            self.log.emit(f"[{idx}/{total}] {base_title} ({fmt_use}) 다운로드 시작")
            ok = self._call_core_download_youtube(
                vurl,
                fmt_use,
                task.output_dir,
                base_title,
                task.yt_speed_limit,
            )
            all_ok = all_ok and ok

        return all_ok


    def _call_core_download_youtube(self, url: str, fmt: str, outdir: str, base_filename: str, speed_limit: str) -> bool:
        try:
            if not hasattr(self.core, "downloadYoutube"):
                self.log.emit("[ERROR] 코어에 downloadYoutube()가 없습니다.")
                return False
            speed = (speed_limit or "").strip()   # 비어있으면 "" 유지

            return bool(self.core.downloadYoutube(url, fmt, outdir, base_filename, speed))

        except Exception as e:
            self.log.emit(f"[ERROR] 유튜브 다운로드 실패: {e}")
            return False

    def _run_chzzk_task(self, task: DownloadTask) -> bool:
        if not hasattr(self.core, "getVODQualities") or not hasattr(self.core, "downloadVOD"):
            self.log.emit("[ERROR] 코어에 치지직 함수(getVODQualities/downloadVOD)가 없습니다.")
            return False

        try:
            qualities, vod_info = self.core.getVODQualities(task.url)
            if not qualities:
                err = ""
                if hasattr(self.core, "getLastChzzkVodError"):
                    err = (self.core.getLastChzzkVodError() or "").strip()
                self.log.emit("[ERROR] 치지직 품질 목록 없음" + (f" - {err}" if err else ""))
                return False

            # 1) 선택 품질 적용(없으면 best로 폴백)
            available = {str(q.get("id") or q.get("quality") or "").strip() for q in qualities}
            want = (task.chzzk_quality or "").strip()

            # DASH 여부 (inKey 있으면 DASH)
            vod_info = vod_info or {}
            is_dash = (vod_info.get("inKey") is not None)

            def _to_int(v, default=0):
                try:
                    return int(float(v))
                except Exception:
                    return default

            def pick_best(qs):
                best_id = ""
                best_h = -1
                best_bw = -1
                for q in qs:
                    h = _to_int(q.get("height") or 0, 0)
                    bw = _to_int(q.get("bandwidth") or q.get("bitrate") or 0, 0)
                    if (h > best_h) or (h == best_h and bw > best_bw):
                        best_h = h
                        best_bw = bw
                        best_id = str(q.get("id") or q.get("quality") or "").strip()
                return best_id or str(qs[0].get("id") or qs[0].get("quality") or "").strip()

            def pick_worst(qs):
                worst_id = ""
                worst_h = 10**18
                worst_bw = 10**18
                for q in qs:
                    h = _to_int(q.get("height") or 0, 0)
                    bw = _to_int(q.get("bandwidth") or q.get("bitrate") or 0, 0)
                    if (h < worst_h) or (h == worst_h and bw < worst_bw):
                        worst_h = h
                        worst_bw = bw
                        worst_id = str(q.get("id") or q.get("quality") or "").strip()
                return worst_id or str(qs[0].get("id") or qs[0].get("quality") or "").strip()

            # DASH에서는 best/worst를 실제 rep_id로 변환
            if want in ("best", "worst"):
                if not is_dash:
                    # HLS(streamlink)에서는 예약키 best/worst 그대로 사용 가능
                    chosen_quality = want
                else:
                    chosen_quality = pick_best(qualities) if want == "best" else pick_worst(qualities)
                    self.log.emit(f"[INFO] DASH {want} 자동선택 → {chosen_quality}")
            else:
                chosen_quality = want if (want and want in available) else pick_best(qualities)


            # 2) 파일명은 CLI처럼 "[recording_time] channelName videoTitle.mp4" 형태로 생성
            vod_info = vod_info or {}
            live_open_date_raw = vod_info.get("liveOpenDate", "")
            recording_time, start_time = ("", "")
            if hasattr(self.core, "formatLiveDate") and live_open_date_raw:
                try:
                    recording_time, start_time = self.core.formatLiveDate(live_open_date_raw)
                except Exception:
                    pass

            channel_info = vod_info.get("channel", {}) or {}
            channel_name = (channel_info.get("channelName") or "").strip()
            video_title = (vod_info.get("videoTitle") or "").strip()
            if not video_title:
                video_title = "chzzk_vod"

            if recording_time:
                raw_name = f"[{recording_time}] {channel_name} {video_title}.mp4".strip()
            else:
                raw_name = f"{channel_name} {video_title}.mp4".strip()

            auto_filename = self._sanitize(raw_name)

            section = task.chzzk_section.strip() or None

            self.log.emit(f"[INFO] 선택/적용 품질: {chosen_quality}  speedOption={task.chzzk_speed_option}  section={section or '전체'}")

            try:
                result = self.core.downloadVOD(
                    vod_url=task.url,
                    quality=chosen_quality,
                    output_folder=task.output_dir,
                    auto_filename=auto_filename,
                    speed_option=task.chzzk_speed_option,
                    download_section=section,
                )
            except TypeError:
                result = self.core.downloadVOD(
                    task.url, chosen_quality, task.output_dir, auto_filename, task.chzzk_speed_option, section
                )

            if result is None:
                return True
            return bool(result)

        except Exception as e:
            self.log.emit(f"[ERROR] 치지직 다운로드 실패: {e}")
            return False

    def _run_cime_task(self, task: DownloadTask) -> bool:
        if not hasattr(self.core, "getCimeVODQualities") or not hasattr(self.core, "downloadVOD"):
            self.log.emit("[ERROR] 코어에 씨미 함수(getCimeVODQualities/downloadVOD)가 없습니다.")
            return False

        try:
            qualities, vod_info = self.core.getCimeVODQualities(task.url)
            if not qualities:
                err = ""
                if hasattr(self.core, "getLastCimeVodError"):
                    err = (self.core.getLastCimeVodError() or "").strip()
                self.log.emit("[ERROR] 씨미 품질 목록 없음" + (f" - {err}" if err else ""))
                return False

            want = (task.chzzk_quality or "best").strip() or "best"
            available = {str(q.get("id") or q.get("quality") or "").strip() for q in qualities}

            def _to_int(v, default=0):
                try:
                    return int(float(v))
                except Exception:
                    return default

            def pick_best(qs):
                return max(qs, key=lambda q: (_to_int(q.get("height") or 0, 0), _to_int(q.get("bandwidth") or 0, 0)))

            def pick_worst(qs):
                return min(qs, key=lambda q: (_to_int(q.get("height") or 0, 0), _to_int(q.get("bandwidth") or 0, 0)))

            if want == "best":
                chosen_item = pick_best(qualities)
            elif want == "worst":
                chosen_item = pick_worst(qualities)
            else:
                chosen_item = None
                for q in qualities:
                    if want == str(q.get("id") or q.get("quality") or "").strip():
                        chosen_item = q
                        break
                if chosen_item is None:
                    chosen_item = pick_best(qualities)

            chosen_quality = str(chosen_item.get("id") or chosen_item.get("quality") or "best").strip()
            quality_label = str(chosen_item.get("quality") or chosen_quality).strip()

            vod_info = vod_info or {}
            channel_info = vod_info.get("channel", {}) or {}
            channel_name = (channel_info.get("channelName") or "cime").strip()
            video_title = (vod_info.get("videoTitle") or vod_info.get("title") or "cime_vod").strip()
            raw_name = f"{channel_name} {video_title} {quality_label}.mp4".strip()
            auto_filename = self._sanitize(raw_name)

            section = task.chzzk_section.strip() or None
            self.log.emit(f"[INFO] 선택/적용 품질: {chosen_quality}  section={section or '전체'}")

            try:
                if hasattr(self.core, "cimeQualityNeedsCookie") and hasattr(self.core, "hasCimeLoginCookie"):
                    if self.core.cimeQualityNeedsCookie(chosen_item) and not self.core.hasCimeLoginCookie():
                        guide = ""
                        if hasattr(self.core, "getCimeCookieGuide"):
                            guide = self.core.getCimeCookieGuide()
                        self.log.emit("[ERROR] 씨미 로그인 쿠키가 필요합니다." + ("\n" + guide if guide else ""))
                        return False
            except Exception:
                pass

            try:
                result = self.core.downloadVOD(
                    vod_url=task.url,
                    quality=chosen_quality,
                    output_folder=task.output_dir,
                    auto_filename=auto_filename,
                    speed_option=task.chzzk_speed_option,
                    download_section=section,
                )
            except TypeError:
                result = self.core.downloadVOD(
                    task.url, chosen_quality, task.output_dir, auto_filename, task.chzzk_speed_option, section
                )

            if result is None:
                return True
            return bool(result)

        except Exception as e:
            self.log.emit(f"[ERROR] 씨미 다운로드 실패: {e}")
            return False

    def _sanitize(self, s: str) -> str:
        if hasattr(self.core, "sanitize_filename"):
            try:
                return self.core.sanitize_filename(s)
            except Exception:
                return fallback_sanitize_filename(s)
        return fallback_sanitize_filename(s)


# Main Window
APP_QSS = """
* {
  font-family: 'Segoe UI', '맑은 고딕';
  font-size: 14px;
}

QMainWindow {
  background: #f6f7fb;
}

QGroupBox {
  border: 1px solid #dcdfe4;
  border-radius: 10px;
  margin-top: 10px;
  background: #ffffff;
}

QGroupBox::title {
  subcontrol-origin: margin;
  left: 10px;
  padding: 0 6px;
  color: #2d3436;
  font-weight: 600;
}

QLabel {
  color: #2d3436;
}

QLineEdit, QPlainTextEdit, QComboBox, QTableWidget {
  border: 1px solid #dcdfe4;
  border-radius: 8px;
  padding: 6px 8px;
  background: #ffffff;
}

QComboBox::drop-down {
  border: none;
}

QPushButton {
  background: #ffffff;
  color: #111827;
  border: 1px solid #e5eaf3;
  border-radius: 4px;
  padding: 8px 14px;
}

QPushButton:hover {
  background: #f3f6ff;
  border-color: #d6deff;
}

QPushButton:pressed {
  background: #e7eeff;
}

QPushButton:disabled {
  background: #f3f4f6;
  color: #9ca3af;
  border-color: #e5eaf3;
}

QPushButton[variant="primary"] {
  background: #ffffff;
  border-color: #a5b4fc;
  color: #111827;
}

QPushButton[variant="primary"]:hover {
  background: #f3f6ff;
}

QPushButton[variant="primary"]:pressed {
  background: #e7eeff;
}

QPushButton[variant="danger"] {
  background: #fff5f5;
  border-color: #ffe4e6;
  color: #7f1d1d;
}

QPushButton[variant="danger"]:hover {
  background: #ffecec;
}

QPushButton[variant="danger"]:pressed {
  background: #ffdede;
}

QTableWidget {
  gridline-color: #e8eaef;
}
QHeaderView::section {
  background: #f0f2f7;
  border: none;
  padding: 8px;
  font-weight: 600;
}
QProgressBar {
  border: 1px solid #dcdfe4;
  border-radius: 8px;
  text-align: center;
  background: #ffffff;
}

QLineEdit:disabled, QComboBox:disabled, QPlainTextEdit:disabled {
  background-color: #eef0f4;
  color: #8a9099;
  border: 1px solid #e0e3e8;
}

QLabel[help="true"] {
  color: #6a737d;
  font-size: 12px;
}
"""



class MainWindow(QMainWindow):
    def __init__(self, core: Any):
        super().__init__()
        self.core = core

        self.setWindowTitle("VOD 다운로더 GUI v1.2.0")
        self.setMinimumSize(1200, 720)

        root = QWidget()
        self.setCentralWidget(root)

        main = QHBoxLayout(root)

        # Left: URL + 옵션 + 작업버튼
        left = QVBoxLayout()
        main.addLayout(left, 3)

        # Right: 로그(상단) + 목록(하단)
        right = QVBoxLayout()
        main.addLayout(right, 2)

        # URL box
        gb_url = QGroupBox("URL 입력")
        left.addWidget(gb_url)
        vb = QVBoxLayout(gb_url)

        self.txt_urls = QPlainTextEdit()
        self.txt_urls.setPlaceholderText("https://chzzk.naver.com/video/...\nhttps://ci.me/@channel/vods/12345\nhttps://www.youtube.com/watch?v=...\nhttps://www.youtube.com/playlist?list=... \n\n유튜브 및 유튜브 재생목록은 단일 다운로드만 지원하며, \n치지직/씨미 VOD는 여러채널 줄바꿈으로 등록 및 다운로드 가능합니다.")
        vb.addWidget(self.txt_urls)

        hb = QHBoxLayout()
        vb.addLayout(hb)

        self.btn_analyze = QPushButton("대표 URL 분석/품질 조회")
        self.btn_analyze.setProperty("variant", "primary")
        hb.addWidget(self.btn_analyze, 1)

        self.btn_add_queue = QPushButton("목록에 추가")
        hb.addWidget(self.btn_add_queue, 1)

        self.btn_clear_queue = QPushButton("목록 비우기")
        hb.addWidget(self.btn_clear_queue, 1)

        self.btn_youtube_cookie_help = QPushButton("유튜브 쿠키 안내")
        hb.addWidget(self.btn_youtube_cookie_help, 1)

        # Options box (좌측 확장)
        gb_opt = QGroupBox("옵션 (대표 URL 기준으로 선택 → 목록 전체에 적용)")
        left.addWidget(gb_opt, 1)
        grid = QGridLayout(gb_opt)

        self.lbl_platform_title = QLabel("플랫폼/제목")
        self.lbl_platform_title_val = QLabel("미분석")
        grid.addWidget(self.lbl_platform_title, 0, 0)
        grid.addWidget(self.lbl_platform_title_val, 0, 1, 1, 3)

        grid.addWidget(QLabel("다운로드할 폴더"), 1, 0)
        self.out_dir = QLineEdit(str(Path.cwd()))
        grid.addWidget(self.out_dir, 1, 1, 1, 2)
        self.btn_browse = QPushButton("찾기")
        grid.addWidget(self.btn_browse, 1, 3)

        grid.addWidget(QLabel("영상 품질 선택"), 2, 0)
        self.cmb_quality = QComboBox()
        grid.addWidget(self.cmb_quality, 2, 1, 1, 3)

        help_quality = QLabel("다운로드 가능한 품질만 나타냅니다.\n복수 플랫폼 영상 다운로드시 best품질을 권장합니다.")
        help_quality.setProperty("help", "true")
        help_quality.setWordWrap(True)
        grid.addWidget(help_quality, 3, 1, 1, 3)

        grid.addWidget(QLabel("유튜브 속도제한 설정"), 4, 0)
        self.yt_speed = QLineEdit()
        self.yt_speed.setPlaceholderText("예) 10M  (비우면 제한없음)")
        grid.addWidget(self.yt_speed, 4, 1, 1, 3)

        help_yt = QLabel("유튜브 URL에서만 적용됩니다. (예: 50M / 5M / 500K)")
        help_yt.setProperty("help", "true")
        help_yt.setWordWrap(True)
        grid.addWidget(help_yt, 5, 1, 1, 3)

        grid.addWidget(QLabel("치지직 인코딩 VOD 분할 다운로드"), 6, 0)
        self.cmb_chzzk_speed = QComboBox()
        self.cmb_chzzk_speed.addItem("100% (16분할)", "100%")
        self.cmb_chzzk_speed.addItem("75% (12분할)", "75%")
        self.cmb_chzzk_speed.addItem("50% (8분할)", "50%")
        self.cmb_chzzk_speed.addItem("25% (4분할)", "25%")
        self.cmb_chzzk_speed.addItem("분할 없음 (1)", "분할 없음")
        grid.addWidget(self.cmb_chzzk_speed, 6, 1, 1, 3)

        help_chzzk_split = QLabel("치지직 일반 인코딩 VOD에서만 적용됩니다.\n치지직 빠른 다시보기, 월드컵 ABR_HLS, 씨미 VOD에서는 비활성화됩니다.\n분할 수가 높을수록 다운로드 속도가 빠릅니다.")
        help_chzzk_split.setProperty("help", "true")
        help_chzzk_split.setWordWrap(True)
        grid.addWidget(help_chzzk_split, 7, 1, 1, 3)

        grid.addWidget(QLabel("치지직/씨미 구간 다운로드"), 8, 0)
        self.chzzk_section = QLineEdit()
        self.chzzk_section.setPlaceholderText("예) 00:10:00~00:30:00  (비우면 전체)")
        grid.addWidget(self.chzzk_section, 8, 1, 1, 3)

        help_section = QLabel("치지직 인코딩 VOD, 월드컵 ABR_HLS, 씨미 VOD에서 적용됩니다. \n치지직 HLS(빠른 다시보기)에서는 비활성화됩니다. \n형식은 00:10:00~00:30:00 (전체는 비움).")
        help_section.setProperty("help", "true")
        help_section.setWordWrap(True)
        grid.addWidget(help_section, 9, 1, 1, 3)

        # Actions (버튼은 좌측 하단 유지)
        gb_act = QGroupBox("작업")
        left.addWidget(gb_act)
        hb_act = QHBoxLayout(gb_act)

        self.btn_start = QPushButton("다운로드 시작")
        self.btn_start.setProperty("variant", "primary")
        hb_act.addWidget(self.btn_start)

        self.btn_stop = QPushButton("중지")
        self.btn_stop.setProperty("variant", "danger")
        hb_act.addWidget(self.btn_stop)

        self.btn_remove = QPushButton("선택 삭제")
        hb_act.addWidget(self.btn_remove)

        # Right side: log (상단, 높이 1/2)
        gb_log = QGroupBox("로그")
        right.addWidget(gb_log, 1)
        vblog = QVBoxLayout(gb_log)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        vblog.addWidget(self.log_view)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        vblog.addWidget(self.progress)

        # Right side: queue (하단, 높이 1/2)
        gb_q = QGroupBox("다운로드 목록")
        right.addWidget(gb_q, 1)
        vbq = QVBoxLayout(gb_q)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["플랫폼", "URL", "상태", "비고"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        vbq.addWidget(self.table)

        self.qualities: list = []
        self.current_platform = ""
        self.current_title = ""
        self.current_vod_info = {}

        # 초기 상태: 미분석이므로 플랫폼 전용 컨트롤은 비활성(혼동 방지)
        self._apply_platform_ui_state("unknown")

        # connect
        self.btn_analyze.clicked.connect(self.on_analyze)
        self.btn_add_queue.clicked.connect(self.on_add_queue)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_remove.clicked.connect(self.on_remove_selected)
        self.btn_clear_queue.clicked.connect(self.on_clear_queue)
        self.btn_youtube_cookie_help.clicked.connect(self.show_youtube_cookie_help)
        self.btn_browse.clicked.connect(self.on_browse)

        # 상태/threads
        self.last_analyze_result: dict = {} 
        self.analyze_thread = None
        self.download_thread: Optional[DownloadThread] = None  

    def append_log(self, s: str):
        self.log_view.appendPlainText(s.rstrip())

    def show_youtube_cookie_help(self):
        ext_url = "https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc"

        msg = QMessageBox(self)
        msg.setWindowTitle("유튜브 쿠키 안내")
        msg.setIcon(QMessageBox.Icon.Information)

        msg.setText("유튜브 쿠키 관리 안내")
        msg.setInformativeText(
            "일반 공개 영상은 쿠키 없이도 다운로드되는 경우가 많지만,\n"
            "연령제한, 로그인 필요 영상, 봇 확인, 일부 재생목록에서는 쿠키가 필요할 수 있습니다.\n\n"
            "[유튜브 쿠키 추출 방법]\n"
            "1. 크롬 브라우저에서 유튜브에 로그인합니다.\n"
            "2. 유튜브 페이지에서 Get cookies.txt LOCALLY 확장 프로그램을 실행합니다.\n"
            "3. Copy 버튼을 눌러 쿠키 값을 복사합니다.\n"
            "4. json/ycookie.txt 파일을 열어 붙여넣기 후 저장합니다.\n\n"
            "※ 쿠키 파일은 Netscape cookies.txt 형식이어야 합니다."
        )

        open_btn = msg.addButton("확장 프로그램 열기", QMessageBox.ButtonRole.ActionRole)
        msg.addButton("닫기", QMessageBox.ButtonRole.RejectRole)

        msg.exec()

        if msg.clickedButton() == open_btn:
            webbrowser.open(ext_url)

    def _set_youtube_controls(self, enabled: bool):
        self.yt_speed.setEnabled(enabled)
        if enabled:
            self.yt_speed.setToolTip("")
        else:
            self.yt_speed.setToolTip("유튜브 URL에서만 사용 가능합니다.")

    def _set_chzzk_dash_controls(self, is_dash: bool, vod_info: Optional[dict] = None):
        vod_info = vod_info or {}
        is_abr_hls = str(vod_info.get("vodStatus") or "").upper() == "ABR_HLS"

        self.cmb_chzzk_speed.setEnabled(is_dash and not is_abr_hls)
        self.chzzk_section.setEnabled(is_dash)

        if not is_dash:
            self.cmb_chzzk_speed.setToolTip("HLS(빠른 다시보기)는 분할옵션이 적용되지 않습니다.")
            self.chzzk_section.setToolTip("HLS(빠른 다시보기)는 구간 다운로드가 지원되지 않습니다.")
            self.chzzk_section.clear()
        elif is_abr_hls:
            self.cmb_chzzk_speed.setToolTip("ABR_HLS VOD는 분할옵션이 적용되지 않습니다.")
            self.chzzk_section.setToolTip("")
        else:
            self.cmb_chzzk_speed.setToolTip("")
            self.chzzk_section.setToolTip("")


    def _set_chzzk_controls_disabled(self, tooltip: str = ""):
        self.cmb_chzzk_speed.setEnabled(False)
        self.chzzk_section.setEnabled(False)
        self.cmb_chzzk_speed.setToolTip(tooltip)
        self.chzzk_section.setToolTip(tooltip)

    def _set_cime_controls(self):
        self.cmb_chzzk_speed.setEnabled(False)
        self.chzzk_section.setEnabled(True)
        self.cmb_chzzk_speed.setToolTip("씨미 VOD는 분할옵션이 적용되지 않습니다.")
        self.chzzk_section.setToolTip("")

    def _apply_platform_ui_state(self, platform: str, vod_info: Optional[dict] = None):
        if platform == "youtube":
            self._set_youtube_controls(True)
            self._set_chzzk_controls_disabled("치지직 URL에서만 사용 가능합니다.")
            return

        if platform == "chzzk":
            self._set_youtube_controls(False)
            is_dash = False
            if isinstance(vod_info, dict):
                is_dash = (vod_info.get("inKey") is not None)
            self._set_chzzk_dash_controls(is_dash, vod_info)
            return

        if platform == "cime":
            self._set_youtube_controls(False)
            self._set_cime_controls()
            return

        self._set_youtube_controls(False)
        self._set_chzzk_controls_disabled("")

    def _show_platform_cookie_notice(self, platform: str):
        try:
            if platform == "cime":
                if hasattr(self.core, "hasCimeLoginCookie") and self.core.hasCimeLoginCookie():
                    return
                guide = self.core.getCimeCookieGuide() if hasattr(self.core, "getCimeCookieGuide") else "씨미 로그인 쿠키가 필요할 수 있습니다."
                QMessageBox.information(self, "씨미 쿠키 안내", guide)
                return

            if platform == "chzzk":
                if hasattr(self.core, "hasChzzkLoginCookie") and self.core.hasChzzkLoginCookie():
                    return
                guide = self.core.getChzzkCookieGuide() if hasattr(self.core, "getChzzkCookieGuide") else "치지직 로그인 쿠키가 필요할 수 있습니다."
                QMessageBox.information(self, "치지직 쿠키 안내", guide)
                return
        except Exception:
            pass


    def on_browse(self):
        d = QFileDialog.getExistingDirectory(self, "출력 폴더 선택", self.out_dir.text().strip() or os.getcwd())
        if d:
            self.out_dir.setText(d)

    def on_analyze(self):
        urls = [u.strip() for u in self.txt_urls.toPlainText().splitlines() if u.strip()]
        if not urls:
            QMessageBox.warning(self, "안내", "URL을 입력하세요.")
            return

        url = urls[0]
        self._show_platform_cookie_notice(self._detect_platform(url))
        self.append_log(f"[ANALYZE] {url}")

        self.progress.setRange(0, 0)  # busy
        self.btn_analyze.setEnabled(False)

        self.analyze_thread = AnalyzeThread(self.core, url)
        self.analyze_thread.analyzed.connect(self.on_analyzed)
        self.analyze_thread.failed.connect(self.on_analyze_failed)
        self.analyze_thread.finished.connect(lambda: self.btn_analyze.setEnabled(True))
        self.analyze_thread.finished.connect(lambda: self.progress.setRange(0, 1))
        self.analyze_thread.start()

    def on_analyzed(self, result: dict):
        self.last_analyze_result = result
        platform = result.get("platform", "?")
        title = result.get("title", "unknown")
        is_playlist = bool(result.get("is_playlist", False))
        pcnt = int(result.get("playlist_count", 0) or 0)

        extra = ""
        if platform == "youtube" and is_playlist:
            extra = f" / 재생목록({pcnt}개)"

        self.lbl_platform_title_val.setText(f"{platform.upper()} / {title}{extra}")
        self.append_log(f"[OK] 플랫폼={platform}, title={title}{extra}")

        if platform == "youtube":
            # 유튜브 속도제한 활성
            self.yt_speed.setEnabled(True)
            self.yt_speed.setToolTip("")

            # 치지직 옵션은 플랫폼이 다르므로 완전 비활성
            self.cmb_chzzk_speed.setEnabled(False)
            self.chzzk_section.setEnabled(False)
            self.cmb_chzzk_speed.setToolTip("치지직 URL에서만 사용 가능합니다.")
            self.chzzk_section.setToolTip("치지직 URL에서만 사용 가능합니다.")

        elif platform == "chzzk":
            # 유튜브 속도제한 비활성
            self.yt_speed.setEnabled(False)
            self.yt_speed.setToolTip("유튜브 URL에서만 사용 가능합니다.")

            # DASH/HLS 판별은 루프 밖에서 1번만
            vod_info = result.get("vod_info") or {}
            is_dash = (vod_info.get("inKey") is not None)
            self._set_chzzk_dash_controls(is_dash, vod_info)

        elif platform == "cime":
            self.yt_speed.setEnabled(False)
            self.yt_speed.setToolTip("유튜브 URL에서만 사용 가능합니다.")
            self._set_cime_controls()

        else:
            # 알 수 없는 플랫폼이면 전부 비활성(혼동 방지)
            self.yt_speed.setEnabled(False)
            self.yt_speed.setToolTip("유튜브 URL에서만 사용 가능합니다.")

            self.cmb_chzzk_speed.setEnabled(False)
            self.chzzk_section.setEnabled(False)
            self.cmb_chzzk_speed.setToolTip("")
            self.chzzk_section.setToolTip("")

        # 품질 콤보 채우기
        self.cmb_quality.clear()
        qualities = result.get("qualities") or []

        if platform in ("chzzk", "cime"):
            def _h(x):
                try:
                    return int(x.get("height") or 0)
                except Exception:
                    return 0

            qualities_sorted = sorted(qualities, key=_h, reverse=True)

            self.cmb_quality.clear()

            # best/worst 옵션을 GUI에 노출
            self.cmb_quality.addItem("best (자동)", "best")
            self.cmb_quality.addItem("worst", "worst")

            # 중복 제거용
            seen = set()
            seen.add("best")
            seen.add("worst")

            for q in qualities_sorted:
                qlabel = str(q.get("quality", "?")).strip()
                qid = str(q.get("id") or qlabel).strip()  
                qid_key = qid.lower()

                if qid_key in seen:
                    continue

                fps = q.get("frameRate")
                label = qlabel
                if fps and not re.search(r"p\d{2,3}$", qlabel, re.IGNORECASE):
                    label = f"{qlabel} / {fps}fps"

                # data에는 "id 문자열" 저장
                self.cmb_quality.addItem(label, qid)
                seen.add(qid_key)

        else:
            self.cmb_quality.clear()
            self.cmb_quality.addItem("best (자동)", {"format_id": "", "raw": None})

            seen_fmt = set()

            for q in (qualities or []):
                # 1) dict 형태 호환 포함
                if isinstance(q, dict):
                    fmt = str(q.get("format_id") or q.get("id") or "").strip()

                    qlabel = (
                        q.get("quality_label")
                        or q.get("quality")
                        or q.get("qualityLabel")
                        or q.get("format_note")
                        or q.get("resolution")
                        or fmt
                    )
                    label = str(qlabel).strip()
                    raw_obj = q

                # 2) tuple/list 형태: (label, format_id)
                elif isinstance(q, (tuple, list)) and len(q) >= 2:
                    label = str(q[0] or "").strip()
                    fmt = str(q[1] or "").strip()
                    if not label:
                        label = fmt
                    raw_obj = q

                else:
                    continue

                if not fmt:
                    continue

                if fmt in seen_fmt:
                    continue
                seen_fmt.add(fmt)

                self.cmb_quality.addItem(label, {"format_id": fmt, "raw": raw_obj})


        self.progress.setRange(0, 1)

    def on_analyze_failed(self, msg: str):
        self.progress.setRange(0, 1)
        QMessageBox.critical(self, "분석 실패", msg)

    def on_add_queue(self):
        urls = [u.strip() for u in self.txt_urls.toPlainText().splitlines() if u.strip()]
        if not urls:
            QMessageBox.warning(self, "안내", "URL을 입력하세요.")
            return

        # 현재 선택 옵션
        quality_data = self.cmb_quality.currentData()
        chzzk_quality_id = str(quality_data or "").strip()

        yt_format_id = ""
        if isinstance(quality_data, dict):
            yt_format_id = str(quality_data.get("format_id", "")).strip()

        yt_speed = self.yt_speed.text().strip()
        chzzk_speed = str(self.cmb_chzzk_speed.currentData() or "100%")
        section = self.chzzk_section.text().strip()

        added = 0
        for url in urls:
            platform = self._detect_platform(url)

            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(platform))
            self.table.setItem(row, 1, QTableWidgetItem(url))
            self.table.setItem(row, 2, QTableWidgetItem("대기"))

            memo = chzzk_quality_id if platform in ("chzzk", "cime") else self.cmb_quality.currentText()
            memo_item = QTableWidgetItem(memo)
            memo_item.setData(Qt.ItemDataRole.UserRole, {
                "chzzk_quality": chzzk_quality_id,
                "chzzk_speed": chzzk_speed,
                "chzzk_section": section,
                "yt_format_id": yt_format_id,
                "yt_speed": yt_speed,
            })
            self.table.setItem(row, 3, memo_item)

            added += 1

        self.append_log(f"[QUEUE] {added}개 추가됨")

    def on_clear_queue(self):
        self.table.setRowCount(0)
        self.append_log("[QUEUE] 비움")

    def on_remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        if rows:
            self.append_log(f"[QUEUE] {len(rows)}개 삭제")

    def on_start(self):
        if self.download_thread and self.download_thread.isRunning():
            QMessageBox.information(self, "안내", "이미 다운로드가 진행 중입니다.")
            return

        outdir = self.out_dir.text().strip()
        if not outdir:
            QMessageBox.warning(self, "안내", "출력 폴더를 지정하세요.")
            return
        os.makedirs(outdir, exist_ok=True)

        tasks: list[DownloadTask] = []
        for r in range(self.table.rowCount()):
            url = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").strip()
            if not url:
                continue

            platform = (self.table.item(r, 0).text() if self.table.item(r, 0) else "-").strip()
            platform = platform if platform in ("youtube", "chzzk", "cime") else self._detect_platform(url)

            is_playlist = False
            if platform == "youtube":
                if hasattr(self.core, "isYoutubePlaylistURL"):
                    try:
                        is_playlist = bool(self.core.isYoutubePlaylistURL(url))
                    except Exception:
                        is_playlist = ("list=" in url and ("playlist" in url or "watch" in url))
                else:
                    is_playlist = ("list=" in url and ("playlist" in url or "watch" in url))

            row_meta = {}
            if self.table.item(r, 3):
                try:
                    row_meta = self.table.item(r, 3).data(Qt.ItemDataRole.UserRole) or {}
                except Exception:
                    row_meta = {}

            selected_chzzk_quality = str(row_meta.get("chzzk_quality") or self.cmb_quality.currentData() or "").strip()
            selected_chzzk_speed = str(row_meta.get("chzzk_speed") or self.cmb_chzzk_speed.currentData() or "100%").strip()
            selected_chzzk_section = str(row_meta.get("chzzk_section") or self.chzzk_section.text()).strip()
            selected_yt_speed = str(row_meta.get("yt_speed") or self.yt_speed.text()).strip()

            task = DownloadTask(
                row_index=r,
                url=url,
                platform=platform,
                is_playlist=is_playlist,
                output_dir=outdir,

                yt_speed_limit=selected_yt_speed,

                chzzk_quality=selected_chzzk_quality if platform in ("chzzk", "cime") else "",
                chzzk_speed_option=selected_chzzk_speed,
                chzzk_section=selected_chzzk_section,
            )

            if platform == "youtube":
                fmt = str(row_meta.get("yt_format_id") or "").strip()
                if not fmt:
                    data = self.cmb_quality.currentData()
                    if isinstance(data, dict):
                        fmt = str(data.get("format_id", "")).strip()
                if fmt:
                    task.yt_format_id = fmt
            tasks.append(task)

        if not tasks:
            QMessageBox.warning(self, "안내", "목록이 비어있습니다.")
            return

        # 치지직 대표 품질(quality/type) 저장
        selected_quality = {}
        if self.last_analyze_result.get("platform") in ("chzzk", "cime"):
            q = self.cmb_quality.currentData()
            if isinstance(q, dict):
                selected_quality = {"quality": q.get("quality"), "type": q.get("type")}

        self.progress.setRange(0, 0)
        self.append_log(f"[START ALL] {len(tasks)}개 작업")

        self.download_thread = DownloadThread(self.core, tasks, selected_quality)
        self.download_thread.log.connect(self.append_log)
        self.download_thread.status.connect(self.set_row_status)
        self.download_thread.finished_all.connect(self.on_finished_all)
        self.download_thread.failed.connect(lambda m: QMessageBox.critical(self, "다운로드 오류", m))
        self.download_thread.start()

    def on_stop(self):
        if self.download_thread and self.download_thread.isRunning():
            self.append_log("[STOP] 중지 요청")
            self.download_thread.request_stop()

    def on_finished_all(self):
        self.progress.setRange(0, 1)
        self.append_log("[DONE] 전체 작업 종료")

    def set_row_status(self, idx: int, st: str):
        if idx < self.table.rowCount():
            self.table.setItem(idx, 2, QTableWidgetItem(st))

    def _detect_platform(self, url: str) -> str:
        if "youtube.com" in url or "youtu.be" in url:
            return "youtube"
        if "ci.me/" in url or "streaming.cf.ci.me" in url:
            return "cime"
        if "chzzk.naver.com/video/" in url:
            return "chzzk"
        # fallback
        return "chzzk"


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)

    try:
        from module import replay_download as core
    except Exception as e:
        QMessageBox.critical(None, "시작 실패", f"코어 import 실패: {e}\n\n{traceback.format_exc()}")
        return 1

    w = MainWindow(core)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
