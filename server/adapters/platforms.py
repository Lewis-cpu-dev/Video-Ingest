"""Strict source identity normalization for the frozen two-platform URL scope."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode

from server.contracts.models import VideoSource
from server.errors import IngestError
from server.security.network import SafeHTTPClient, parse_network_url

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_BILIBILI_SHORT_HOSTS = {"b23.tv"}
_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_BILIBILI_ID = re.compile(r"(?:BV[1-9A-HJ-NP-Za-km-z]{10}|av[1-9][0-9]*)\Z")
_TIME = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?\Z")


def _unsupported(message: str, code="UNSUPPORTED_URL"):
    return IngestError(code, message, stage="resolving", next_action="provide_supported_video_url")


def _single(query, name):
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1 or not values[0]:
        raise _unsupported(f"The URL has an ambiguous or empty {name} parameter.")
    return values[0]


def _time_ms(value: str | None):
    if value is None:
        return None
    if len(value) > 32:
        raise _unsupported("The playback start time is invalid.")
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        seconds = float(value)
    else:
        match = _TIME.fullmatch(value)
        if not match or not any(match.groups()):
            raise _unsupported("The playback start time is invalid.")
        h, m, s = match.groups()
        seconds = int(h or 0) * 3600 + int(m or 0) * 60 + float(s or 0)
    if seconds > 7 * 24 * 3600:
        raise _unsupported("The playback start time exceeds the supported range.")
    return round(seconds * 1000)


def normalize_url(url: str, part: int | None = None, *, client: SafeHTTPClient | None = None) -> VideoSource:
    parsed = parse_network_url(url)
    host = parsed.hostname.lower()
    if part is not None and (isinstance(part, bool) or not isinstance(part, int) or not 1 <= part <= 10000):
        raise _unsupported("The part must be an integer from 1 through 10000.")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
        fragment = parse_qs(parsed.fragment, keep_blank_values=True, max_num_fields=16)
    except ValueError as exc:
        raise _unsupported("The URL contains too many parameters.") from exc
    time_values = [_single(query, key) for key in ("t", "start", "time_continue")]
    fragment_time = _single(fragment, "t")
    times = {_time_ms(v) for v in [*time_values, fragment_time] if v is not None}
    if len(times) > 1:
        raise _unsupported("The URL contains conflicting playback start times.")
    focus = next(iter(times), None)
    if host in _BILIBILI_SHORT_HOSTS:
        if not re.fullmatch(r"/[A-Za-z0-9]+/?", parsed.path):
            raise _unsupported("This Bilibili short-link path is unsupported.")
        resolved = (client or SafeHTTPClient()).resolve_redirect(url)
        destination = parse_network_url(resolved)
        if destination.hostname.lower() not in _BILIBILI_HOSTS:
            raise _unsupported("The Bilibili short link did not resolve to a supported video.")
        short_part = _single(query, "p")
        if short_part is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,4}", short_part) or int(short_part) > 10000:
                raise _unsupported("The Bilibili part parameter is invalid.")
            if part is not None and part != int(short_part):
                raise _unsupported("The requested part conflicts with the URL part.")
            part = int(short_part)
        source = normalize_url(resolved, part, client=client)
        if focus is not None:
            if source.focus_timestamp_ms is not None and source.focus_timestamp_ms != focus:
                raise _unsupported("The short link and destination contain conflicting playback start times.")
            source = source.model_copy(update={"focus_timestamp_ms": focus})
        return source
    if host in _YOUTUBE_HOSTS | _YOUTUBE_SHORT_HOSTS:
        if part not in (None, 1):
            raise _unsupported("YouTube source URLs do not support part selection.")
        if host in _YOUTUBE_SHORT_HOSTS:
            source_id = parsed.path.strip("/")
            if parsed.path not in {f"/{source_id}", f"/{source_id}/"}:
                raise _unsupported("This YouTube short-link path is unsupported.")
            if _single(query, "v") not in {None, source_id}:
                raise _unsupported("The URL contains conflicting video identifiers.")
        elif parsed.path in {"/watch", "/watch/"}:
            source_id = _single(query, "v")
            if source_id is None:
                raise _unsupported("Select one video; playlist-only URLs are not supported.")
        else:
            match = re.fullmatch(r"/(?:shorts|embed)/([^/]+)/?", parsed.path)
            if not match:
                raise _unsupported("Only YouTube watch, shorts, embed and youtu.be video URLs are supported.")
            source_id = match.group(1)
            if _single(query, "v") not in {None, source_id}:
                raise _unsupported("The URL contains conflicting video identifiers.")
        if not _YOUTUBE_ID.fullmatch(source_id or ""):
            raise _unsupported("The YouTube video identifier is invalid.")
        return VideoSource(platform="youtube", source_id=source_id, part_id="1",
                           canonical_url=f"https://www.youtube.com/watch?v={source_id}", focus_timestamp_ms=focus)
    if host in _BILIBILI_HOSTS:
        match = re.fullmatch(r"/video/([^/]+)/?", parsed.path)
        if not match or not _BILIBILI_ID.fullmatch(match.group(1)):
            raise _unsupported("Only Bilibili BV and av video URLs are supported.")
        source_id = match.group(1)
        url_part = _single(query, "p")
        if url_part is not None and (not re.fullmatch(r"[1-9][0-9]{0,4}", url_part) or int(url_part) > 10000):
            raise _unsupported("The Bilibili part parameter is invalid.")
        if part is not None and url_part is not None and part != int(url_part):
            raise _unsupported("The requested part conflicts with the URL part.")
        chosen_part = part or int(url_part or "1")
        return VideoSource(platform="bilibili", source_id=source_id, part_id=str(chosen_part),
                           canonical_url=f"https://www.bilibili.com/video/{source_id}?p={chosen_part}", focus_timestamp_ms=focus)
    raise _unsupported("Only the supported YouTube and Bilibili hostnames are accepted.", "UNSUPPORTED_PLATFORM")


def source_locator(source: VideoSource, timestamp_ms: int) -> str:
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms < 0:
        raise ValueError("timestamp_ms must be a nonnegative integer")
    # Integer seconds are accepted by both platforms; evidence retains precise ms.
    seconds = timestamp_ms // 1000
    if source.platform == "youtube":
        return "https://www.youtube.com/watch?" + urlencode({"v": source.source_id, "t": f"{seconds}s"})
    return f"https://www.bilibili.com/video/{source.source_id}?" + urlencode({"p": source.part_id, "t": seconds})
