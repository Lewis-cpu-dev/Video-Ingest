"""Generate machine-local MCP configuration after cloning or moving this checkout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr", action="store_true", help="Enable the explicitly provisioned English ASR model")
    args = parser.parse_args()
    python = ROOT / ".venv/bin/python"
    if not python.is_file():
        parser.error("Create .venv with uv sync --frozen --group dev first")
    config = {"command": str(python), "args": [str(ROOT / "scripts/run_server.py")], "cwd": str(ROOT)}
    if args.asr:
        model = ROOT / ".models/faster-whisper-tiny.en"
        if not (model / "model.bin").is_file():
            parser.error("Run scripts/provision_asr_model.py --download first")
        config["env"] = {"VIDEO_INGEST_ALLOW_ASR": "1", "VIDEO_INGEST_ASR_MODEL": str(model)}
    destination = ROOT / "plugin/.mcp.json"
    destination.write_text(json.dumps({"mcpServers": {"video-ingest": config}}, indent=2) + "\n")
    print("Configured plugin/.mcp.json for this checkout. It is local-only and excluded from Git.")


if __name__ == "__main__":
    main()
