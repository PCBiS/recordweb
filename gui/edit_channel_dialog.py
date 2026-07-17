# FILE_VERSION : FSM 251229_1

import requests

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLabel, QLineEdit, QComboBox,
    QHBoxLayout, QPushButton, QFileDialog, QMessageBox
)

from module.data_manager import getBaseUrl

CHZZK_QUALITIES = ["best", "1080p", "720p", "480p", "360p"]
CIME_QUALITIES = ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "worst"]

class EditChannelDialog(QDialog):
    def __init__(self, channel_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("채널 수정")
        self.setModal(True)
        self.resize(400, 400)  # 구버전 사이즈 유지
        self.channel_data = channel_data.copy()
        self._updated_data = None

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

        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        # (1) 플랫폼
        self.platform_combo = QComboBox()
        self.platform_combo.addItem("치지직", "chzzk")
        self.platform_combo.addItem("씨미", "cime")

        platform_val = self.channel_data.get("platform", "chzzk")

        # 기존 유튜브 채널 데이터가 남아있는 경우 씨미로 보정
        if platform_val == "youtube":
            platform_val = "cime"

        idx = self.platform_combo.findData(platform_val)
        if idx >= 0:
            self.platform_combo.setCurrentIndex(idx)
        form_layout.addRow("플랫폼:", self.platform_combo)

        # (2) 채널명
        self.name_edit = QLineEdit(self.channel_data.get("name", ""))
        form_layout.addRow("채널명:", self.name_edit)

        # (3) 저장 폴더
        self.output_dir_edit = QLineEdit(self.channel_data.get("output_dir", ""))
        browse_btn = QPushButton("찾아보기...")
        browse_btn.setProperty("variant", "secondary")
        browse_btn.clicked.connect(self.browseFolder)
        hbox_dir = QHBoxLayout()
        hbox_dir.addWidget(self.output_dir_edit)
        hbox_dir.addWidget(browse_btn)
        form_layout.addRow("저장 폴더:", hbox_dir)

        # (4) 화질
        self.quality_combo = QComboBox()
        form_layout.addRow("화질:", self.quality_combo)
        self.quality_info = QLabel("최고 품질(일반적으로 1080p, 정규 해상도가 아닌 경우 'best' 선택)")
        self.quality_info.setObjectName("InfoHint")
        form_layout.addRow("", self.quality_info)

        # (5) 파일 확장자
        self.ext_combo = QComboBox()
        form_layout.addRow("파일 확장자:", self.ext_combo)
        self.ext_info = QLabel("치지직: ts 또는 mp4 선택 / 씨미: mp4 고정")
        self.ext_info.setObjectName("InfoHint")
        form_layout.addRow("", self.ext_info)

        # (6) 반복 녹화 ON/OFF
        self.autoRec_combo = QComboBox()
        self.autoRec_combo.addItem("예", True)
        self.autoRec_combo.addItem("아니오", False)
        current_auto = bool(self.channel_data.get("record_enabled", True))
        self.autoRec_combo.setCurrentIndex(0 if current_auto else 1)
        self.autoRec_info = QLabel("OFF시 일회성 녹화만 진행되며 자동녹화/모두녹화에서 제외됩니다.")
        self.autoRec_info.setObjectName("InfoHint")
        form_layout.addRow("반복 녹화:", self.autoRec_combo)
        form_layout.addRow("", self.autoRec_info)

        # (7) 같이보기만 녹화
        self.watchParty_combo = QComboBox()
        self.watchParty_combo.addItem("예", True)
        self.watchParty_combo.addItem("아니오", False)
        current_watch = bool(self.channel_data.get("recordWatchParty", False))
        self.watchParty_combo.setCurrentIndex(0 if current_watch else 1)
        self.watchParty_info = QLabel("치지직의 같이보기 컨텐츠만 녹화합니다. 씨미 사용불가")
        self.watchParty_info.setObjectName("InfoHint")
        form_layout.addRow("같이보기만 녹화:", self.watchParty_combo)
        form_layout.addRow("", self.watchParty_info)

        # (8) 녹화 제외할 태그
        self.watchPartyExclude_edit = QLineEdit(", ".join(self.channel_data.get("watchPartyExcludeTags") or []))
        self.watchPartyExclude_edit.setPlaceholderText("예: LCK, VCT")
        self.watchPartyExclude_info = QLabel("같이보기만 녹화 사용시 녹화 제외할 태그를 입력해주세요.\n2개 이상은 쉼표로 구분할 수 있습니다")
        self.watchPartyExclude_info.setObjectName("InfoHint")
        form_layout.addRow("녹화 제외할 태그:", self.watchPartyExclude_edit)
        form_layout.addRow("", self.watchPartyExclude_info) 

        # 같이보기만 녹화 콤보 변경 시 현재 플랫폼 기준으로 토글 재적용
        self.watchParty_combo.currentIndexChanged.connect(lambda _i: self.onPlatformChanged())
        self.platform_combo.currentIndexChanged.connect(lambda _i: self.onPlatformChanged())

        layout.addLayout(form_layout)

        # 버튼 영역
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("저장")
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
        new_platform = self.platform_combo.currentData() or "chzzk"

        # 1) 플랫폼별 화질 목록 적용
        self.quality_combo.clear()
        if new_platform == "chzzk":
            self.quality_combo.addItems(CHZZK_QUALITIES)
        else:
            self.quality_combo.addItems(CIME_QUALITIES)

        old_quality = self.channel_data.get("quality", "best")
        i = self.quality_combo.findText(old_quality)
        self.quality_combo.setCurrentIndex(i if i >= 0 else 0)

        # 2) 플랫폼별 확장자 목록 적용
        self.ext_combo.clear()
        if new_platform == "chzzk":
            self.ext_combo.addItems([".ts", ".mp4"])
            self.ext_combo.setEnabled(True)

            old_ext = self.channel_data.get("extension", ".ts")
            j = self.ext_combo.findText(old_ext)
            self.ext_combo.setCurrentIndex(j if j >= 0 else 0)
        else:
            # 씨미는 mp4 고정
            self.ext_combo.addItem(".mp4")
            self.ext_combo.setEnabled(False)
            self.ext_combo.setCurrentIndex(0)

        # 3) 같이보기/제외태그 토글 + 안내 라벨/툴팁 동기화
        if new_platform == "cime":
            # 씨미는 같이보기 불가 → 전부 비활성
            self.watchParty_combo.setCurrentIndex(1)  # '아니오'
            self.watchParty_combo.setEnabled(False)
            self.watchPartyExclude_edit.setEnabled(False)

            if hasattr(self, "watchPartyExclude_info"):
                self.watchPartyExclude_info.setEnabled(False)
            if hasattr(self, "watchParty_info"):
                self.watchParty_info.setEnabled(False)

            # 왜 비활성인지 툴팁 제공
            self.watchParty_combo.setToolTip("씨미는 같이보기 옵션을 지원하지 않습니다.")
            self.watchPartyExclude_edit.setToolTip("씨미 플랫폼에서는 사용하지 않습니다.")
        else:
            # 치지직: 같이보기 콤보 활성 + 현재 선택에 따라 '제외태그' on/off
            self.watchParty_combo.setEnabled(True)
            wp_on = bool(self.watchParty_combo.currentData())
            self.watchPartyExclude_edit.setEnabled(wp_on)

            if hasattr(self, "watchPartyExclude_info"):
                self.watchPartyExclude_info.setEnabled(wp_on)
            if hasattr(self, "watchParty_info"):
                self.watchParty_info.setEnabled(True)

            # 툴팁 정리
            self.watchParty_combo.setToolTip("")
            self.watchPartyExclude_edit.setToolTip("같이보기 '예'일 때만 입력 가능합니다.")


    def browseFolder(self):
        path = QFileDialog.getExistingDirectory(self, "저장 폴더 선택")
        if path:
            self.output_dir_edit.setText(path)

    def getUpdatedData(self):
        return self._updated_data

    def saveAndClose(self):
        plat = self.platform_combo.currentData() or "chzzk"
        raw_tags = self.watchPartyExclude_edit.text().strip()
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        extension = self.ext_combo.currentText()

        # 씨미는 mp4 고정
        if plat == "cime":
            extension = ".mp4"
            tags = []

        new_data = {
            "platform": plat,
            "name": self.name_edit.text().strip(),
            "output_dir": self.output_dir_edit.text().strip(),
            "quality": self.quality_combo.currentText(),
            "extension": extension,
            "record_enabled": self.autoRec_combo.currentData(),
            "recordWatchParty": self.watchParty_combo.currentData() if plat == "chzzk" else False,
            "watchPartyExcludeTags": tags if plat == "chzzk" else [],
        }
        # 입력 검증(구버전 UX 유지)
        if not new_data["name"]:
            QMessageBox.warning(self, "입력 오류", "채널명을 입력하세요.")
            return
        if not new_data["output_dir"]:
            QMessageBox.warning(self, "입력 오류", "저장 폴더를 선택하세요.")
            return

        try:
            channel_id = self.channel_data.get("id")
            base_url = getBaseUrl()
            url = f"{base_url}/api/channels/{channel_id}"
            response = requests.put(url, json=new_data, timeout=10)
            if response.status_code == 200:
                self._updated_data = new_data
                QMessageBox.information(self, "채널 수정 완료", "채널이 성공적으로 수정되었습니다.")
                self.accept()
            else:
                # 서버가 메시지를 주면 그대로 표시
                msg = ""
                try:
                    msg = response.json().get("detail")
                except Exception:
                    msg = response.text
                raise RuntimeError(msg or f"HTTP {response.status_code}")
        except Exception as e:
            QMessageBox.warning(self, "오류", f"채널 수정 중 오류 발생: {e}")
