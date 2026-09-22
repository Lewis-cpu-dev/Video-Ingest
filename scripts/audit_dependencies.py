"""Inventory installed workspace distributions and retain their available license texts."""
from __future__ import annotations

import email
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT / "docs/licenses"
    output.mkdir(exist_ok=True)
    rows = []
    for path in sorted((ROOT / ".venv/lib").glob("python*/site-packages/*.dist-info")):
        metadata = email.message_from_string((path / "METADATA").read_text())
        copied = []
        for license_file in path.rglob("*"):
            if license_file.is_file() and any(word in license_file.name.lower() for word in ("license", "copying")):
                data = license_file.read_bytes()
                # Keep nested-license names unique instead of overwriting a transitive LICENSE.
                relative = str(license_file.relative_to(path)).replace("/", "__")
                destination = output / (path.name + "__" + relative)
                destination.write_bytes(data)
                copied.append({"file": str(destination.relative_to(ROOT)),
                               "sha256": hashlib.sha256(data).hexdigest()})
        rows.append({"name": metadata["Name"], "version": metadata["Version"],
                     "license": metadata["License-Expression"] or metadata["License"], "license_files": copied})
    (ROOT / "docs/dependency_inventory.json").write_text(json.dumps(rows, indent=2) + "\n")
    print(f"Recorded {len(rows)} workspace distributions")


if __name__ == "__main__":
    main()
