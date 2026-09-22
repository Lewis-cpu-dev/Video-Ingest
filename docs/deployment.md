# Deploy Video Ingest

This guide deploys the private MCP backend and connects its eight tools to a client. The
local stdio and authenticated HTTP startup procedures are checked by
[`scripts/check_deployment.py`](../scripts/check_deployment.py). Browser ChatGPT still needs a
reachable, authenticated remote connection and its separate host acceptance tests.

## 1. Install prerequisites and clone

Use **Linux**, Python **3.11 or newer**, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and FFmpeg/ffprobe. The reviewed Deno installer supports Linux x86-64. On Ubuntu/Debian an
administrator can install the system media tools with `sudo apt-get install ffmpeg`.
Check the prerequisites before proceeding:

```bash
python3 --version
uv --version
ffmpeg -version
ffprobe -version
git clone https://github.com/Lewis-cpu-dev/Video-Ingest.git
cd Video-Ingest
```

All following shell commands run from this checkout. Python packages, runtime files, models,
caches and temporary files stay inside it. The current implementation uses Linux process locks
and resource limits; native Windows support is not implemented.

## 2. Install the locked Python environment

```bash
mkdir -p .tmp .cache
export UV_CACHE_DIR="$PWD/.cache/uv"
export TMPDIR="$PWD/.tmp"
export XDG_CACHE_HOME="$PWD/.cache"
uv sync --frozen --group dev --extra asr
```

`uv.lock` pins the Python packages and their distribution hashes. The ASR extra installs local
transcription dependencies; inference remains disabled until step 4 enables a model. The lockfile
does not pin your system FFmpeg binary. Retain its version/build configuration for deployment
regressions, and review the selected binary's component licenses before redistributing it.

## 3. Provision the reviewed YouTube JavaScript runtime

```bash
.venv/bin/python scripts/provision_deno.py --download
.venv/bin/python scripts/provision_deno.py
```

The first command explicitly downloads the checksum-pinned Deno runtime into `.tools/`; the
second verifies existing files without downloading. The downloader only permits the reviewed
runtime and restricted bundled EJS invocation. The runtime denies filesystem, network,
environment, subprocess, FFI and system access. Platform authentication/challenges can still fail.

## 4. Provision optional English speech recognition

```bash
.venv/bin/python scripts/provision_asr_model.py --download
.venv/bin/python scripts/provision_asr_model.py
```

This provisions the pinned `faster-whisper-tiny.en` model into `.models/`. It passed the recorded
English speech/timing tests. Use a separately reviewed multilingual model for Chinese/Bilibili
speech: tiny.en is English-only, and Chinese requests are rejected. Platform captions can still
be read in their original language without ASR. No model download happens during a tool call.

## 5. Generate this machine's connection and verify deployment

```bash
.venv/bin/python scripts/configure_plugin.py
.venv/bin/python scripts/check_deployment.py
.venv/bin/python -m pytest -q
```

Expected deployment output:

```text
PASS: stdio and authenticated HTTP deployments expose all eight tools.
```

The checker launches temporary real servers, initializes both MCP transports, checks the eight
tools and structured errors, verifies that missing HTTP credentials are rejected, and stops the
servers afterward. It also tests launching from a different working directory. Results are saved
in `docs/runs/deployment-verification.json`. It performs no platform downloads and does not
connect to your ChatGPT account. The full provisioned regression suite currently has 179 tests.

The generated `plugin/.mcp.json` contains absolute paths for **this checkout**, is ignored by Git,
and must be regenerated after moving/cloning the repository. Add `--asr` to
`configure_plugin.py` to enable the provisioned English model in that plugin configuration.
Keep this backend checkout in place when using a packaged plugin.

## 6. Connect a local Codex client

With the Codex CLI installed, register the local MCP server from the checkout root:

```bash
codex mcp add video-ingest -- "$PWD/.venv/bin/python" "$PWD/scripts/run_server.py"
codex mcp list
codex mcp get video-ingest
```

Start a new Codex session after registration. Codex launches the stdio backend itself; you do
not need to keep another stdio server running. The launcher resolves the checkout from its own
location, so your client can start from a different directory. These commands modify your client
configuration when **you run them**; development verification did not install outside the project.

To enable the provisioned English ASR model for a direct MCP registration, replace that entry:

```bash
codex mcp remove video-ingest
codex mcp add video-ingest \
  --env VIDEO_INGEST_ALLOW_ASR=1 \
  --env VIDEO_INGEST_ASR_MODEL="$PWD/.models/faster-whisper-tiny.en" \
  -- "$PWD/.venv/bin/python" "$PWD/scripts/run_server.py"
```

The [official Codex CLI reference](https://developers.openai.com/codex/cli/reference) documents
`mcp add`, `list`, `get`, `remove`, and environment options. To connect another local MCP client,
copy the generated server entry from `plugin/.mcp.json` into that client's MCP configuration.
The workflow is also available at `plugin/skills/video-ingest/SKILL.md`.

Build the private plugin archive when your client supports local plugin import:

```bash
.venv/bin/python scripts/package_plugin.py
```

This creates `dist/video-ingest-plugin-0.1.0.zip`, including the configured connection and Skill.
It requires this checkout and environment on the same machine. The archive is generated locally;
GitHub source checkout users should build their own copy after step 5. The main MCP command is
verified; installation through a particular client plugin-import UI remains a client-side check.

## 7. Run authenticated HTTP instead of stdio

Use one process per data directory. Stop a client-managed stdio server before starting HTTP on
the same default `.runtime` directory, or give each process a separate in-checkout data directory.
In a terminal at the repository root:

```bash
export VIDEO_INGEST_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
# Optional English ASR:
export VIDEO_INGEST_ALLOW_ASR=1
export VIDEO_INGEST_ASR_MODEL="$PWD/.models/faster-whisper-tiny.en"
.venv/bin/python scripts/run_server.py --transport http --host 127.0.0.1 --port 8765
```

Keep that process running; Ctrl+C stops it. Keep the token private and provide the same token to
the client through its secret/environment configuration. The token is not written to source files.
Both `/mcp` and `/artifacts/{artifact_id}` require `Authorization: Bearer <token>`. A missing or
incorrect token returns HTTP 401. The MCP resource reader allows up to 16 MiB; larger prepared
media uses the authenticated HTTP artifact endpoint returned by `get_media`.

For a Codex HTTP connection, run the following in a separate shell where the **same** token is
available. Restart Codex from an environment containing that token:

```bash
codex mcp add video-ingest-http \
  --url http://127.0.0.1:8765/mcp \
  --bearer-token-env-var VIDEO_INGEST_TOKEN
codex mcp list
```

Startup refuses a missing/short token or a public bind address. This is private single-user bearer
authentication; public multi-user OAuth is a separate deployment requirement.

## 8. Connect the ChatGPT browser host and run Gate 0

The browser needs a remote HTTPS MCP endpoint; the GitHub URL and local plugin ZIP are not MCP
endpoints. To use the backend from ChatGPT:

1. Complete steps 1–5 and run the HTTP backend from step 7 on your Linux server/workstation.
2. Configure a private authenticated HTTPS gateway or approved secure tunnel that can reach
   `127.0.0.1:8765/mcp`. The gateway must support the target host's authentication flow and forward
   the backend bearer token. The backend's static bearer token alone does not implement OAuth.
3. In your actual ChatGPT account, follow the current [official connection/testing procedure](https://developers.openai.com/plugins/deploy/connect-chatgpt)
   to enable the applicable developer/custom-connection feature and register the HTTPS MCP URL.
   Availability and permission depend on the account and client; this repository supplies no app ID
   or public hosting endpoint.
4. For the independent host probe, start
   `.venv/bin/python -m server.probe --transport streamable-http --port 8766` and expose it through
   the same **authenticated private** gateway. The probe has no bearer auth of its own.
5. Follow [the Gate 0 checklist](compatibility_matrix.md): random text, pixel-only image digits,
   frame/time binding, job lookup, and permissions. Record real host answers; backend responses
   alone do not prove the model consumed the content. File/audio/video are tested separately.
6. Enable the production connection for video requests only after the required host checks pass.

An actual account connection, remote gateway and browser perception have not been validated in
this development environment. Local stdio and bearer-authenticated HTTP have been exercised by
the checker. See [the acceptance report](acceptance_report.md) for the remaining product gates.

## 9. Try a video and verify the evidence

In a connected local client, ask:

> Read https://www.youtube.com/watch?v=YE7VzlLtp-4 using Video Ingest. Prepare its English
> transcript and a visual index, then show timestamped evidence and disclose missing coverage.

The expected tool sequence is `resolve_video` → `prepare_video` → `get_job` →
`read_transcript` / `inspect_segment`. The worker returns a job ID; poll at the suggested interval
until a terminal state. A visual answer should include actual image content blocks. Use
`get_media` for an authenticated reference, `cancel_job` to stop work, and `delete_asset` to revoke
and clean up the asset. Source access and limits can change; partial/error results are valid
outcomes and must not be presented as full success.

## Troubleshooting

| Symptom | Action |
|---|---|
| Missing `mcp`, `yt_dlp`, or other imports | Run locked `uv sync` from step 2 and use `.venv/bin/python`. |
| Missing FFmpeg/ffprobe | Install the system executables, put them on PATH, and rerun the checker. |
| Plugin refers to another machine's path | Rerun `scripts/configure_plugin.py` and rebuild the archive. |
| A manually started stdio process appears to wait | It is waiting for MCP messages; register it in a client or run the checker. |
| Data directory already has a worker | Stop the other process or set a distinct `VIDEO_INGEST_DATA_DIR` beneath this checkout. |
| HTTP 401 | Supply the same valid token to server and client; do not put it in Git/chat. |
| `ASR_UNAVAILABLE` | Install the ASR extra, provision the model, and explicitly enable it. |
| `LANGUAGE_UNSUPPORTED` | Choose a reviewed model supporting the requested language. |
| `NO_CAPTIONS` | Enable an appropriate local ASR model if media access is available. |
| `PROCESSING_REQUIRED` | Call `prepare_video` for the needed evidence/range before reading. |
| `AUTH_REQUIRED` / `RATE_LIMITED` | Respect platform authorization and retry guidance; browser cookies are not imported. |
| A file link appears but the browser model cannot read it | Record that modality as unsupported; complete Gate 0 rather than claiming it was watched. |

## Resource and retention policy

Initial server policies cap content at 120 minutes, cumulative media transfer at 1 GiB per asset,
one heavy worker, six images per response, 24 overview samples, 60-second detail ranges and
24-hour retention. Actual policy is in `server/config.py`; tool arguments cannot raise it. A
requested local time range can still require downloading more upstream bytes, charged to the
same budget. Failed retries count. A partial result is not an entirely completed asset.

`prepare_video` creates or reuses a persisted job. `get_job` is read-only and can return after a
client disconnect or service restart. Respect its suggested poll interval. On restart, interrupted
work must be inspected using the documented recovery state; no unknown external paid request is
resubmitted. `cancel_job` stops pending/running work as the worker checks cancellation. Deletion
revokes backend access and removes owned artifacts after work has stopped. It cannot erase media
already sent to a host or copied into conversation history.

The server creates opaque artifact URIs. `get_media` may return a backend-readable reference even
when the default unverified host profile cannot consume file resources. Files and images must never
be represented to the model as arbitrary server filesystem paths. Existing `read_transcript` pages,
coverage metadata and frames remain usable after another requested modality fails.

## Platform smoke, records and rollback

Only run against content you are authorized to process. Start with bounded metadata/caption tests,
then deliberately enable real media with a download budget:

```bash
.venv/bin/python scripts/integration_smoke_tests.py --network \
  --url 'https://www.bilibili.com/video/BV1eZdzBGEvE' --captions --media \
  --max-download-mib 32 --region 'record actual network region' \
  --output docs/runs/bilibili-smoke.json
```

The smoke harness never claims host perception. It records canonical identity, versions, outcome,
caption provenance/count/hash, file hash/size, measured transfers and error codes. It excludes
caption text, signed CDN URLs, credentials, raw upstream errors and backend artifact paths from
published records. Private downloaded smoke artifacts remain under `.runtime/smoke/<run_id>`;
remove those specific run directories when no longer needed. Do not delete unrelated runtime data.

Freeze 20 authorized entries in `tests/fixtures/live_matrix.json` before reporting platform rates.
The supplied matrix intentionally records unknown samples as pending. Run `--matrix` only after
setting `enabled` and permission/expected-evidence fields. Never remove failures to improve rates.

Before upgrades, retain the working lockfile, runtime binary inventory, model hashes and a private
backup of the database plus asset directory after stopping the worker. Validate a new environment
against deterministic tests, both platforms and the same host cases. Roll back code, lockfile and
runtime artifacts together; do not run older code on an incompatible database without a compatible
backup. v0.1 does not implement database schema downgrade migrations. Store backups only in an
operator-approved private location inside the workspace during this restricted development session.

## Production isolation still required

Before any public or multi-user deployment, run a dedicated unprivileged service identity and
an operator-reviewed supervisor/container policy. Code, configuration and model files should be
read-only to the worker; only the private asset, temporary and cache directories should be writable.
Apply an actual disk/filesystem quota and cgroup CPU, memory, process-count and wall-time budgets.
Keep authentication credentials out of downloader/decoder environments. Apply network isolation
that forces downloader sockets through the guarded egress proxy, including IPv6 and DNS, and
prevents decoder and JavaScript processes from opening independent outbound connections. The
application's DNS checks, Python audit hooks, Deno permissions and subprocess limits complement
these controls; they are not evidence that a container firewall or cgroup policy has been tested.

No Docker/systemd deployment or external firewall mutation was performed in this workspace-only
session. A single broad container network allowlist does not by itself restrict individual worker
processes. Validate the chosen deployment's behavior with the same redirect, rebinding, resource
exhaustion and native-extension escape tests before accepting production isolation. Public
OAuth and genuine independent-user separation require separate acceptance from private bearer auth.
