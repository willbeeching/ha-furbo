"""Validate custom_components/furbo/quality_scale.yaml.

Checks that: the file lists exactly the pinned rule inventory (offline,
deterministic); every status is done/todo/exempt; every entry carries a
comment; the manifest claims no official tier; and, when the ``brands`` rule
claims done, that the shipped brand images are present at the sizes Home
Assistant expects.
"""

from __future__ import annotations

import json
from pathlib import Path
import struct
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
QS = ROOT / "custom_components" / "furbo" / "quality_scale.yaml"
PIN = ROOT / "scripts" / "quality_scale_rules.txt"
MANIFEST = ROOT / "custom_components" / "furbo" / "manifest.json"
BRAND_DIR = ROOT / "custom_components" / "furbo" / "brand"
ADDON_DIR = ROOT / "furbo-bridge"
VALID = {"done", "todo", "exempt"}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Home Assistant serves a custom integration's own brand images from
# <integration>/brand/ since 2026.3 (homeassistant/components/brands, gated on
# Integration.has_branding, which is just "brand" being a top-level entry).
# Only these eight filenames are served, and everything except icon.png falls
# back to another image, so icon.png is the one file that must exist.
# Sizes follow https://github.com/home-assistant/brands#image-requirements
ALLOWED_IMAGES = frozenset(
    {
        "icon.png",
        "logo.png",
        "icon@2x.png",
        "logo@2x.png",
        "dark_icon.png",
        "dark_logo.png",
        "dark_icon@2x.png",
        "dark_logo@2x.png",
    }
)
ICON_SIZES = {"icon.png": (256, 256), "icon@2x.png": (512, 512)}
LOGO_BASES = ("logo", "dark_logo")
# The shortest side of a logo, per variant.
MIN_SHORTEST_SIDE = {"": 128, "@2x": 256}
MAX_SHORTEST_SIDE = {"": 256, "@2x": 512}
MIN_ADDON_ICON = 128


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG IHDR. No imaging library required."""
    data = path.read_bytes()[:24]
    if data[:8] != PNG_SIGNATURE:
        raise ValueError(f"{path.name} is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _tracked(paths: list[Path]) -> set[str]:
    """Return the subset of paths git tracks, so they reach archive and image."""
    rel = [str(p.relative_to(ROOT)) for p in paths]
    out = subprocess.run(
        ["git", "ls-files", "--", *rel],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def _check_icons(errors: list[str]) -> list[Path]:
    """Check the integration icons are present at their exact sizes."""
    found: list[Path] = []
    for name, expected in ICON_SIZES.items():
        path = BRAND_DIR / name
        if not path.is_file():
            errors.append(f"brands: missing {path.relative_to(ROOT)}")
            continue
        found.append(path)
        size = png_size(path)
        if size != expected:
            errors.append(f"brands: {name} is {size}, expected {expected}")
    return found


def _check_logo_pair(base: str, errors: list[str]) -> list[Path]:
    """Check one logo pair: both sizes present, capped, and @2x exactly twice."""
    one, two = BRAND_DIR / f"{base}.png", BRAND_DIR / f"{base}@2x.png"
    if not (one.is_file() and two.is_file()):
        errors.append(f"brands: {base} must ship both the 1x and the @2x")
        return []
    for path, suffix in ((one, ""), (two, "@2x")):
        shortest = min(png_size(path))
        low, high = MIN_SHORTEST_SIDE[suffix], MAX_SHORTEST_SIDE[suffix]
        if not low <= shortest <= high:
            errors.append(
                f"brands: {path.name} shortest side {shortest} outside {low}-{high}"
            )
    if png_size(two) != tuple(2 * v for v in png_size(one)):
        errors.append(f"brands: {base}@2x.png is not exactly twice {base}.png")
    return [one, two]


def _check_addon_images(errors: list[str]) -> list[Path]:
    """Check the add-on ships a square icon and a logo."""
    icon, logo = ADDON_DIR / "icon.png", ADDON_DIR / "logo.png"
    for path in (icon, logo):
        if not path.is_file():
            errors.append(f"brands: missing {path.relative_to(ROOT)}")
    if icon.is_file():
        width, height = png_size(icon)
        # Supervisor requires a 1:1 add-on icon and recommends at least 128px.
        if width != height:
            errors.append(f"brands: add-on icon.png is {width}x{height}, not square")
        elif width < MIN_ADDON_ICON:
            errors.append(
                f"brands: add-on icon.png is {width}px, below {MIN_ADDON_ICON}"
            )
    return [p for p in (icon, logo) if p.is_file()]


def _check_no_stray_files(errors: list[str]) -> None:
    """Home Assistant serves only the eight known names; anything else is dead."""
    for path in sorted(BRAND_DIR.iterdir()):
        if path.name not in ALLOWED_IMAGES:
            errors.append(f"brands: {path.name} is not one of the served brand images")
        elif path.is_symlink():
            errors.append(f"brands: {path.name} is a symlink, which does not ship")


def check_brand_images(errors: list[str]) -> None:
    """Validate the brand images the integration and the add-on ship."""
    if not BRAND_DIR.is_dir():
        errors.append("brands: custom_components/furbo/brand/ is missing")
        return
    _check_no_stray_files(errors)
    required = _check_icons(errors)
    supplied = [b for b in LOGO_BASES if (BRAND_DIR / f"{b}.png").is_file()]
    if not supplied:
        errors.append("brands: no logo pair supplied")
    for base in supplied:
        required += _check_logo_pair(base, errors)
    required += _check_addon_images(errors)

    untracked = {str(p.relative_to(ROOT)) for p in required} - _tracked(required)
    for rel in sorted(untracked):
        errors.append(f"brands: {rel} is not tracked by git, so it would not ship")


def main() -> int:
    """Return non-zero on any inconsistency."""
    expected = [
        line.split(":", 1)[0].strip()
        for line in PIN.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
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

    if rules.get("brands", {}).get("status") == "done":
        check_brand_images(errors)

    if errors:
        print("quality_scale.yaml problems:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    counts = {s: sum(1 for e in rules.values() if e["status"] == s) for s in VALID}
    print(f"quality_scale.yaml OK: {len(rules)} rules {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
