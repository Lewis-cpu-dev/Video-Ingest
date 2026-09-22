# Internal implementation contract

This file coordinates independent implementation. All code/data/caches stay inside this workspace.
Errors use `server.errors.IngestError`; shared Pydantic models live in `server.contracts/models.py`.

Platform API: `normalize_url(url: str, part: int | None = None) -> VideoSource`; short links may
use a separately guarded resolver. `source_locator(source, timestamp_ms) -> str`.
Security API: `SafeHTTPClient.get_bytes(url, max_bytes, headers=None) -> bytes`,
`SafeHTTPClient.resolve_redirect(url) -> str`. Network destinations must be checked and pinned.

Provider API (synchronous, called in one background worker):
`YtDlpProvider(settings)` with `inspect(source) -> VideoMetadata`,
`fetch_captions(source, metadata, language, cancelled) -> (list[TranscriptSegment], str)` (language),
`acquire(source, kind, output_dir: Path, quality, cancelled, on_bytes) -> Path`.
`cancelled` is a zero-argument bool callback; `on_bytes(delta: int)` charges cumulative actual bytes
including failed downloads, and may raise an IngestError. No title-based content synthesis.

Processing API: `MediaProcessor(settings)` with `probe(path) -> dict` (duration_ms, width, height),
`extract_audio(path, output_path, time_range=None, cancelled=lambda: False) -> Path`,
`extract_frames(path, output_dir, time_range, max_frames, quality, cancelled=lambda: False)
 -> list[dict]` containing path, timestamp_ms, width, height, selection_reason,
`make_clip(path, output_path, time_range, cancelled=lambda: False) -> Path`.
`FasterWhisperASR(settings).transcribe(audio_path, language, offset_ms, cancelled)
 -> list[TranscriptSegment]`; model files must be explicitly provisioned locally.

Host delivery: `HostDeliveryAdapter(profile, max_bytes=...)` returns MCP `CallToolResult`;
`render_text(payload: dict)`, `render_images(payload: dict, frames: list[tuple[dict, Path]])`,
`render_artifact_reference(payload: dict)`.

Main agent owns Store, Service, Worker, MCP tool wrappers, main CLI, and overall integration tests.
G0/host checks and live-link acceptance remain pending until real evidence is recorded.
