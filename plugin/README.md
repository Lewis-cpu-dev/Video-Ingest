# Private Video Ingest plugin

This is a concrete Codex compatibility bundle: `.codex-plugin/plugin.json` declares the workflow
Skill and the actual `.mcp.json` server connection. Its manifest passed the official OpenAI Codex
validator; provenance and checksums are in `docs/runs/plugin-validation.json` in the backend repo.
Run `.venv/bin/python scripts/configure_plugin.py` from the backend checkout to generate
`plugin/.mcp.json` with this machine's interpreter and launcher paths. The connection launches
that checkout's backend over stdio; rerun configuration after moving the checkout.
Its command, arguments and working directory are exercised by the main MCP roundtrip test.

Build the installable archive with `.venv/bin/python scripts/package_plugin.py` from the backend
root. The output is `dist/video-ingest-plugin-0.1.0.zip`. Configure a supported private/local plugin
source in your client to install the bundle when appropriate. For a concrete, directly verified
MCP setup, follow [the step-by-step deployment guide](../docs/deployment.md), including
`codex mcp add video-ingest -- "$PWD/.venv/bin/python" "$PWD/scripts/run_server.py"`
from the backend root. No installation into the client's
external cache, no marketplace mutation and no public publication occurred in this development
session. The absolute backend paths must be updated if the workspace is moved; the bundle does
not contain Python packages, model weights or runtime executables.

OpenAI still supports this compatibility layout, including `mcpServers: "./.mcp.json"` and
`skills: "./skills/"`. New portable packages can use root manifests; this release deliberately
retains the validated Codex compatibility format. See the
[official packaging documentation](https://developers.openai.com/plugins/build/plugins) and
[official manifest contract](https://github.com/openai/codex/blob/main/codex-rs/skills/src/assets/samples/plugin-creator/references/plugin-json-spec.md).

The browser-only ChatGPT host cannot execute this workspace's stdio command. Connect its approved
private remote MCP endpoint/gateway separately, register the actual connection, and complete
Gate 0 before claiming browser compatibility. No invented registered app ID or public endpoint is
included. Authentication and consent still apply when a plugin is enabled. See the backend's
`docs/deployment.md` and `docs/compatibility_matrix.md` for actual tested and pending capabilities.
