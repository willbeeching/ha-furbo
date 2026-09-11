#!/usr/bin/env python3
"""Read one version's section out of the integration changelog.

GitHub's generated notes are built from merged pull requests. This repository
is developed by pushing to main, so they come out as nothing but a "Full
Changelog" compare link, which tells a user reading a HACS update prompt
nothing at all about what changed. The release workflow uses this instead, so
the release body is the changelog entry itself.

Used twice: the release workflow writes the section to a file and hands it to
``gh release create --notes-file``, and CI runs it with no arguments to fail a
push whose manifest version has no entry yet. Missing notes are an error in
both, because a release nobody can read is the thing this exists to prevent.

Usage:
    python scripts/release_notes.py [--version X.Y.Z] [--output FILE]

With no --version it reads the version from the integration manifest.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
MANIFEST = ROOT / "custom_components" / "furbo" / "manifest.json"


def manifest_version() -> str:
    """Return the version the integration currently declares."""
    return str(json.loads(MANIFEST.read_text())["version"])


def section(version: str, text: str) -> str | None:
    """Return the body under this version's heading, or None when absent.

    A heading is a line like ``## 1.3.2`` (any depth, an optional leading v).
    The section runs to the next heading of any depth.
    """
    start = re.compile(rf"^(#+)\s*v?{re.escape(version)}\s*$", re.MULTILINE)
    match = start.search(text)
    if match is None:
        return None
    rest = text[match.end() :]
    end = re.search(r"^#+\s", rest, re.MULTILINE)
    return (rest[: end.start()] if end else rest).strip()


def main() -> int:
    """Print or write the section, or explain what to add and fail."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="defaults to the manifest version")
    parser.add_argument("--output", help="write here instead of stdout")
    args = parser.parse_args()

    version = args.version or manifest_version()
    # A prerelease ships the base version's notes: 1.3.2-beta.1 -> 1.3.2.
    base = version.split("-", 1)[0]
    body = section(base, CHANGELOG.read_text())
    if not body:
        print(
            f"{CHANGELOG.name} has no entry for {base}. Add a '## {base}' "
            "section saying what changed, so the release and the HACS update "
            "prompt are readable.",
            file=sys.stderr,
        )
        return 1

    if args.output:
        Path(args.output).write_text(body + "\n")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
