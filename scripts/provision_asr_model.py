"""Explicitly provision the pinned English ASR fixture model inside this workspace."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from provision_deno import fetch_verified

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Systran/faster-whisper-tiny.en"
REVISION = "0d3d19a32d3338f10357c0889762bd8d64bbdeba"
FILES = {"README.md", "config.json", "tokenizer.json", "vocabulary.txt", "model.bin"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Explicitly allow immutable model file downloads")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "docs/asr_model_inventory.json").read_text())
    if manifest["repo"] != REPOSITORY or manifest["revision"] != REVISION or set(manifest["files"]) != FILES:
        raise ValueError("Model manifest does not match the reviewed fixture model")
    directory = ROOT / ".models/faster-whisper-tiny.en"
    if not directory.resolve().is_relative_to(ROOT):
        raise ValueError("Model files must remain inside the workspace")
    directory.mkdir(parents=True, exist_ok=True)
    for filename, metadata in manifest["files"].items():
        destination = directory / filename
        if destination.is_symlink():
            raise ValueError("Model files may not be symlinks")
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{filename}"
        fetch_verified(url, destination, metadata["sha256"], args.download)
        if destination.stat().st_size != metadata["bytes"]:
            raise ValueError("Model artifact size mismatch")
    (directory / "provisioning.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"model": REPOSITORY, "revision": REVISION, "verified_files": len(FILES),
                      "language_scope": "English fixture model; broader quality not asserted"}))


if __name__ == "__main__":
    main()
