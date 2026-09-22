# Dependency review — 2026-09-21

The review began before provider/delivery implementation and now records the final installed
environment. `uv.lock` pins the dependency graph and distribution hashes. The workspace environment
was inspected directly; `dependency_inventory.json` inventories 61 installed distributions and
hashes available license texts copied to `docs/licenses/`. `scripts/audit_dependencies.py` can
refresh that evidence. The lock check resolves 65 packages across all groups/platform conditions;
that is distinct from the 61 distributions installed on this machine.

| Component | Reviewed version | License / evidence | Use and observed result |
|---|---|---|---|
| Official Python MCP SDK | 1.30.0; requirement `<2` | MIT, installed text | Tool schemas, stdio and HTTP; actual protocol tests passed |
| yt-dlp | 2026.8.19 | Unlicense, installed Python distribution text | Guarded YouTube/Bilibili metadata/media; both platforms yielded real media |
| yt-dlp-ejs | 0.8.0 | Unlicense AND MIT AND ISC, installed text | Locked YouTube challenge solver; no remote component downloads |
| Deno | 2.9.7 Linux x86-64 | MIT, immutable release license retained | Exact reviewed executable/argv, permission-restricted EJS; sandbox and live YouTube checks passed |
| FFmpeg / ffprobe | 4.4.2-0ubuntu0.22.04.1 | GPL-enabled build; executable-reported notices/config | Local probing, audio, source-PTS frames, clips and merge; real fixtures passed |
| faster-whisper | 1.2.1 | MIT, installed text | Optional local CPU int8 ASR; four real English service/timing/language/silence tests passed |
| CTranslate2 / PyAV / onnxruntime | 4.8.2 / 18.1.0 / 1.30.0 | MIT / BSD-3-Clause / MIT; retained texts/notices | Local inference and audio support; native runtime provenance remains deployment responsibility |
| Pillow | 12.3.0 | MIT-CMU, installed text | Local image validation and randomized pixel probes |
| Pydantic / HTTPX / Uvicorn | 2.13.5 / 0.28.1 / 0.53.0 | MIT / BSD-3-Clause / BSD-3-Clause | Schema validation and private service transport |
| pytest / pytest-asyncio / Ruff | 9.1.1 / 1.4.0 / 0.16.8 | MIT / Apache-2.0 / MIT | Local verification; no production data upload |

Deno's 41,596,794-byte archive was checked against the first-party immutable release API digest
before extraction/execution. Archive, executable and license hashes are retained in
`deno_runtime_inventory.json`; `scripts/provision_deno.py --download` reproduces provisioning.
The actual FFmpeg configuration has `--enable-gpl`. `runtime_inventory.json` retains version,
configuration and license output. System executable files were not read to obtain binary hashes;
production deployment still needs a fixed binary/container digest and corresponding notices.

The model is `Systran/faster-whisper-tiny.en`, immutable revision
`0d3d19a32d3338f10357c0889762bd8d64bbdeba`. Its retained model card declares MIT, and
`asr_model_inventory.json` records all five file sizes/checksums. Explicit provisioning is
`scripts/provision_asr_model.py --download`. This English fixture model establishes local ASR
behavior, not multilingual accuracy. Missing dependencies/models return structured errors rather
than downloading at inference time or calling a hosted transcription service.

Where wheel metadata omitted texts, tagged upstream licenses were checked and retained for
[CTranslate2](https://raw.githubusercontent.com/OpenNMT/CTranslate2/v4.8.2/LICENSE) and
[tokenizers](https://raw.githubusercontent.com/huggingface/tokenizers/v0.23.2/LICENSE).
The installed onnxruntime package's LICENSE and ThirdPartyNotices are also copied. Full native
binary redistribution review depends on the deployment artifacts actually shipped.

No code from the four reference MCP projects is vendored, and no links, media or credentials are
sent to their hosted services. Platform credentials/browser cookies are not read. The external
network flow is canonical public source → guarded metadata/caption/media requests → local
processing → configured host delivery. Provisioning downloads occur only through explicit scripts.
Providers remain replaceable behind adapters. No paid API billing integration exists; hosting,
network/storage, local compute and host model usage have separate operational costs. Provider
failure never silently switches to a paid or third-party service.

First-party sources checked:

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): upstream now uses v2;
  this project intentionally pins and tests the supported v1 API below 2.
- [yt-dlp dependencies/embedding](https://github.com/yt-dlp/yt-dlp): FFmpeg and JavaScript support
  affect full platform coverage; bundled solver plus reviewed Deno are used here.
- [Deno immutable release](https://github.com/denoland/deno/releases/tag/v2.9.7) and
  [license](https://raw.githubusercontent.com/denoland/deno/v2.9.7/LICENSE.md).
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper): local CTranslate2 inference.
- [FFmpeg licensing](https://ffmpeg.org/legal.html): build components determine obligations.

Real results are in `acceptance_report.md` and `docs/runs/`: YouTube original captions/media/frame,
Bilibili media/frame with missing captions, and real local English ASR. Earlier unavailable-source
and budget failures are preserved. Target ChatGPT consumption is still unverified.
