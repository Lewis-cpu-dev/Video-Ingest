"""Only the reviewed Deno binary may evaluate bundled EJS with zero permissions."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from server.config import PROJECT_ROOT
from server.errors import IngestError

DENO_PATH = PROJECT_ROOT / ".tools" / "deno-2.9.7" / "deno"
DENO_SHA256 = "ce6a052beb97c2b92de67077e3f3924ba7c9661ede0d8dc2f66a116a3c841f21"
DENO_ARGUMENTS = (
    "run", "--ext=js", "--no-code-cache", "--no-prompt", "--no-remote", "--no-lock",
    "--node-modules-dir=none", "--no-config", "--no-npm", "--cached-only", "--deny-read",
    "--deny-write", "--deny-net", "--deny-env", "--deny-run", "--deny-ffi", "--deny-sys",
    "--deny-import", "--v8-flags=--max-old-space-size=512,--jitless", "-",
)


def provisioned_deno() -> Path | None:
    if not DENO_PATH.is_file():
        return None
    if DENO_PATH.resolve() != DENO_PATH:
        raise IngestError("DEPENDENCY_UNAVAILABLE", "The provisioned JavaScript runtime must not be a symlink.")
    digest = hashlib.sha256()
    with DENO_PATH.open("rb") as source:
        for block in iter(lambda: source.read(1024**2), b""):
            digest.update(block)
    if digest.hexdigest() != DENO_SHA256:
        raise IngestError("DEPENDENCY_UNAVAILABLE", "The JavaScript runtime does not match its reviewed checksum.")
    return DENO_PATH


def allowed_runtime_command(executable, args) -> bool:
    return executable == str(DENO_PATH) and list(args) in (
        [str(DENO_PATH), "--version"], [str(DENO_PATH), *DENO_ARGUMENTS])


def isolated_script(source: str) -> str:
    # Deno allows initial module-graph reads even with --deny-read. Never put a source-controlled
    # import expression in that graph: compile the solver at runtime, where dynamic imports undergo
    # ordinary read permission checks. The bootstrap itself has no imports or privileged operations.
    return ("Object.defineProperty(globalThis, 'Worker', {value:undefined, writable:false, configurable:false});\n"
            "const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;\n"
            f"await new AsyncFunction({json.dumps(source)})();\n")


def install_restricted_ejs() -> None:
    from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
    from yt_dlp.extractor.youtube.jsc.provider import JsChallengeProviderError

    def run_deno(self, stdin, options):
        # Input contains the locked yt-dlp-ejs solver and source challenge. No runtime module imports,
        # files, sockets, subprocesses, environment reads or extra executable arguments are granted.
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"NODE_OPTIONS", "NODE_PATH"} and not key.startswith("DENO_")}
        environment.update(DENO_DIR=str(PROJECT_ROOT / ".cache" / "deno"), DENO_NO_UPDATE_CHECK="1",
                           DENO_NO_PROMPT="1")
        result = subprocess.run([str(DENO_PATH), *DENO_ARGUMENTS], input=isolated_script(stdin), text=True,
                                capture_output=True, env=environment, timeout=60, check=False)
        if result.returncode:
            raise JsChallengeProviderError("The restricted local JavaScript solver failed.")
        return result.stdout

    DenoJCP._run_deno = run_deno
