import json
import requests
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QScrollArea, QWidget, QLabel, QLineEdit, QComboBox, QPushButton, 
                             QHBoxLayout, QFileDialog, QCheckBox, QMessageBox, QFrame)
from PyQt6.QtGui import QPainter, QPen, QColor
from PyQt6.QtCore import Qt

from module.data_manager import (
    toBool, loadConfig,
    loadNotification, saveNotification, getBaseUrl,
    loadChannels
)


class DashedLine(QWidget):
    def __init__(self, color="#cccccc", parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedHeight(2)

    def paintEvent(self, _):
        p = QPainter(self)
        pen = QPen(self._color)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidth(1)
        p.setPen(pen)
        y = self.height() // 2
        p.drawLine(0, y, self.width(), y)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정 관리")
        self.resize(800, 800)

        # 1) config, notification 로드
        self.config = loadConfig()
        self.notification_data = loadNotification()

        # 2) 채널 분배 UI 구성을 위해 channels 로드
        self.channels = loadChannels() or []
        self.gpuAssignCombos = {}  

        # 3) 메인 레이아웃 (QVBoxLayout)
        mainLayout = QVBoxLayout(self)
        self.setLayout(mainLayout)

        # 4) 스크롤 영역 + container
        scrollArea = QScrollArea()
        scrollArea.setObjectName("SettingsArea")          
        scrollArea.setWidgetResizable(True)
        scrollArea.setFrameShape(QFrame.Shape.NoFrame)    

        container = QWidget()
        container.setObjectName("SettingsBody")     

        scrollArea.setWidget(container)
        mainLayout.addWidget(scrollArea)

        # 5) containerLayout 내부에 모든 위젯을 배치
        containerLayout = QVBoxLayout(container)
        containerLayout.setSpacing(30)
        containerLayout.setContentsMargins(20, 20, 20, 20)

        # 자동녹화 모드
        containerLayout.addWidget(self.htmlLabel("<h2>자동녹화 모드 사용 [치지직/씨미]</h2>"))
        self.autoRecCombo = self.buildOnOffCombo(self.config.get("autoRecordingMode", False))
        containerLayout.addWidget(self.autoRecCombo)
        containerLayout.addWidget(self.htmlLabel(
        """<div style="text-align:center;">
        <p class="description" style="margin-top:5px;">
        자동녹화 모드가 활성화되면 프로그램 시작 시 모든 채널의 녹화를 자동으로 시작합니다.<br>
        <strong>(프로그램 재시작 필요)</strong>
        </p>
        </div>""".strip()))

        # 시스템 트레이 옵션
        containerLayout.addWidget(self.htmlLabel("<h2>최소화 시스템 트레이</h2>"))

        self.chkEnableTray   = QCheckBox("최소화 시스템 트레이 사용")
        self.chkTrayOnClose  = QCheckBox("GUI창 닫기(X) 시 트레이로 최소화")
        self.chkTrayOnStart  = QCheckBox("시작 시 트레이로 최소화")

        cfg = self.config or {}
        self.chkEnableTray.setChecked(bool(cfg.get("enableTray", False)))
        self.chkTrayOnClose.setChecked(bool(cfg.get("minimizeToTrayOnClose", False)))
        self.chkTrayOnStart.setChecked(bool(cfg.get("minimizeToTrayOnStart", False)))

        def _toggle_tray_dependents():
            on = self.chkEnableTray.isChecked()
            self.chkTrayOnClose.setEnabled(on)
            self.chkTrayOnStart.setEnabled(on)

        self.chkEnableTray.stateChanged.connect(_toggle_tray_dependents)
        _toggle_tray_dependents()

        col = QVBoxLayout()
        col.setSpacing(8)
        col.setContentsMargins(0, 0, 0, 0)

        col.addWidget(self.chkEnableTray,  0, Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self.chkTrayOnClose, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self.chkTrayOnStart, 0, Qt.AlignmentFlag.AlignHCenter)

        row = QHBoxLayout()
        row.addStretch(1)     # 왼쪽 여백
        row.addLayout(col)    # 가운데
        row.addStretch(1)     # 오른쪽 여백

        containerLayout.addLayout(row)

        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
              <table style="border-collapse:collapse; margin-top:10px; margin-left:auto; margin-right:auto;">
                <thead>
                  <tr style="background-color:#f2f2f2; text-align:center;">
                    <th style="padding:10px; border:1px solid #ddd;">옵션</th>
                    <th style="padding:10px; border:1px solid #ddd;">설명</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td style="padding:10px; border:1px solid #ddd;">최소화 시스템 트레이 사용</td>
                    <td style="padding:10px; border:1px solid #ddd;">트레이 아이콘을 만들고 백그라운드 실행을 유지합니다.</td>
                  </tr>
                  <tr>
                    <td style="padding:10px; border:1px solid #ddd;">GUI창 닫기(X) 시 트레이로 최소화</td>
                    <td style="padding:10px; border:1px solid #ddd;">GUI창 X 버튼을 눌러도 종료 대신 숨기기만 하고 명령창은 최소화 됩니다.</td>
                  </tr>
                  <tr>
                    <td style="padding:10px; border:1px solid #ddd;">시작 시 트레이로 최소화</td>
                    <td style="padding:10px; border:1px solid #ddd;">실행 직후 창을 띄우지 않고 트레이로 보냅니다.</td>
                  </tr>
                </tbody>
              </table>
              <p class="description" style="margin-top:10px;">
                ※ 최소화 트레이 사용을 OFF하면 다른 두 옵션은 자동으로 비활성화됩니다.
              </p>
            </div>"""
        ))

        self.addDashedDivider(containerLayout) # 분류 점선

        # 플러그인 선택
        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <h2>플러그인 선택 [치지직]</h2>
            </div>"""
        ))

        self.pluginCombo = QComboBox()
        # addItem(표시 텍스트, 내부 데이터) 형태로 추가하면, 사용자에게는 한글이 보이지만 내부적으로는 영문값을 유지할 수 있습니다.
        self.pluginCombo.addItem("기본 플러그인", "basic")
        self.pluginCombo.addItem("타임머신 플러스 플러그인", "timemachine_plus")
        # 설정에 저장된 값에 맞게 선택
        current_plug = self.config.get("plugin_type", "basic")
        if current_plug not in ["basic", "timemachine_plus"]:
            current_plug = "basic"
        # 내부 데이터 기준으로 현재 아이템을 설정
        index = self.pluginCombo.findData(current_plug)
        if index >= 0:
            self.pluginCombo.setCurrentIndex(index)
        containerLayout.addWidget(self.pluginCombo)

        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <p class="description" style="margin-top:5px;">원하는 플러그인을 선택하세요.</p>
            <table style="border-collapse: collapse; margin-top: 10px; margin-left:auto; margin-right:auto;">
              <thead>
                <tr style="background-color: #f2f2f2; text-align: center;">
                  <th style="padding: 10px; border: 1px solid #ddd;">플러그인</th>
                  <th style="padding: 10px; border: 1px solid #ddd;">설명</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style="padding: 10px; border: 1px solid #ddd;">기본 플러그인</td>
                  <td style="padding: 10px; border: 1px solid #ddd;">타임머신 시프트 최대 6초 및 1회 녹화에 9시간 녹화 제한</td>
                </tr>
                <tr>
                  <td style="padding: 10px; border: 1px solid #ddd;">타임머신 플러스 플러그인</td>
                  <td style="padding: 10px; border: 1px solid #ddd;">타임머신 시프트 최대 1시간(3600초) 및<br>9시간이상 한 파일로 연속녹화가 가능
                  </td>
                </tr>
              </tbody>
            </table>
            <p></p>
            </div>""".strip()
        ))

        # 타임머신 녹화 시작 시점
        containerLayout.addWidget(self.htmlLabel("<h2>타임머신 플러그인 앞당김 시간(초) [치지직]</h2>"))
        self.timeShiftEdit = QLineEdit(str(self.config.get("timemachine_time_shift",600)))
        containerLayout.addWidget(self.timeShiftEdit)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        타임머신 플러스 사용시 현재 라이브 시간보다 n초 앞선<br>시점부터 녹화를 시작하도록 설정할 수 있습니다.<br>
        최대 3600초(1시간) 최소 0초 까지 설정가능
        </p>
        """.strip()))

        self.addDashedDivider(containerLayout) # 분류 점선

        # 자동 후처리 / 원본파일삭제 / fixed_ 접두사 제거
        containerLayout.addWidget(self.htmlLabel("<h2>녹화 완료 후 자동 후처리 [치지직 전용]</h2>"))
        self.autoPostCombo = self.buildOnOffCombo(self.config.get("autoPostProcessing", True))
        containerLayout.addWidget(self.autoPostCombo)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        치지직 녹화 완료 후 스트림복사 또는 열화인코딩 후처리를 실행할지 설정합니다.<br>
        씨미는 이 옵션을 사용하지 않고, 녹화 종료 후 내부적으로 <b>.part.ts → .mp4 자동 remux</b>를 수행합니다.
        </p>
        """.strip()))

        containerLayout.addWidget(self.htmlLabel("<h2>후처리 완료 후 원본파일 삭제 [치지직]</h2>"))
        self.deleteOrigCombo = self.buildOnOffCombo(self.config.get("deleteAfterPostProcessing", True))
        containerLayout.addWidget(self.deleteOrigCombo)
        containerLayout.addWidget(self.htmlLabel("<p class='description'>후처리 후 원본 파일을 삭제할지 여부를 설정합니다.</p>"))

        containerLayout.addWidget(self.htmlLabel("<h2>후처리 완료파일 fixed_ 접두사 제거 [치지직]</h2>"))
        self.removeFixedCombo = self.buildOnOffCombo(self.config.get("removeFixedPrefix", True))
        containerLayout.addWidget(self.removeFixedCombo)
        containerLayout.addWidget(self.htmlLabel("<p class='description'>후처리 후 'fixed_' 접두사를 제거할지 여부</p>"))

        containerLayout.addWidget(self.htmlLabel("<h2>녹화 완료 파일 자동이동 [치지직/씨미]</h2>"))
        self.moveAfterProcCombo = self.buildOnOffCombo(self.config.get("moveAfterProcessingEnabled", False))
        containerLayout.addWidget(self.moveAfterProcCombo)
        containerLayout.addWidget(self.htmlLabel(
            "<p class='description'>"
            "치지직은 후처리 완료 파일을, 씨미는 자동 remux 완료된 mp4 파일을 지정 경로로 이동합니다."
            "</p>"
        ))

        containerLayout.addWidget(self.htmlLabel("<h2>녹화 완료 파일 이동경로 [치지직/씨미]</h2>"))
        self.moveAfterEdit = QLineEdit(self.config.get("moveAfterProcessing",""))
        containerLayout.addWidget(self.moveAfterEdit)

        containerLayout.addWidget(self.htmlLabel("<h2>후처리 명령창을 항상 새창으로 실행 [치지직]</h2>"))
        self.postNewWinCombo = self.buildOnOffCombo(self.config.get("postNewWindow", False))
        containerLayout.addWidget(self.postNewWinCombo)
        containerLayout.addWidget(self.htmlLabel("<p class='description'>후처리 작업 명령창을 항상 메인창이 아닌 새창에서 실행하도록 합니다.</p>"))

        # recheckInterval / filenamePattern
        containerLayout.addWidget(self.htmlLabel("<h2>방송 재탐색 주기(초) [치지직/씨미]</h2>"))
        self.recheckEdit = QLineEdit(str(self.config.get("recheckInterval", 60)))
        containerLayout.addWidget(self.recheckEdit)
        containerLayout.addWidget(self.htmlLabel(
            "<p class='description'>"
            "등록된 채널의 방송이 시작되었는지 재탐색하는 간격을 설정합니다.<br>"
            "치지직 타임머신 사용 시에는 120초 이상을 권장합니다.<br>"
            "씨미도 같은 재탐색 주기를 사용하지만, 타임머신 기능은 적용되지 않습니다.<br>"
            "너무 짧으면 IP밴이나 채널 패싱이 발생할 수 있습니다."
            "</p>"
        ))

        containerLayout.addWidget(self.htmlLabel("<h2>파일명 생성규칙 [치지직/씨미]</h2>"))
        self.filenameEdit = QLineEdit(self.config.get("filenamePattern",
            "[{start_time}] {channel_name} {safe_live_title} {record_quality}{frame_rate}{file_extension}"))
        containerLayout.addWidget(self.filenameEdit)

        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <table style="border-collapse: collapse; margin-top: 10px; margin-left:auto; margin-right:auto;">
              <thead>
                <tr style="background-color: #f2f2f2;">
                  <th style="border: 1px solid #ddd; padding: 8px;">설정 값</th>
                  <th style="border: 1px solid #ddd; padding: 8px;">설명</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{recording_time}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 6px;">녹화 시작 날짜_시간 (예: 240801_183507)
                  </td>
                </tr>
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{broadcast_time}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 6px;">방송 시작 날짜_시간 (예: 240801_183507)
                  </td>
                </tr>
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{start_time}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 6px;">방송 시작 날짜 (예: 2024-08-01)
                  </td>
                </tr>
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{safe_live_title}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 6px;">방송 제목
                  </td>
                </tr>
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{channel_name}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 6px;">스트리머 채널명
                  </td>
                </tr>
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{record_quality}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 6px;">녹화 해상도
                  </td>
                </tr>
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{frame_rate}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 6px;">녹화 프레임
                  </td>
                </tr>
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{file_extension}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 6px;">파일 확장자
                  </td>
                </tr>
              </tbody>
            </table>
            </div>
            """.strip()
        ))

        # 분할녹화
        containerLayout.addWidget(self.htmlLabel("<h2>분할녹화 모드 사용 [치지직 전용]</h2>"))
        self.splitCombo = self.buildOnOffCombo(self.config.get("splitRecordingMode",False))
        containerLayout.addWidget(self.splitCombo)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        치지직 녹화파일을 지정한 시간 단위로 나누어 생성합니다.<br>
        3600초를 지정하면 녹화파일이 1시간씩 나누어 생성됩니다.<br>
        씨미는 현재 단일 .part.ts 임시파일을 mp4로 remux하는 구조라 이 옵션을 사용하지 않습니다.
        </p>
        """.strip()
        ))

        containerLayout.addWidget(self.htmlLabel("<h2>분할녹화 완료 후 자동 후처리 [치지직]</h2>"))
        self.splitPostCombo = self.buildOnOffCombo(self.config.get("splitPostProcessing", True))
        self.splitPostCombo.setEnabled(bool(self.config.get("splitRecordingMode", False)))
        containerLayout.addWidget(self.splitPostCombo)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        분할녹화 완료 후 자동 후속 후처리 여부를 설정합니다.
        </p>
        """.strip()
        ))

        containerLayout.addWidget(self.htmlLabel("<h2>분할녹화 시간 (초) [치지직]</h2>"))
        self.autoStopEdit = QLineEdit(str(self.config.get("autoStopInterval", 0)))
        self.autoStopEdit.setEnabled(bool(self.config.get("splitRecordingMode",False)))
        containerLayout.addWidget(self.autoStopEdit)
        containerLayout.addWidget(self.htmlLabel("<p class='description'>분할녹화 모드 사용시, 분할녹화 시간을 설정합니다.</p>"))

        containerLayout.addWidget(self.htmlLabel("<h2>분할 파일간 오버랩 시간(초) [치지직]</h2>"))
        self.overlapEdit = QLineEdit(str(self.config.get("splitOverlapSec", 0)))
        self.overlapEdit.setEnabled(bool(self.config.get("splitRecordingMode", False)))
        containerLayout.addWidget(self.overlapEdit)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
          분할녹화시 분할 파일 사이의 누락된 부분이 없도록 앞/뒤 영상에 겹치는 시간을 설정합니다.<br>
          권장 겹침시간 : 6 ~ 10초, 0 이면 겹침 없음.
        </p>
        """.strip()
        ))

        self.splitCombo.currentIndexChanged.connect(self.toggleSplitRecording)
        self.toggleSplitRecording()

        self.addDashedDivider(containerLayout) # 분류 점선

        # 인코딩에 사용할 GPU 수
        containerLayout.addWidget(self.htmlLabel("<h2>후처리 인코딩에 사용할 GPU 수 [치지직 전용]</h2>"))
        self.gpuCountCombo = QComboBox()
        self.gpuCountCombo.addItem("1 (단일 GPU)", 1)
        self.gpuCountCombo.addItem("2 (GPU0/GPU1 분리)", 2)

        try:
            gc = int((self.config.get("gpuCount", 1) or 1))
        except Exception:
            gc = 1
        self.gpuCountCombo.setCurrentIndex(1 if gc == 2 else 0)
        containerLayout.addWidget(self.gpuCountCombo)

        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <p class="description">
            치지직 열화인코딩 후처리에 사용할 GPU 프로필 수를 설정합니다.<br>
            GPU 수가 1이면 채널 분배 UI가 숨겨지고, 후처리 인코딩은 GPU0 프로필만 사용합니다.<br>
            GPU 수가 2이면 GPU1 프로필과 치지직 채널 분배 UI가 표시됩니다.<br>
            씨미는 원본 스트림을 저장한 뒤 mp4로 remux하므로 이 GPU 인코딩 설정을 사용하지 않습니다.
            </p>
            </div>""".strip()
        ))

        # 후처리 인코딩 or 스트림복사
        containerLayout.addWidget(self.htmlLabel("<h2>후처리 방식 선택 (스트림복사/열화인코딩) [치지직 전용]</h2>"))
        self.streamChoiceCombo = QComboBox()
        self.streamChoiceCombo.addItem("스트림복사", True)
        self.streamChoiceCombo.addItem("열화인코딩", False)
        if self.config.get("stream_copy", True):
            self.streamChoiceCombo.setCurrentIndex(0)
        else:
            self.streamChoiceCombo.setCurrentIndex(1)
        containerLayout.addWidget(self.streamChoiceCombo)        
        containerLayout.addWidget(self.htmlLabel(
            "<p class='description'>"
            "치지직 후처리 방식을 선택합니다. 스트림복사는 원본 유지, 열화인코딩은 용량 절약 목적입니다.<br>"
            "씨미는 이 설정을 사용하지 않고, 항상 원본 스트림을 복사한 뒤 mp4로 remux합니다."
            "</p>"
        ))

        # 인코딩 비디오 코덱 종류 [치지직]
        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <h2>인코딩 비디오 코덱 종류 [치지직]</h2>
            </div>"""
        ))

        self.videoCodecCombo = QComboBox()
        # display text / stored data
        codecOptions = [
            ("x264 CPU (libx264)", "libx264"),
            ("HEVC CPU (libx265)", "libx265"),
            ("H264 인텔GPU (h264_qsv)", "h264_qsv"),
            ("HEVC 인텔GPU (hevc_qsv)", "hevc_qsv"),
            ("H264 지포스GPU (h264_nvenc)", "h264_nvenc"),
            ("HEVC 지포스GPU (hevc_nvenc)", "hevc_nvenc"),
            ("H264 라데온 (h264_amf)", "h264_amf"),
            ("HEVC 라데온 (hevc_amf)", "hevc_amf"),
        ]
        current_codec_val = self.config.get("video_codec", "libx264")

        # 콤보박스에 (표시이름, 실제값)으로 아이템 추가
        selected_index = 0
        for i, (display_text, real_value) in enumerate(codecOptions):
            self.videoCodecCombo.addItem(display_text, real_value)
            if real_value == current_codec_val:
                selected_index = i
        self.videoCodecCombo.setCurrentIndex(selected_index)

        containerLayout.addWidget(self.videoCodecCombo)
        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <p class="description">
            사용할 비디오 코덱을 선택합니다. (표시는 사람에게 친절한 설명, 내부값은 FFmpeg용)
            </p>
            </div>""".strip()
        ))

        # 비디오 인코딩 프리셋 [치지직]
        containerLayout.addWidget(self.htmlLabel("<h2>비디오 인코딩 프리셋 [치지직]</h2>"))

        # 코덱별 허용 프리셋 목록 (드롭다운)
        self.codec_presets = {
            "libx264":  ["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow","placebo"],
            "libx265":  ["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow","placebo"],
            "h264_qsv": ["veryfast","faster","fast","medium","slow","slower","veryslow"],
            "hevc_qsv": ["veryfast","faster","fast","medium","slow","slower","veryslow"],
            "h264_nvenc": ["p1","p2","p3","p4","p5","p6","p7","default","slow","medium","fast","hp","hq","bd","ll","llhq","llhp","lossless","losslesshp"],
            "hevc_nvenc": ["p1","p2","p3","p4","p5","p6","p7","default","slow","medium","fast","hp","hq","bd","ll","llhq","llhp","lossless","losslesshp"],
            "h264_amf": ["balanced","speed","quality"],
            "hevc_amf": ["balanced","speed","quality"],
        }

        self.presetCombo = QComboBox()
        containerLayout.addWidget(self.presetCombo)

        def updatePresetOptions():
            sel_codec = self.videoCodecCombo.currentData() or "libx264"
            opts = self.codec_presets.get(sel_codec, ["fast"])
            self.presetCombo.clear()
            self.presetCombo.addItems(opts)

            # 저장된 프리셋 복원
            saved = self.config.get("preset", "fast")
            if saved in opts:
                self.presetCombo.setCurrentText(saved)
            else:
                default_pick = "medium" if (sel_codec in ("h264_qsv","hevc_qsv") and "medium" in opts) else ("fast" if "fast" in opts else opts[0])
                self.presetCombo.setCurrentText(default_pick)

        # 초기 세팅 + 코덱 변경 시 갱신
        updatePresetOptions()
        self.videoCodecCombo.currentIndexChanged.connect(updatePresetOptions)

        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <table style="border-collapse: collapse; margin-top: 10px; margin-left:auto; margin-right:auto;">
             <thead>
               <tr style="background-color:#f2f2f2;">
                 <th style="border:1px solid #ddd;padding:6px;">코덱</th>
                 <th style="border:1px solid #ddd;padding:6px;">사용 가능한 인코딩 프리셋</th>
               </tr>
             </thead>
             <tbody>
               <tr>
                 <td style="border:1px solid #ddd;padding:6px;"><b>[x264 / HEVC]</b> CPU만 사용</td>
                 <td style="border:1px solid #ddd;padding:6px;">
                   ultrafast / superfast / veryfast / faster / fast / medium / slow / slower / veryslow / placebo
                 </td>
               </tr>

               <tr>
                 <td style="border:1px solid #ddd;padding:6px;color:blue;"><b>[h264 / HEVC]</b> 인텔 GPU</td>
                 <td style="border:1px solid #ddd;padding:6px;">
                   veryfast / faster / fast / medium / slow / slower / veryslow
                 </td>
               </tr>

               <tr>
                 <td style="border:1px solid #ddd;padding:6px;color:darkgreen;"><b>[h264 / HEVC]</b> 지포스 GPU<br>(NVENC 7세대 이상)</td>
                 <td style="border:1px solid #ddd;padding:6px;">
                   p1 (ultrafast) / p2 (superfast) / p3 (veryfast) / p4 (faster) / p5 (fast) / p6 (medium) / p7 (slow)
                 </td>
               </tr>

               <tr>
                 <td style="border:1px solid #ddd;padding:6px;color:darkgreen;"><b>[h264 / HEVC]</b> 지포스 GPU<br>(NVENC 6세대 이하)</td>
                 <td style="border:1px solid #ddd;padding:6px;">
                   default / slow / medium / fast / hp / hq / bd / ll / llhq / llhp / lossless / losslesshp
                 </td>
               </tr>

               <tr>
                 <td style="border:1px solid #ddd;padding:6px;color:red;"><b>[h264 / HEVC]</b> 라데온 GPU</td>
                 <td style="border:1px solid #ddd;padding:6px;">
                   balanced / speed / quality
                 </td>
               </tr>
             </tbody>
            </table>
            <p class="description" style="margin-top:10px;">
            * NVENC 7세대 이상 지포스 GPU 가속 : 16,20,30,40,50 시리즈 이상 (GTX1650 GDDR5 제외)<br>
            * NVENC 6세대 이하 지포스 GPU : 지포스600~900, 1000, GTX1650 GDDR5 등
            </p>
            </div>""".strip()
        ))

        # 영상 출력 해상도
        containerLayout.addWidget(self.htmlLabel('<h2 style="margin-bottom:4px;">영상 출력 해상도(원본/1080p/720p/480p) [치지직]</h2>'))
        self.postprocessResolutionCombo = QComboBox()
        self.postprocessResolutionCombo.addItem("원본유지", "source")
        self.postprocessResolutionCombo.addItem("1080p", "1080p")
        self.postprocessResolutionCombo.addItem("720p", "720p")
        self.postprocessResolutionCombo.addItem("480p", "480p")
        _pp0 = str(self.config.get("postprocess_resolution", "source") or "source").strip().lower()
        if _pp0 not in ("source", "1080p", "720p", "480p"):
            _pp0 = "source"
        _i0 = self.postprocessResolutionCombo.findData(_pp0)
        self.postprocessResolutionCombo.setCurrentIndex(_i0 if _i0 >= 0 else 0)
        containerLayout.addWidget(self.postprocessResolutionCombo)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        열화인코딩에서 해상도 값을 변경합니다(스트림복사 사용 시 미적용).
        </p>
        """.strip()))

        # 화질 및 용량 비율 제어 방식 (비트레이트/퀄리티) [치지직]
        containerLayout.addWidget(self.htmlLabel("<h2>화질 및 용량 비율 제어 방식 (비트레이트/퀄리티) [치지직]</h2>"))

        # config.html에서는 <select id="use_bitrate_mode">에 true=비트레이트, false=퀄리티
        self.rateControlCombo = QComboBox()
        self.rateControlCombo.addItem("비트레이트 모드", True)
        self.rateControlCombo.addItem("퀄리티 모드", False)

        use_bitrate = self.config.get("use_bitrate_mode", True)
        if use_bitrate:
            self.rateControlCombo.setCurrentIndex(0)  # 비트레이트
        else:
            self.rateControlCombo.setCurrentIndex(1)  # 퀄리티

        containerLayout.addWidget(self.rateControlCombo)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        비트레이트 모드와 퀄리티 모드 중 선택합니다.
        </p>
        """.strip()))

        # 비디오 퀄리티 모드 값 (퀄리티 모드 사용 시 입력) [치지직]
        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <h2>비디오 퀄리티 모드 값 (퀄리티 모드 사용 시 입력) [치지직]</h2>
            </div>"""
        ))

        self.videoQualityEdit = QLineEdit(str(self.config.get("video_quality", 33)))
        containerLayout.addWidget(self.videoQualityEdit)

        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <table style="border-collapse: collapse; margin-top: 10px; margin-left:auto; margin-right:auto;">
              <thead>
                <tr style="background-color: #f2f2f2;">
                  <th style="border:1px solid #ddd;padding:8px;">퀄리티 모드 값 (예시)</th>
                  <th style="border:1px solid #ddd;padding:8px;">설명</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style="border:1px solid #ddd;padding:6px;">23~24</td>
                  <td style="border:1px solid #ddd;padding:6px;">5~10% 화질저하, 약간의 용량 절약</td>
                </tr>
                <tr>
                  <td style="border:1px solid #ddd;padding:6px;">26~28</td>
                  <td style="border:1px solid #ddd;padding:6px;">15~25% 화질저하, 용량 절약</td>
                </tr>
                <tr>
                  <td style="border:1px solid #ddd;padding:6px;">32~35</td>
                  <td style="border:1px solid #ddd;padding:6px;">30~40% 화질저하, 용량 절약</td>
                </tr>
              </tbody>
            </table>
            <p class="description" style="margin-top:10px;">
            퀄리티 값이 낮을수록 화질은 좋아지지만 파일 용량은 커집니다.<br>
            표는 x264 CPU 기준 추정치이며, GPU 가속이나 HEVC에서는 다를 수 있습니다.
            </p>
            </div>""".strip()
        ))

        # 비디오 비트레이트
        containerLayout.addWidget(self.htmlLabel("<h2>평균 비트레이트 값 (비트레이트 모드 사용 시 입력) [치지직]</h2>"))

        self.videoBitrateEdit = QLineEdit(self.config.get("video_bitrate", "1000k"))
        containerLayout.addWidget(self.videoBitrateEdit)

        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        평균 비트레이트 값을 설정합니다. 값이 낮을수록 화질이 떨어지고 용량이 줄어듭니다.<br>
        치지직 1080P 기준 8000k 값입니다.<br><br>(예: 5000k)
        </p>
        """.strip()))

        # 최대 비트레이트 값 (vbv_maxrate)
        containerLayout.addWidget(self.htmlLabel("<h2>최대 비트레이트 값 (비트레이트 모드 사용 시 입력) [치지직]</h2>"))
        self.vbvMaxEdit = QLineEdit(self.config.get("vbv_maxrate", ""))
        containerLayout.addWidget(self.vbvMaxEdit)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        인코딩시 비트레이트 가변 폭의 최대값을 지정합니다.<br>
        (예: 7500k, 권장: 평균 비트레이트 값의 1.5~2배)
        </p>
        """.strip()))

        # 비트레이트 버퍼 값 (vbv_bufsize)
        containerLayout.addWidget(self.htmlLabel("<h2>비트레이트 버퍼 값 (비트레이트 모드 사용 시 입력) [치지직]</h2>"))
        self.vbvBufEdit = QLineEdit(self.config.get("vbv_bufsize", ""))
        containerLayout.addWidget(self.vbvBufEdit)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        인코딩 시 화려한 장면이 갑자기 늘어날 때,<br>
        부드럽게 처리하기 위한 버퍼 크기를 설정합니다.<br>
        (예: 15000k, 권장: 평균 비트레이트 값의 3~4배)
        </p>
        """.strip()))

        # 추가 인코딩 명령어 입력
        containerLayout.addWidget(self.htmlLabel("<h2>추가 인코딩 명령어 입력 [치지직 전용]</h2>"))
        self.extraFfmpegEdit = QLineEdit(self.config.get("extra_ffmpeg_options",""))
        containerLayout.addWidget(self.extraFfmpegEdit)

        containerLayout.addWidget(self.htmlLabel("""
        <div style="text-align:center;">

          <p>
            • <b>Look-ahead</b>: 다음 프레임을 사전 확인하여 인코딩 압축 효율을 높입니다.<br>
            • 장점: <b>선명도</b>가 상승하고 <b>용량</b>이 줄어듭니다.<br>
            • 단점: 인코딩 속도가 <b>느려집니다.</b>
          </p>

          <table style="border-collapse: collapse; margin-top:10px; margin-left:auto; margin-right:auto; font-size:12px;">
            <thead>
              <tr style="background-color:#f2f2f2;">
                <th style="border:1px solid #ddd; padding:8px;">GPU</th>
                <th style="border:1px solid #ddd; padding:8px;">사용 인코더</th>
                <th style="border:1px solid #ddd; padding:8px;">낮은 압축효율 예시</th>
                <th style="border:1px solid #ddd; padding:8px;">높은 압축효율 예시</th>
              </tr>
            </thead>
            <tbody>
              <!-- Intel QSV -->
              <tr>
                <td style="border:1px solid #ddd; padding:6px;">
                  <span style="color:blue; font-weight:bold;">인텔</span>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>h264_qsv</code> / <code>hevc_qsv</code>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>-look_ahead 1 -bf 2</code>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>-look_ahead 1 -look_ahead_depth 20 -bf 5</code>
                </td>
              </tr>

              <!-- NVIDIA NVENC -->
              <tr>
                <td style="border:1px solid #ddd; padding:6px;">
                  <span style="color:darkgreen; font-weight:bold;">지포스</span>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>h264_nvenc</code> / <code>hevc_nvenc</code>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>-rc-lookahead 10 -bf 2</code>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>-rc-lookahead 20 -bf 3</code>
                </td>
              </tr>

              <!-- AMD AMF -->
              <tr>
                <td style="border:1px solid #ddd; padding:6px;">
                  <span style="color:red; font-weight:bold;">라데온</span>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>h264_amf</code> / <code>hevc_amf</code>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>-preanalysis true -pa_lookahead_buffer_depth 10 -bf 2</code>
                </td>
                <td style="border:1px solid #ddd; padding:6px;">
                  <code>-preanalysis true -pa_lookahead_buffer_depth 20 -bf 3</code>
                </td>
              </tr>
            </tbody>
          </table>

          <p style="margin-top:8px;">
            • 비교적 빠른 인코딩 속도와 적당한 화질 향상을 원하면 <b>낮은 압축효율</b> 예시를 사용하세요.<br>
            • 인코딩 속도가 느려져도 더 높은 화질을 원하면 <b>높은 압축효율</b> 예시를 사용하세요.<br><br>
            • <b>B-프레임이 0</b>이면 look-ahead 효과가 거의 없습니다. 보통 <code>-bf 2~4</code> 권장.<br>
            • 일부 구형/특수 모델은 제한이 있습니다(예: 지포스 GTX 1630, 1650 GDDR5 등 B-프레임 제한).<br>
            • 인텔 QSV는 6세대(Skylake) 이상 지원(효율은 <b>7세대+</b> 권장), 라데온은 RX 6000 이상 권장.
          </p>
        </div>
        """.strip()))

        # 오디오 코덱
        containerLayout.addWidget(self.htmlLabel("<h2>오디오 코덱 종류 [치지직]</h2>"))
        self.audioCodecCombo = QComboBox()
        self.audioCodecCombo.addItems(["aac","mp3"])
        self.audioCodecCombo.setCurrentText(self.config.get("audio_codec","aac"))
        containerLayout.addWidget(self.audioCodecCombo)
        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <p class="description">
            오디오 코덱을 선택합니다.<br>치지직 기본 오디오 코덱은 AAC 입니다.
            </p>
            </div>""".strip()
        ))

        # 오디오 비트레이트
        containerLayout.addWidget(self.htmlLabel("<h2>오디오 비트레이트 값 [치지직]</h2>"))
        self.audioBitCombo = QComboBox()

        options = [
            ("스트림복사(원본유지)", "copy"),
            ("64k",  "64k"),
            ("96k",  "96k"),
            ("128k", "128k"),
            ("160k", "160k"),
            ("192k", "192k"),
        ]

        for label, value in options:
            self.audioBitCombo.addItem(label, value)

        # 저장된 값과 매칭
        saved = self.config.get("audio_bitrate", "copy")
        idx = self.audioBitCombo.findData(saved)
        if idx < 0:
            idx = self.audioBitCombo.findData("copy")
        self.audioBitCombo.setCurrentIndex(idx)

        containerLayout.addWidget(self.audioBitCombo)
        containerLayout.addWidget(self.htmlLabel(
            """<div style="text-align:center;">
            <p class="description">
            오디오 비트레이트 값을 선택합니다.<br>치지직 오디오 비트레이트 최대값은 192k(원본) 입니다.
            </p>
            </div>""".strip()
        ))

        # GPU1 프로필 + 채널 분배 UI (GPU 수=2일 때만 표시)
        self.gpu2SectionWidget = QWidget()
        gpu2Layout = QVBoxLayout(self.gpu2SectionWidget)
        gpu2Layout.setContentsMargins(0, 0, 0, 0)

        gpu2Layout.addWidget(self.htmlLabel("<h2>GPU1 인코딩 프로필 (GPU 수=2일 때만)</h2>"))

        # GPU1 코덱 콤보
        self.videoCodecComboGpu1 = QComboBox()
        codecOptions = [
            ("x264 CPU (libx264)", "libx264"),
            ("HEVC CPU (libx265)", "libx265"),
            ("H264 인텔GPU (h264_qsv)", "h264_qsv"),
            ("HEVC 인텔GPU (hevc_qsv)", "hevc_qsv"),
            ("H264 지포스GPU (h264_nvenc)", "h264_nvenc"),
            ("HEVC 지포스GPU (hevc_nvenc)", "hevc_nvenc"),
            ("H264 라데온 (h264_amf)", "h264_amf"),
            ("HEVC 라데온 (hevc_amf)", "hevc_amf"),
        ]
        cur_vc1 = self.config.get("video_codec_gpu1", self.config.get("video_codec", "libx264"))
        for label, value in codecOptions:
            self.videoCodecComboGpu1.addItem(label, value)
        idx_vc1 = self.videoCodecComboGpu1.findData(cur_vc1)
        self.videoCodecComboGpu1.setCurrentIndex(idx_vc1 if idx_vc1 >= 0 else 0)

        gpu2Layout.addWidget(self.htmlLabel("<h3>비디오 코덱(GPU1)</h3>"))
        gpu2Layout.addWidget(self.videoCodecComboGpu1)

        # GPU1 프리셋 콤보 (코덱에 따라 옵션 갱신)
        self.presetComboGpu1 = QComboBox()
        gpu2Layout.addWidget(self.htmlLabel("<h3>프리셋(GPU1)</h3>"))
        gpu2Layout.addWidget(self.presetComboGpu1)

        def updatePresetOptionsGpu1():
            sel_codec = self.videoCodecComboGpu1.currentData() or "libx264"
            opts = self.codec_presets.get(sel_codec, ["fast"])
            self.presetComboGpu1.clear()
            self.presetComboGpu1.addItems(opts)

            saved = self.config.get("preset_gpu1", self.config.get("preset", "fast"))
            if saved in opts:
                self.presetComboGpu1.setCurrentText(saved)
            else:
                # qsv면 medium 선호
                if sel_codec in ("h264_qsv", "hevc_qsv") and "medium" in opts:
                    self.presetComboGpu1.setCurrentText("medium")
                elif "fast" in opts:
                    self.presetComboGpu1.setCurrentText("fast")
                else:
                    self.presetComboGpu1.setCurrentText(opts[0])

        updatePresetOptionsGpu1()
        self.videoCodecComboGpu1.currentIndexChanged.connect(updatePresetOptionsGpu1)

        # 영상 출력 해상도
        gpu2Layout.addWidget(self.htmlLabel('<h3>영상 출력 해상도(GPU1)</h3>'))
        self.postprocessResolutionCombo_gpu1 = QComboBox()
        self.postprocessResolutionCombo_gpu1.addItem("원본유지", "source")
        self.postprocessResolutionCombo_gpu1.addItem("1080p", "1080p")
        self.postprocessResolutionCombo_gpu1.addItem("720p", "720p")
        self.postprocessResolutionCombo_gpu1.addItem("480p", "480p")
        _pp1 = str(self.config.get("postprocess_resolution_gpu1", self.config.get("postprocess_resolution", "source")) or "source").strip().lower()
        if _pp1 not in ("source", "1080p", "720p", "480p"):
            _pp1 = "source"
        _i1 = self.postprocessResolutionCombo_gpu1.findData(_pp1)
        self.postprocessResolutionCombo_gpu1.setCurrentIndex(_i1 if _i1 >= 0 else 0)
        gpu2Layout.addWidget(self.postprocessResolutionCombo_gpu1)

        # GPU1 비트레이트/퀄리티 모드
        gpu2Layout.addWidget(self.htmlLabel("<h3>화질/용량 제어 방식(GPU1)</h3>"))
        self.rateControlComboGpu1 = QComboBox()
        self.rateControlComboGpu1.addItem("비트레이트 모드", True)
        self.rateControlComboGpu1.addItem("퀄리티 모드", False)
        use_bitrate_1 = self.config.get("use_bitrate_mode_gpu1", self.config.get("use_bitrate_mode", True))
        self.rateControlComboGpu1.setCurrentIndex(0 if use_bitrate_1 else 1)
        gpu2Layout.addWidget(self.rateControlComboGpu1)

        # GPU1 퀄리티/비트레이트/VBV/추가옵션
        gpu2Layout.addWidget(self.htmlLabel("<h3>퀄리티 값(GPU1)</h3>"))
        self.videoQualityEditGpu1 = QLineEdit(str(self.config.get("video_quality_gpu1", self.config.get("video_quality", 23))))
        gpu2Layout.addWidget(self.videoQualityEditGpu1)

        gpu2Layout.addWidget(self.htmlLabel("<h3>비트레이트 값(GPU1)</h3>"))
        self.videoBitrateEditGpu1 = QLineEdit(str(self.config.get("video_bitrate_gpu1", self.config.get("video_bitrate", "")) or ""))
        gpu2Layout.addWidget(self.videoBitrateEditGpu1)

        gpu2Layout.addWidget(self.htmlLabel("<h3>VBV maxrate(GPU1)</h3>"))
        self.vbvMaxEditGpu1 = QLineEdit(str(self.config.get("vbv_maxrate_gpu1", "")) or "")
        gpu2Layout.addWidget(self.vbvMaxEditGpu1)

        gpu2Layout.addWidget(self.htmlLabel("<h3>VBV bufsize(GPU1)</h3>"))
        self.vbvBufEditGpu1 = QLineEdit(str(self.config.get("vbv_bufsize_gpu1", "")) or "")
        gpu2Layout.addWidget(self.vbvBufEditGpu1)

        gpu2Layout.addWidget(self.htmlLabel("<h3>추가 FFmpeg 옵션(GPU1)</h3>"))
        self.extraFfmpegEditGpu1 = QLineEdit(str(self.config.get("extra_ffmpeg_options_gpu1", "")) or "")
        gpu2Layout.addWidget(self.extraFfmpegEditGpu1)

        # GPU1 오디오 코덱/비트레이트
        gpu2Layout.addWidget(self.htmlLabel("<h3>오디오 코덱(GPU1)</h3>"))
        self.audioCodecComboGpu1 = QComboBox()
        self.audioCodecComboGpu1.addItems(["aac", "mp3"])
        self.audioCodecComboGpu1.setCurrentText(self.config.get("audio_codec_gpu1", self.config.get("audio_codec", "aac")))
        gpu2Layout.addWidget(self.audioCodecComboGpu1)

        gpu2Layout.addWidget(self.htmlLabel("<h3>오디오 비트레이트(GPU1)</h3>"))
        self.audioBitComboGpu1 = QComboBox()
        options = [
            ("스트림복사(원본유지)", "copy"),
            ("64k",  "64k"),
            ("96k",  "96k"),
            ("128k", "128k"),
            ("160k", "160k"),
            ("192k", "192k"),
        ]
        for label, value in options:
            self.audioBitComboGpu1.addItem(label, value)

        saved_ab1 = self.config.get("audio_bitrate_gpu1", self.config.get("audio_bitrate", "copy"))
        idx_ab1 = self.audioBitComboGpu1.findData(saved_ab1)
        if idx_ab1 < 0:
            idx_ab1 = self.audioBitComboGpu1.findData("copy")
        self.audioBitComboGpu1.setCurrentIndex(idx_ab1)
        gpu2Layout.addWidget(self.audioBitComboGpu1)

        # 채널 GPU 분배 (치지직 전용)
        gpu2Layout.addWidget(self.htmlLabel("<h2>치지직 채널 GPU 분배 (치지직 후처리 인코딩 전용)</h2>"))
        gpu2Layout.addWidget(self.htmlLabel(
            '<p style="margin-top:0; color:#777;">'
            '※ 이 분배는 <b>치지직(chzzk) 후처리 인코딩</b>에만 적용됩니다. '
            '씨미(cime)는 mp4 저장/마무리 작업만 하므로 분배 대상이 아닙니다.'
            '</p>'
        ))

        self.gpuAssignWrap = QWidget()
        assignLayout = QVBoxLayout(self.gpuAssignWrap)
        assignLayout.setContentsMargins(0, 0, 0, 0)

        # 치지직 채널만 필터링
        chans = []
        for c in (self.channels or []):
            plat = str(c.get("platform", "") or "").strip().lower()
            if plat == "chzzk":
                chans.append(c)

        if not chans:
            assignLayout.addWidget(QLabel("등록된 치지직(chzzk) 채널이 없습니다."))
        else:
            for ch in chans:
                cid = str(ch.get("id", "") or "").strip()
                if not cid:
                    continue

                plat = str(ch.get("platform", "") or "").strip().lower()  # 여기선 항상 chzzk
                key = f"{plat}:{cid}" 

                name = str(ch.get("name", "") or cid)

                row = QHBoxLayout()
                lbl = QLabel(f"{name} (chzzk)")

                combo = QComboBox()
                combo.addItem("GPU0", 0)
                combo.addItem("GPU1", 1)

                gi = ch.get("gpu_index", 0)
                try:
                    gi = 1 if int(gi) == 1 else 0
                except Exception:
                    gi = 0
                combo.setCurrentIndex(1 if gi == 1 else 0)

                self.gpuAssignCombos[key] = combo  

                row.addWidget(lbl)
                row.addStretch(1)
                row.addWidget(combo)
                assignLayout.addLayout(row)

        gpu2Layout.addWidget(self.gpuAssignWrap)

        containerLayout.addWidget(self.gpu2SectionWidget)

        # GPU 수 변경 시 GPU1/채널분배 UI 표시/숨김
        self.gpuCountCombo.currentIndexChanged.connect(self.toggleGpuUi)
        self.toggleGpuUi()

        self.addDashedDivider(containerLayout) # 분류 점선

        # 알림 설정
        containerLayout.addWidget(self.htmlLabel("<h2>알림 설정</h2>"))

        notification = self.notification_data or {}
        events = notification.get("events") or {}
        limits = notification.get("limits") or {}

        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        알림 설정은 <code>json/notification.json</code>에 저장됩니다.
        텔레그램과 디스코드 중 사용자가 선택하여 녹화진행 상황을 알림으로 받을 수 있으며,
        아래 각 이벤트별 ON/OFF 설정은 치지직과 씨미 녹화에 모두 공통 적용됩니다.
        </p>
        """.strip()))

        containerLayout.addWidget(QLabel("텔레그램 알림 사용"))
        self.telegramEnabledCombo = self.buildOnOffCombo(bool(notification.get("telegram_enabled", False)))
        containerLayout.addWidget(self.telegramEnabledCombo)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        ON이면 선택한 알림 항목을 텔레그램 봇으로 전송합니다.<br>
        기존 <code>telegram.json</code>에서 직접 옮기기 쉽도록 봇 토큰과 채팅방 ID 항목명은 그대로 유지합니다.
        </p>
        """.strip()))

        lblToken = QLabel("Telegram 봇 토큰:")
        containerLayout.addWidget(lblToken)
        self.editTelegramToken = QLineEdit(notification.get("telegram_bot_token", ""))
        containerLayout.addWidget(self.editTelegramToken)

        containerLayout.addWidget(self.htmlLabel("""
        <div style="color:#666; font-size:10pt; line-height:1.5;">
        (1) 텔레그램 앱에서 <b>@BotFather</b>를 검색하여 대화를 시작합니다.<br>
        (2) <code>/newbot</code> 명령어로 봇을 만들고 봇 토큰을 복사합니다.<br>
        (3) 봇 채팅방에서 <code>/start</code>를 누른 뒤 아무 메시지나 보냅니다.<br>
        (4) 웹브라우저에서 <code>https://api.telegram.org/bot토큰값/getUpdates</code>를 열고 chat id 값을 확인합니다.
        </div>
        """.strip()))

        lblChatId = QLabel("Telegram 채팅 ID:")
        containerLayout.addWidget(lblChatId)
        self.editChatId = QLineEdit(notification.get("telegram_chat_id", ""))
        containerLayout.addWidget(self.editChatId)

        self.btnTestTelegram = QPushButton("텔레그램 테스트 전송")
        self.btnTestTelegram.clicked.connect(lambda: self.onTestNotification("telegram"))
        containerLayout.addWidget(self.btnTestTelegram)

        self.addDashedDivider(containerLayout, top=10, bottom=10)

        containerLayout.addWidget(QLabel("디스코드 알림 사용"))
        self.discordEnabledCombo = self.buildOnOffCombo(bool(notification.get("discord_enabled", False)))
        containerLayout.addWidget(self.discordEnabledCombo)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        ON이면 선택한 알림 항목을 디스코드 웹훅으로 전송합니다.<br>
        텔레그램과 동시에 켤 수 있으며, 같은 이벤트가 두 대상으로 함께 전송됩니다.
        </p>
        """.strip()))

        lblDiscordWebhook = QLabel("Discord 웹훅 URL:")
        containerLayout.addWidget(lblDiscordWebhook)
        self.editDiscordWebhook = QLineEdit(notification.get("discord_webhook_url", ""))
        containerLayout.addWidget(self.editDiscordWebhook)

        containerLayout.addWidget(self.htmlLabel(
            '<div style="color:#666; font-size:10pt;">'
            '디스코드 채널 설정의 웹훅 메뉴에서 생성한 URL입니다. 디스코드 알림을 ON으로 사용할 때 필수 입력사항입니다.'
            '</div>'
        ))

        self.btnTestDiscord = QPushButton("디스코드 테스트 전송")
        self.btnTestDiscord.clicked.connect(lambda: self.onTestNotification("discord"))
        containerLayout.addWidget(self.btnTestDiscord)

        self.addDashedDivider(containerLayout, top=10, bottom=10)

        containerLayout.addWidget(self.htmlLabel("<h3>알림 받을 이벤트</h3>"))
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        알림받고 싶은 각 항목을 ON/OFF로 개별 설정합니다.
        녹화 시작/종료/사용자 중지처럼 자주 발생할 수 있는 항목이 메신저를 통해 전달되며,
        각 사용자의 상황에 따라 필요한 알람만 설정하는 것을 권장합니다.
        </p>
        """.strip()))
        self.notificationEventCombos = {}

        event_labels = [
            ("record_start_failed", "녹화 시작 실패", "녹화 의존 프로그램 실패, 녹화 명령 생성 실패 등 녹화가 시작되지 못한 경우 알림을 보냅니다."),
            ("record_abnormally_stopped", "녹화 비정상 종료", "방송 종료가 아닌 오류, 파일 병합 실패, 프로세스 비정상 종료처럼 확인이 필요한 종료 상황을 알립니다."),
            ("postprocess_failed", "후처리 실패", "치지직 후처리 인코딩/스트림복사 작업이 실패했을 때 알림을 보냅니다."),
            ("postprocess_finished", "후처리 완료", "치지직 후처리 작업이 정상 완료되었을 때 알림을 보냅니다. 씨미는 별도 후처리 없이 녹화 완료 이벤트로 처리됩니다."),
            ("cookie_auth_failed", "쿠키/권한 문제", "연령제한, 유로구독 권한 필요, 쿠키 만료, 로그인 필요 등 계정 인증 확인이 필요한 상황을 알립니다."),
            ("disk_space_low", "디스크 용량 부족", "녹화 저장 경로의 남은 용량이 아래 기준값 이하로 떨어지면 알림을 보냅니다."),
            ("watchparty_skipped", "같이보기 조건 불일치", "치지직 같이보기만 녹화를 선택한 채널에서 해당 조건이 맞지 않아 녹화를 건너뛴 경우 알림을 보냅니다."),
            ("record_started", "녹화 시작", "치지직 또는 씨미 녹화가 시작되었을 때 알림을 보냅니다."),
            ("record_finished", "녹화 종료", "치지직 또는 씨미 녹화가 정상 종료되었을 때 알림을 보냅니다."),
            ("record_user_stopped", "사용자 중지", "사용자가 직접 녹화를 중지했을 때 알림을 보냅니다."),
        ]

        default_on_events = {
            "record_start_failed",
            "record_abnormally_stopped",
            "postprocess_finished",
            "postprocess_failed",
            "cookie_auth_failed",
            "disk_space_low",
        }

        for event_key, label, description in event_labels:
            containerLayout.addWidget(QLabel(label))
            combo = self.buildOnOffCombo(bool(events.get(event_key, event_key in default_on_events)))
            self.notificationEventCombos[event_key] = combo
            containerLayout.addWidget(combo)
            containerLayout.addWidget(self.htmlLabel(f"<p class='description'>{description}</p>"))

        containerLayout.addWidget(self.htmlLabel("<h3>알림 제한 설정</h3>"))

        lblDiskLow = QLabel("디스크 용량 부족 경고 기준(GB):")
        containerLayout.addWidget(lblDiskLow)
        self.editNotifyDiskSpaceLowGb = QLineEdit(str(limits.get("disk_space_low_gb", 10)))
        containerLayout.addWidget(self.editNotifyDiskSpaceLowGb)
        containerLayout.addWidget(self.htmlLabel(
            "<p class='description'>녹화파일이 생성되는 디스크 기준으로, 남은 디스크 용량이 설정한 값 이하가 되면 디스크 용량 부족 알림을 보냅니다.</p>"
        ))

        lblDedupe = QLabel("중복 알림 방지 시간(초):")
        containerLayout.addWidget(lblDedupe)
        self.editNotifyDedupeSeconds = QLineEdit(str(limits.get("dedupe_seconds", 300)))
        containerLayout.addWidget(self.editNotifyDedupeSeconds)
        containerLayout.addWidget(self.htmlLabel("""
        <p class="description">
        같은 채널의 같은 동일한 이벤트를 지정한 시간 내에 반복 전송하지 않습니다.
        0으로 설정하면 중복 방지를 사용하지 않습니다. 테스트 메시지는 이 제한과 무관하게 전송됩니다.
        </p>
        """.strip()))

        self.addDashedDivider(containerLayout) # 분류 점선

        # 하단 버튼 레이아웃
        btnLayout = QHBoxLayout()

        self.saveBtn = QPushButton("저장")
        self.saveBtn.setMinimumSize(100, 40)
        self.saveBtn.clicked.connect(self.saveSettings)
        btnLayout.addWidget(self.saveBtn)

        self.cancelBtn = QPushButton("취소")
        self.cancelBtn.setMinimumSize(100, 40)
        self.cancelBtn.clicked.connect(self.close)
        btnLayout.addWidget(self.cancelBtn)

        containerLayout.addSpacing(20)
        containerLayout.addLayout(btnLayout)


    def comboBool(self, combo: QComboBox) -> bool:
        val = combo.currentData()
        if val is None:
            val = combo.currentText()
        return toBool(val)

    def toggleSplitRecording(self):
        use_split = bool(self.splitCombo.itemData(self.splitCombo.currentIndex()))
        if hasattr(self, "autoStopEdit"):
            self.autoStopEdit.setEnabled(use_split)
        if hasattr(self, "overlapEdit"):
            self.overlapEdit.setEnabled(use_split)
        if hasattr(self, "splitPostCombo"):
            self.splitPostCombo.setEnabled(use_split)

    # GPU 수에 따라 GPU1/채널분배 UI 표시/숨김
    def toggleGpuUi(self):
        if not hasattr(self, "gpuCountCombo"):
            return

        try:
            gc = int(self.gpuCountCombo.currentData() or 1)
        except Exception:
            gc = 1
        show2 = (gc == 2)

        if hasattr(self, "gpu2SectionWidget"):
            self.gpu2SectionWidget.setVisible(show2)


    def buildOnOffCombo(self, current_bool: bool):
        combo = QComboBox()
        combo.addItem("ON", True)
        combo.addItem("OFF", False)
        if current_bool:
            combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(1)
        return combo

    def htmlLabel(self, html_text: str):
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        label.setOpenExternalLinks(True)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)  
        label.setText(html_text)
        return label

    def buildNotificationData(self):
        try:
            dedupe_seconds = int((self.editNotifyDedupeSeconds.text() or "300").strip())
        except Exception:
            dedupe_seconds = 300
        dedupe_seconds = max(0, min(86400, dedupe_seconds))

        try:
            disk_space_low_gb = int((self.editNotifyDiskSpaceLowGb.text() or "10").strip())
        except Exception:
            disk_space_low_gb = 10
        disk_space_low_gb = max(1, min(1024, disk_space_low_gb))

        events = {}
        for key, combo in (self.notificationEventCombos or {}).items():
            events[key] = self.comboBool(combo)

        return {
            "telegram_enabled": self.comboBool(self.telegramEnabledCombo),
            "telegram_bot_token": self.editTelegramToken.text().strip(),
            "telegram_chat_id": self.editChatId.text().strip(),

            "discord_enabled": self.comboBool(self.discordEnabledCombo),
            "discord_webhook_url": self.editDiscordWebhook.text().strip(),

            "events": events,
            "limits": {
                "dedupe_seconds": dedupe_seconds,
                "disk_space_low_gb": disk_space_low_gb,
            },
        }


    def validateNotificationData(self, notification_data, target=None):
        if target in (None, "telegram") and notification_data.get("telegram_enabled"):
            if not notification_data.get("telegram_bot_token") or not notification_data.get("telegram_chat_id"):
                return "텔레그램 알림 사용 시 봇 토큰과 채팅 ID를 모두 입력해주세요."

        if target in (None, "discord") and notification_data.get("discord_enabled"):
            if not notification_data.get("discord_webhook_url"):
                return "디스코드 알림 사용 시 웹훅 URL을 입력해주세요."

        return ""


    def onTestNotification(self, target):
        target = (target or "").strip().lower()
        notification_data = self.buildNotificationData()

        if target == "telegram" and not notification_data.get("telegram_enabled"):
            QMessageBox.warning(self, "알림", "텔레그램 알림 활성화가 꺼져 있습니다.")
            return

        if target == "discord" and not notification_data.get("discord_enabled"):
            QMessageBox.warning(self, "알림", "디스코드 알림 활성화가 꺼져 있습니다.")
            return

        error_message = self.validateNotificationData(notification_data, target=target)
        if error_message:
            QMessageBox.warning(self, "알림", error_message)
            return

        try:
            saveNotification(notification_data)

            base_url = getBaseUrl()
            url = f"{base_url}/api/test_notification/{target}"
            response = requests.get(url, timeout=10)

            try:
                data = response.json()
            except Exception:
                data = {}

            if response.status_code == 200 and data.get("status") == "success":
                QMessageBox.information(self, "알림", data.get("message") or "테스트 메시지를 전송했습니다.")
            else:
                msg = data.get("message") or response.text or f"HTTP {response.status_code}"
                QMessageBox.critical(self, "오류", f"전송 실패:\n{msg}")

        except Exception as e:
            QMessageBox.critical(self, "오류", f"테스트 중 오류:\n{e}")


    def addDashedDivider(self, layout, top=20, bottom=20):
        w = DashedLine("#cccccc", self)
        w.setContentsMargins(0, top, 0, bottom)
        layout.addWidget(w)


    def saveSettings(self):
        try:
            cb = self.comboBool  

            new_config = {}
            new_config["autoRecordingMode"] = cb(self.autoRecCombo)

            new_config["enableTray"] = bool(self.chkEnableTray.isChecked())
            new_config["minimizeToTrayOnClose"] = bool(self.chkTrayOnClose.isChecked())
            new_config["minimizeToTrayOnStart"] = bool(self.chkTrayOnStart.isChecked())
            if not new_config["enableTray"]:
                new_config["minimizeToTrayOnClose"] = False
                new_config["minimizeToTrayOnStart"] = False
           
            plugin_val = self.pluginCombo.currentData()
            if plugin_val not in ("basic", "timemachine_plus"):
                plugin_val = "basic"
            new_config["plugin_type"] = plugin_val

            # 숫자 변환은 가드
            try:
                new_config["timemachine_time_shift"] = int((self.timeShiftEdit.text() or "0").strip())
            except Exception:
                new_config["timemachine_time_shift"] = 0
            
            new_config["autoPostProcessing"]        = cb(self.autoPostCombo)
            new_config["deleteAfterPostProcessing"] = cb(self.deleteOrigCombo)
            new_config["removeFixedPrefix"]         = cb(self.removeFixedCombo)
            new_config["moveAfterProcessingEnabled"]= cb(self.moveAfterProcCombo)
            new_config["moveAfterProcessing"]       = self.moveAfterEdit.text().strip()
            new_config["postNewWindow"]             = cb(self.postNewWinCombo)

            try:
                new_config["recheckInterval"] = int((self.recheckEdit.text() or "60").strip())
            except Exception:
                new_config["recheckInterval"] = 60

            new_config["filenamePattern"]  = self.filenameEdit.text().strip() or "[{start_time}] {safe_live_title}"
            new_config["splitRecordingMode"] = cb(self.splitCombo)
            if new_config["splitRecordingMode"]:
                try:
                    new_config["autoStopInterval"] = int((self.autoStopEdit.text() or "0").strip())
                except Exception:
                    new_config["autoStopInterval"] = 0
                try:
                    _ovl = int((self.overlapEdit.text() or "0").strip())
                except Exception:
                    _ovl = 0
                new_config["splitOverlapSec"] = max(0, min(30, _ovl))
            else:
                new_config["autoStopInterval"] = 0
                new_config["splitOverlapSec"] = 0

            try:
                new_config["splitPostProcessing"] = cb(self.splitPostCombo)
            except Exception:
                new_config["splitPostProcessing"] = True 

            new_config["stream_copy"]      = cb(self.streamChoiceCombo)
            new_config["video_codec"]      = self.videoCodecCombo.currentData()
            new_config["preset"]           = self.presetCombo.currentText()
            new_config["postprocess_resolution"] = self.postprocessResolutionCombo.currentData() or "source"
            new_config["use_bitrate_mode"] = cb(self.rateControlCombo)

            try:
                new_config["video_quality"] = int((self.videoQualityEdit.text() or "23").strip())
            except Exception:
                new_config["video_quality"] = 23

            new_config["video_bitrate"]        = self.videoBitrateEdit.text().strip()
            new_config["vbv_maxrate"]          = self.vbvMaxEdit.text().strip()
            new_config["vbv_bufsize"]          = self.vbvBufEdit.text().strip()
            new_config["extra_ffmpeg_options"] = self.extraFfmpegEdit.text().strip()
            
            new_config["audio_codec"]   = self.audioCodecCombo.currentText()
            new_config["audio_bitrate"] = (self.audioBitCombo.currentData() or "copy")

            # GPU 수 + GPU1 프로필 + 채널 분배(JSON)
            try:
                gc = int(self.gpuCountCombo.currentData() or 1)
            except Exception:
                gc = 1
            gc = 2 if gc == 2 else 1
            new_config["gpuCount"] = gc

            # GPU1 프로필(항상 저장 가능 / GPU 수 1이면 백엔드가 무시)
            new_config["video_codec_gpu1"]          = (self.videoCodecComboGpu1.currentData() or new_config["video_codec"])
            new_config["preset_gpu1"]               = (self.presetComboGpu1.currentText() or new_config["preset"])
            new_config["postprocess_resolution_gpu1"] = (
                (self.postprocessResolutionCombo_gpu1.currentData() if hasattr(self, "postprocessResolutionCombo_gpu1") else None)
                or new_config["postprocess_resolution"]
            )
            new_config["use_bitrate_mode_gpu1"]     = bool(self.rateControlComboGpu1.currentData())
            try:
                new_config["video_quality_gpu1"]    = int((self.videoQualityEditGpu1.text() or str(new_config["video_quality"])).strip())
            except Exception:
                new_config["video_quality_gpu1"]    = int(new_config["video_quality"])
            new_config["video_bitrate_gpu1"]        = self.videoBitrateEditGpu1.text().strip()
            new_config["vbv_maxrate_gpu1"]          = self.vbvMaxEditGpu1.text().strip()
            new_config["vbv_bufsize_gpu1"]          = self.vbvBufEditGpu1.text().strip()
            new_config["extra_ffmpeg_options_gpu1"] = self.extraFfmpegEditGpu1.text().strip()
            new_config["audio_codec_gpu1"]          = self.audioCodecComboGpu1.currentText()
            new_config["audio_bitrate_gpu1"]        = (self.audioBitComboGpu1.currentData() or "copy")

            # 채널 분배는 GPU 수=2일 때만 전송
            if gc == 2:
                mapping = {}
                for cid, combo in (self.gpuAssignCombos or {}).items():
                    try:
                        mapping[str(cid)] = 1 if int(combo.currentData()) == 1 else 0
                    except Exception:
                        mapping[str(cid)] = 0
                new_config["gpuAssignmentsJson"] = json.dumps(mapping, ensure_ascii=False)

            notification_data = self.buildNotificationData()
            error_message = self.validateNotificationData(notification_data)
            if error_message:
                QMessageBox.warning(self, "알림 설정 확인", error_message)
                return

            new_config["telegram_enabled"] = "on" if notification_data["telegram_enabled"] else "off"
            new_config["telegram_bot_token"] = notification_data["telegram_bot_token"]
            new_config["telegram_chat_id"] = notification_data["telegram_chat_id"]

            new_config["discord_enabled"] = "on" if notification_data["discord_enabled"] else "off"
            new_config["discord_webhook_url"] = notification_data["discord_webhook_url"]

            for event_key, enabled in (notification_data.get("events") or {}).items():
                new_config[f"notify_{event_key}"] = "on" if enabled else "off"

            limits = notification_data.get("limits") or {}
            new_config["notify_dedupe_seconds"] = str(limits.get("dedupe_seconds", 300))
            new_config["notify_disk_space_low_gb"] = str(limits.get("disk_space_low_gb", 10))
            
            base_url = getBaseUrl()
            url = f"{base_url}/config"
            response = requests.post(url, data=new_config, timeout=10)
            if response.status_code in (200, 303):
                QMessageBox.information(self, "설정 저장", "설정이 성공적으로 저장되었습니다.")
                self.close()
            else:
                raise Exception("설정 저장 실패: " + response.text)

        except Exception as e:
            QMessageBox.critical(self, "오류", f"설정 저장 중 오류:\n{e}")
