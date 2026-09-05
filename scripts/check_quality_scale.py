"""Validate custom_components/furbo/quality_scale.yaml.

Checks that: the file lists exactly the pinned rule inventory (offline,
deterministic); every status is done/todo/exempt; every entry carries a
comment; and the manifest claims no official tier.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
QS = ROOT / "custom_components" / "furbo" / "quality_scale.yaml"
PIN = ROOT / "scripts" / "quality_scale_rules.txt"
MANIFEST = ROOT / "custom_components" / "furbo" / "manifest.json"
VALID = {"done", "todo", "exempt"}


def main() -> int:
    """Return non-zero on any inconsistency."""
    expected = [line.strip() for line in PIN.read_text().splitlines() if line.strip()]
    doc = yaml.safe_load(QS.read_text())
    rules = doc.get("rules", {})
    errors: list[str] = []

    missing = [r for r in expected if r not in rules]
    extra = [r for r in rules if r not in expected]
    if missing:
        errors.append(f"missing rules: {missing}")
    if extra:
        errors.append(f"unknown rules (drift from pin): {extra}")

    for name, entry in rules.items():
        status = entry.get("status")
        if status not in VALID:
            errors.append(f"{name}: bad status {status!r}")
        if not str(entry.get("comment", "")).strip():
            errors.append(f"{name}: missing comment/evidence")

    manifest = json.loads(MANIFEST.read_text())
    if "quality_scale" in manifest:
        errors.append("manifest.json must not claim an official quality_scale tier")

    if errors:
        print("quality_scale.yaml problems:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    counts = {s: sum(1 for e in rules.values() if e["status"] == s) for s in VALID}
    print(f"quality_scale.yaml OK: {len(rules)} rules {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
