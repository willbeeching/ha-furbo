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
    py_body: str = "exit 0",
    timeout: float = 15,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    """Run run.sh against a temp data dir; return (result, data, py_log, session).

    ``py_body`` is the tail of the stubbed app: it decides what that run of the
    app "did" -- succeeded, failed, wrote a session, wrote a pending login --
    which is exactly what run.sh now reads to decide what to tell the user.
    """
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "options.json").write_text(json.dumps(options))
    session_file = data / "furbo_session.json"
    pending_file = data / "furbo_session.pending.json"
    if session:
        session_file.write_text("{}")
    if pending:
        pending_file.write_text("{}")

    # Stubs that record their args instead of running the app or go2rtc.
    py_log = tmp_path / "py.log"
    stub = tmp_path / "stub.sh"
    stub.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "$@" >> "{py_log}"
        {py_body}
        """)
    )
    stub.chmod(0o755)

    # go2rtc runs for the life of the add-on, and run.sh now shuts everything
    # down as soon as either child exits. A stub that returned immediately
    # would race the bridge it just launched, so this one stays up and lets
    # the (immediately exiting) app stub end the run.
    go2rtc_stub = tmp_path / "go2rtc.sh"
    go2rtc_stub.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "$@" >> "{py_log}"
        trap 'exit 0' TERM
        sleep 5 & wait
        """)
    )
    go2rtc_stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(RUN_SH)],
        env={
            # Inherit the real PATH so run.sh's opt() can find python3.
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FURBO_DATA": str(data),
            "FURBO_PY": str(stub),
            "FURBO_GO2RTC": str(go2rtc_stub),
        },
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result, data, py_log, session_file


def _run_until_idle(tmp_path: Path, options: dict[str, object], *, py_body: str) -> str:
    """Run run.sh as far as the 'waiting for a person' idle, return its stderr.

    The branches that ask for a code deliberately sleep for ever so the add-on
    stays started and its log stays on screen, so the only way to read what
    they said is to let it get there and then stop it.
    """
    try:
        result, _, _, _ = _run(tmp_path, options, py_body=py_body, timeout=4)
    except subprocess.TimeoutExpired as expired:
        return (
            (expired.stderr or b"").decode()
            if isinstance(expired.stderr, bytes)
            else (expired.stderr or "")
        )
    return result.stderr


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


def test_go2rtc_config_is_written_before_go2rtc_starts(tmp_path: Path) -> None:
    """The streams depend on which cameras the account has, so they cannot be
    shipped static in the image: run.sh generates the config each start."""
    result, data, py_log, _sess = _run(
        tmp_path, {**BASE, "email": "e", "password": "p"}, session=True
    )
    assert result.returncode == 0, result.stderr
    log = py_log.read_text()
    assert "go2rtc-config" in log
    # And before the bridge itself, so go2rtc never reads a stale config.
    assert log.index("go2rtc-config") < log.index("serve")
    assert f"--output {data}/go2rtc.yaml" in log


def test_a_code_is_only_announced_when_one_was_sent(tmp_path: Path) -> None:
    """A login that failed must not be reported as an email on its way.

    The send was run with its failure swallowed and the "a code was emailed"
    message printed regardless, so a login refused by the cloud sent people to
    watch an inbox that nothing was ever going to arrive in.
    """
    stderr = _run_until_idle(
        tmp_path,
        {**BASE, "email": "a@b.c", "password": "pw"},
        py_body="exit 1",
    )
    assert "No code was sent" in stderr
    assert "A code was emailed" not in stderr


def test_a_code_that_was_sent_is_announced(tmp_path: Path) -> None:
    """The pending login on disk is what says a code really went out."""
    stderr = _run_until_idle(
        tmp_path,
        {**BASE, "email": "a@b.c", "password": "pw"},
        py_body='touch "${FURBO_SESSION_FILE%.json}.pending.json"; exit 0',
    )
    assert "A code was emailed to a@b.c" in stderr
    assert "No code was sent" not in stderr


def test_a_stale_code_is_called_out_rather_than_dropped(tmp_path: Path) -> None:
    """An mfa_code with no login waiting for it is said out loud.

    It used to fall through to requesting a new code, which silently made the
    code the user had just typed useless -- and the next restart then verified
    that stale code against the new login and blamed the user for it.
    """
    stderr = _run_until_idle(
        tmp_path,
        {**BASE, "email": "a@b.c", "password": "pw", "mfa_code": "1234"},
        py_body='touch "${FURBO_SESSION_FILE%.json}.pending.json"; exit 0',
    )
    assert "no login is waiting for one" in stderr
    assert "use the code from the next" in stderr.lower()


def test_a_login_needing_no_code_goes_straight_on(tmp_path: Path) -> None:
    """Some accounts log in without a code; that must not look like waiting."""
    result, _, py_log, _ = _run(
        tmp_path,
        {**BASE, "email": "a@b.c", "password": "pw"},
        py_body='printf "{}" > "$FURBO_SESSION_FILE"; exit 0',
    )
    assert "no code was needed" in result.stderr
    assert "A code was emailed" not in result.stderr
    # And it carried on to start the bridge rather than idling.
    assert "serve" in py_log.read_text()


def test_reset_session_left_on_does_not_keep_wiping(tmp_path: Path) -> None:
    """The option asks for a reset, not for one on every restart.

    Left on by accident it threw away a working session every time the add-on
    started, which sends the user round the emailed-code loop again and looks
    exactly like the add-on refusing to start.
    """
    options = {**BASE, "email": "a@b.c", "password": "pw", "reset_session": True}
    writes_session = 'printf "{}" > "$FURBO_SESSION_FILE"; exit 0'

    # First start: a session exists, so it is reset and a new one is made.
    first, data, _, session_file = _run(tmp_path, options, session=True, py_body=writes_session)
    assert "clearing the stored session" in first.stderr
    assert (data / "furbo_reset_done").exists()
    assert session_file.exists()

    # Restarting with the option still on keeps that session.
    second, _, _, _ = _run(tmp_path, options, py_body="exit 0")
    assert "already been" in second.stderr
    assert "clearing the stored session" not in second.stderr
    assert session_file.exists()


def test_turning_reset_session_off_arms_it_again(tmp_path: Path) -> None:
    """Toggling it off and on resets once more, so a real reset still works."""
    on = {**BASE, "email": "a@b.c", "password": "pw", "reset_session": True}
    off = {**BASE, "email": "a@b.c", "password": "pw", "reset_session": False}
    writes_session = 'printf "{}" > "$FURBO_SESSION_FILE"; exit 0'

    _run(tmp_path, on, session=True, py_body=writes_session)
    # Off: the marker goes, so the option is armed for next time.
    _, data, _, _ = _run(tmp_path, off, py_body="exit 0")
    assert not (data / "furbo_reset_done").exists()

    again, _, _, _ = _run(tmp_path, on, py_body=writes_session)
    assert "clearing the stored session" in again.stderr
