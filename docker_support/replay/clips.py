"""Resolve public CHZZK clips without persisting playback credentials or URLs."""

import json
import math
import re
import urllib.parse
import urllib.request


MAX_PAYLOAD = 2 * 1024 * 1024
TIMEOUT = 20
_ERROR = "This public clip is unavailable."
_CLIP_ID = re.compile(r"[A-Za-z0-9]{1,100}\Z")
_FORMAT_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z")
_FPS = re.compile(r"[0-9]+(?:\.[0-9]+|/[1-9][0-9]*)?\Z")


class ClipUnavailableError(ValueError):
    """Safe to expose to a caller; never includes provider payloads or URLs."""


def validate_media_url(url):
    """Return an absolute HTTPS MP4 URL on pstatic.net, or fail safely."""
    try:
        if not isinstance(url, str) or not url or len(url) > 32768:
            raise ValueError()
        if any(ord(c) <= 32 or ord(c) >= 127 for c in url) or "\\" in url:
            raise ValueError()
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme != "https" or not _HOST.fullmatch(host)
                or not (host == "pstatic.net" or host.endswith(".pstatic.net"))
                or parts.username is not None or parts.password is not None
                or parts.port is not None or ":" in parts.netloc
                or parts.fragment or not parts.path.lower().endswith(".mp4")):
            raise ValueError()
        return url
    except Exception:
        raise ClipUnavailableError(_ERROR) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClipUnavailableError(_ERROR)


def _get_json(url, referer):
    # No cookie jar or ambient proxy credentials are used by this public adapter.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": referer,
        "Origin": "https://chzzk.naver.com", "Accept": "application/json",
        "Accept-Encoding": "identity",
    })
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise ValueError()
            size = response.headers.get("Content-Length")
            if size is not None and (int(size) < 0 or int(size) > MAX_PAYLOAD):
                raise ValueError()
            payload = response.read(MAX_PAYLOAD + 1)
        if len(payload) > MAX_PAYLOAD:
            raise ValueError()
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except Exception:
        raise ClipUnavailableError(_ERROR) from None


def _items(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def _unavailable(data):
    """Check explicit availability flags without interpreting provider messages."""
    if isinstance(data, dict):
        if (data.get("playable") is False or data.get("adult") is True
                or data.get("private") is True or data.get("isPrivate") is True
                or data.get("available") is False):
            return True
        if str(data.get("visibility", "")).upper() in {"PRIVATE", "DELETED", "UNAVAILABLE"}:
            return True
        return any(_unavailable(value) for value in data.values() if isinstance(value, (dict, list)))
    if isinstance(data, list):
        return any(_unavailable(value) for value in data)
    return False


def _dimension(value):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError()
    result = int(value)
    if not 0 < result <= 16384:
        raise ValueError()
    return result


def _formats(card):
    mpds = card["card"]["content"]["vod"]["playback"]["MPD"]
    formats = []
    seen = set()
    for mpd in _items(mpds):
        for period in _items(mpd.get("Period")):
            for adaptation in _items(period.get("AdaptationSet")):
                if adaptation.get("@mimeType") != "video/mp4":
                    continue
                for representation in _items(adaptation.get("Representation")):
                    try:
                        identifier = representation.get("@id")
                        if not isinstance(identifier, str) or not _FORMAT_ID.fullmatch(identifier):
                            continue
                        width = _dimension(representation.get("@width"))
                        height = _dimension(representation.get("@height"))
                        fps = str(representation.get("@frameRate", adaptation.get("@frameRate", "")))
                        if fps and not _FPS.fullmatch(fps):
                            continue
                        urls = representation.get("BaseURL")
                        if isinstance(urls, str):
                            urls = [urls]
                        if not isinstance(urls, list) or not urls:
                            continue
                        url = validate_media_url(urls[0])
                        if identifier in seen:
                            continue
                        seen.add(identifier)
                        formats.append({"id": identifier, "width": width, "height": height,
                                        "fps": fps, "url": url})
                    except (ValueError, TypeError):
                        continue
    return formats


def resolve_clip(clip_id):
    """Resolve a public clip into metadata and ephemeral signed MP4 variants."""
    try:
        if not isinstance(clip_id, str) or not _CLIP_ID.fullmatch(clip_id):
            raise ValueError()
        referer = "https://chzzk.naver.com/clips/" + clip_id
        detail = _get_json("https://api.chzzk.naver.com/service/v1/clips/" + clip_id + "/detail", referer)
        if detail.get("code") != 200:
            raise ValueError()
        content = detail["content"]
        if not isinstance(content, dict) or content.get("clipUID") != clip_id or _unavailable(content):
            raise ValueError()
        title, duration, video_id = content["clipTitle"], content["duration"], content["videoId"]
        if not isinstance(title, str) or not isinstance(video_id, str) or not video_id or len(video_id) > 200:
            raise ValueError()
        if isinstance(duration, bool):
            raise ValueError()
        duration = float(duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        params = {
            "seedMediaId": video_id, "seedType": "SPECIFIC", "serviceType": "CHZZK",
            "mediaType": "VOD", "panelType": "sdk_chzzk", "referer": referer,
            "recType": "CHZZK", "enableReverse": "false", "adAllowed": "Y", "deviceType": "html5_mo",
        }
        card = _get_json("https://api-videohub.naver.com/shortformhub/feeds/v9/card?" + urllib.parse.urlencode(params), referer)
        if ("code" in card and card["code"] != 200) or _unavailable(card):
            raise ValueError()
        formats = _formats(card)
        if not formats:
            raise ValueError()
        return {"title": title, "duration": duration, "formats": formats}
    except Exception:
        raise ClipUnavailableError(_ERROR) from None
