"""Explicitly provision the reviewed Deno runtime inside this workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.9.7"
ARCHIVE_SHA256 = "c6527f24f4b16031d3ae4fa9f658d5f11534c8d84ce7dc8502420280919c3490"
BINARY_SHA256 = "ce6a052beb97c2b92de67077e3f3924ba7c9661ede0d8dc2f66a116a3c841f21"
LICENSE_SHA256 = "f62497fffecc0852960c8d3e6934b9db86d16396e9b604072e923892cae3a588"
ARCHIVE_URL = f"https://github.com/denoland/deno/releases/download/v{VERSION}/deno-x86_64-unknown-linux-gnu.zip"
LICENSE_URL = f"https://raw.githubusercontent.com/denoland/deno/v{VERSION}/LICENSE.md"


def checksum(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def fetch_verified(url: str, destination: Path, expected: str, allow_download: bool):
    if destination.is_symlink() or not destination.resolve().is_relative_to(ROOT):
        raise ValueError("Provisioning artifacts must remain inside the workspace")
    if destination.is_file() and checksum(destination) == expected:
        return
    if not allow_download:
        raise SystemExit("A verified artifact is missing; explicitly pass --download to provision it")
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as source, temporary.open("wb") as output:
            total = 0
            while block := source.read(1024**2):
                total += len(block)
                if total > 100 * 1024**2:
                    raise ValueError("Provisioning artifact exceeds the fixed 100 MiB limit")
                output.write(block)
        if checksum(temporary) != expected:
            raise ValueError("Provisioning checksum mismatch")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Explicitly allow download of pinned artifacts")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        parser.error("This reviewed runtime build is for Linux x86-64 only")
    directory = ROOT / f".tools/deno-{VERSION}"
    if not directory.resolve().is_relative_to(ROOT):
        raise ValueError("Provisioning directory must remain inside the workspace")
    directory.mkdir(parents=True, exist_ok=True)
    archive, license_file, binary = directory / "deno.zip", directory / "LICENSE.md", directory / "deno"
    fetch_verified(ARCHIVE_URL, archive, ARCHIVE_SHA256, args.download)
    fetch_verified(LICENSE_URL, license_file, LICENSE_SHA256, args.download)
    if not binary.is_file() or checksum(binary) != BINARY_SHA256:
        with zipfile.ZipFile(archive) as package:
            if package.namelist() != ["deno"]:
                raise ValueError("Unexpected archive layout")
            data = package.read("deno")
        if hashlib.sha256(data).hexdigest() != BINARY_SHA256:
            raise ValueError("Executable checksum mismatch")
        temporary = directory / "deno.pending"
        temporary.write_bytes(data)
        temporary.chmod(0o755)
        temporary.replace(binary)
    report = {"version": VERSION, "release_url": f"https://github.com/denoland/deno/releases/tag/v{VERSION}",
              "archive_url": ARCHIVE_URL, "archive_sha256": ARCHIVE_SHA256, "binary_sha256": BINARY_SHA256,
              "license": "MIT", "license_sha256": LICENSE_SHA256,
              "local_executable": str(binary.relative_to(ROOT)),
              "verified_against": "Pinned digests reviewed against first-party immutable GitHub release API"}
    (ROOT / "docs/deno_runtime_inventory.json").write_text(json.dumps(report, indent=2) + "\n")
    (ROOT / f"docs/licenses/deno-{VERSION}-LICENSE.md").write_bytes(license_file.read_bytes())
    print(f"Verified .tools/deno-{VERSION}/deno")


if __name__ == "__main__":
    main()
