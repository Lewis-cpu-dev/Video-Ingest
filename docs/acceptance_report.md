# Acceptance report — 2026-09-21

**Private backend implementation delivered; target-host and full P0 product acceptance remain
open.** Actual YouTube captions/video/frames, Bilibili video/frames and local English ASR have
been exercised. No claim is made that a ChatGPT browser model consumed these results.

## Deployment documentation update

The GitHub checkout now includes a machine-local connection generator and a launcher that works
from another working directory. `scripts/check_deployment.py` passed real stdio and authenticated
HTTP MCP initialization, tool calls and authentication checks. See
[runs/deployment-verification.json](runs/deployment-verification.json) and the updated
[deployment guide](deployment.md). The complete suite passed again: **179 passed in 14.14s**.
Local secrets, generated connection paths, media, model weights and environments are excluded from Git.

## Recorded checks

| Scope | Result | Evidence / boundary |
|---|---|---|
| Dependency review | Complete for installed packages and reviewed local runtimes | `dependency_review.md`, inventories, license texts, `uv.lock` |
| Host adapter / real MCP transport | 17 local checks passed at adapter checkpoint | Actual stdio, text/image binding, WAV/MP4 resources, main tools and HTTP auth; no host model |
| Full deterministic suite | **179 passed in 14.14s** | [Final verification](runs/final-verification.json) and [JUnit](runs/pytest.xml); Ruff, compileall and frozen-lock check also passed |
| YouTube English captions + video + frame | Passed on Blender sample | `runs/youtube-blender-64mib-smoke.json` |
| Bilibili video + frame | Passed; combined caption request partial | `runs/bilibili-smoke.json`; source advertised zero suitable captions |
| Local English ASR + service fallback | Four real integration checks passed | `runs/asr-integration-smoke.json`: actual no-caption prepare/job/read, 4-second silence + 12-second offset, English-model mismatch rejection and silence without hallucinated words; standalone transcript in `runs/asr-smoke.json` |
| Private plugin package | Official manifest validator passed; archive built | `runs/plugin-validation.json`, `runs/plugin-package.json`; actual configured stdio command exercised |
| Actual target ChatGPT perception and workflow | Pending | No authenticated target-host session controlled |

The final full suite returned **179 passed in 14.14s**, including actual optional ASR and Deno
checks in this provisioned Linux environment. The [machine-readable verification](runs/final-verification.json)
records 179 tests, zero failures/errors/skips and the source-tree SHA-256. Ruff,
compileall and `uv lock --check` passed. Reproducing every optional check requires the pinned local
runtime/model plus `uv sync --frozen --group dev --extra asr`; missing optional assets are skipped
explicitly. The earlier 17-test adapter checkpoint remains a narrower transport observation.

## Every live attempt

These are backend checks from this development environment without platform cookies. Network
region was not established and is explicitly recorded as `unrecorded`. Byte counts below are the
acquisition callback's measured transfer, including failed acquisitions; they are not an estimate
of platform success rates, model cost, or all metadata/caption networking. Each smoke invocation
has its own budget; production service tests separately verify cumulative per-asset accounting.

| Attempt | Requested / limit | Recorded outcome | Acquisition transfer |
|---|---|---|---:|
| Bilibili `BV1eZdzBGEvE` | Captions + video + frame; 32 MiB, 45s | Partial: `NO_CAPTIONS`; real 13,166,230-byte video and one delivered frame | 13,332,456 bytes |
| YouTube `BaW_jenozKc` | Captions + video + frame; 32 MiB, 45s | No content evidence: extractor reported unusable metadata/no formats, acquisition `MEDIA_UNAVAILABLE` | 107,982 bytes |
| YouTube `YE7VzlLtp-4` | Video + frame; 32 MiB, 45s | Partial: real metadata/30 tracks; acquisition `BUDGET_EXCEEDED` | 33,556,144 bytes |
| YouTube `YE7VzlLtp-4` | English captions + video + frame; 64 MiB, 90s | Passed requested backend steps: 6 human-caption segments, 46,207,015-byte video and one delivered frame | 46,534,801 bytes |

Raw records are `runs/bilibili-smoke.json`, `runs/youtube-smoke.json`,
`runs/youtube-blender-smoke.json` and `runs/youtube-blender-64mib-smoke.json`. Content hashes,
durations and versions are retained without caption text, signed CDN URLs or credentials. The
unusable legacy YouTube metadata is not treated as readable video content or confirmed subtitle
absence; provider regression fixes tighten this distinction. Earlier failures remain in the record.
The successful Blender media duration is 596,501 ms versus the platform's 597,000 ms metadata.

## Acceptance gates still open

1. **G0 target host:** in the actual ChatGPT web account/model/mode, prove random text reading,
   pixel-only digits and time binding, proper permissions, workflow tool selection, negative
   requests and later job lookup. Use `compatibility_matrix.md` and retain original redacted
   responses plus recorder comparisons. Local tool transport is insufficient for this gate.
2. **Frozen 20-link matrix:** `tests/fixtures/live_matrix.json` contains ten slots per platform.
   Three distinct URLs have observed attempts; 17 authorized sources and their expected content
   still need selection. Freeze all inputs and test ten per platform across required caption,
   no-caption ASR, visual-only, table, short-event, language and follow-up paths. Do not remove
   unavailable or partial samples from the final denominator.
3. **ASR and timing quality:** the installed tiny.en model and synthetic English fixture prove a
   real local inference path, not multilingual real-video quality. Provision suitable non-English
   models, annotate speech/source times, freeze language-specific error-rate criteria and verify
   the plan's <=1-second target. Original subtitle timing is not an independent alignment test.
4. **Deployment evidence:** retain a pinned FFmpeg binary/container digest and validate actual
   unprivileged process, cgroup/disk and outbound-network isolation. Application guards are tested;
   no Docker/systemd/firewall deployment was configured or tested here. Public multi-user release
   additionally needs validated OAuth/identity, tenant onboarding and an approved HTTPS gateway.
5. **Client installation:** the concrete Codex private archive is built and schema-validated,
   but not installed into external client directories. Browser use requires a registered actual
   remote connection. No registered app ID, account permission or host modality is invented.

File/audio/video host consumption is tracked separately; an accessible backend URI or download
button is not native media perception. Those host claims remain disabled until their independent
probes pass. Uniform frames remain sparse samples and can miss short visual events; use detail
ranges and report insufficient evidence rather than guessing.

## Gate summary

G1 now has real platform content evidence, with the Bilibili missing-caption limitation preserved.
G2 and local G3 are implemented and checked by deterministic/real-media tests. The local ASR and
raw-image paths exist, and the private workflow bundle is concrete. G0 still needs the target-host
session; the full G3/G4 product gates need the frozen matrix and deployment/quality evidence above.
No overall completion percentage or 20-link success rate is reported before those gates close.
