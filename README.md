# Video Ingest MCP

A private MCP server that gives a model timestamped evidence from YouTube and Bilibili:
transcripts, optional local speech recognition, video frames, and authorized media references.
The model uses that evidence to answer questions and cite moments in the video.

The server exposes eight tools: `resolve_video`, `prepare_video`, `get_job`, `read_transcript`,
`inspect_segment`, `get_media`, `cancel_job`, and `delete_asset`.

## Install on your MCP host

Use an existing **Linux x86-64** machine with **Python 3.11+**, **uv**, **FFmpeg**, and **ffprobe**.
The reviewed JavaScript runtime installer targets Linux x86-64. No new server or domain is
required for a private stdio or Tunnel deployment.

```bash
git clone https://github.com/Lewis-cpu-dev/Video-Ingest.git
cd Video-Ingest
mkdir -p .tmp .cache
export UV_CACHE_DIR="$PWD/.cache/uv"
export TMPDIR="$PWD/.tmp"
uv sync --frozen --group dev
.venv/bin/python scripts/provision_deno.py --download
.venv/bin/python scripts/configure_plugin.py
.venv/bin/python scripts/check_deployment.py
.venv/bin/python scripts/check_probe.py
```

These checks launch temporary servers and verify real MCP text/image delivery and authenticated
HTTP. They do not connect to ChatGPT or prove that its model can read the images.

## Connect a local MCP client

The default transport is stdio. Configure your client to launch the absolute paths to
`.venv/bin/python` and `scripts/run_server.py`. The launcher works from any working directory.
A machine-local MCP configuration is generated in `plugin/.mcp.json` and excluded from Git.

For Codex:

```bash
codex mcp add video-ingest -- "$PWD/.venv/bin/python" "$PWD/scripts/run_server.py"
codex mcp list
```

Start a new client session after registering the server. The client starts the process; a
separate HTTP listener is unnecessary for stdio.

## Connect ChatGPT through Secure MCP Tunnel

Tunnel forwards requests to the MCP on your existing Linux machine over an outbound connection.
Install the official `tunnel-client` on that machine using the
[OpenAI setup instructions](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
You need a tunnel associated with the target ChatGPT workspace, a runtime key with the required
Tunnel permissions, and access to ChatGPT developer mode. Account access is separate from
successful connection and model-reading tests.

First prepare a profile for the independent text/image probe:

```bash
.venv/bin/python scripts/configure_tunnel.py --tunnel-id tunnel_YOUR_ID --target probe
```

Use `--client /absolute/path/to/tunnel-client` if the executable is not on PATH. This command
only creates a local profile under ignored `.runtime/tunnel/probe/`; it does not create a remote
tunnel or start a connection. Existing profiles are not forcibly overwritten. The profile
references `CONTROL_PLANE_API_KEY` from the environment rather than storing the key.

Set that environment variable privately in your terminal or secret manager, then run the
`doctor` and `run` commands printed by the script. Keep `run` in the foreground while connecting
and testing; Ctrl+C stops it. The local health listener uses loopback and an available port.

In ChatGPT, create a developer-mode app, choose **Tunnel**, and select the same tunnel.
Follow the [host verification procedure](docs/personal_prototype.md) to read fresh random text,
three pixel-only image numbers, and their timestamps. Record real answers with
`scripts/record_host_probe.py --connection secure-mcp-tunnel` and the other documented fields.

After those checks pass, stop the probe tunnel process and prepare the actual video server:

```bash
.venv/bin/python scripts/configure_tunnel.py --tunnel-id tunnel_YOUR_ID --target backend
```

Run the printed backend `doctor` and `run` commands, then refresh or recreate the ChatGPT app's
tool discovery so it exposes the eight video tools. Run only one target for a given tunnel at a
time, and one backend process per data directory. For unattended use, configure a supervisor
following the official tunnel-client guidance.

The repository contains the deployment code, not an active tunnel, credentials, or public URL.
Live ChatGPT connection and image perception remain unverified until tested in the target account.

## Optional local speech recognition

ASR is off by default. To enable the reviewed English model:

```bash
uv sync --frozen --group dev --extra asr
.venv/bin/python scripts/provision_asr_model.py --download
export VIDEO_INGEST_ALLOW_ASR=1
export VIDEO_INGEST_ASR_MODEL="$PWD/.models/faster-whisper-tiny.en"
```

Export these variables before launching the backend or tunnel process. For the generated local
plugin config, rerun `scripts/configure_plugin.py --asr`. This model is English-only; multilingual
ASR quality is a separate acceptance gate. Platform captions can retain their original language.

## Verify and use

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

Provision the optional English ASR model above to reproduce the full provisioned test suite.
Real YouTube and Bilibili processing has been exercised; historical evidence and remaining
platform, language, and host gates are in the [acceptance report](docs/acceptance_report.md).
Download success alone does not establish model consumption.

In your connected client, ask it to inspect an authorized video URL and cite timestamped evidence.
The usual sequence is `resolve_video` → `prepare_video` → `get_job` → `read_transcript` /
`inspect_segment`. Preparation is asynchronous: poll `get_job` at its suggested interval.

For private bearer-authenticated HTTP, resource limits, retention, rollback, and troubleshooting,
see the [deployment guide](docs/deployment.md). The current HTTP service binds only to loopback;
public multi-user OAuth deployment is outside this personal-use setup.

- [Host compatibility and acceptance](docs/compatibility_matrix.md)
- [Dependency and license review](docs/dependency_review.md)
- [Private plugin packaging](plugin/README.md)
- [Local transport verification](docs/runs/prototype-local-verification.json)
