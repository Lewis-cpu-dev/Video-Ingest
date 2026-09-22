"""Parse original timed platform captions without inferring content from metadata."""
from __future__ import annotations

import html
import json
import re
from xml.etree import ElementTree

from server.contracts.models import CaptionTrack, TranscriptSegment
from server.errors import IngestError


def _text(value: str, markup: bool) -> str:
    if not isinstance(value, str):
        raise TypeError("Caption text must be a string")
    if markup:
        # Strip only cue-formatting tags; literal formulas such as x < y and z > 1 are evidence.
        tags = (r"</?(?:b|i|u|ruby|rt)>|</?c(?:[.\s][^>]*)?>|"
                r"</?(?:v|lang)(?:\s[^>]*)?>|<\d{1,2}:\d{2}(?::\d{2})?\.\d{3}>")
        value = html.unescape(re.sub(tags, "", value, flags=re.IGNORECASE))
    return value.strip()


def _time(value: str) -> int:
    parts = value.replace(",", ".").split(":")
    seconds = sum(float(part) * 60**index for index, part in enumerate(reversed(parts)))
    return round(seconds * 1000)


def parse_captions(payload: bytes, track: CaptionTrack) -> list[TranscriptSegment]:
    entries = []
    try:
        raw = payload.decode("utf-8-sig")
        if track.format in ("json3", "json"):
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Caption response must be an object")
            if isinstance(data.get("body"), list):  # Bilibili original subtitle JSON.
                entries = [(round(float(item["from"]) * 1000), round(float(item["to"]) * 1000),
                            item["content"]) for item in data["body"]]
            else:
                for event in data.get("events", []):
                    text = "".join(seg.get("utf8", "") for seg in event.get("segs", []))
                    start = int(event.get("tStartMs", 0))
                    entries.append((start, start + int(event.get("dDurationMs", 0)), text))
        elif track.format in ("vtt", "srt"):
            blocks = re.split(r"\r?\n\s*\r?\n", raw)
            for block in blocks:
                lines = block.splitlines()
                for index, line in enumerate(lines):
                    match = re.match(r"\s*([\d:.,]+)\s+-->\s+([\d:.,]+)", line)
                    if match:
                        entries.append((_time(match[1]), _time(match[2]), "\n".join(lines[index + 1:])))
                        break
        elif track.format in ("srv1", "srv2", "srv3", "ttml", "xml"):
            # DTD/entities are not required for platform subtitles; refuse expansion payloads.
            if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
                raise ValueError("DTD is unsupported")
            root = ElementTree.fromstring(raw)
            for item in root.iter():
                tag = item.tag.rsplit("}", 1)[-1]
                if tag == "text" and "start" in item.attrib:
                    start = round(float(item.attrib["start"]) * 1000)
                    end = start + round(float(item.attrib.get("dur", 0)) * 1000)
                elif tag == "p" and "t" in item.attrib:
                    start = int(item.attrib["t"])
                    end = start + int(item.attrib.get("d", 0))
                elif tag == "p" and "begin" in item.attrib:
                    start = _time(item.attrib["begin"])
                    end = _time(item.attrib["end"])
                else:
                    continue
                entries.append((start, end, "".join(item.itertext())))
        else:
            raise ValueError("Unsupported caption format")
        segments = []
        for start, end, value in sorted(entries, key=lambda entry: entry[0]):
            value = _text(value, markup=track.format in ("vtt", "srt", "srv1", "srv2", "srv3", "ttml", "xml"))
            if not value:
                continue
            if start < 0 or end < start:
                raise ValueError("Invalid caption timestamp")
            if segments and segments[-1].text == value and start <= segments[-1].end_ms:
                segments[-1].end_ms = max(end, segments[-1].end_ms)
                continue
            segments.append(TranscriptSegment(segment_id=f"seg_{len(segments):06d}", start_ms=start,
                                              end_ms=end, text=value, language=track.language,
                                              provenance=track.provenance))
        if not segments:
            raise ValueError("Caption response contained no readable timed segments")
        return segments
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError,
            UnicodeDecodeError, ElementTree.ParseError) as exc:
        raise IngestError("CAPTION_FETCH_FAILED", "The available caption track could not be parsed.",
                          "fetching_captions", next_action="retry_or_request_asr") from exc


def choose_caption(tracks: list[CaptionTrack], language: str | None) -> CaptionTrack:
    supported = {"json3": 0, "json": 1, "vtt": 2, "srt": 3, "srv3": 4, "srv1": 5,
                 "srv2": 6, "ttml": 7, "xml": 8}
    eligible = tracks
    if language:
        exact = [t for t in tracks if t.language.lower() == language.lower()]
        base = [t for t in tracks if t.language.lower().split("-")[0] == language.lower().split("-")[0]]
        eligible = exact or base
    if not eligible:
        raise IngestError("NO_CAPTIONS", "No original caption track matches the requested language.",
                          "fetching_captions", next_action="prepare_asr")
    readable = [t for t in eligible if t.format in supported]
    if not readable:
        raise IngestError("CAPTION_FETCH_FAILED", "Available captions use an unsupported format.",
                          "fetching_captions", next_action="retry_or_request_asr")
    return min(readable, key=lambda t: (t.provenance != "human_caption", supported[t.format]))
