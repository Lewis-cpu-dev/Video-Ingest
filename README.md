# Video Ingest

Private MCP backend for YouTube and Bilibili evidence: timed captions, optional local ASR,
actual video frames and authorized artifact references. The host model writes the analysis.
The eight tools are `resolve_video`, `prepare_video`, `get_job`, `read_transcript`,
`inspect_segment`, `get_media`, `cancel_job` and `delete_asset`.

**Status:** backend development is delivered; **179 tests pass**, with Ruff, compilation and
frozen-lock checks passing. Real YouTube captions/video/frames, Bilibili video/frames and local
English ASR were exercised. Target ChatGPT web perception, multilingual quality and the complete
20-link platform matrix remain acceptance gates; this is not a public release.

```bash
git clone https://github.com/Lewis-cpu-dev/Video-Ingest.git
cd Video-Ingest
mkdir -p .tmp .cache
UV_CACHE_DIR="$PWD/.cache/uv" TMPDIR="$PWD/.tmp" uv sync --frozen --group dev --extra asr
.venv/bin/python scripts/provision_deno.py --download
.venv/bin/python scripts/provision_asr_model.py --download
.venv/bin/python scripts/configure_plugin.py
.venv/bin/python scripts/check_deployment.py
.venv/bin/python -m pytest
codex mcp add video-ingest -- "$PWD/.venv/bin/python" "$PWD/scripts/run_server.py"
codex mcp list
```

Linux is required for the current `fcntl`, `resource` and process-group implementation.
The default transport is stdio. FFmpeg/ffprobe are required for media processing. ASR is disabled
by default; a reviewed English test model is provisioned locally. Runtime data stays in this workspace.
Install Python 3.11+, uv, FFmpeg/ffprobe and a compatible client first. The runtime installer
currently targets Linux x86-64. Start a new Codex session after registering the MCP server.
The client launches the backend automatically. No public endpoint is included.

**[Follow the complete deployment guide](docs/deployment.md)** for prerequisites, optional English
ASR, local plugin packaging, authenticated HTTP, ChatGPT connection requirements and troubleshooting.
Machine-specific `plugin/.mcp.json` is generated locally and excluded from Git.

- [Deployment, limits and rollback](docs/deployment.md)
- [Host Gate 0 procedure and compatibility](docs/compatibility_matrix.md)
- [Dependency review and license evidence](docs/dependency_review.md)
- [Acceptance results and remaining gates](docs/acceptance_report.md)
- [Final verification record](docs/runs/final-verification.json)
- [Private workflow package](plugin/README.md)
- [Original development plan](video_ingest_development_plan_v0.1.md)

Use `python -m server.probe` for randomized text/image/file/audio/video challenges independent of
video platforms. Use `scripts/integration_smoke_tests.py --help` for explicit, bounded live tests.

After configuring the connection, run `.venv/bin/python scripts/package_plugin.py` to build
`dist/video-ingest-plugin-0.1.0.zip` locally; generated archives are excluded from Git. Its manifest
passed the official validator and its actual MCP command passed a protocol roundtrip. Installation
in a specific client and target-browser perception remain separate checks.

To reproduce all optional checks in a fresh checkout, explicitly provision the reviewed Deno
runtime and English model using the commands in the deployment guide before running pytest.
