from __future__ import annotations

import requests


CHZZK_USER_STATUS_API = (
    "https://comm-api.game.naver.com/"
    "nng_main/v1/user/getUserStatus"
)


def buildNaverCookieHeader(cookie_data: dict) -> str:
    root = cookie_data if isinstance(cookie_data, dict) else {}
    chzzk = root.get("chzzk") if isinstance(root.get("chzzk"), dict) else {}

    nid_aut = str(chzzk.get("NID_AUT") or "").strip()
    nid_ses = str(chzzk.get("NID_SES") or "").strip()

    parts = []

    if nid_ses:
        parts.append(f"NID_SES={nid_ses}")
    if nid_aut:
        parts.append(f"NID_AUT={nid_aut}")

    return "; ".join(parts)


def checkChzzkCookie(cookie_data: dict, timeout: int = 5) -> dict:
    root = cookie_data if isinstance(cookie_data, dict) else {}
    chzzk = (
        root.get("chzzk")
        if isinstance(root.get("chzzk"), dict)
        else {}
    )

    nid_aut = str(chzzk.get("NID_AUT") or "").strip()
    nid_ses = str(chzzk.get("NID_SES") or "").strip()

    if not nid_aut or not nid_ses:
        return {
            "ok": False,
            "status": "missing",
            "message": (
                "치지직 NID_AUT / NID_SES 쿠키값을 "
                "모두 입력해야 합니다."
            )
        }

    cookie_header = buildNaverCookieHeader(cookie_data)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://chzzk.naver.com/",
        "Origin": "https://chzzk.naver.com",
        "Cookie": cookie_header,
    }

    try:
        response = requests.get(CHZZK_USER_STATUS_API, headers=headers, timeout=timeout)
    except requests.Timeout:
        return {
            "ok": False,
            "status": "timeout",
            "message": "쿠키 확인 요청이 시간 초과되었습니다."
        }
    except requests.RequestException as e:
        return {
            "ok": False,
            "status": "network_error",
            "message": f"쿠키 확인 중 네트워크 오류가 발생했습니다: {e}"
        }

    if response.status_code in (401, 403):
        return {
            "ok": False,
            "status": "expired",
            "message": "쿠키가 만료되었거나 로그인이 필요합니다.",
            "http_status": response.status_code,
        }

    if response.status_code != 200:
        return {
            "ok": False,
            "status": "unknown_response",
            "message": f"쿠키 확인 API가 예상과 다른 응답을 반환했습니다. HTTP {response.status_code}",
            "http_status": response.status_code,
        }

    try:
        data = response.json()
    except ValueError:
        return {
            "ok": False,
            "status": "invalid_json",
            "message": "쿠키 확인 API 응답을 JSON으로 해석할 수 없습니다.",
            "http_status": response.status_code,
        }

    content = (
        data.get("content")
        if isinstance(data, dict)
        else None
    )

    logged_in = (
        content.get("loggedIn")
        if isinstance(content, dict)
        else False
    )

    user_id_hash = (
        str(content.get("userIdHash") or "").strip()
        if isinstance(content, dict)
        else ""
    )

    if logged_in is True and user_id_hash:
        return {
            "ok": True,
            "status": "valid",
            "message": "치지직 쿠키가 정상으로 확인되었습니다.",
            "http_status": response.status_code,
        }

    return {
        "ok": False,
        "status": "not_logged_in",
        "message": "응답은 정상이나 로그인 사용자 정보가 확인되지 않았습니다.",
        "http_status": response.status_code,
    }
