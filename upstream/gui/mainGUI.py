import os
import sys
import time
import ctypes
import platform
import asyncio
import requests
import psutil
try:
    import cpuinfo
except Exception:
    cpuinfo = None

from collections import defaultdict
from html.parser import HTMLParser

from PyQt6.QtWidgets import (QApplication, QMainWindow, QTableWidget, QToolBar, QLabel, QLineEdit, QDialog, QWidget,
                             QComboBox, QPushButton, QHBoxLayout, QVBoxLayout, QGridLayout, QFrame, QProgressBar,
                             QMessageBox, QHeaderView, QTableWidgetItem, QTabWidget, QTextEdit,
                             QSizePolicy, QSystemTrayIcon, QMenu)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtCore import QTimer, QSize, QUrl, Qt, QThread, pyqtSignal, QEvent, QSettings
from PyQt6.QtGui import QPixmap, QIcon, QAction

from gui.cookie_manager_dialog import CookieManagerDialog
from gui.add_channel_dialog import AddChannelDialog
from gui.settings_dialog import SettingsDialog
from gui.edit_channel_dialog import EditChannelDialog

from module.data_manager import (loadChannels, saveChannels, loadConfig, getBaseUrl,
                                 PROGRAM_VERSION, GUI_TITLE)

# 파일 상대경로 기준(루트경로)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 폰트 경고로그 숨기기
os.environ["QT_LOGGING_RULES"] = "qt.text.font.db=false;qt.qpa.fonts=false"

GUI_STATUS_INTERVAL_MS = 2500
GUI_STATUS_MIN_MS = 800  

# CPU 명칭 조회
_CPU_NAME = None


class PatchNotesParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_history = False
        self.history_depth = 0
        self.patch_depth = 0
        self.details_depth = 0
        self.mode = ""
        self.buffer = []
        self.current = []
        self.notes = []

    @staticmethod
    def _attrs(attrs):
        return {key: value or "" for key, value in attrs}

    @staticmethod
    def _classes(attrs):
        return set(attrs.get("class", "").split())

    def handle_starttag(self, tag, attrs):
        attrs = self._attrs(attrs)
        classes = self._classes(attrs)

        if tag == "div" and attrs.get("id") == "version-history":
            self.in_history = True
            self.history_depth = 1
            return

        if self.in_history:
            self.history_depth += 1

        if not self.in_history:
            return

        if tag == "li":
            if self.patch_depth == 0 and "patch-version" in classes:
                self.patch_depth = 1
                self.current = []
                return

            if self.patch_depth:
                self.patch_depth += 1
                if self.details_depth:
                    self.mode = "detail"
                    self.buffer = []
            return

        if not self.patch_depth:
            return

        if tag == "strong":
            self.mode = "title"
            self.buffer = []
        elif tag == "div" and "patch-topic" in classes:
            self.mode = "topic"
            self.buffer = []
        elif tag == "ul" and "patch-details" in classes:
            self.details_depth = self.patch_depth

    def handle_data(self, data):
        if self.mode:
            self.buffer.append(data)

    def _flush(self):
        text = " ".join("".join(self.buffer).split())
        if text:
            prefix = {"title": "# ", "topic": "## ", "detail": "- "}.get(self.mode, "")
            self.current.append(f"{prefix}{text}")
        self.mode = ""
        self.buffer = []

    def handle_endtag(self, tag):
        if self.patch_depth:
            if tag == "strong" and self.mode == "title":
                self._flush()
            elif tag == "div" and self.mode == "topic":
                self._flush()
            elif tag == "li":
                if self.mode == "detail":
                    self._flush()
                self.patch_depth -= 1
                if self.patch_depth == 0:
                    self.notes.append("\n\n".join(self.current))
                    self.current = []
                    self.details_depth = 0
                    self.mode = ""
            elif tag == "ul" and self.details_depth:
                self.details_depth = 0

        if self.in_history:
            self.history_depth -= 1
            if self.history_depth == 0:
                self.in_history = False


def loadPatchNotes() -> str:
    try:
        index_path = os.path.join(BASE_DIR, "templates", "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()

        parser = PatchNotesParser()
        parser.feed(html)
        notes = [note for note in parser.notes if note.strip()]
        if notes:
            return "\n\n---\n\n".join(notes)
    except Exception:
        pass

    return "패치노트를 불러오지 못했습니다."


class VersionInfoDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("recordGUI 버전 정보")
        self.resize(760, 680)
        self._buildUi()

    def _buildUi(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel(f"recordGUI {PROGRAM_VERSION}")
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        box = QFrame()
        box.setObjectName("sectionBox")
        boxLayout = QVBoxLayout(box)
        boxLayout.setContentsMargins(12, 12, 12, 12)

        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setMarkdown(loadPatchNotes())
        boxLayout.addWidget(notes)
        root.addWidget(box, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        closeButton = QPushButton("닫기")
        closeButton.clicked.connect(self.accept)
        buttons.addWidget(closeButton)

        root.addLayout(buttons)

        self.setStyleSheet("""
            QDialog { background: #f6f7fb; color: #111827; }
            QLabel#dialogTitle { font-size: 20px; font-weight: 800; }
            QFrame#sectionBox {
                background: #ffffff;
                border: 1px solid #dfe4ee;
                border-radius: 8px;
            }
            QTextEdit {
                background: #ffffff;
                color: #111827;
                border: 0;
                font-size: 13px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 7px 20px;
            }
            QPushButton:hover { background: #eef2ff; }
        """)


def _getCpuName():
    global _CPU_NAME
    if _CPU_NAME:
        return _CPU_NAME

    name = None

    # 1) py-cpuinfo 우선
    try:
        if cpuinfo is not None:
            info = cpuinfo.get_cpu_info() or {}
            name = info.get('brand_raw') or info.get('brand')
    except Exception:
        name = None

    # 2) Linux 전용 간단 폴백(/proc/cpuinfo)
    if not name:
        import os
        try:
            if os.path.exists("/proc/cpuinfo"):
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "model name" in line:
                            name = line.split(":", 1)[1].strip()
                            break
        except Exception:
            pass

    # 3) 최종 폴백
    if not name:
        name = platform.processor() or platform.machine() or "Unknown CPU"

    _CPU_NAME = name
    return _CPU_NAME

# 전역 표시용(1회 평가 후 캐시)
cpu_name = _getCpuName()

# 툴바 버튼 디자인
def createToolbarButton(text):
    btn = QPushButton(text)
    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    btn.setFixedHeight(40)
    return btn


# 명령창 최소화
def minimizeConsole():
    if os.name != "nt":
        return
    try:       
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            SW_MINIMIZE = 6
            ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)
    except Exception as e:
        print(f"[WARN] minimizeConsole failed: {e}")


# 글자수 50바이트 제한
def truncateText(text, limit=50):
    try:
        encoded = text.encode('utf-8')
        if len(encoded) > limit:
            return encoded[:limit].decode('utf-8', errors='ignore')
    except Exception as e:
        print("truncateText 오류:", e)
    return text


def _autoThumbConcurrency() -> int:
    cores = os.cpu_count() or 2
    # 너무 높이면 NAM/디코딩이 병목되니 상한 둡니다.
    if cores >= 24: return 12
    if cores >= 12: return 8
    if cores >= 8:  return 6
    if cores >= 4:  return 4
    return 3


def _resolveThumbConcurrency(cfg: dict) -> int:
    # 1) 환경변수 최우선
    env = os.environ.get("THUMB_CONCURRENCY", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)

    # 2) 설정파일 값
    val = (cfg or {}).get("thumbConcurrency", "auto")
    if isinstance(val, int) and val > 0:
        return val

    # 3) AUTO
    return _autoThumbConcurrency()


def _autoMetaConcurrency() -> int:
    cores = os.cpu_count() or 2
    auto = int(cores * 0.75)
    return max(2, min(12, auto))


def _resolveMetaConcurrency(cfg: dict) -> int:
    # 1) 환경변수 우선 (META_CONCURRENCY)
    env = os.environ.get("META_CONCURRENCY", "").strip()
    if env.isdigit() and int(env) > 0:
        return max(1, int(env))

    # 2) 설정파일 값 우선
    val = (cfg or {}).get("metaConcurrency", "auto")
    if isinstance(val, int) and val > 0:
        return max(1, val)

    # 3) AUTO
    return _autoMetaConcurrency()


# 상태 폴링 워커
class _StatusPoller(QThread):
    result = pyqtSignal(dict)

    def __init__(self, base_url: str, timeout=(1.2, 2.5), parent=None):
        super().__init__(parent)
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def run(self):
        # 네트워크는 워커 스레드에서 1회만 수행
        try:
            r = requests.get(f"{self._base}/status", timeout=self._timeout)
            j = r.json() if r.ok else {}
        except Exception:
            j = {}
        self.result.emit(j)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{GUI_TITLE} {PROGRAM_VERSION}")
        self.resize(1200, 900)

        self.network_manager = QNetworkAccessManager(self)

        self.config = loadConfig() or {}

        icon_path = os.path.join(BASE_DIR, "templates", "static", "img", "chzzk_icon.png")
        self.setWindowIcon(QIcon(icon_path))

        self.tray_icon_path = os.path.join(BASE_DIR, "templates", "static", "img", "tray_icon.png")

        self.channels = loadChannels()

        cfg = loadConfig() or {}
        self._trayEnabled = bool(cfg.get("enableTray", False))             # config로 켜고/끄고
        self._trayClose   = bool(cfg.get("minimizeToTrayOnClose", False))  # 닫기(X) → 트레이로
        self._reallyClose = False                                          # 트레이에서 '완전 종료' 눌렀을 때만 True

        if self._trayEnabled:
            self._setupTray()
        else:
            self.tray = None                                               # 트레이 준비

        if self._trayEnabled and cfg.get("minimizeToTrayOnStart", False):  # 시작 시 트레이로 최소화
            self.hide()
            self.tray.showMessage("recordGUI", "트레이로 최소화되었습니다.", QSystemTrayIcon.MessageIcon.Information, 2500)

        for ch in self.channels:
            if 'auto_record' in ch:
                ch['record_enabled'] = bool(ch['auto_record'])
                del ch['auto_record']

        saveChannels(self.channels)

        self.config = loadConfig()
        self._last_status = {}                                             # 마지막 정상 /status 캐시
        self._recordingStickyUntil = {}                                    # 채널별 '녹화 중' 점착 만료 시각(monotonic)

        # Fallback 이미지 캐시
        fallback_cime_live_path = os.path.join(BASE_DIR, "templates", "static", "img", "cime_thumbnail.png")
        fallback_cime_closed_path = os.path.join(BASE_DIR, "templates", "static", "img", "cimeclosed_thumbnail.png")
        fallback_chzzk_path = os.path.join(BASE_DIR, "templates", "static", "img", "liveclosed_thumbnail.png")
        self.fallback_pixmaps = {}

        if os.path.exists(fallback_cime_live_path):
            pix = QPixmap(fallback_cime_live_path)
            self.fallback_pixmaps["cime_live"] = pix.scaled(
                160, 90, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )

        if os.path.exists(fallback_cime_closed_path):
            pix = QPixmap(fallback_cime_closed_path)
            self.fallback_pixmaps["cime_closed"] = pix.scaled(
                160, 90, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )

        if os.path.exists(fallback_chzzk_path):
            pix = QPixmap(fallback_chzzk_path)
            self.fallback_pixmaps["default"] = pix.scaled(
                160, 90, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "썸네일", "채널명", "방송제목", "카테고리", "상태",
            "시작", "중지", "설정", "삭제"
        ])

        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(5, 90)
        self.table.setColumnWidth(6, 90)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

        self.central = QWidget()
        root = QVBoxLayout(self.central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        self.dashboard = self._buildDashboard()
        root.addWidget(self.dashboard)

        root.addWidget(self.table)

        self.setCentralWidget(self.central)

        # 툴바에 버튼 추가
        toolbar = QToolBar("Main Toolbar")

        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # 툴바 버튼 생성
        btn_add = createToolbarButton("채널추가")
        btn_cookie = createToolbarButton("쿠키관리")
        btn_start_all = createToolbarButton("모두녹화시작")
        btn_stop_all = createToolbarButton("모두녹화중지")
        btn_settings = createToolbarButton("설정관리")

        btn_add.clicked.connect(self.openAddChannelDialog)
        btn_cookie.clicked.connect(self.openCookieManager)
        btn_start_all.clicked.connect(lambda: self._qt_create_task(self.startAllRecordings()))
        btn_stop_all.clicked.connect(lambda: self._qt_create_task(self.stopAllRecordings()))
        btn_settings.clicked.connect(self.openSettingsDialog)

        toolbar.addWidget(btn_add)
        toolbar.addWidget(btn_cookie)
        toolbar.addWidget(btn_settings)
        toolbar.addWidget(btn_start_all)
        toolbar.addWidget(btn_stop_all)

        # 필터 검색 위젯
        filterWidget = QWidget()
        filterLayout = QHBoxLayout(filterWidget)
        filterLayout.setContentsMargins(10,0,10,0) 

        self.comboFilter = QComboBox()
        self.comboFilter.setFixedWidth(160)
        self.comboFilter.addItem("모든 채널", "all")
        self.comboFilter.addItem("녹화 중", "recording")
        self.comboFilter.addItem("예약녹화 중", "reserved")
        filterLayout.addWidget(self.comboFilter)

        self.lineSearch = QLineEdit()
        self.lineSearch.setPlaceholderText("채널 검색")
        self.lineSearch.setFixedWidth(200)
        filterLayout.addWidget(self.lineSearch)

        toolbar.addWidget(filterWidget)

        self.comboFilter.currentIndexChanged.connect(self.applyFilter)
        self.lineSearch.textChanged.connect(self.applyFilter)

        self.metadata_timer = QTimer(self)
        self.metadata_timer.setInterval(300000)  # 5분마다 메타데이터(섬네일 등) 업데이트
        self.metadata_timer.timeout.connect(lambda: self._qt_create_task(self.updateAllThumbnails()))
        self.metadata_timer.start()

        # 나머지 초기화
        self.loadChannelsIntoTable()

        # 상태 갱신 전용 타이머
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(max(GUI_STATUS_MIN_MS, GUI_STATUS_INTERVAL_MS))
        self.status_timer.timeout.connect(self.refreshStatusIndicators)
        self.status_timer.start()

        # 메타 API 동시성
        meta_conc = _resolveMetaConcurrency(self.config)
        print(f"[DEBUG] Metadata concurrency = {meta_conc}")
        self.meta_semaphore = asyncio.Semaphore(meta_conc)

        # 메타데이터 동시성 조절
        self.isThumbJobRunning = False
        self.lastMetaFetchAt = {}       
        self.META_COOLDOWN_MS = 20000  
        self.MAX_CONCURRENCY = meta_conc

        # 썸네일 동시성
        thumb_conc = _resolveThumbConcurrency(self.config)
        thumb_conc = max(2, min(thumb_conc, 12))
        print(f"[DEBUG] Thumbnail concurrency = {thumb_conc}")
        self.thumbnail_semaphore = asyncio.Semaphore(thumb_conc)

        # 메타 갱신 재진입/재시도 상태
        self._meta_refresh_lock = asyncio.Lock()
        self._meta_retry_per_cid = defaultdict(int)
        self._meta_retry_scheduled = False
        self._meta_retry_backoff_ms = 0  # 0 → 2000 → 4000 → 최대 8000

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()

        if self.config.get("autoRecordingMode", False):
            # 프로그램 구동 후 8초 뒤에 자동녹화를 시작
            QTimer.singleShot(8000, lambda: self._qt_create_task(self.startAllRecordings()))

        # 백엔드 준비 대기 + 초기 버스트
        QTimer.singleShot(0, lambda: self._qt_create_task(self._startupWarmup()))
        # /status도 준비된 뒤 1회 반영
        QTimer.singleShot(0, lambda: self._qt_create_task(self._statusWarmup()))

        self._bg_tasks = set()

        # 시작 시 트레이 최소화 옵션
        if self._trayEnabled and (self.config or {}).get("minimizeToTrayOnStart", False):
            self.hide()

            try:
                minimizeConsole() 

            except Exception:
                pass

            if self.tray and self.tray.isVisible():
                self.tray.showMessage("recordGUI", "트레이로 최소화되었습니다.", QSystemTrayIcon.MessageIcon.Information, 2000)


    def _qt_create_task(self, coro):
        if not hasattr(self, "_bg_tasks"):
            self._bg_tasks = set()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, "loop", None) or asyncio.get_event_loop()

        task = loop.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task


    def _setupTray(self):
        if not self._trayEnabled or not QSystemTrayIcon.isSystemTrayAvailable():
            self._trayEnabled = False
            return

        self.tray = QSystemTrayIcon(QIcon(self.tray_icon_path), self)
        menu = QMenu()

        act_show = QAction("창 표시", self)
        act_show.triggered.connect(lambda: (self.showNormal(), self.activateWindow()))
        menu.addAction(act_show)

        act_start = QAction("모두 녹화 시작", self)
        act_stop  = QAction("모두 녹화 중지", self)
        act_start.triggered.connect(lambda: self._qt_create_task(self.startAllRecordings()))
        act_stop.triggered.connect(lambda: self._qt_create_task(self.stopAllRecordings()))
        menu.addSeparator()
        menu.addAction(act_start)
        menu.addAction(act_stop)

        act_quit = QAction("완전 종료", self)
        act_quit.triggered.connect(self._quitFromTray)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.setToolTip("recordGUI")
        self.tray.show()


    def _quitFromTray(self):
        # recordGUI 런처가 재시작하지 않도록 센티넬 생성
        sentinel = os.path.join(BASE_DIR, ".shutdown")
        try:
            with open(sentinel, "w", encoding="utf-8") as f:
                f.write("1")
        except Exception:
            pass
        self._reallyClose = True
        self.close()  # → 아래 closeEvent 분기


    def closeEvent(self, event):
        if self._trayEnabled and self._trayClose and not self._reallyClose:
            event.ignore()
            self.hide()
            try:
                minimizeConsole() 

            except Exception:
                pass

            if self.tray and self.tray.isVisible():
                self.tray.showMessage("recordGUI", "백그라운드에서 계속 실행됩니다.",
                                      QSystemTrayIcon.MessageIcon.Information, 2000)
            return

        self._perform_real_close(event)  # 실제 종료 루틴

    def _setSystemMonitorActive(self, active: bool):
        timer = getattr(self, "_sysTimer", None)
        if timer is None:
            return
        active = active and getattr(self, "_systemMonitorExpanded", True)
        if active:
            if not timer.isActive():
                timer.start()
            self._updateSystemStats()
        elif timer.isActive():
            timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._setSystemMonitorActive(True)

    def hideEvent(self, event):
        self._setSystemMonitorActive(False)
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._setSystemMonitorActive(not self.isMinimized() and self.isVisible())

    def _perform_real_close(self, event):
        print("[DEBUG] closeEvent 시작됨.")

        # 타이머 정지
        if getattr(self, "metadata_timer", None) and self.metadata_timer.isActive():
            try: self.metadata_timer.stop()
            except Exception as e: print("[ERROR] metadata_timer stop:", e)
        if getattr(self, "status_timer", None) and self.status_timer.isActive():
            try: self.status_timer.stop()
            except Exception as e: print("[ERROR] status_timer stop:", e)
        if getattr(self, "_sysTimer", None) and self._sysTimer.isActive():
            try: self._sysTimer.stop()
            except Exception as e: print("[ERROR] _sysTimer stop:", e)

        # 상태 폴러 스레드 정지
        try:
            if getattr(self, "_statusThread", None):
                try: self._statusThread.requestInterruption()
                except Exception: pass
                if self._statusThread.isRunning():
                    self._statusThread.quit()
                    self._statusThread.wait(1000)
        except Exception:
            pass

        # 활성 녹화 감지 -> 사용자 확인 후 stop_all_recording
        base_url = getBaseUrl()
        active_channels = []
        try:
            r = requests.get(f"{base_url}/status", timeout=3)
            if r.ok:
                status_data = r.json()
                active_channels = [cid for cid, s in status_data.items()
                                   if s.get("recording") or s.get("reserved")]
        except Exception as e:
            print(f"[WARN] 종료 전 상태 조회 실패: {e}")

        if active_channels:
            reply = QMessageBox.question(
                self, "종료 확인",
                "현재 녹화가 진행 중입니다. 모든 녹화를 중지하고 종료하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        try:
            requests.post(f"{base_url}/api/stop_all_recording", json={"is_user_request": True}, timeout=5)
        except Exception as e:
            print(f"[WARN] stop_all_recording 호출 실패: {e}")

        event.accept()


    def loadChannelsIntoTable(self):
        self.table.setRowCount(0)
        self.channel_row_map = {}
        
        # 전체 행의 기본 높이 설정 (예: 60px)
        default_row_height = 60

        # 테이블 아이콘 크기를 설정 (예: 16x16)
        self.table.setIconSize(QSize(16, 16))

        # 각 채널 정보를 테이블에 추가
        for channel in self.channels:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setRowHeight(row, default_row_height)

            channel_id = channel.get('id')
            self.channel_row_map[channel_id] = row

            # (1) 썸네일 셀
            thumb_label = QLabel()
            thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(row, 0, thumb_label)

            # (2) 채널 이름 셀
            channel_name = channel.get('name', '')
            # 플랫폼에 따라 아이콘 경로 결정
            if channel.get('platform', '').lower() == "cime":
                icon_path = os.path.join(BASE_DIR, "templates", "static", "img", "cime_icon.png")
            else:
                icon_path = os.path.join(BASE_DIR, "templates", "static", "img", "chzzk_icon.png")
            name_item = QTableWidgetItem(channel_name)
            name_item.setIcon(QIcon(icon_path))
            self.table.setItem(row, 1, name_item)

            # (3) 제목 / 카테고리 셀
            title_item = QTableWidgetItem(channel.get('live_title', ''))
            category_item = QTableWidgetItem(channel.get('category', ''))
            self.table.setItem(row, 2, title_item)
            self.table.setItem(row, 3, category_item)

            # (4) 상태 셀
            status_item = QTableWidgetItem("상태")
            self.table.setItem(row, 4, status_item)

            # (5) 시작/중지 버튼 셀
            button_size = QSize(100, 60)  # 모든 버튼의 고정 크기
            start_btn = QPushButton("시작")
            stop_btn  = QPushButton("중지")
            start_btn.setFixedSize(button_size)
            stop_btn.setFixedSize(button_size)

            start_btn.setProperty("context", "rowaction")
            stop_btn.setProperty("context", "rowaction")

            start_btn.style().unpolish(start_btn); start_btn.style().polish(start_btn)
            stop_btn.style().unpolish(stop_btn);   stop_btn.style().polish(stop_btn)

            self.table.setCellWidget(row, 5, start_btn)
            self.table.setCellWidget(row, 6, stop_btn)

            # (6) 설정/삭제 버튼 셀
            edit_btn   = QPushButton("설정")
            delete_btn = QPushButton("삭제")
            edit_btn.setFixedSize(button_size)
            delete_btn.setFixedSize(button_size)

            edit_btn.setProperty("context", "rowaction")
            delete_btn.setProperty("context", "rowaction")
            delete_btn.setProperty("variant", "danger")

            edit_btn.style().unpolish(edit_btn);     edit_btn.style().polish(edit_btn)
            delete_btn.style().unpolish(delete_btn); delete_btn.style().polish(delete_btn)

            self.table.setCellWidget(row, 7, edit_btn)
            self.table.setCellWidget(row, 8, delete_btn)

            # (7) 대기상태로 가정하고 refreshStatusIndicators에서 실제 상태로 갱신
            start_btn.setEnabled(True)
            stop_btn.setEnabled(False)

            # (8) 각 버튼의 시그널 연결
            def _on_start_clicked(cid, btn):
                btn.setEnabled(False)  # 즉시 잠금
                self._qt_create_task(asyncio.to_thread(self.startRecordingInBackground, cid, True))

            def _on_stop_clicked(cid, btn):
                btn.setEnabled(False)
                self._qt_create_task(asyncio.to_thread(self.stopRecordingInBackground, cid))

            start_btn.clicked.connect(lambda _, cid=channel_id, b=start_btn: _on_start_clicked(cid, b))
            stop_btn.clicked.connect(lambda _, cid=channel_id, b=stop_btn: _on_stop_clicked(cid, b))
            edit_btn.clicked.connect(lambda _, cid=channel_id: self.editChannel(cid))
            delete_btn.clicked.connect(lambda _, cid=channel_id: self.confirmDeleteChannel(cid))

        self.applyFilter()

        # 고정 열의 너비를 설정 (필요에 따라 조정)
        self.table.setColumnWidth(0, 160)  # 썸네일 열
        self.table.setColumnWidth(5, 90)   # 시작 버튼 열
        self.table.setColumnWidth(6, 90)   # 중지 버튼 열
        self.table.setColumnWidth(7, 90)   # 설정 버튼 열
        self.table.setColumnWidth(8, 90)   # 삭제 버튼 열

        # 나머지 열은 Stretch 모드로 설정하여 가변적으로 채움
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)


    def getFallbackPixmap(self, platform, is_live=None):
        platform = (platform or "").lower()

        if platform == "cime":
            if is_live is True:
                return self.fallback_pixmaps.get("cime_live") or self.fallback_pixmaps.get("cime_closed")
            return self.fallback_pixmaps.get("cime_closed") or self.fallback_pixmaps.get("cime_live")

        return self.fallback_pixmaps.get("default")


    @staticmethod
    def processImage(data):
        from PyQt6.QtGui import QImage
        image = QImage()
        if not image.loadFromData(data):
            return None
        scaled_image = image.scaled(160, 90, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
        return scaled_image


    async def processSetImage(self, data, label, platform):
        # QImage 처리와 scaling을 백그라운드에서 진행
        scaled_image = await asyncio.to_thread(self.processImage, data)
        if scaled_image is not None:
            pixmap = QPixmap.fromImage(scaled_image)
            label.setPixmap(pixmap)
        else:
            # 처리 실패 시 fallback 이미지 사용
            pixmap = self.getFallbackPixmap(platform)
            if pixmap:
                label.setPixmap(pixmap)


    def thumbnailNetwork(self, thumbnail_url, label, platform) -> bool:
        # 1) 서버 상대경로면 절대경로로
        if thumbnail_url.startswith("/"):
            thumbnail_url = f"{getBaseUrl()}{thumbnail_url}"

        # 2) http(s)가 아니면 네트워크 시도하지 말고 폴백 이미지 → '요청 시작 안 함'
        if not thumbnail_url.startswith(("http://", "https://")):
            pixmap = self.getFallbackPixmap(platform)
            if pixmap:
                label.setPixmap(pixmap)
            return False

        # 3) 치지직 토큰 치환 (씨미 URL엔 영향 없음)
        thumbnail_url = thumbnail_url.replace("{type}", "144")

        request = QNetworkRequest(QUrl(thumbnail_url))
        reply = self.network_manager.get(request)

        timeout_timer = QTimer(reply)
        timeout_timer.setSingleShot(True)
        timeout_timer.setInterval(8000)

        def _abort_reply_on_timeout():
            try:
                if not reply.isFinished():
                    print(f"[WARN] thumbnail timeout → abort: {thumbnail_url}")
                    reply.abort()
            except Exception:
                pass

        timeout_timer.timeout.connect(_abort_reply_on_timeout)
        reply.finished.connect(timeout_timer.stop)
        reply.finished.connect(lambda: self.onthumbnailFinished(reply, label, platform))
        timeout_timer.start()

        return True


    def onthumbnailFinished(self, reply, label, platform):
        # 작업 완료 후 semaphore 해제
        try:
            self.thumbnail_semaphore.release()
        except Exception:
            pass

        if reply.error() != QNetworkReply.NetworkError.NoError:
            print(f"[ERROR] Thumbnail load error: {reply.errorString()} for {reply.request().url().toString()}")
            # fallback: 캐시된 fallback 이미지를 사용
            pixmap = self.getFallbackPixmap(platform)
            if pixmap:
                label.setPixmap(pixmap)
            reply.deleteLater()
            return

        # 네트워크에서 받은 data를 bytes로 복사해 스레드 경계 이슈 방지
        data = bytes(reply.readAll())
        asyncio.create_task(self.processSetImage(data, label, platform))
        reply.deleteLater()


    # 메타 키 헬퍼
    def _pick(self, d, keys, default=None):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return default


    def isLive(self, meta: dict) -> bool:
        return bool(self._pick(meta, ['is_live','isLive','live','online'], False))


    def isOpenStatus(self, meta: dict) -> bool:
        # status / open_status 등 다양한 키 대응
        st = str(self._pick(meta, ['status','open_status'], '')).upper()
        return st == 'OPEN'


    async def _statusWarmup(self):
        await self._waitApiReady(30)
        self.refreshStatusIndicators()


    #  상태/버튼 즉시 반영    
    def _applyImmediateState(self, channel_id: str, state: str | None, duration: str | None = None):
        row = self.channel_row_map.get(channel_id)
        if row is None:
            return

        st = (state or "").strip() or "대기 중"
        status_item = self.table.item(row, 4)
        start_btn   = self.table.cellWidget(row, 5)
        stop_btn    = self.table.cellWidget(row, 6)

        # 1) 상태 텍스트
        if st == "녹화 중":
            text = "녹화중"
            if duration:
                text += f"\n{duration}"
        elif st == "예약녹화 중":
            text = "예약녹화 중"
        else:
            # 모르는 문자열/None → 대기 중으로 통일
            st = "대기 중"
            text = "대기 중"

        if status_item:
            status_item.setText(text)

        # 2) 버튼 활성/비활성
        if st == "녹화 중" or st == "예약녹화 중":
            if start_btn: start_btn.setEnabled(False)
            if stop_btn:  stop_btn.setEnabled(True)

        else:
            if start_btn: start_btn.setEnabled(True)
            if stop_btn:  stop_btn.setEnabled(False)


    def applyStatus(self, status_data: dict):
        # 빈 응답/형식 오류는 무시 → 깜빡임 방지
        if not isinstance(status_data, dict) or not status_data:
            return

        channels_snapshot = list(self.channels or [])
        for channel in channels_snapshot:
            cid = channel.get("id")
            if not cid:
                continue

            stat = status_data.get(cid)
            if stat is None:
                # 새 응답에 이 CID가 없으면 기존 상태 유지
                stat = self._last_status.get(cid)
                if stat is None:
                    continue  # 아예 모르면 건드리지 않음

            prev = self._last_status.get(cid) or {}
            nowm = time.monotonic()
            was_recording = bool(prev.get("recording"))

            # 1) 기본 표기
            st = "녹화 중" if stat.get("recording") else ("예약녹화 중" if stat.get("reserved") else "대기 중")

            # 2) 점착 창구: 직전 폴링까지 '녹화 중'이었다면, 다음 짧은 창(예: 8초) 동안
            sticky_until = float(self._recordingStickyUntil.get(cid) or 0.0)
            if stat.get("recording"):
                self._recordingStickyUntil[cid] = nowm + 8.0
            elif st == "대기 중" and sticky_until > nowm:
                st = "녹화 중"

            dur = stat.get("recording_duration") or None
            self._applyImmediateState(cid, st, dur)

            # 캐시 갱신
            self._last_status[cid] = stat


    def refreshStatusIndicators(self):
        # 이전 폴링이 아직 돌고 있으면 건너뜀
        if getattr(self, "_statusThread", None) and self._statusThread.isRunning():
            return

        base_url = getBaseUrl()
        t = _StatusPoller(base_url, timeout=(1.5, 3.5), parent=self)
        t.result.connect(self.applyStatus)
        t.finished.connect(t.deleteLater)              
        t.finished.connect(lambda: setattr(self, "_statusThread", None))
        self._statusThread = t
        t.start()


    async def _waitApiReady(self, timeout_sec: int = 30) -> bool:
        base_url = getBaseUrl()
        deadline = time.monotonic() + timeout_sec
        backoff = 0.5
        while time.monotonic() < deadline:
            try:
                r = await asyncio.to_thread(requests.get, f"{base_url}/status", timeout=2)
                if r.ok:
                    return True
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 3.0)
        return False


    async def _startupWarmup(self):
        ok = await self._waitApiReady(30)
        if not ok:
            print("[WARN] API ready timeout; 계속 진행")

        # 초기 부팅 직후 캐시 미스 보정용 빠른 재시도
        self.quickRetryLeft = 3

        try:
            await self.updateAllThumbnails()
        except Exception as e:
            print("[WARN] warmup refresh failed:", e)


    async def updateAllThumbnails(self, force: bool = False):
        if self.isThumbJobRunning:
            return
        self.isThumbJobRunning = True

        base_url = getBaseUrl()
        try:
            now_ts = int(time.time() * 1000)
            if force:
                self.lastMetaFetchAt.clear()

            # (A) 메타 스냅샷으로 제목/카테고리 먼저 채우기
            r_meta = await asyncio.to_thread(requests.get, f"{base_url}/api/metadata_snapshot", timeout=10)
            meta_bulk = (r_meta.json() or {}).get("channels", []) if r_meta.ok else []
            meta_map  = {str(it.get("id")): it for it in meta_bulk}

            BAD_TXT = {"", None, "방송 제목 없음", "정보 없음", "카테고리 없음", "불러오는 중..."}

            def _is_placeholder(v) -> bool:
                s = (str(v).strip() if v is not None else "")
                return s in BAD_TXT

            def _set_text_if_better(row: int, col: int, new_text):
                it = self.table.item(row, col)
                if not it:
                    return
                cur = (it.text() or "").strip()

                if cur and not _is_placeholder(cur) and _is_placeholder(new_text):
                    return
                if new_text is None:
                    return
                new_s = truncateText(str(new_text).strip(), 40)
                if cur == new_s:
                    return  # 같은 값이면 무의미한 갱신 방지
                it.setText(new_s)

            def _apply_text(cid, info):
                row = self.channel_row_map.get(cid)
                if row is None:
                    return

                title = self._pick(
                    info,
                    ['live_title', 'liveTitle', 'title', 'video_title', 'name', 'channel_name'],
                    default=''
                )
                cate = self._pick(
                    info,
                    ['category', 'liveCategoryValue', 'category_name', 'categoryValue', 'game', 'game_name', 'genre', 'tag'],
                    default=''
                )

                if title != "":
                    _set_text_if_better(row, 2, title)
                if cate  != "":
                    _set_text_if_better(row, 3, cate)

            for ch in self.channels:
                cid = ch.get("id")
                if not cid: 
                    continue
                info = meta_map.get(cid, {}) or {}
                _apply_text(cid, info)

            # (B) 썸네일 상태로 이미지 채우기
            r = await asyncio.to_thread(requests.get, f"{base_url}/api/thumbnail_status", timeout=10)
            bulk = (r.json() or {}).get('channels', []) if r.ok else []
            bulk_map = {str(it.get('id')): it for it in bulk}

            def _apply_thumb(cid, info):
                row = self.channel_row_map.get(cid)
                if row is None: 
                    return
                turl = (info.get("thumbnail_url") or "")
                if turl.startswith("/"):
                    turl = f"{base_url}{turl}"
                if turl:
                    lbl = self.table.cellWidget(row, 0)
                    if isinstance(lbl, QLabel):
                        asyncio.create_task(self._loadThumbSafe(turl, lbl, info.get("platform","")))
            for cid, info in bulk_map.items():
                _apply_thumb(cid, info)

            # (C) 빈 텍스트 보강
            def _blankish(s):
                return _is_placeholder(s)

            needs = []
            for ch in self.channels:
                cid = ch.get("id")
                if not cid: 
                    continue
                info  = meta_map.get(cid, {}) or {}
                title = self._pick(info, ['live_title','title','video_title','name'])
                cate  = self._pick(info, ['category','category_name','game','game_name','genre','tag'])
                missing_text = _blankish(title) or _blankish(cate)

                last = self.lastMetaFetchAt.get(cid, 0)
                if force or (missing_text and (now_ts - last > self.META_COOLDOWN_MS)):
                    self.lastMetaFetchAt[cid] = now_ts
                    needs.append(cid)

            async def _patch_one(cid: str):
                async with self.meta_semaphore:
                    try:
                        r = await asyncio.to_thread(
                            requests.get, f"{base_url}/api/update_metadata/{cid}", timeout=10
                        )
                        if not r.ok:
                            return
                        j = r.json() or {}
                        meta = j.get("metadata") if isinstance(j, dict) else j
                        if not isinstance(meta, dict):
                            return

                        row = self.channel_row_map.get(cid)
                        if row is None:
                            return

                        # 텍스트 갱신
                        title = self._pick(
                            meta,
                            ['live_title','liveTitle','title','video_title','name','channel_name'],
                            '정보 없음'
                        )
                        cate = self._pick(
                            meta,
                            ['category','liveCategoryValue','category_name','categoryValue','game','game_name','genre','tag'],
                            '정보 없음'
                        )

                        _set_text_if_better(row, 2, title)
                        _set_text_if_better(row, 3, cate)

                    except Exception:
                        pass

            await asyncio.gather(*[asyncio.create_task(_patch_one(cid)) for cid in needs])

            # (D) 초기 부팅 직후 빠른 재시도
            any_stale = any(cid in needs for cid in [c.get("id") for c in self.channels if c.get("id")])
            if any_stale and getattr(self, "quickRetryLeft", 0) > 0:
                self.quickRetryLeft -= 1
                QTimer.singleShot(1500, lambda: asyncio.create_task(self.updateAllThumbnails()))

        except Exception as e:
            print("[WARN] updateAllThumbnails error:", e)
        finally:
            self.isThumbJobRunning = False


    async def _loadThumbSafe(self, url, label, platform):
        await self.thumbnail_semaphore.acquire()
        started = False
        try:
            started = self.thumbnailNetwork(url, label, platform)
        except Exception:
            # 네트워크 시작 자체가 실패한 경우
            started = False
        finally:
            # 네트워크 요청을 시작하지 못했다면 여기서 풀어준다
            if not started:
                self.thumbnail_semaphore.release()


    async def _refreshChannelMetadata(self, channel):
        cid = channel.get('id')
        platform = (channel.get('platform') or '').lower()
        base_url = getBaseUrl()

        # 1) API 호출 (실패 시 짧은 재시도 스케줄)
        try:
            r = await asyncio.to_thread(
                requests.get,
                f"{base_url}/api/update_metadata/{cid}",
                timeout=8
            )
            r.raise_for_status()
            meta = r.json() or {}
            # 응답이 {"metadata": {...}} 형태면 내부 dict로 평탄화
            if isinstance(meta, dict) and isinstance(meta.get("metadata"), dict):
                meta = meta["metadata"]

            # 성공했으면 채널별 재시도 카운터 초기화
            self._meta_retry_per_cid[cid] = 0

        except Exception as e:
            print(f"[ERROR] metadata API error for {cid}: {e}")
            # --- 채널별 짧은 재시도: 1.5s → 3s → 4.5s (최대 3회) ---
            cnt = self._meta_retry_per_cid[cid]
            if cnt < 3:
                self._meta_retry_per_cid[cid] += 1
                delay = 1500 * (cnt + 1)
                # lambda 기본인자에 channel 바인딩(클로저 캡처 이슈 방지)
                QTimer.singleShot(delay, lambda ch=channel: asyncio.create_task(self._refreshChannelMetadata(ch)))
            else:
                # 3회 실패 후 카운터 리셋하고 종료(다음 상위 주기에 맡김)
                self._meta_retry_per_cid[cid] = 0
            return  # 실패 시 여기서 끝. (UI에 '정보 없음'으로 덮지 않음)

        # 2) 테이블(제목/카테고리) 갱신
        row = self.channel_row_map.get(cid)
        temp_thumb_url = None

        if meta and row is not None:
            title    = self._pick(meta, ['live_title','liveTitle','title','video_title','name'], '정보 없음')
            category = self._pick(meta, ['category','liveCategoryValue','category_name'], '정보 없음')
            title_item = self.table.item(row, 2)
            category_item = self.table.item(row, 3)
            if title_item:
                title_item.setText(truncateText(str(title), 40))
            if category_item:
                category_item.setText(truncateText(str(category), 40))
            temp_thumb_url = meta.get('thumbnail_url')
        elif row is not None:
            title_item = self.table.item(row, 2)
            category_item = self.table.item(row, 3)
            if title_item:
                title_item.setText('정보 없음')
            if category_item:
                category_item.setText('정보 없음')
            temp_thumb_url = None

        # 3) 썸네일 URL 보정
        thumb_url = None
        if temp_thumb_url:
            if temp_thumb_url.startswith('http://') or temp_thumb_url.startswith('https://'):
                thumb_url = temp_thumb_url
            elif temp_thumb_url.startswith('/'):
                thumb_url = f"{getBaseUrl()}{temp_thumb_url}"
            else:
                print(f"[WARN] 알 수 없는 썸네일 경로 형식: {temp_thumb_url}")

        # 4) 썸네일 QLabel 찾기
        label = None
        if row is not None:
            widget = self.table.cellWidget(row, 0)
            if isinstance(widget, QLabel):
                label = widget
            else:
                print(f"[WARN] Row {row}, Col 0 is not a QLabel for channel {cid}")
        if label is None:
            print(f"[WARN] QLabel not found for channel {cid}, skipping thumbnail update.")
            return

        # 5) 썸네일 로딩 or 폴백
        if thumb_url:
            await self.thumbnail_semaphore.acquire()
            self.thumbnailNetwork(thumb_url, label, platform)
        else:
            pixmap = self.getFallbackPixmap(platform)
            if pixmap:
                label.setPixmap(pixmap)
            else:
                print(f"[WARN] Fallback 이미지도 로드할 수 없습니다.")


    @staticmethod
    def _shortDiskLabel(p):
        raw_mp = (p.mountpoint or "").strip()
        mp = raw_mp if raw_mp == os.sep else raw_mp.rstrip(os.sep)

        if os.name == "nt":
            label = mp.upper() if (len(mp) >= 2 and mp[1] == ":") else (p.device or mp)
        else:
            if mp in ("/", "/home", "/boot"):
                label = mp
            elif mp.startswith("/sys/fs/cgroup/"):
                parts = [x for x in mp.split("/") if x]
                label = "cgroup/" + (parts[3] if len(parts) > 3 else "")
            elif len(mp) > 16:
                base = os.path.basename(mp)
                label = base if base else mp
            else:
                label = mp

        return label


    def _buildDashboard(self) -> QWidget:
        root = QWidget()
        root.setObjectName("SystemDashboard")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        header = QWidget()
        header.setObjectName("SystemDashboardHeader")
        headerLayout = QHBoxLayout(header)
        headerLayout.setContentsMargins(4, 0, 0, 0)
        brand = QLabel("recordGUI")
        brand.setObjectName("SystemDashboardBrand")
        self._versionButton = QPushButton(PROGRAM_VERSION)
        self._versionButton.setObjectName("VersionButton")
        self._versionButton.clicked.connect(self._showVersionInfo)
        title = QLabel("시스템 상태")
        title.setObjectName("SystemDashboardTitle")
        self._systemMonitorToggle = QPushButton()
        self._systemMonitorToggle.setObjectName("SystemMonitorToggle")
        self._systemMonitorToggle.setFixedWidth(64)
        headerLayout.addWidget(brand)
        headerLayout.addWidget(self._versionButton)
        headerLayout.addStretch(1)
        headerLayout.addWidget(title)
        headerLayout.addWidget(self._systemMonitorToggle)
        lay.addWidget(header)

        self._systemMonitorBody = QWidget()
        bodyLayout = QVBoxLayout(self._systemMonitorBody)
        bodyLayout.setContentsMargins(0, 0, 0, 0)
        bodyLayout.setSpacing(8)
        lay.addWidget(self._systemMonitorBody)

        def make_card(title: str, detail: str = ""):
            card = QFrame()
            card.setObjectName("MetricCard")
            card.setProperty("level", "normal")
            card.setMinimumHeight(88)
            v = QVBoxLayout(card)
            v.setContentsMargins(12, 10, 12, 10)
            v.setSpacing(5)

            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            lblTitle = QLabel(title)
            lblTitle.setObjectName("CardTitle")
            detailLabel = QLabel(detail)
            detailLabel.setObjectName("CardSub")
            detailLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            head.addWidget(lblTitle)
            head.addStretch(1)
            head.addWidget(detailLabel)

            val = QLabel("--")
            val.setObjectName("CardValue")
            bar = QProgressBar()
            bar.setObjectName("MetricBar")
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(6)
            bar.setTextVisible(False)
            v.addLayout(head)
            v.addWidget(val)
            v.addWidget(bar)
            return card, val, bar, detailLabel

        self._cpuCard, self._cpuValue, self._cpuBar, self._cpuDetail = make_card("CPU", cpu_name)
        self._memCard, self._memValue, self._memBar, self._memDetail = make_card("메모리", "사용량")
        self._netCard, self._netValue, self._netBar, self._netAccum = make_card("네트워크", "프로그램 실행 후 누적")

        self._diskCards = []
        try:
            parts = psutil.disk_partitions(all=True)
        except Exception:
            parts = []

        seen = set()
        for p in parts:
            mp = (p.mountpoint or "").strip()
            if not mp or mp in seen:
                continue

            # 임시 파일시스템 제외
            EPHEMERAL = {"tmpfs", "proc", "sysfs", "cgroup", "cgroup2", "squashfs", "devpts", "overlay"}
            try:
                fstype = (p.fstype or "").lower()
            except Exception:
                fstype = ""
            if fstype in EPHEMERAL and mp not in ("/", "/home", "/boot"):
                continue

            try:
                psutil.disk_usage(mp)
            except Exception:
                continue

            seen.add(mp)
            label = self._shortDiskLabel(p)
            card, val, bar, detail = make_card(label, fstype.upper())
            self._diskCards.append((card, val, bar, detail, p))

        self._diskCards = self._diskCards[:10]

        topGrid = QGridLayout()
        topGrid.setContentsMargins(0, 0, 0, 0)
        topGrid.setSpacing(8)
        topGrid.addWidget(self._cpuCard, 0, 0)
        topGrid.addWidget(self._memCard, 0, 1)
        topGrid.addWidget(self._netCard, 0, 2)
        for column in range(3):
            topGrid.setColumnStretch(column, 1)
        bodyLayout.addLayout(topGrid)

        diskGrid = QGridLayout()
        diskGrid.setContentsMargins(0, 0, 0, 0)
        diskGrid.setSpacing(8)
        columns = 4
        for idx, (card, _, _, _, _) in enumerate(self._diskCards):
            diskGrid.addWidget(card, idx // columns, idx % columns)
        for column in range(columns):
            diskGrid.setColumnStretch(column, 1)
        bodyLayout.addLayout(diskGrid)

        self._net_prev = psutil.net_io_counters()
        self._net_base = self._net_prev
        self._net_prev_t = time.monotonic()
        psutil.cpu_percent(interval=None)

        self._sysTimer = QTimer(self)
        self._sysTimer.setTimerType(Qt.TimerType.CoarseTimer)
        self._sysTimer.setInterval(2000)
        self._sysTimer.timeout.connect(self._updateSystemStats)

        settings = QSettings("recordFSM", "recordGUI")
        saved = settings.value("ui/systemMonitorExpanded", True)
        self._systemMonitorExpanded = str(saved).lower() not in {"false", "0", "no"}
        self._systemMonitorToggle.clicked.connect(self._toggleSystemDashboard)
        self._setSystemDashboardExpanded(self._systemMonitorExpanded, save=False)

        return root

    def _toggleSystemDashboard(self):
        self._setSystemDashboardExpanded(not self._systemMonitorExpanded)

    def _showVersionInfo(self):
        VersionInfoDialog(self).exec()

    def _setSystemDashboardExpanded(self, expanded: bool, save: bool = True):
        self._systemMonitorExpanded = bool(expanded)
        self._systemMonitorBody.setVisible(self._systemMonitorExpanded)
        self._systemMonitorToggle.setText("접기" if self._systemMonitorExpanded else "펼치기")
        self._systemMonitorToggle.setProperty("expanded", self._systemMonitorExpanded)
        if save:
            QSettings("recordFSM", "recordGUI").setValue(
                "ui/systemMonitorExpanded",
                self._systemMonitorExpanded,
            )
        self._setSystemMonitorActive(self.isVisible() and not self.isMinimized())


    def _updateSystemStats(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            self._cpuValue.setText(f"{cpu:.0f}%")
            self._cpuBar.setValue(int(cpu))
            self._setDashboardLevel(self._cpuCard, cpu)

            vm = psutil.virtual_memory()
            mem = vm.percent
            self._memValue.setText(f"{mem:.0f}%")
            self._memDetail.setText(f"{self._formatBytes(vm.used)} / {self._formatBytes(vm.total)}")
            self._memBar.setValue(int(mem))
            self._setDashboardLevel(self._memCard, mem)

            for card, val, bar, detail, part in getattr(self, "_diskCards", []):
                try:
                    u = psutil.disk_usage(part.mountpoint)
                    pct = int(u.percent)
                    val.setText(f"{pct:.0f}%")
                    fs = (part.fstype or "").upper()
                    usage = f"{self._formatBytes(u.used)} / {self._formatBytes(u.total)}"
                    detail.setText(f"{fs} · {usage}" if fs else usage)
                    bar.setValue(pct)
                    self._setDashboardLevel(card, pct, warning=75, danger=90)
                except Exception:
                    val.setText("--")
                    detail.setText("연결 확인 필요")
                    bar.setValue(0)

            now = time.monotonic()
            cur = psutil.net_io_counters()
            dt = max(1e-3, now - self._net_prev_t)

            up_bps   = (cur.bytes_sent - self._net_prev.bytes_sent) / dt
            down_bps = (cur.bytes_recv - self._net_prev.bytes_recv) / dt

            self._net_prev = cur
            self._net_prev_t = now

            self._netValue.setText(f"↑ {self._formatBytes(up_bps)}/s  ·  ↓ {self._formatBytes(down_bps)}/s")

            if getattr(self, "_net_base", None):
                up_total   = cur.bytes_sent - self._net_base.bytes_sent
                down_total = cur.bytes_recv - self._net_base.bytes_recv
                self._netAccum.setText(f"↑ {self._formatBytes(up_total)} · ↓ {self._formatBytes(down_total)}")

            scale = 100 * 1024 * 1024
            score = min(100, int((up_bps + down_bps) * 100 / scale))
            self._netBar.setValue(score)
            self._setDashboardLevel(self._netCard, score, warning=65, danger=90)

        except Exception:
            pass


    def _setDashboardLevel(self, card: QWidget, value: float, warning: int = 70, danger: int = 90):
        level = "danger" if value >= danger else "warning" if value >= warning else "normal"
        if card.property("level") == level:
            return
        card.setProperty("level", level)
        card.style().unpolish(card)
        card.style().polish(card)


    # 바이트 변환    
    def _formatBytes(self, b: float) -> str:
        n = float(b)
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while n >= 1024 and i < len(units) - 1:
            n /= 1024.0
            i += 1
        if i == 0:
            return f"{int(n)}{units[i]}"
        return f"{n:.1f}{units[i]}"


    async def startAllRecordings(self):
        base_url = getBaseUrl()
        try:
            r = await asyncio.to_thread(
                requests.post,
                f"{base_url}/api/start_all_recording",
                json={"is_user_request": True},
                timeout=10
            )
            if r.status_code == 200:
                data = {}
                try:
                    data = r.json() or {}
                except Exception:
                    pass
                cs = data.get("channels_status") or {}
                for cid, info in cs.items():
                    st  = info.get("state")
                    dur = info.get("recording_duration")
                    QTimer.singleShot(0, lambda c=cid, s=st, d=dur: self._applyImmediateState(c, s, d))
            else:
                print(f"[ERROR] start_all_recording 실패: {r.status_code} {r.text}")
        except Exception as e:
            print(f"[ERROR] startAllRecordings 예외: {e}")

        QTimer.singleShot(0, self.refreshStatusIndicators)


    async def stopAllRecordings(self):
        base_url = getBaseUrl()
        url = f"{base_url}/api/stop_all_recording"
        try:
            r = await asyncio.to_thread(
                requests.post,
                url,
                json={"is_user_request": True},
                timeout=10
            )
            if r.status_code == 200:
                data = {}
                try:
                    data = r.json() or {}
                except Exception:
                    pass
                cs = data.get("channels_status") or {}
                # 채널별 즉시 반영
                for cid, info in cs.items():
                    st  = info.get("state") or "대기 중"
                    dur = info.get("recording_duration")
                    QTimer.singleShot(0, lambda c=cid, s=st, d=dur: self._applyImmediateState(c, s, d))
            else:
                print(f"[ERROR] stop_all_recording 실패: {r.status_code} {r.text}")
        except Exception as e:
            print(f"[ERROR] stopAllRecordings 예외: {e}")

        QTimer.singleShot(0, self.refreshStatusIndicators)  # 폴링 재동기화



    def applyFilter(self):
        filterMode = self.comboFilter.currentData()
        searchText = self.lineSearch.text().strip().lower()

        rowCount = self.table.rowCount()
        for row in range(rowCount):
            # 상태/이름 텍스트 가져오기
            status_item = self.table.item(row, 4)
            status_text = status_item.text() if status_item else ""

            name_item = self.table.item(row, 1)
            channel_name = name_item.text() if name_item else ""

            # 1) 상태 필터
            if filterMode == "all":
                matchFilter = True
            elif filterMode == "recording":
                matchFilter = ("녹화중" in status_text) and ("예약녹화 중" not in status_text)
            elif filterMode == "reserved":
                matchFilter = ("예약녹화 중" in status_text)
            else:
                matchFilter = True  

            # 2) 검색어
            matchSearch = (searchText == "") or (searchText in channel_name.lower())

            # 최종 표시 여부
            visible = (matchFilter and matchSearch)
            self.table.setRowHidden(row, not visible)


    def editChannel(self, channel_id: str):
        channel = next((c for c in self.channels if c['id'] == channel_id), None)
        if not channel:
            QMessageBox.warning(self, "오류", "채널 정보를 찾을 수 없습니다.")
            return

        dlg = EditChannelDialog(channel, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            updated_data = dlg.getUpdatedData()
            if not updated_data:
                return

            # 채널 리스트 업데이트
            for i, ch in enumerate(self.channels):
                if ch['id'] == channel_id:
                    self.channels[i].update(updated_data)
                    break
            saveChannels(self.channels)

            QMessageBox.information(self, "완료", "채널 정보가 수정되었습니다.")

            # 1) 테이블 재구성
            self.loadChannelsIntoTable()

            # 상태/메타 즉시 강제 갱신
            QTimer.singleShot(0, self.refreshStatusIndicators)
            self._qt_create_task(self.updateAllThumbnails(force=True))

            # 1-1) 서버에서 최신 채널 목록을 한번 받아서 self.channels 싱크
            try:
                base_url = getBaseUrl()
                r = requests.get(f"{base_url}/api/channels", timeout=4)
                if r.ok:
                    j = r.json()
                    self.channels = j["channels"] if isinstance(j, dict) and "channels" in j else j
            except Exception:
                pass

            # 2) 즉시 상태/메타 갱신 트리거
            QTimer.singleShot(0, self.refreshStatusIndicators)          # 상태 텍스트 즉시 동기화
            self._qt_create_task(self.updateAllThumbnails())             # 썸네일/제목/카테고리 보강


    def confirmDeleteChannel(self, channel_id: str):
        confirm = QMessageBox.question(
            self,
            "채널 삭제",
            f"정말 채널 '{channel_id}'을(를) 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._qt_create_task(self.deleteChannelAsync(channel_id))


    async def deleteChannelAsync(self, channel_id: str):
        if not hasattr(self, "_delete_lock"):
            self._delete_lock = asyncio.Lock()

        async with self._delete_lock:
            # 상태 갱신 타이머가 있다면 잠시 중단
            timer_was_active = False
            if hasattr(self, "status_timer") and self.status_timer and self.status_timer.isActive():
                self.status_timer.stop()
                timer_was_active = True

            try:
                base_url = getBaseUrl()
                # 서버에 삭제 요청
                r = await asyncio.to_thread(
                    requests.delete,
                    f"{base_url}/api/channels/{channel_id}",
                    timeout=10
                )
                if r.status_code != 200:
                    try:
                        msg = r.json().get("detail")
                    except Exception:
                        msg = r.text or f"HTTP {r.status_code}"
                    raise Exception(msg)

                # 1) 로컬 목록에서 제거
                self.channels = [c for c in self.channels if c.get("id") != channel_id]

                # 2) 테이블에서 해당 행만 제거 + map 보정
                row = self.channel_row_map.pop(channel_id, None)
                if row is not None and 0 <= row < self.table.rowCount():
                    self.table.setUpdatesEnabled(False)
                    try:
                        self.table.removeRow(row)
                        # 인덱스가 한 칸씩 당겨지므로 map 보정
                        for cid, rindex in list(self.channel_row_map.items()):
                            if rindex > row:
                                self.channel_row_map[cid] = rindex - 1
                    finally:
                        self.table.setUpdatesEnabled(True)

                # 3) 필터/검색 재적용
                self.applyFilter()

                # 4) 알림 + 후속 동기화
                QTimer.singleShot(0, lambda: QMessageBox.information(
                    self, "삭제 완료", f"'{channel_id}' 채널을 삭제했습니다.")
                )
                QTimer.singleShot(0, self.refreshStatusIndicators)
                # 썸네일 벌크 갱신은 살짝 지연시켜 레이스 완화
                QTimer.singleShot(150, lambda: self._qt_create_task(self.updateAllThumbnails()))

            except Exception as e:
                QTimer.singleShot(0, lambda: QMessageBox.warning(
                    self, "오류", f"채널 삭제 중 오류 발생: {e}")
                )
            finally:
                # 타이머 재개
                if timer_was_active:
                    QTimer.singleShot(200, self.status_timer.start)


    def openAddChannelDialog(self):
        dlg = AddChannelDialog(parent=self)
        dlg.setModal(True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # 추가 직전 상태 기억
            before_ids = {c.get("id") for c in (self.channels or [])}

            # 짧게 재시도하며 “새 ID가 포함된” 목록이 올 때까지 기다린다.
            def _delayed_reload(tries=0):
                base_url = getBaseUrl()
                updated = None
                try:
                    rr = requests.get(f"{base_url}/api/channels", timeout=3)
                    if rr.ok:
                        j = rr.json()
                        updated = j["channels"] if isinstance(j, dict) and "channels" in j else j
                except Exception:
                    updated = None
                if updated is None:
                    updated = loadChannels()

                has_new = any(c.get("id") not in before_ids for c in (updated or []))
                if has_new or tries >= 5:  # 최대 ~750ms
                    # 동시 갱신으로 인한 UI 흔들림 방지
                    self.table.setUpdatesEnabled(False)
                    # 썸네일 작업 진행 중이면 아주 잠깐 늦춘다
                    if self.isThumbJobRunning:
                        QTimer.singleShot(50, lambda: _delayed_apply(updated))
                    else:
                        _delayed_apply(updated)
                else:
                    QTimer.singleShot(150, lambda: _delayed_reload(tries + 1))

            def _delayed_apply(updated):
                self.channels = updated or loadChannels()
                self.loadChannelsIntoTable()
                self.table.setUpdatesEnabled(True)
                self._qt_create_task(self.updateAllThumbnails())
                QTimer.singleShot(0, self.refreshStatusIndicators)

            _delayed_reload()


    def openCookieManager(self):
        dlg = CookieManagerDialog(parent=self)
        dlg.setModal(False)
        dlg.show()


    def openSettingsDialog(self):
        dlg = SettingsDialog(self)
        dlg.setModal(False)
        dlg.show()


    def startRecordingForChannel(self, channel_id: str, is_user_request: bool = True):
        base_url = getBaseUrl()
        url = f"{base_url}/api/start_recording/{channel_id}"
        payload = {"is_user_request": is_user_request}
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                data = {}
                try:
                    data = response.json() or {}
                except Exception:
                    pass
                st  = data.get("state") or "녹화 중"
                dur = data.get("recording_duration")
                QTimer.singleShot(0, lambda cid=channel_id, s=st, d=dur: self._applyImmediateState(cid, s, d))
                self.refreshStatusIndicators()  # 폴링 재동기화
            else:
                raise Exception(response.json().get("detail", "알 수 없는 오류"))
        except Exception as e:
            QMessageBox.warning(self, "녹화 시작 오류", f"{channel_id} 녹화 시작 실패: {e}")


    def stopRecordingForChannel(self, channel_id: str):
        base_url = getBaseUrl()
        url = f"{base_url}/api/stop_recording/{channel_id}"
        payload = {"is_user_request": True}
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                data = {}
                try:
                    data = response.json() or {}
                except Exception:
                    pass
                st  = data.get("state") or "대기 중"
                dur = data.get("recording_duration")
                QTimer.singleShot(0, lambda cid=channel_id, s=st, d=dur: self._applyImmediateState(cid, s, d))
                self.refreshStatusIndicators()  # 폴링 재동기화
            else:
                raise Exception(response.json().get("detail", "알 수 없는 오류"))
        except Exception as e:
            QMessageBox.warning(self, "녹화 중지 오류", f"{channel_id} 녹화 중지 실패: {e}")


    def startRecordingInBackground(self, channel_id: str, is_user_request: bool = True):
        base_url = getBaseUrl()
        url = f"{base_url}/api/start_recording/{channel_id}"
        payload = {"is_user_request": is_user_request}
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                data = {}
                try:
                    data = response.json() or {}
                except Exception:
                    pass
                # 1) 응답 내 상태를 즉시 반영
                st  = data.get("state") or "녹화 중"
                dur = data.get("recording_duration")
                QTimer.singleShot(0, lambda cid=channel_id, s=st, d=dur: self._applyImmediateState(cid, s, d))
                # 2) 폴링 재동기화 예약
                self.loop.call_soon_threadsafe(self.refreshStatusIndicators)
            else:
                raise Exception(response.json().get("detail", "알 수 없는 오류"))
        except Exception as e:
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "녹화 시작 오류",
                                                             f"{channel_id} 녹화 시작 실패: {e}"))


    def stopRecordingInBackground(self, channel_id: str, is_user_request: bool = True):
        base_url = getBaseUrl()
        url = f"{base_url}/api/stop_recording/{channel_id}"
        payload = {"is_user_request": is_user_request}
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                data = {}
                try:
                    data = response.json() or {}
                except Exception:
                    pass
                st  = data.get("state") or "대기 중"
                dur = data.get("recording_duration")
                QTimer.singleShot(0, lambda cid=channel_id, s=st, d=dur: self._applyImmediateState(cid, s, d))
                self.loop.call_soon_threadsafe(self.refreshStatusIndicators)
            else:
                raise Exception(response.json().get("detail", "알 수 없는 오류"))
        except Exception as e:
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "녹화 중지 오류",
                                                             f"{channel_id} 녹화 중지 실패: {e}"))



if __name__ == "__main__":
    import qasync
    app = QApplication(sys.argv)

    app.setStyleSheet("""
    /* =========================================================
         배경   : #f6f7fb
         액센트 : #eef2ff
         핸들   : #c7d2fe
         테두리 : #e5eaf3
         텍스트 : #111827
    ========================================================= */

    /* 전체 배경/텍스트 */
    QMainWindow, QDialog { background: #f6f7fb; color: #111827; }

    /* 상단 툴바 */
    QToolBar { background: transparent; border: none; padding: 8px 12px; }
    QToolBar QPushButton {
      background: #ffffff; color: #111827;
      border-top: 1px solid transparent; border-bottom: 1px solid transparent;
      border-left: 1px solid #e5eaf3; border-right: 1px solid #e5eaf3;
      border-radius: 0; padding: 8px 12px;
    }
    QToolBar QPushButton:hover  { background: #eef2ff; }
    QToolBar QPushButton:pressed{ background: #dbe4ff; }

    /* 툴바 검색창 */
    QToolBar QLineEdit {
      background: #ffffff; color: #111827;
      border: 1px solid #e5eaf3; border-radius: 0; padding: 6px 8px;
    }
    QToolBar QLineEdit:hover { border-color: #d6deeb; }
    QToolBar QLineEdit:focus { border-color: #c7d2fe; }

    /* 공통 입력 위젯 */
    QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox {
      background: #ffffff; color: #111827;
      border: 1px solid #e5eaf3; border-radius: 0; padding: 6px 8px;
    }
    QTextBrowser {
      background: #ffffff; color: #111827;
      border: 1px solid #e5eaf3; border-radius: 0; padding: 8px 10px;
    }
    QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover, QSpinBox:hover { border-color: #d6deff; }
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus   { border-color: #c7d2fe; }

    /* ScrollArea 기본값 */
    QAbstractScrollArea { background: transparent; border: none; }
    QAbstractScrollArea::viewport { background: #f6f7fb; }

    /* 설정관리창 스크롤영역/컨테이너를 #f9f9f9으로 강제 */
    QScrollArea#SettingsArea { background: transparent; border: none; }
    QScrollArea#SettingsArea::viewport { background: #f9f9f9; }
    #SettingsBody { background: #f9f9f9; }

    /* 표/텍스트브라우저 흰색 */
    QTableView, QTableWidget, QTableView::viewport, QTableWidget::viewport { background: #ffffff; }
    QTextBrowser, QTextBrowser::viewport { background: #ffffff; }

    /* 전역 콤보박스 (라운딩 0) */
    QComboBox {
      background: #ffffff; color: #111827;
      border: 1px solid #e5eaf3; border-radius: 0; padding: 6px 8px 6px 10px;
    }
    QComboBox:hover { border-color: #d6deff; }
    QComboBox:focus { border-color: #c7d2fe; }

    /* 드롭다운 버튼 영역을 스크롤바와 동일 톤 */
    QComboBox::drop-down {
      width: 30px; subcontrol-origin: padding; subcontrol-position: top right;
      border-left: 1px solid #e5eaf3; background: #eef2ff;
      border-top-right-radius: 0; border-bottom-right-radius: 0;
    }
    /* 화살표 스크롤 핸들보다 살짝 진하게 */
    QComboBox::down-arrow {
      width: 0; height: 0; margin-right: 9px;
      border-left: 6px solid transparent; border-right: 6px solid transparent;
      border-top: 7px solid #a5b4fc;
    }
    QComboBox::down-arrow:on { border-top-color: #7c89e5; }

    /* 드롭다운 리스트(팝업) */
    QComboBox QAbstractItemView {
      background: #ffffff; color: #111827;
      border: 1px solid #e5eaf3; border-radius: 0;
      selection-background-color: #e0e7ff; selection-color: #111827;
      outline: 0;
    }
    QComboBox QAbstractItemView::item { padding: 6px 10px; }

    /* 시스템 모니터 */
    #SystemDashboard { background: transparent; }
    #SystemDashboardHeader { background: transparent; }
    #SystemDashboardBrand {
      font-size: 16px;
      font-weight: 800;
      color: #111827;
    }
    #SystemDashboardTitle {
      font-size: 12px;
      font-weight: 700;
      color: #475569;
    }
    QPushButton#VersionButton {
      min-height: 24px;
      max-height: 24px;
      padding: 0 9px;
      border: 1px solid #d7ddea;
      border-radius: 12px;
      background: #ffffff;
      color: #4f46e5;
      font-size: 11px;
      font-weight: 800;
    }
    QPushButton#VersionButton:hover { background: #eef2ff; }
    QPushButton#SystemMonitorToggle {
      min-height: 26px;
      max-height: 26px;
      padding: 0 8px;
      border: 1px solid #d7ddea;
      border-radius: 5px;
      background: #ffffff;
      color: #475569;
      font-size: 11px;
      font-weight: 700;
    }
    QPushButton#SystemMonitorToggle:hover { background: #f3f5ff; }
    QFrame#MetricCard {
      background: #ffffff;
      border: 1px solid #dfe4ee;
      border-left: 3px solid #6366f1;
      border-radius: 6px;
    }
    QFrame#MetricCard[level="warning"] { border-left-color: #f59e0b; }
    QFrame#MetricCard[level="danger"]  { border-left-color: #ef4444; }
    #CardTitle { font-size: 12px; font-weight: 700; color: #334155; }
    #CardValue { font-size: 17px; font-weight: 800; color: #111827; }
    #CardSub   { font-size: 11px; color: #64748b; }
    QProgressBar#MetricBar {
      min-height: 6px;
      max-height: 6px;
      background: #eef2f7;
      border: none;
      border-radius: 3px;
    }
    QProgressBar#MetricBar::chunk {
      background: #6366f1;
      border-radius: 3px;
    }


    /* 진행바 (라운딩 0) */
    QProgressBar {
      background: #eef2ff; border: 1px solid #e5eaf3; border-radius: 0;
      text-align: center; color: #475569;
    }
    QProgressBar::chunk { background: #c7d2fe; border-radius: 0; }

    /* 채널 리스트 테이블 */
    QHeaderView::section {
      background: #f3f4f6; color: #334155; border: 1px solid #e5eaf3;
      padding: 6px 8px; font-weight: 600; border-radius: 0;
    }
    QTableWidget {
      background: #ffffff; 
      color: #111827;
      alternate-background-color: #fafafa;
      gridline-color: #e5eaf3; 
      selection-background-color: #e0e7ff; 
      selection-color: #111827;
    }
    /* 아이템 텍스트에도 적용 */
    QTableWidget::item { color: #111827; }

    QTableWidget::item:hover { background: #f5f7ff; }


    /* 전역 스크롤바 */
    QScrollBar:vertical {
      background: #eef2ff; width: 12px; border: 1px solid #e5eaf3; border-radius: 6px; margin: 0;
    }
    QScrollBar::handle:vertical {
      background: #c7d2fe; min-height: 24px; border-radius: 6px;
    }
    QScrollBar::handle:vertical:hover { background: #b9c6ff; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

    QScrollBar:horizontal {
      background: #eef2ff; height: 12px; border: 1px solid #e5eaf3; border-radius: 6px; margin: 0;
    }
    QScrollBar::handle:horizontal {
      background: #c7d2fe; min-width: 24px; border-radius: 6px;
    }
    QScrollBar::handle:horizontal:hover { background: #b9c6ff; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

    /* 버튼만 최소 라운딩(4px) */
    QPushButton {
      background: #ffffff; color: #111827;
      border: 1px solid #e5eaf3; border-radius: 4px; padding: 8px 14px;
    }
    QPushButton:hover  { background: #f3f6ff; border-color: #d6deff; }
    QPushButton:pressed{ background: #e7eeff; }
    QPushButton:disabled { background: #f3f4f6; color: #9ca3af; border-color: #e5eaf3; }

    /* 버튼 기본형 primary */
    QPushButton[variant="primary"] {
      background: #ffffff; border-color: #a5b4fc; color: #111827;
    }
    QPushButton[variant="primary"]:hover  { background: #f3f6ff; }
    QPushButton[variant="primary"]:pressed{ background: #e7eeff; }

    /* 보통 버튼 */
    QPushButton[variant="secondary"] {
      background: #ffffff; border-color: #e5eaf3; color: #111827;
    }

    /* 매우 옅은 레드(삭제 전용) */
    QPushButton[variant="danger"] {
      background: #fff5f5; border-color: #ffe4e6; color: #7f1d1d;
    }
    QPushButton[variant="danger"]:hover  { background: #ffecec; }
    QPushButton[variant="danger"]:pressed{ background: #ffdede; }

    /* 채널 액션 버튼 전용 (라운딩 0) */
    QPushButton[context="rowaction"] {
      background: #ffffff; color: #111827;
      border: 1px solid #e5eaf3; border-radius: 0; padding: 6px 10px;
    }
    QPushButton[context="rowaction"]:hover  { background: #f3f6ff; }
    QPushButton[context="rowaction"]:pressed{ background: #e7eeff; }

    /* 행 액션 + 삭제(옅은 레드), 비활성화 별도 정의 */
    QPushButton[context="rowaction"][variant="danger"] {
      background: #fff5f5; border-color: #ffe4e6; color: #7f1d1d;
    }
    QPushButton[context="rowaction"][variant="danger"]:hover  { background: #ffecec; }
    QPushButton[context="rowaction"][variant="danger"]:pressed{ background: #ffdede; }
    QPushButton[context="rowaction"]:disabled {
      background: #f3f4f6; color: #c0c4ce; border-color: #e5eaf3;
    }

    /* 탭(QTabWidget/QTabBar) — 상단 정리*/
    QTabWidget { background: transparent; }
    QTabWidget::tab-bar { alignment: left; }
    QTabWidget::pane {
      background: transparent;
      border: none;         
      border-radius: 0;
      top: 0;
    }
    QTabBar { background: transparent; }
    QTabBar::tab {
      background: #eef2ff; color: #374151;
      border: 1px solid #e5eaf3; border-bottom: none;
      padding: 8px 12px; margin-right: 6px; border-radius: 0;
    }
    QTabBar::tab:selected {
      background: #ffffff; color: #111827; border-color: #e5eaf3;
    }
    QTabBar::tab:!selected:hover { background: #f3f6ff; }

    /* 도움말 라벨 */
    #InfoHint { color: #64748b; font-size: 12px; }
    """)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()

    cfg = loadConfig() or {}
    _tray_on_start = bool(cfg.get("enableTray", False) and cfg.get("minimizeToTrayOnStart", False))
    if not _tray_on_start:
        window.show()

    print("[DEBUG] 이벤트 루프 시작.")
    try:
        with loop:
            loop.run_forever()
    finally:
        print("[DEBUG] 이벤트 루프 종료됨.")
        print("[DEBUG] 애플리케이션 종료.")
