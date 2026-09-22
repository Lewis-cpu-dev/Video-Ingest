"""MCP content delivery with explicit evidence bindings and bounded payloads."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent
from PIL import Image, UnidentifiedImageError

from server.config import PROJECT_ROOT
from server.contracts.models import ClientProfile
from server.errors import IngestError


class HostDeliveryAdapter:
    def __init__(self, profile: ClientProfile, max_bytes: int = 8 * 1024**2):
        self.profile = profile
        self.max_bytes = max_bytes

    @staticmethod
    def _clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: HostDeliveryAdapter._clean(item) for key, item in value.items()
                    if key not in {"path", "local_path", "output_path", "output_dir", "internal_path"}}
        if isinstance(value, (list, tuple)):
            return [HostDeliveryAdapter._clean(item) for item in value]
        if isinstance(value, Path):
            raise IngestError("UNSAFE_PATH", "Local paths cannot be delivered to a host.")
        return value

    def _payload(self, payload: dict) -> dict:
        result = self._clean(payload)
        result["host_delivery"] = {
            "profile": self.profile.name,
            "verification": self.profile.verification,
            "host_consumption_proven": self.profile.verification == "host_verified",
        }
        return result

    def _bounded(self, result: CallToolResult) -> CallToolResult:
        if len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) > self.max_bytes:
            raise IngestError("LIMIT_EXCEEDED", "Tool result exceeds the configured response budget.",
                              stage="delivery", next_action="reduce_range_or_page_size")
        return result

    def render_text(self, payload: dict) -> CallToolResult:
        if not self.profile.text:
            raise IngestError("CLIENT_MODALITY_UNSUPPORTED", "This client profile disables text.",
                              stage="delivery", next_action="configure_verified_client")
        clean = self._payload(payload)
        return self._bounded(CallToolResult(
            content=[TextContent(type="text", text=json.dumps(clean, ensure_ascii=False))],
            structuredContent=clean,
        ))

    def render_images(self, payload: dict, frames: list[tuple[dict, Path]]) -> CallToolResult:
        if not self.profile.images:
            raise IngestError("CLIENT_MODALITY_UNSUPPORTED", "This client profile disables images.",
                              stage="delivery", next_action="configure_verified_client")
        if len(frames) > 6:
            raise IngestError("LIMIT_EXCEEDED", "At most six images may be delivered per result.",
                              stage="delivery", next_action="reduce_range_or_page_size")
        clean = self._payload(payload)
        clean["frames"] = []
        content: list = [TextContent(type="text", text="")]
        running_bytes = 0
        for frame, path in frames:
            path = path.resolve()
            if not path.is_relative_to(PROJECT_ROOT):
                raise IngestError("UNSAFE_PATH", "Image evidence must remain within the workspace.",
                                  stage="delivery")
            if path.stat().st_size > self.max_bytes:
                raise IngestError("LIMIT_EXCEEDED", "An image exceeds the response budget.",
                                  stage="delivery", next_action="reduce_range_or_page_size")
            raw = path.read_bytes()
            running_bytes += 4 * ((len(raw) + 2) // 3)
            if running_bytes > self.max_bytes:
                raise IngestError("LIMIT_EXCEEDED", "Images exceed the response budget.",
                                  stage="delivery", next_action="reduce_range_or_page_size")
            try:
                with Image.open(io.BytesIO(raw)) as decoded:
                    mime_type = Image.MIME.get(decoded.format)
                    dimensions = decoded.size
                    decoded.verify()
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise IngestError("DECODE_FAILED", "Image evidence could not be validated.",
                                  stage="delivery", next_action="prepare_video") from exc
            if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise IngestError("DECODE_FAILED", "Image format is not supported for delivery.",
                                  stage="delivery", next_action="prepare_video")
            metadata = self._clean(frame)
            if any(metadata.get(key, val) != val for key, val in zip(("width", "height"), dimensions)):
                raise IngestError("DECODE_FAILED", "Frame dimensions do not match image bytes.",
                                  stage="delivery", next_action="prepare_video")
            metadata.update(width=dimensions[0], height=dimensions[1], image_content_index=len(content))
            clean["frames"].append(metadata)
            content.append(ImageContent(type="image", mimeType=mime_type,
                                        data=base64.b64encode(raw).decode("ascii")))
        content[0] = TextContent(type="text", text=json.dumps(clean, ensure_ascii=False))
        return self._bounded(CallToolResult(content=content, structuredContent=clean))

    def render_artifact_reference(self, payload: dict) -> CallToolResult:
        clean = self._payload(payload)
        resource = clean.get("resource")
        if not resource:
            return self.render_text(clean)
        uri = resource.get("uri", "")
        if urlsplit(uri).scheme not in {"video-ingest", "https"}:
            raise IngestError("UNSAFE_URL", "Artifact references must use an approved delivery scheme.",
                              stage="delivery")
        clean["host_delivery"]["file_resources_enabled"] = self.profile.file_resources
        clean["host_delivery"]["native_media_consumption_proven"] = False
        content: list = [TextContent(type="text", text=json.dumps(clean, ensure_ascii=False))]
        if self.profile.file_resources:
            content.append(ResourceLink(type="resource_link", uri=uri,
                                        name=resource.get("name", "media-artifact"),
                                        mimeType=resource.get("mimeType"), size=resource.get("size")))
        return self._bounded(CallToolResult(content=content, structuredContent=clean))
