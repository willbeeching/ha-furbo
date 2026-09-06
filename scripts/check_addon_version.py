"""Fail when shipped add-on content changes without a version bump.

Home Assistant installs add-on updates by comparing the ``version`` in
``furbo-bridge/config.yaml``. If the image or its config changes but the
version does not, users never receive the update. This runs in CI on pull
requests: it diffs the shipped add-on files against the base branch and, if any
changed, requires ``config.yaml``'s version to differ too.

Usage:
    python scripts/check_addon_version.py <base-ref>

Only files that end up in the published add-on count. Dev-only files (the test
suite, lint/test config) are excluded so they can change without a bump.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

ADDON_DIR = "furbo-bridge"
CONFIG = f"{ADDON_DIR}/config.yaml"
# Files under furbo-bridge/ that are NOT part of the published image, so they
# may change without forcing a user-facing version bump.
DEV_ONLY = {
    f"{ADDON_DIR}/ruff.toml",
    f"{ADDON_DIR}/pytest.ini",
    f"{ADDON_DIR}/requirements-test.txt",
}
DEV_ONLY_PREFIXES = (f"{ADDON_DIR}/tests/",)

_VERSION_RE = re.compile(r"""^version:\s*["']?([^"'\s]+)["']?\s*$""", re.MULTILINE)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _version_at(ref: str | None) -> str | None:
    """Return the add-on version at a git ref (None = working tree), or None."""
    try:
        text = (
            Path(CONFIG).read_text() if ref is None else _git("show", f"{ref}:{CONFIG}")
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def _shipped_changes(base: str) -> list[str]:
    diff = _git("diff", "--name-only", f"{base}...HEAD", "--", ADDON_DIR)
    changed = [line for line in diff.splitlines() if line]
    return [
        path
        for path in changed
        if path not in DEV_ONLY and not path.startswith(DEV_ONLY_PREFIXES)
    ]


def main(base: str) -> int:
    """Return non-zero if shipped add-on files changed without a version bump."""
    shipped = _shipped_changes(base)
    if not shipped:
        print(f"No shipped add-on files changed against {base}; no bump needed.")
        return 0
    old = _version_at(base)
    new = _version_at(None)
    print(f"Shipped add-on files changed: {', '.join(shipped)}")
    print(f"config.yaml version: {old} (base) -> {new} (head)")
    if new is None:
        print("FAIL: could not read a version from config.yaml.")
        return 1
    if old == new:
        print(
            "FAIL: add-on content changed but the version is unchanged. "
            f"Bump 'version' in {CONFIG}."
        )
        return 1
    print("OK: version was bumped.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
