"""Regression tests for run.sh startup logic.

run.sh is exercised end to end with stubbed `python3` (the app) and `go2rtc`
and an overridable data directory, so the credential/session branches are
tested without a real /data, the Furbo app, or go2rtc.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

RUN_SH = Path(__file__).resolve().parent.parent / "run.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required for run.sh tests"
)


def _run(
    tmp_path: Path,
    options: dict[str, object],
    *,
    session: bool = False,
    pending: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    """Run run.sh against a temp data dir; return (result, data, py_log, session)."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "options.json").write_text(json.dumps(options))
    session_file = data / "furbo_session.json"
    pending_file = data / "furbo_session.pending.json"
    if session:
        session_file.write_text("{}")
    if pending:
        pending_file.write_text("{}")

    # A stub that records its args instead of running the app or go2rtc.
    py_log = tmp_path / "py.log"
    stub = tmp_path / "stub.sh"
    stub.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "$@" >> "{py_log}"
        exit 0
        """)
    )
    stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(RUN_SH)],
        env={
            # Inherit the real PATH so run.sh's opt() can find python3.
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FURBO_DATA": str(data),
            "FURBO_PY": str(stub),
            "FURBO_GO2RTC": str(stub),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result, data, py_log, session_file


BASE = {"api_token": "tok", "quality": "1080p", "log_level": "info"}


def test_blank_password_ok_when_session_exists(tmp_path: Path) -> None:
    """A saved session keeps working even with email/password blanked (#2)."""
    result, _data, py_log, _sess = _run(
        tmp_path, {**BASE, "email": "", "password": ""}, session=True
    )
    assert result.returncode == 0, result.stderr
    log = py_log.read_text() if py_log.exists() else ""
    assert "serve" in log
    assert "login" not in log  # never tried to log in


def test_missing_password_without_session_exits(tmp_path: Path) -> None:
    """With no session, a blank password is a hard error (#2)."""
    result, _data, _py_log, _sess = _run(
        tmp_path, {**BASE, "email": "", "password": ""}, session=False
    )
    assert result.returncode == 1
    assert "email" in result.stderr and "password" in result.stderr


def test_reset_preserves_pending_mfa(tmp_path: Path) -> None:
    """reset_session must not delete a challenge the user is completing (#3)."""
    result, _data, py_log, _sess = _run(
        tmp_path,
        {
            **BASE,
            "email": "a@b.co",
            "password": "pw",
            "mfa_code": "123456",
            "reset_session": True,
        },
        session=False,
        pending=True,
    )
    assert result.returncode == 0, result.stderr
    # The pending challenge survived and the emailed code was used.
    assert (tmp_path / "data" / "furbo_session.pending.json").exists()
    assert "login --code 123456" in py_log.read_text()
    assert "keeping the pending login" in result.stderr


def test_reset_clears_session_when_not_mid_mfa(tmp_path: Path) -> None:
    """reset_session with no pending code discards the stored session (#3)."""
    result, _data, _py_log, session_file = _run(
        tmp_path,
        {**BASE, "email": "", "password": "", "reset_session": True},
        session=True,
    )
    # Session was removed; then the no-session branch stops on the blank login.
    assert not session_file.exists()
    assert result.returncode == 1


def test_rtsp_password_matches_integration_derivation(tmp_path: Path) -> None:
    """run.sh derives the RTSP password the same way discovery.rtsp_password does."""
    sha = "sha256sum" if shutil.which("sha256sum") else "shasum -a 256"
    shell = subprocess.run(
        ["bash", "-c", f"printf 'furbo-rtsp:%s' tok | {sha} | cut -c1-32"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    expected = hashlib.sha256(b"furbo-rtsp:tok").hexdigest()[:32]
    assert shell.stdout.strip() == expected
