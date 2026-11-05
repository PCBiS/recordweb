# FILE_VERSION : FSM 260605_1

import requests

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QHBoxLayout, QMessageBox, QTabWidget, QTextBrowser
)

from module.data_manager import loadCookies, getBaseUrl


class CookieManagerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("쿠키관리")
        self.resize(560, 460)

        main_layout = QVBoxLayout(self)

        # 1) 안내 탭 생성
        tabs = QTabWidget()
        tabs.setDocumentMode(False)

        # 치지직 안내 탭
        chzzk_tab = QTextBrowser()
        chzzk_tab.setOpenExternalLinks(True)
        chzzk_tab.setHtml("""
            <h3>치지직 쿠키 추출 방법</h3>
            <ol>
              <li>브라우저에서 네이버(<code>https://www.naver.com</code>)에 로그인합니다.</li>
              <li>F12 → Application(애플리케이션) 탭 → Cookies → <code>https://www.naver.com</code> 또는 <code>https://chzzk.naver.com</code>을 선택합니다.</li>
              <li><code>NID_AUT</code>, <code>NID_SES</code> 값을 복사합니다.</li>
              <li>아래 입력칸에 붙여넣고 저장합니다.</li>
            </ol>
            <p>
              ※ 치지직 연령제한 방송은 해당 계정 쿠키가 있어야 합니다.<br>
              ※ 쿠키는 로그인 세션 정보이므로 다른 사람에게 공유하지 마세요.
            </p>
        """)
        tabs.addTab(chzzk_tab, "치지직 안내")

        cime_tab = QTextBrowser()
        cime_tab.setOpenExternalLinks(True)
        cime_tab.setHtml("""
            <h3>씨미 쿠키 추출 방법</h3>
            <ol>
              <li>브라우저에서 씨미(<code>https://ci.me</code>)에 로그인합니다.</li>
              <li>F12 → Application(애플리케이션) 탭 → Cookies → <code>https://ci.me</code>를 선택합니다.</li>
              <li><code>mauth-authorization-code</code>, <code>session-id</code> 값을 복사합니다.</li>
              <li>아래 입력칸에 붙여넣고 저장합니다.</li>
            </ol>
            <p>
              ※ 씨미 연령제한/4K 구독 방송은 해당 계정 쿠키가 있어야 합니다.<br>
              ※ 쿠키는 로그인 세션 정보이므로 다른 사람에게 공유하지 마세요.
            </p>
        """)
        tabs.addTab(cime_tab, "씨미 안내")

        main_layout.addWidget(tabs)

        # 2) 쿠키 입력 폼
        form_layout = QFormLayout()
        self.fields = {}

        cookie_data = loadCookies() or {}
        chzzk = cookie_data.get("chzzk", {})
        cime = cookie_data.get("cime", {})

        self.fields = {
            "chzzk": {},
            "cime": {},
        }

        form_layout.addRow(QLabel("<b>[치지직 쿠키]</b>"))

        for key in ("NID_AUT", "NID_SES"):
            label = QLabel(f"{key}:")
            edit = QLineEdit(str(chzzk.get(key, "")))
            edit.setPlaceholderText(f"{key} 값을 입력하세요")
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            form_layout.addRow(label, edit)
            self.fields["chzzk"][key] = edit

        form_layout.addRow(QLabel("<b>[씨미 쿠키]</b>"))

        for key in ("mauth-authorization-code", "session-id"):
            label = QLabel(f"{key}:")
            edit = QLineEdit(str(cime.get(key, "")))
            edit.setPlaceholderText(f"{key} 값을 입력하세요")
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            form_layout.addRow(label, edit)
            self.fields["cime"][key] = edit

        main_layout.addLayout(form_layout)

        # 3) 쿠키 확인 결과
        self.check_result_label = QLabel("")
        self.check_result_label.setWordWrap(True)
        main_layout.addWidget(self.check_result_label)

        # 4) 버튼 영역
        btn_row = QHBoxLayout()
        self.check_chzzk_btn = QPushButton("치지직 쿠키 확인")
        self.check_cime_btn = QPushButton("씨미 쿠키 확인")
        save_btn = QPushButton("저장")
        cancel_btn = QPushButton("취소")

        self.check_chzzk_btn.setMinimumHeight(40)
        self.check_cime_btn.setMinimumHeight(40)
        save_btn.setMinimumHeight(40)
        cancel_btn.setMinimumHeight(40)

        self.check_chzzk_btn.setProperty("variant", "secondary")
        self.check_cime_btn.setProperty("variant", "secondary")
        save_btn.setProperty("variant", "primary")
        cancel_btn.setProperty("variant", "secondary")

        btn_row.addWidget(self.check_chzzk_btn)
        btn_row.addWidget(self.check_cime_btn)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        main_layout.addLayout(btn_row)

        # 5) 시그널 연결
        self.check_chzzk_btn.clicked.connect(self.checkChzzkCookie)
        self.check_cime_btn.clicked.connect(self.checkCimeCookie)
        save_btn.clicked.connect(self.saveCookies)
        cancel_btn.clicked.connect(self.close)

    def _collectCookies(self):
        return {
            "chzzk": {
                key: edit.text().strip()
                for key, edit in self.fields["chzzk"].items()
            },
            "cime": {
                key: edit.text().strip()
                for key, edit in self.fields["cime"].items()
            }
        }

    def saveCookies(self):
        new_data = self._collectCookies()

        try:
            base_url = getBaseUrl()
            url = f"{base_url}/cookies"
            response = requests.post(url, json=new_data, timeout=5)

            if response.status_code == 200:
                QMessageBox.information(self, "저장 완료", "쿠키값이 저장되었습니다.")
                self.close()
                return

            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text

            raise RuntimeError(detail or f"HTTP {response.status_code}")

        except Exception as e:
            QMessageBox.warning(self, "오류", f"쿠키 저장 중 오류 발생: {e}")

    def checkChzzkCookie(self):
        new_data = self._collectCookies()

        try:
            base_url = getBaseUrl()

            # 사용자가 방금 입력한 값을 기준으로 검사해야 하므로 먼저 저장합니다.
            save_resp = requests.post(f"{base_url}/cookies", json=new_data, timeout=5)
            if save_resp.status_code != 200:
                try:
                    detail = save_resp.json().get("detail")
                except Exception:
                    detail = save_resp.text
                raise RuntimeError(detail or f"쿠키 저장 실패 HTTP {save_resp.status_code}")

            self.check_result_label.setText("치지직 쿠키 확인 중...")

            resp = requests.get(f"{base_url}/api/check_chzzk_cookie", timeout=5)
            data = resp.json()

            if data.get("ok"):
                msg = data.get("message", "치지직 쿠키가 정상입니다.")
                self.check_result_label.setText(f"정상: {msg}")
                QMessageBox.information(self, "쿠키 확인", msg)
            else:
                msg = data.get("message", "쿠키가 만료되었거나 확인이 필요합니다.")
                self.check_result_label.setText(f"확인 필요: {msg}")
                QMessageBox.warning(self, "쿠키 확인 필요", msg)

        except Exception as e:
            self.check_result_label.setText(f"확인 실패: {e}")
            QMessageBox.warning(self, "확인 실패", f"치지직 쿠키 확인 중 오류가 발생했습니다.\n{e}")


    def checkCimeCookie(self):
        new_data = self._collectCookies()

        try:
            base_url = getBaseUrl()

            save_resp = requests.post(f"{base_url}/cookies", json=new_data, timeout=5)
            if save_resp.status_code != 200:
                try:
                    detail = save_resp.json().get("detail")
                except Exception:
                    detail = save_resp.text
                raise RuntimeError(detail or f"쿠키 저장 실패 HTTP {save_resp.status_code}")

            self.check_result_label.setText("씨미 쿠키 확인 중...")

            resp = requests.get(f"{base_url}/api/check_cime_cookie", timeout=10)
            data = resp.json()

            if data.get("ok"):
                msg = data.get("message", "씨미 쿠키가 정상입니다.")
                self.check_result_label.setText(f"정상: {msg}")
                QMessageBox.information(self, "씨미 쿠키 확인", msg)
            else:
                msg = data.get("message", "씨미 쿠키가 만료되었거나 확인이 필요합니다.")
                self.check_result_label.setText(f"확인 필요: {msg}")
                QMessageBox.warning(self, "씨미 쿠키 확인 필요", msg)

        except Exception as e:
            self.check_result_label.setText(f"확인 실패: {e}")
            QMessageBox.warning(self, "확인 실패", f"씨미 쿠키 확인 중 오류가 발생했습니다.\n{e}")
