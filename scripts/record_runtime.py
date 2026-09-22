"""Record executable-reported runtime versions without reading external binary files."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = {"created_at": datetime.now(UTC).isoformat(), "python": platform.python_version(),
              "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
              "binary_hashes": "not recorded: no external executable files read during restricted development",
              "executables": {}}
    for name in ("ffmpeg", "ffprobe"):
        record = {}
        for option, key in (("-version", "version"), ("-buildconf", "configuration"), ("-L", "license")):
            try:
                process = subprocess.run([name, option], cwd=ROOT, capture_output=True, text=True,
                                         timeout=10, check=False)
                record[key] = {"exit_code": process.returncode, "output": process.stdout + process.stderr}
            except (OSError, subprocess.TimeoutExpired) as exc:
                record[key] = {"status": "unavailable", "exception_type": type(exc).__name__}
        report["executables"][name] = record
    destination = ROOT / "docs/runtime_inventory.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print("Wrote docs/runtime_inventory.json")


if __name__ == "__main__":
    main()
