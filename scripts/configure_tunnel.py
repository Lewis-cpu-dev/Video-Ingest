"""Prepare a local tunnel-client profile; does not create a tunnel or start a connection."""
from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tunnel-id", required=True, help="Existing OpenAI tunnel ID, not an API key")
    parser.add_argument("--target", choices=["probe", "backend"], default="probe")
    parser.add_argument("--client", default="tunnel-client", help="Executable name or path")
    args = parser.parse_args()
    if not re.fullmatch(r"tunnel_[A-Za-z0-9_-]+", args.tunnel_id):
        parser.error("Expected a tunnel_ identifier")
    client = shutil.which(args.client)
    if not client:
        parser.error("Install the official tunnel-client or specify --client /path/to/tunnel-client")
    python = ROOT / ".venv/bin/python"
    if not python.is_file():
        parser.error("Create the project environment with uv sync --frozen first")
    launcher = ROOT / "scripts" / ("run_probe.py" if args.target == "probe" else "run_server.py")
    profile = f"video-ingest-{args.target}"
    directory = ROOT / ".runtime/tunnel" / args.target
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [client, "init", "--sample", "sample_mcp_stdio_local", "--profile", profile,
               "--profile-dir", str(directory), "--tunnel-id", args.tunnel_id,
               "--mcp-command", shlex.join([str(python), str(launcher)]),
               "--control-plane-api-key-ref", "env:CONTROL_PLANE_API_KEY",
               "--health-listen-addr", "127.0.0.1:0"]
    result = subprocess.run(command, check=False)
    if result.returncode:
        return result.returncode
    print("Profile prepared. No tunnel connection has been started.")
    for action in ("doctor", "run"):
        cmd = [client, action, "--profile", profile, "--profile-dir", str(directory)]
        if action == "doctor":
            cmd.append("--explain")
        print(shlex.join(cmd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
