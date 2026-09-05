"""Build and verify a reproducible release archive of the integration.

The archive contains only git-tracked files under custom_components/furbo,
with fixed timestamps, permissions, ordering and compression, so the same
commit always yields byte-identical output. After writing it, the archive is
reopened and every member is checked against the tracked tree, and the
manifest version is checked against an expected tag when given.

Usage:
    python scripts/build_release.py [--expect-version X.Y.Z] [--output DIR]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent
PKG = "custom_components/furbo"
EPOCH = (1980, 1, 1, 0, 0, 0)  # zip/tar friendly fixed timestamp


def tracked_files() -> list[str]:
    """Return sorted git-tracked files under the integration package."""
    out = subprocess.run(
        ["git", "ls-files", PKG],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line.strip())


def manifest_version() -> str:
    """Return the version declared in the manifest."""
    data = json.loads((ROOT / PKG / "manifest.json").read_text())
    return str(data["version"])


def build(files: list[str]) -> bytes:
    """Return deterministic .tar.gz bytes for the given files."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for rel in files:
            data = (ROOT / rel).read_bytes()
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            info.mtime = 315532800  # 1980-01-01, fixed
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    # Recompress with a fixed mtime so gzip headers are stable too.
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


def verify(archive: bytes, files: list[str]) -> None:
    """Reopen the archive and check every member against the tree."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getnames()
        if members != files:
            raise SystemExit(f"archive members differ from tracked tree:\n{members}")
        for rel in files:
            extracted = tar.extractfile(rel)
            assert extracted is not None
            if extracted.read() != (ROOT / rel).read_bytes():
                raise SystemExit(f"content mismatch for {rel}")


def main() -> int:
    """Build, verify and checksum the archive."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-version")
    parser.add_argument("--output", default="dist")
    args = parser.parse_args()

    version = manifest_version()
    if args.expect_version and args.expect_version != version:
        raise SystemExit(
            f"manifest version {version} != expected tag {args.expect_version}"
        )

    files = tracked_files()
    if f"{PKG}/manifest.json" not in files:
        raise SystemExit("manifest.json is not tracked")
    archive = build(files)
    verify(archive, files)

    out_dir = ROOT / args.output
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"furbo-{version}.tar.gz"
    (out_dir / name).write_bytes(archive)
    digest = hashlib.sha256(archive).hexdigest()
    (out_dir / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    print(f"built {name} ({len(archive)} bytes, {len(files)} files)")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
