# FILE_VERSION : FSM 251229_1

import os
import re
import requests

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLabel, QLineEdit, QComboBox,
    QHBoxLayout, QPushButton, QFileDialog, QMessageBox
)
from module.data_manager import getBaseUrl

CHZZK_QUALITIES = ["best", "1080p", "720p", "480p", "360p"]
CIME_QUALITIES = ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "worst"]

def _sanitizeChannelId(platform: str, raw: str) -> str:
    s = (raw or "").strip()
    if platform == "cime":
        s = s.replace("https://ci.me/", "").replace("http://ci.me/", "")
        s = s.replace("/live", "").strip("/")
        s = s.lstrip("@").strip()
        s = re.sub(r'\s+', '', s)
    else:
        s = re.sub(r'\s+', '', s)
    return s

class AddChannelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("채널 추가")
        self.setModal(True)
        self.resize(400, 420)

        # 비활성화 위젯 더 어둡게 변환
        self.setStyleSheet("""
            QLineEdit:disabled, QComboBox:disabled {
                background: #f2f2f2;
                color: #888;
            }
            QLabel#InfoHint:disabled {
                color: #9a9a9a;
            }
        """)

        self._created_data = None

        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        # (1) 플랫폼
        self.platform_combo = QComboBox()
        self.platform_combo.addItem("치지직", "chzzk")
        self.platform_combo.addItem("씨미", "cime")
        form_layout.addRow("플랫폼:", self.platform_combo)

        # (2) 채널 ID
        self.id_edit = QLineEdit()
        form_layout.addRow("채널 ID:", self.id_edit)
        self.channel_info = QLabel("치지직은 chzzk.naver.com/live/UID 에서 UID만 입력하세요\n씨미는 ci.me/@abc/live에서 @를 제외한 abc만 입력하세요")
        self.channel_info.setObjectName("InfoHint")
        form_layout.addRow("", self.channel_info)

        # (3) 채널명
        self.name_edit = QLineEdit()
        form_layout.addRow("채널명:", self.name_edit)    

        # (4) 저장 폴더
        self.output_dir_edit = QLineEdit()
        browse_btn = QPushButton("찾아보기...")
        browse_btn.setProperty("variant", "secondary") 
        browse_btn.clicked.connect(self.browseFolder)
        hbox_dir = QHBoxLayout()
        hbox_dir.addWidget(self.output_dir_edit)
        hbox_dir.addWidget(browse_btn)
        form_layout.addRow("저장 폴더:", hbox_dir)

        # (5) 화질
        self.quality_combo = QComboBox()
        form_layout.addRow("화질:", self.quality_combo)
        self.quality_info = QLabel("최고 품질(일반적으로 1080p, 정규 해상도가 아닌 경우 'best' 선택)")
        self.quality_info.setObjectName("InfoHint")
        form_layout.addRow("", self.quality_info)

        # (6) 파일 확장자
        self.ext_combo = QComboBox()
        form_layout.addRow("파일 확장자:", self.ext_combo)
        self.ext_info = QLabel("치지직: ts 또는 mp4 선택 / 씨미: mp4 고정")
        self.ext_info.setObjectName("InfoHint")
        form_layout.addRow("", self.ext_info)

        # (7) 반복 녹화 ON/OFF
        self.autoRec_combo = QComboBox()
        self.autoRec_combo.addItem("ON", True)
        self.autoRec_combo.addItem("OFF", False)
        self.autoRec_combo.setCurrentIndex(0)
        self.autoRec_info = QLabel("OFF시 일회성 녹화만 진행되며 자동녹화/모두녹화에서 제외됩니다.")
        self.autoRec_info.setObjectName("InfoHint")
        form_layout.addRow("반복 녹화:", self.autoRec_combo)
        form_layout.addRow("", self.autoRec_info)

        # (8) 같이보기만 녹화
        self.watchParty_combo = QComboBox()
        self.watchParty_combo.addItem("예", True)
        self.watchParty_combo.addItem("아니오", False)
        self.watchParty_combo.setCurrentIndex(1)
        self.watchParty_info = QLabel("치지직의 같이보기 컨텐츠만 녹화합니다. 씨미 사용불가")
        self.watchParty_info.setObjectName("InfoHint")
        form_layout.addRow("같이보기만 녹화:", self.watchParty_combo)
        form_layout.addRow("", self.watchParty_info)

        # (9) 녹화 제외할 태그
        self.watchPartyExclude_edit = QLineEdit()
        self.watchPartyExclude_edit.setPlaceholderText("예: LCK, VCT")
        self.watchPartyExclude_info = QLabel("같이보기만 녹화 사용시 녹화 제외할 태그를 입력해주세요.\n2개 이상은 쉼표로 구분할 수 있습니다")
        self.watchPartyExclude_info.setObjectName("InfoHint")
        form_layout.addRow("녹화 제외할 태그:", self.watchPartyExclude_edit)
        form_layout.addRow("", self.watchPartyExclude_info) 

        # 같이보기만 녹화 콤보가 바뀌면 현재 플랫폼 기준으로 토글 재적용
        self.watchParty_combo.currentIndexChanged.connect(lambda _i: self.onPlatformChanged())

        # 플랫폼 콤보도 바뀌면 바로 반영
        self.platform_combo.currentIndexChanged.connect(lambda _i: self.onPlatformChanged())

        layout.addLayout(form_layout)

        # 버튼 영역
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("추가")
        save_btn.setFixedHeight(40)
        save_btn.setProperty("variant", "primary")

        cancel_btn = QPushButton("취소")
        cancel_btn.setFixedHeight(40)
        cancel_btn.setProperty("variant", "secondary") 

        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        # 초기 상태 1회만 적용
        self.onPlatformChanged()

        save_btn.clicked.connect(self.saveAndClose)
        cancel_btn.clicked.connect(self.close)
        self.setLayout(layout)

    def onPlatformChanged(self):
        plat = self.platform_combo.currentData() or "chzzk"

        # 1) 화질: 기존 선택 유지 시도, 없으면 0번
        prev_quality = self.quality_combo.currentText()
        self.quality_combo.clear()
        if plat == "chzzk":
            self.quality_combo.addItems(CHZZK_QUALITIES)
        else:
            self.quality_combo.addItems(CIME_QUALITIES)
        qi = self.quality_combo.findText(prev_quality)
        self.quality_combo.setCurrentIndex(qi if qi >= 0 else 0)

        # 2) 확장자: 기존 선택 유지 시도, 씨미는 .mp4 고정
        prev_ext = self.ext_combo.currentText()
        self.ext_combo.clear()
        if plat == "chzzk":
            self.ext_combo.addItems([".ts", ".mp4"])
            self.ext_combo.setEnabled(True)
            ej = self.ext_combo.findText(prev_ext)
            self.ext_combo.setCurrentIndex(ej if ej >= 0 else 0)
        else:
            self.ext_combo.addItem(".mp4")
            self.ext_combo.setEnabled(False)  # 씨미는 확장자 고정

        # 3) 같이보기/제외태그 토글 + 안내 라벨/툴팁
        if plat == "cime":
            # 씨미는 같이보기 불가 → 전부 비활성화
            self.watchParty_combo.setCurrentIndex(1)  # '아니오'
            self.watchParty_combo.setEnabled(False)
            self.watchPartyExclude_edit.setEnabled(False)
            if hasattr(self, "watchPartyExclude_info"):
                self.watchPartyExclude_info.setEnabled(False)
            if hasattr(self, "watchParty_info"):
                self.watchParty_info.setEnabled(False)
            # 왜 비활성인지 즉시 안내
            self.watchParty_combo.setToolTip("씨미는 같이보기 옵션을 지원하지 않습니다.")
            self.watchPartyExclude_edit.setToolTip("씨미 플랫폼에서는 사용하지 않습니다.")
        else:
            # CHZZK: '같이보기만 녹화' 콤보는 활성, 제외태그는 콤보 선택에 따라 토글
            self.watchParty_combo.setEnabled(True)
            wp_on = bool(self.watchParty_combo.currentData())
            self.watchPartyExclude_edit.setEnabled(wp_on)
            if hasattr(self, "watchPartyExclude_info"):
                self.watchPartyExclude_info.setEnabled(wp_on)
            if hasattr(self, "watchParty_info"):
                self.watchParty_info.setEnabled(True)
            # 헷갈리지 않게 툴팁 정리
            self.watchParty_combo.setToolTip("")
            self.watchPartyExclude_edit.setToolTip("같이보기 '예'일 때만 입력 가능합니다.")


    def browseFolder(self):
        path = QFileDialog.getExistingDirectory(self, "저장 폴더 선택")
        if path:
            self.output_dir_edit.setText(path)

    def getCreatedData(self):
        return self._created_data

    def saveAndClose(self):
        plat = self.platform_combo.currentData() or "chzzk"
        cid  = _sanitizeChannelId(plat, self.id_edit.text())
        name = self.name_edit.text().strip()
        out  = self.output_dir_edit.text().strip()
        qual = self.quality_combo.currentText()
        ext  = self.ext_combo.currentText()
        auto = bool(self.autoRec_combo.currentData())

        rwp = bool(self.watchParty_combo.currentData()) if plat == "chzzk" else False

        raw_tags = self.watchPartyExclude_edit.text().strip()
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        # 씨미는 mp4 고정 + 같이보기/제외태그 미사용
        if plat == "cime":
            ext = ".mp4"
            rwp = False
            tags = []

        if not cid:
            QMessageBox.warning(self, "입력 오류", "채널 ID를 입력하세요.")
            return
        if not name:
            QMessageBox.warning(self, "입력 오류", "채널명을 입력하세요.")
            return
        if not out:
            QMessageBox.warning(self, "입력 오류", "저장 폴더를 선택하세요.")
            return

        payload = {
            "platform": plat,
            "id": cid,
            "name": name,
            "output_dir": out,
            "quality": qual,
            "extension": ext,
            "record_enabled": auto,
            "recordWatchParty": rwp,
            "watchPartyExcludeTags": tags,
        }

        try:
            base = getBaseUrl()
            url = f"{base}/api/channels"
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                self._created_data = payload
                QMessageBox.information(self, "채널 추가 완료", "채널이 성공적으로 추가되었습니다.")
                self.accept()
            else:
                msg = ""
                try:
                    msg = r.json().get("detail")
                except Exception:
                    msg = r.text
                raise RuntimeError(msg or f"HTTP {r.status_code}")
        except Exception as e:
            QMessageBox.warning(self, "오류", f"채널 추가 중 오류 발생: {e}")
