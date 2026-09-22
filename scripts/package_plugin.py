"""Build the private plugin archive without installing anything outside the workspace."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    plugin = ROOT / "plugin"
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    configuration = json.loads((plugin / manifest["mcpServers"]).read_text())
    if "video-ingest" not in configuration["mcpServers"]:
        raise ValueError("Missing video-ingest MCP configuration")
    destination = ROOT / "dist" / f"video-ingest-plugin-{manifest['version']}.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(plugin.rglob("*")):
            if path.is_symlink():
                raise ValueError("Plugin archive must not contain symlinks")
            if path.is_file():
                archive.write(path, str(path.relative_to(plugin)))
    record = {"archive": str(destination.relative_to(ROOT)),
              "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
              "version": manifest["version"], "scope": "private workspace-backed Codex plugin",
              "installed": False}
    (ROOT / "docs/runs/plugin-package.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
