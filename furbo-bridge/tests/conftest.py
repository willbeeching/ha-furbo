"""Test configuration for the Furbo Bridge add-on.

Puts the add-on source on the import path and points the quality file at a
temp location so tests never touch ``/data``.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

# furbo_bridge reads QUALITY_FILE at import time; give it a writable temp path
# before it is imported anywhere so no test writes to /data/quality.
os.environ.setdefault(
    "FURBO_QUALITY_FILE",
    str(Path(tempfile.gettempdir()) / "furbo_bridge_test_quality"),
)
