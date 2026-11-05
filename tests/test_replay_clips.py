import copy
import io
import json
import unittest
import urllib.parse
from unittest.mock import patch

from docker_support.replay import clips


UID = "2fkqV67jY2"
URL = "https://g-example.pstatic.net/clip/video.mp4?__gda__=public-test-signature"


def detail():
    return {"code": 200, "content": {"clipUID": UID, "clipTitle": "Public clip", "duration": 20.5,
                                     "videoId": "A1B2C3", "adult": False}}


def card():
    representation = {"@id": "1080p-1", "@width": "1920", "@height": "1080", "@frameRate": "60000/1001", "BaseURL": [URL]}
    return {"session": {}, "card": {"content": {"vod": {"playback": {"MPD": [{"Period": [{"AdaptationSet": [
        {"@mimeType": "video/mp4", "Representation": [representation]},
        {"@mimeType": "audio/mp4", "Representation": [{"@id": "audio", "BaseURL": [URL]}]},
    ]}]}]}}}}, "feedAdData": None, "relatedHashtags": []}


def representations(value):
    return value["card"]["content"]["vod"]["playback"]["MPD"][0]["Period"][0]["AdaptationSet"][0]["Representation"]


class ResolverTests(unittest.TestCase):
    def resolve(self, first=None, second=None):
        with patch.object(clips, "_get_json", side_effect=[first if first is not None else detail(), second if second is not None else card()]) as get:
            result = clips.resolve_clip(UID)
            self.assertEqual(get.call_count, 2)
            request, referer = get.call_args.args
            parsed = urllib.parse.urlsplit(request)
            self.assertEqual(parsed.hostname, "api-videohub.naver.com")
            params = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(params["seedMediaId"], ["A1B2C3"])
            self.assertEqual(params["referer"], ["https://chzzk.naver.com/clips/" + UID])
            self.assertEqual(params["deviceType"], ["html5_mo"])
            self.assertEqual(referer, params["referer"][0])
            return result

    def assert_unavailable(self, first=None, second=None):
        with patch.object(clips, "_get_json", side_effect=[first if first is not None else detail(), second if second is not None else card()]):
            with self.assertRaises(clips.ClipUnavailableError) as error:
                clips.resolve_clip(UID)
        self.assertEqual(str(error.exception), "This public clip is unavailable.")
        self.assertTrue(error.exception.__suppress_context__)

    def test_public_formats(self):
        result = self.resolve()
        self.assertEqual(result, {"title": "Public clip", "duration": 20.5, "formats": [
            {"id": "1080p-1", "width": 1920, "height": 1080, "fps": "60000/1001", "url": URL}]})

    def test_mpd_single_objects_and_baseurl_string(self):
        value = card()
        mpd = value["card"]["content"]["vod"]["playback"]["MPD"][0]
        period = mpd["Period"][0]
        adaptation = period["AdaptationSet"][0]
        representation = adaptation["Representation"][0]
        representation["BaseURL"] = URL
        adaptation["Representation"] = representation
        period["AdaptationSet"] = adaptation
        mpd["Period"] = period
        value["card"]["content"]["vod"]["playback"]["MPD"] = mpd
        self.assertEqual(len(self.resolve(second=value)["formats"]), 1)

    def test_invalid_ids_no_network(self):
        with patch.object(clips, "_get_json") as get:
            for value in [None, 1, "", "a" * 101, "abc/def", "abc?x=1", "abc\n", "클립"]:
                with self.subTest(value=value), self.assertRaises(clips.ClipUnavailableError):
                    clips.resolve_clip(value)
            get.assert_not_called()

    def test_identity_required(self):
        for uid in [None, "other"]:
            value = detail()
            value["content"]["clipUID"] = uid
            self.assert_unavailable(first=value)

    def test_unavailable_and_adult(self):
        for key, value in [("playable", False), ("adult", True), ("private", True), ("isPrivate", True), ("available", False), ("visibility", "PRIVATE")]:
            with self.subTest(key=key):
                first = detail()
                first["content"][key] = value
                self.assert_unavailable(first=first)
        value = card()
        value["card"]["content"]["vod"]["playable"] = False
        self.assert_unavailable(second=value)

    def test_provider_errors(self):
        self.assert_unavailable(first={"code": 403, "message": "not public", "content": None})
        self.assert_unavailable(second={"code": 403, "content": None})

    def test_bad_metadata(self):
        for key, value in [("duration", 0), ("duration", float("nan")), ("duration", float("inf")), ("duration", True), ("clipTitle", None), ("videoId", "")]:
            with self.subTest(key=key, value=value):
                first = detail()
                first["content"][key] = value
                self.assert_unavailable(first=first)

    def test_empty_and_non_mpd_formats(self):
        self.assert_unavailable(second={"card": {"content": {"vod": {"playback": {"MPD": []}}}}})
        self.assert_unavailable(second={"content": {"BaseURL": URL}})

    def test_invalid_representations(self):
        for key, value in [("@id", "unsafe/id"), ("@height", 0), ("@width", -1), ("@frameRate", "1/0"), ("BaseURL", ["https://example.com/file.mp4"])]:
            with self.subTest(key=key):
                second = card()
                representations(second)[0][key] = value
                self.assert_unavailable(second=second)

    def test_safe_variants_only_and_duplicate_ids(self):
        second = card()
        variants = representations(second)
        variants.append(copy.deepcopy(variants[0]))
        invalid = copy.deepcopy(variants[0])
        invalid["@id"] = "unsafe"
        invalid["BaseURL"] = ["https://127.0.0.1/file.mp4"]
        variants.append(invalid)
        self.assertEqual(len(self.resolve(second=second)["formats"]), 1)


class UrlTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(clips.validate_media_url(URL), URL)

    def test_reject_unsafe_urls(self):
        for url in [None, "", "http://g.pstatic.net/a.mp4", "https://g.pstatic.net:443/a.mp4",
                    "https://user@g.pstatic.net/a.mp4", "https://user:pass@g.pstatic.net/a.mp4",
                    "https://127.0.0.1/a.mp4", "https://[::1]/a.mp4", "https://pstatic.net.evil.com/a.mp4",
                    "https://evilpstatic.net/a.mp4", "https://g.pstatic.net./a.mp4", "https://g.pstatic.net/a.m3u8",
                    "https://g.pstatic.net/a.mp4#fragment", "https://g.pstatic.net/a.mp4\n",
                    "https://g.pstatic.net\\@evil.com/a.mp4", "https://g.pstatic.net:/a.mp4",
                    "//g.pstatic.net/a.mp4", "https://g.pstatic.net/a.mp4?x=\x00"]:
            with self.subTest(url=url), self.assertRaises(clips.ClipUnavailableError):
                clips.validate_media_url(url)


class Response(io.BytesIO):
    def __init__(self, payload, length=None, status=200):
        super().__init__(payload)
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.status = status


class FetchTests(unittest.TestCase):
    def get(self, response):
        with patch.object(clips.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            result = clips._get_json("https://api.chzzk.naver.com/path", "https://chzzk.naver.com/")
            args, kwargs = build.return_value.open.call_args
            self.assertEqual(kwargs["timeout"], 20)
            self.assertNotIn("Cookie", args[0].headers)
            return result

    def test_json(self):
        self.assertEqual(self.get(Response(b'{"code":200}')), {"code": 200})

    def test_size_caps_declared_and_actual(self):
        for response in [Response(b"{}", clips.MAX_PAYLOAD + 1), Response(b" " * (clips.MAX_PAYLOAD + 1)), Response(b"{}", -1)]:
            with self.subTest(), self.assertRaises(clips.ClipUnavailableError):
                self.get(response)

    def test_invalid_json_status_and_shape(self):
        for response in [Response(b"not json"), Response(b"[]"), Response(b"{}", status=403)]:
            with self.subTest(), self.assertRaises(clips.ClipUnavailableError):
                self.get(response)

    def test_network_errors_redacted(self):
        with patch.object(clips.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = OSError("secret-signed-url")
            with self.assertRaises(clips.ClipUnavailableError) as error:
                clips._get_json("https://api.chzzk.naver.com/path", "https://chzzk.naver.com/")
            self.assertNotIn("secret", str(error.exception))
            self.assertTrue(error.exception.__suppress_context__)

    def test_redirects_rejected(self):
        with self.assertRaises(clips.ClipUnavailableError):
            clips._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.com/")


if __name__ == "__main__":
    unittest.main()
