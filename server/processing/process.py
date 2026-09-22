"""Cancellable, bounded subprocesses; never invoke a shell."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from server.config import PROJECT_ROOT
from server.errors import IngestError


def workspace_path(path: Path, *, exists: bool = False) -> Path:
    result = Path(path).resolve()
    if not result.is_relative_to(PROJECT_ROOT) or result == PROJECT_ROOT:
        raise IngestError("UNSAFE_PATH", "Media paths must remain inside the workspace.")
    if exists and not result.is_file():
        raise IngestError("MEDIA_UNAVAILABLE", "The local media artifact is unavailable.")
    return result


def run_process(command: list[str], *, timeout: float, max_file_bytes: int,
                cancelled: Callable[[], bool] = lambda: False, input_data: bytes | None = None,
                stage: str = "validating", max_output_bytes: int = 64 * 1024**2,
                max_address_bytes: int = 4 * 1024**3) -> bytes:
    if cancelled():
        raise IngestError("CANCELLED", "The operation was cancelled.", stage, next_action="none")
    temporary = PROJECT_ROOT / ".tmp" / "process"
    temporary.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}}
    env.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
               XDG_CACHE_HOME=str(PROJECT_ROOT / ".cache"), PYTHONDONTWRITEBYTECODE="1",
               PYTHONNOUSERSITE="1", PYTHONPATH=str(PROJECT_ROOT),
               HF_HOME=str(PROJECT_ROOT / ".cache" / "huggingface"), HF_HUB_OFFLINE="1",
               TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
               DENO_DIR=str(PROJECT_ROOT / ".cache" / "deno"), DENO_NO_UPDATE_CHECK="1", DENO_NO_PROMPT="1")
    args = [sys.executable, "-m", "server.processing.subprocess_runner", str(int(timeout) + 2),
            str(max_file_bytes), str(max_address_bytes), *command]
    try:
        process = subprocess.Popen(args, cwd=PROJECT_ROOT, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        raise IngestError("DEPENDENCY_UNAVAILABLE", "A required processing executable is unavailable.",
                          stage, next_action="check_deployment") from exc
    deadline = time.monotonic() + timeout
    try:
        first = True
        while True:
            if cancelled():
                raise IngestError("CANCELLED", "The operation was cancelled.", stage, next_action="none")
            if time.monotonic() >= deadline:
                raise IngestError("TIMEOUT", "The processing time limit was reached.", stage,
                                  next_action="reduce_scope")
            try:
                stdout, stderr = process.communicate(input=input_data if first else None, timeout=0.2)
                break
            except subprocess.TimeoutExpired as exc:
                first = False
                if len(exc.output or b"") + len(exc.stderr or b"") > max_output_bytes:
                    raise IngestError("LIMIT_EXCEEDED", "Processing output exceeded its limit.", stage,
                                      next_action="reduce_scope") from exc
        if len(stdout) + len(stderr) > max_output_bytes:
            raise IngestError("LIMIT_EXCEEDED", "Processing output exceeded its limit.", stage,
                              next_action="reduce_scope")
        if process.returncode:
            # Internal stderr may contain signed URLs or local paths. Never send it to clients.
            raise IngestError("DECODE_FAILED", "The media processor could not complete this operation.",
                              stage, next_action="check_source")
        return stdout
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
