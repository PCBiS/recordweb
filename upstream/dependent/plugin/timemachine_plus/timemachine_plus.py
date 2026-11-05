"""
$description South Korean live-streaming platform for gaming, entertainment, and other creative content. Owned by Naver.
$url chzzk.naver.com
$type live, vod
"""

import logging
import re
import time

from streamlink.exceptions import StreamError
from streamlink.plugin import Plugin, pluginmatcher
from streamlink.plugin.api import validate
from streamlink.stream.dash import DASHStream
from streamlink.stream.hls import HLSStream, HLSStreamReader, HLSStreamWorker, parse_m3u8


log = logging.getLogger(__name__)


class ChzzkHLSStreamWorker(HLSStreamWorker):
    stream: "ChzzkHLSStream"

    def _fetch_playlist(self):
        if self.stream._should_refresh():
            self.stream.refresh_playlist()
        try:
            return super()._fetch_playlist()
        except StreamError as err:
            if err.err and err.err.response and err.err.response.status_code >= 400:
                log.warning(f"Force-reloading the channel playlist on error: {err}")
                self.stream.refresh_playlist()
            raise err


class ChzzkHLSStreamReader(HLSStreamReader):
    __worker__ = ChzzkHLSStreamWorker


class ChzzkHLSStream(HLSStream):
    __shortname__ = "hls-chzzk"
    __reader__ = ChzzkHLSStreamReader

    _REFRESH_INTERVAL = 5 * 60 * 60
    _last_refresh_time = 0

    def __init__(self, session, url, channel_id, live_id,
                 quality_key=None, master_url=None,  
                 *args, **kwargs):

        super().__init__(session, url, *args, **kwargs)
        self._url = url  
        self._channel_id = channel_id
        self._live_id = live_id
        self._api = ChzzkAPI(session)
        self._quality_key = quality_key   
        self._master_url = master_url   
        self._last_refresh_time = time.time()

    def refresh_playlist(self):
        log.info("Refreshing stream URL ...")

        max_retries = 3
        wait_secs = 3
        last_err = None

        for attempt in range(1, max_retries + 1):
            try:
                datatype, data = self._api.get_time_machine(self._live_id)
                if datatype == "error":
                    raise StreamError(data)
                    
                if isinstance(data, dict):
                    media_items = data.get("media", [])
                else:
                    media_items = data or []

                if not media_items:
                    raise StreamError("No media info from time_machine API")
                break
            except StreamError as err:
                last_err = err
                if attempt < max_retries:
                    log.warning(f"Time-machine API failed (attempt {attempt}/{max_retries}), retrying in {wait_secs}s: {err}")
                    time.sleep(wait_secs)
                else:
                    raise err

        new_master_url = None

        # 1) mediaId: HLS 우선
        for media in media_items:
            media_id, media_protocol, media_path = media[:3]
            if str(media_id).upper() == "HLS":
                new_master_url = media_path
                break

        # 2) fallback
        if not new_master_url:
            for media in media_items:
                media_id, media_protocol, media_path = media[:3]
                if str(media_protocol).upper() == "HLS":
                    new_master_url = media_path
                    break

        if not new_master_url:
            raise StreamError("No HLS stream found in time_machine API")

        streams_dict = HLSStream.parse_variant_playlist(self.session, new_master_url)
        if not streams_dict:
            raise StreamError("No variant streams found in new master playlist")

        selected_url = None
        if self._quality_key and self._quality_key in streams_dict:
            selected_url = streams_dict[self._quality_key].url
        else:
            best_stream = None
            best_bw = 0
            for k, s in streams_dict.items():
                bw = s.stream_name.bandwidth or 0
                if bw > best_bw:
                    best_bw = bw
                    best_stream = s
            if best_stream:
                selected_url = best_stream.url

        if not selected_url:
            raise StreamError(f"Unable to find matching quality {self._quality_key}, no fallback")

        self._url = selected_url
        self._master_url = new_master_url
        self._last_refresh_time = time.time()

        log.info(f"Refreshed stream URL to final media URL: {self._url}")

    def _should_refresh(self):
        now = time.time()
        return (now - self._last_refresh_time) >= self._REFRESH_INTERVAL

    @property
    def url(self):
        return self._url


class ChzzkAPI:
    _CHANNELS_LIVE_DETAIL_URL = "https://api.chzzk.naver.com/service/v3/channels/{channel_id}/live-detail"
    _VIDEOS_URL = "https://api.chzzk.naver.com/service/v2/videos/{video_id}"
    _TIME_MACHINE_URL = "https://api.chzzk.naver.com/service/v1/live/{live_id}/playback/time-machine"

    def __init__(self, session):
        self._session = session

    def _query_api(self, url, *schemas):
        response = self._session.http.get(
            url,
            acceptable_status=(200, 404),
            schema=validate.Schema(
                validate.parse_json(),
                validate.any(
                    validate.all(
                        {
                            "code": int,
                            "message": str,
                        },
                        validate.transform(lambda data: ("error", data["message"])),
                    ),
                    validate.all(
                        {
                            "code": 200,
                            "content": dict,
                        },
                        validate.get("content"),
                        *schemas,
                        validate.transform(lambda data: ("success", data)),
                    ),
                ),
            ),
        )
        return response

    def get_live_detail(self, channel_id):
        return self._query_api(
            self._CHANNELS_LIVE_DETAIL_URL.format(channel_id=channel_id),
            {
                "status": str,
                "liveId": int,
                "liveTitle": validate.any(str, None),
                "liveCategory": validate.any(str, None),
                "adult": bool,
                "channel": validate.all(
                    {"channelName": str},
                    validate.get("channelName"),
                ),
                "livePlaybackJson": validate.any(
                    None,
                    validate.all(
                        str,
                        validate.parse_json(),
                        {
                            "media": [
                                validate.all(
                                    {
                                        "mediaId": str,
                                        "protocol": str,
                                        "path": validate.url(),
                                    },
                                    validate.union_get(
                                        "mediaId",
                                        "protocol",
                                        "path",
                                    ),
                                ),
                            ],
                        },
                        validate.get("media"),
                    ),
                ),
            },
            validate.union_get(
                "livePlaybackJson",
                "status",
                "liveId",
                "channel",
                "liveCategory",
                "liveTitle",
                "adult",
            ),
        )

    def get_time_machine(self, live_id):
        return self._query_api(
            self._TIME_MACHINE_URL.format(live_id=live_id),
            {
                "playback": {
                    "media": [
                        validate.all(
                            {
                                "mediaId": str,
                                "protocol": str,
                                "path": validate.url(),
                            },
                            validate.union_get(
                                "mediaId",
                                "protocol",
                                "path",
                            ),
                        ),
                    ],
                },
            },
            validate.get("playback", "media"),
        )


    def get_videos(self, video_id):
        return self._query_api(
            self._VIDEOS_URL.format(video_id=video_id),
            {
                "inKey": validate.any(str, None),
                "videoId": validate.any(str, None),
                "videoTitle": validate.any(str, None),
                "videoCategory": validate.any(str, None),
                "adult": bool,
                "channel": validate.all(
                    {"channelName": str},
                    validate.get("channelName"),
                ),
            },
            validate.union_get(
                "inKey",
                "videoId",
                "channel",
                "videoCategory",
                "videoTitle",
                "adult",
            ),
        )


@pluginmatcher(name="live", pattern=re.compile(r"https?://chzzk\.naver\.com/live/(?P<channel_id>[^/?]+)"))
@pluginmatcher(name="video", pattern=re.compile(r"https?://chzzk\.naver\.com/video/(?P<video_id>[^/?]+)"))
class TimemachinePlus(Plugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._api = ChzzkAPI(self.session)

    def _get_live(self, channel_id):
        datatype, data = self._api.get_live_detail(channel_id)
        if datatype == "error":
            log.error(data)
            return

        media_list, status, live_id, author, category, title, adult = data
        if status != "OPEN":
            log.info("The stream is unavailable")
            return
        if not media_list:
            if adult:
                log.info("This stream is for adults only.")
            else:
                log.info("This stream is unavailable.")
            return

        # 1) 타임머신 API
        datatype, data = self._api.get_time_machine(live_id)
        if datatype == "error":
            log.error(data)
            return

        if isinstance(data, dict):
            media_items = data.get("media", []) 
        else:
            media_items = data or []

        master_url = None

        # 1) mediaId: HLS 우선 선택
        for media in media_items:
            media_id, media_protocol, media_path = media[:3]
            if str(media_id).upper() == "HLS":
                master_url = media_path
                break

        # 2) fallback: 그래도 없으면 HLS 중 첫번째
        if not master_url:
            for media in media_items:
                media_id, media_protocol, media_path = media[:3]
                if str(media_protocol).upper() == "HLS":
                    master_url = media_path
                    break

        if not master_url:
            log.error("No HLS stream found in time_machine API")
            return


        variant_streams = HLSStream.parse_variant_playlist(self.session, master_url)
        results = {}
        for quality_name, hls_stream_obj in variant_streams.items():
            new_stream = ChzzkHLSStream(
                self.session,
                url=hls_stream_obj.url, 
                channel_id=channel_id,
                live_id=live_id,
                quality_key=quality_name,  
                master_url=master_url     
            )

            results[quality_name] = new_stream

        return results


    def _get_video(self, video_id):
        datatype, data = self._api.get_videos(video_id)
        if datatype == "error":
            log.error(data)
            return

        self.id = video_id
        in_key, vod_id, self.author, self.category, self.title, adult = data

        if in_key is None or vod_id is None:
            if adult:
                log.error(
                    "This video is for adults only.",
                )
            else:
                log.error("This video is unavailable")
            return

        for name, stream in DASHStream.parse_manifest(
            self.session,
            self._API_VOD_PLAYBACK_URL.format(video_id=vod_id, in_key=in_key),
            headers={"Accept": "application/dash+xml"},
        ).items():
            if stream.video_representation.mimeType == "video/mp2t":
                yield name, stream

    def _get_streams(self):
        if self.matches["live"]:
            return self._get_live(self.match["channel_id"])
        elif self.matches["video"]:
            return self._get_video(self.match["video_id"])


__plugin__ = TimemachinePlus