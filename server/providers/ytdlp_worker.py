"""Worker entry point: no direct network, plugins, arbitrary extractors or external downloaders."""
from __future__ import annotations

import functools
import json
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from server.contracts.models import CaptionTrack, VideoMetadata, VideoSource
from server.errors import IngestError
from server.processing.process import workspace_path
from server.providers.js_runtime import allowed_runtime_command, install_restricted_ejs, provisioned_deno


def _network_lock(proxy_url: str) -> None:
    proxy = urlsplit(proxy_url)
    allowed = ("127.0.0.1", proxy.port)
    if proxy.scheme != "http" or proxy.hostname != allowed[0] or proxy.port is None:
        raise IngestError("UNSAFE_URL", "Invalid internal egress proxy.")
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock, address):
        if not isinstance(address, tuple) or address[:2] != allowed:
            raise IngestError("UNSAFE_URL", "The downloader attempted to bypass controlled egress.")
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not isinstance(address, tuple) or address[:2] != allowed:
            raise IngestError("UNSAFE_URL", "The downloader attempted to bypass controlled egress.")
        return original_connect_ex(sock, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex

    def audit(event, args):
        if event == "subprocess.Popen":
            if allowed_runtime_command(args[0], args[1]):
                return
            raise IngestError("UNSAFE_EXECUTION", "Downloader child commands are disabled.")
        if event in {"os.system", "os.exec", "os.posix_spawn"}:
            raise IngestError("UNSAFE_EXECUTION", "Downloader child commands are disabled.")
        if event == "socket.getaddrinfo" and args[0] != allowed[0]:
            raise IngestError("UNSAFE_URL", "Downloader DNS must be resolved by controlled egress.")
        if event == "socket.connect" and (not isinstance(args[1], tuple) or args[1][:2] != allowed):
            raise IngestError("UNSAFE_URL", "The downloader attempted to bypass controlled egress.")

    sys.addaudithook(audit)


class SilentLogger:
    def __init__(self):
        self.warnings = []
        self.preview_only = False
        self.failures = []

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        lowered = message.lower()
        if "only the preview" in lowered:
            self.preview_only = True
        if any(term in lowered for term in ("caption", "subtitle")) and any(
                term in lowered for term in ("skip", "failed", "unable", "requires", "only available")):
            self.warnings.append("CAPTION_DISCOVERY_FAILED")
        if "javascript runtime" in lowered or "js runtime" in lowered:
            self.warnings.append("EXTRACTOR_CAPABILITIES_LIMITED")
        classified = _failure(Exception(message), "resolving")
        if classified.code != "PROVIDER_FAILED":
            self.failures.append(classified)

    def error(self, message):
        self.failures.append(_failure(Exception(message), "resolving"))


def metadata_from_info(info: dict) -> VideoMetadata:
    tracks = []
    for field, provenance in (("subtitles", "human_caption"), ("automatic_captions", "platform_auto_caption")):
        for language, options in (info.get(field) or {}).items():
            if language in {"danmaku", "live_chat"}:
                continue
            for item in options:
                url = item.get("url", "")
                # Machine translations are not original-language captions.
                if not url or "tlang" in parse_qs(urlsplit(url).query):
                    continue
                original_language = language.removesuffix("-orig")
                original_provenance = item.get("provenance", provenance)
                if original_language.startswith("ai-"):
                    original_language = original_language[3:]
                    original_provenance = "platform_auto_caption"
                tracks.append(CaptionTrack(language=original_language, provenance=original_provenance,
                                           format=item.get("ext", "unknown"), url=url))
    duration = info.get("duration")
    return VideoMetadata(title=str(info.get("title") or "Untitled video")[:2000],
                         duration_ms=round(float(duration) * 1000) if duration is not None else None,
                         caption_tracks=tracks, media_available=bool(info.get("formats") or info.get("url")),
                         is_live=bool(info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")),
                         warnings=["CAPTION_CONTENT_UNVERIFIED"] if tracks else [])


def _failure(exc, stage):
    if isinstance(exc, IngestError):
        return exc
    message = str(exc).lower()
    if "429" in message or "too many requests" in message:
        return IngestError("RATE_LIMITED", "The video platform rate limited this request.", stage, True,
                           "retry_later", 60_000)
    if any(term in message for term in ("sign in", "log in", "login required", "private video")):
        return IngestError("AUTH_REQUIRED", "The video platform requires authorization.", stage,
                           next_action="use_public_source")
    if "403" in message or "forbidden" in message:
        return IngestError("ACCESS_DENIED", "The video platform refused access.", stage,
                           next_action="check_source")
    if any(term in message for term in ("not available", "unavailable", "removed", "404")):
        return IngestError("CONTENT_UNAVAILABLE", "The requested platform content is unavailable.", stage,
                           next_action="check_source")
    return IngestError("PROVIDER_FAILED", "The platform extractor could not complete this request.",
                       stage, next_action="check_source_or_deployment")


def perform(payload):
    import yt_dlp
    from yt_dlp import globals as ytdlp_globals
    from yt_dlp.extractor.bilibili import BiliBiliIE
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.networking._urllib import UrllibRH

    ytdlp_globals.plugin_dirs.value = []
    ytdlp_globals.all_plugins_loaded.value = True

    class RestrictedYoutubeDL(yt_dlp.YoutubeDL):
        @functools.cached_property
        def _request_director(self):
            return self.build_request_director([UrllibRH])

    class BilibiliWithOriginalCaptions(BiliBiliIE):
        """Discover URLs only; upstream normally fetches caption bodies and discards their URLs."""
        caption_warning = None

        def extract_subtitles(self, video_id, cid, aid=None):
            try:
                result = self._download_json(
                    "https://api.bilibili.com/x/player/wbi/v2", video_id,
                    query={"aid": aid, "cid": cid} if aid else {"bvid": video_id, "cid": cid},
                    headers=self._HEADERS)
                if result.get("code") != 0 or not isinstance(result.get("data"), dict):
                    self.caption_warning = "CAPTION_DISCOVERY_FAILED"
                    return {}
                data = result["data"]
                if data.get("need_login_subtitle"):
                    self.caption_warning = "CAPTION_AUTH_REQUIRED"
                subtitles = {}
                for item in (data.get("subtitle") or {}).get("subtitles", []):
                    if not item.get("lan") or not item.get("subtitle_url"):
                        continue
                    url = item["subtitle_url"]
                    if url.startswith("//"):
                        url = "https:" + url
                    automatic = item["lan"].startswith("ai-") or bool(item.get("ai_status"))
                    subtitles.setdefault(item["lan"], []).append({
                        "url": url, "ext": "json",
                        "provenance": "platform_auto_caption" if automatic else "human_caption"})
                return subtitles
            except Exception:  # noqa: BLE001 - retain media availability when caption discovery fails.
                self.caption_warning = "CAPTION_DISCOVERY_FAILED"
                return {}

    source = VideoSource.model_validate(payload["source"])
    operation = payload["operation"]
    deno = provisioned_deno()
    if deno:
        install_restricted_ejs()
    _network_lock(payload["proxy"])
    logger = SilentLogger()
    options = {
        "quiet": True, "no_warnings": False, "logger": logger, "noplaylist": True,
        "cachedir": False, "cookiefile": None, "cookiesfrombrowser": None, "usenetrc": False,
        "proxy": payload["proxy"], "geo_verification_proxy": payload["proxy"],
        "socket_timeout": 15, "retries": 2, "fragment_retries": 2, "extractor_retries": 2,
        "retry_sleep_functions": {"http": lambda attempt: min(2**attempt, 4),
                                  "fragment": lambda attempt: min(2**attempt, 4)},
        "concurrent_fragment_downloads": 1, "hls_prefer_native": True, "external_downloader": None,
        "skip_unavailable_fragments": False, "allow_unplayable_formats": False,
        "enable_file_urls": False, "geo_bypass": False,
        "js_runtimes": {"deno": {"path": str(deno)}} if deno else {}, "remote_components": [],
        "postprocessors": [], "writethumbnail": False, "writeinfojson": False,
        "overwrites": True, "continuedl": False, "noprogress": True, "fixup": "never",
        "format": "best/bestvideo+bestaudio", "check_formats": False,
        "ignore_no_formats_error": True,
    }
    with RestrictedYoutubeDL(options, auto_init=False) as downloader:
        extractor = YoutubeIE() if source.platform == "youtube" else BilibiliWithOriginalCaptions()
        downloader.add_info_extractor(extractor)
        info = downloader.extract_info(source.canonical_url, download=False)
        if not info or info.get("_type") in ("playlist", "multi_video"):
            raise IngestError("UNSUPPORTED_URL", "Select one specific video part.")
        metadata = metadata_from_info(info)
        metadata.warnings.extend(sorted(set(logger.warnings)))
        if getattr(extractor, "caption_warning", None):
            metadata.warnings.append(extractor.caption_warning)
        if not metadata.media_available and not metadata.caption_tracks:
            if logger.failures:
                raise logger.failures[-1]
            if metadata.duration_ms is None:
                raise IngestError("MEDIA_UNAVAILABLE", "The platform did not expose usable video or caption metadata.",
                                  "resolving", next_action="check_source_or_deployment")
            # A restricted player response cannot prove that the source has no original captions.
            metadata.warnings.append("CAPTION_DISCOVERY_FAILED")
        if logger.preview_only:
            raise IngestError("AUTH_REQUIRED", "Only an unauthorized preview is available.",
                              next_action="use_public_source")
        if metadata.is_live:
            raise IngestError("UNSUPPORTED_URL", "Live and upcoming streams are outside the supported scope.")
        if metadata.duration_ms is not None and metadata.duration_ms > payload["max_duration_ms"]:
            raise IngestError("LIMIT_EXCEEDED", "The video exceeds the server duration limit.")
        if operation == "inspect":
            return {"metadata": metadata.model_dump()}
        output_dir = workspace_path(Path(payload["output_dir"]))
        formats = info.get("formats") or [info]
        protocols = {"http", "https", "http_dash_segments", "m3u8_native"}
        usable = [fmt for fmt in formats if fmt.get("protocol") in protocols and not fmt.get("has_drm")]
        audios = [fmt for fmt in usable if fmt.get("acodec") not in (None, "none")]
        if payload["kind"] == "audio":
            choices = [fmt for fmt in audios if fmt.get("vcodec") == "none"] or audios
            selected = choices[-1:]  # yt-dlp sorts by increasing quality.
        else:
            height = 1080 if payload["quality"] == "detail" else 720
            videos = [fmt for fmt in usable if fmt.get("vcodec") not in (None, "none")
                      and (fmt.get("height") or 0) <= height]
            selected = videos[-1:]
            if selected and selected[0].get("acodec") in (None, "none"):
                audio_only = [fmt for fmt in audios if fmt.get("vcodec") == "none"]
                selected += audio_only[-1:]
        if not selected:
            raise IngestError("MEDIA_UNAVAILABLE", "No supported public media stream is available.",
                              "downloading", next_action="check_source_or_deployment")
        paths = []
        from yt_dlp.downloader import get_suitable_downloader

        for index, stream in enumerate(selected):
            ext = stream.get("ext", "bin")
            if ext not in {"mp4", "m4a", "webm", "mkv", "mp3", "ogg", "opus", "flac", "wav", "ts"}:
                raise IngestError("MEDIA_UNAVAILABLE", "The selected media container is unsupported.", "downloading")
            file = output_dir / f"stream-{index}.{ext}"
            stream_info = {**info, **stream}
            stream_info.pop("requested_formats", None)
            stream_info.pop("requested_downloads", None)
            implementation = get_suitable_downloader(stream_info, options)
            if implementation.FD_NAME not in {"http", "dashsegments", "hlsnative"}:
                raise IngestError("UNSAFE_URL", "An external downloader was requested and blocked.", "downloading")
            success, _ = downloader.dl(str(file), stream_info)
            if not success or not file.is_file():
                raise IngestError("MEDIA_UNAVAILABLE", "The media transfer did not complete.", "downloading")
            paths.append(str(file))
        return {"paths": paths}


def main():
    payload = json.loads(sys.stdin.buffer.read(1024**2))
    try:
        result = perform(payload)
    except Exception as exc:  # noqa: BLE001 - isolated upstream failures become redacted provider errors.
        stage = "resolving" if payload.get("operation") == "inspect" else "downloading"
        result = {"error": _failure(exc, stage).as_dict()}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
