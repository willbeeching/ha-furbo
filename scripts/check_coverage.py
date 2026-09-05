"""Enforce per-module coverage: every module >=95%, config_flow.py ==100%.

Reads a coverage.xml (Cobertura) produced by pytest-cov. Repo-wide averages
hide a thin module, so this checks each file on its own.
"""

from __future__ import annotations

from pathlib import Path
import sys
import xml.etree.ElementTree as ET

MIN_PER_MODULE = 95.0
FULL_COVERAGE = {"config_flow.py"}


def main(path: str = "coverage.xml") -> int:
    """Return non-zero if any module misses its coverage floor."""
    tree = ET.parse(path)
    failures: list[str] = []
    seen = 0
    for cls in tree.iter("class"):
        filename = cls.get("filename", "")
        if "custom_components/furbo/" not in filename.replace("\\", "/"):
            continue
        seen += 1
        rate = float(cls.get("line-rate", "0")) * 100
        name = Path(filename).name
        floor = 100.0 if name in FULL_COVERAGE else MIN_PER_MODULE
        status = "OK" if rate + 1e-9 >= floor else "FAIL"
        print(f"{status:4} {name:22} {rate:6.2f}% (floor {floor:.0f}%)")
        if status == "FAIL":
            failures.append(f"{name}: {rate:.2f}% < {floor:.0f}%")
    if seen == 0:
        print("No integration modules found in coverage report", file=sys.stderr)
        return 1
    if failures:
        print("\nCoverage below floor:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print(f"\nAll {seen} modules meet their coverage floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
