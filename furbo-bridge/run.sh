#!/usr/bin/env bash
# Furbo Bridge startup:
#   1. read the add-on options,
#   2. make sure there is a valid cloud session (two-step MFA login),
#   3. start go2rtc (RTSP/WebRTC) and the HTTP state/control API.
set -euo pipefail

# The data directory and the two external commands are overridable so this
# script can be exercised by the test suite without a real /data, Python app or
# go2rtc. In the add-on they are the defaults.
DATA="${FURBO_DATA:-/data}"
PY="${FURBO_PY:-python3}"
GO2RTC="${FURBO_GO2RTC:-go2rtc}"

OPTIONS="$DATA/options.json"
export FURBO_SESSION_FILE="$DATA/furbo_session.json"
PENDING="$DATA/furbo_session.pending.json"

# --- read options (no bashio dependency; options.json is written by HA) -------
# JSON booleans are printed lowercase ("true"/"false"), so `= "true"` works.
opt() { python3 -c "import json,sys; v=json.load(open('$OPTIONS')).get(sys.argv[1],''); print(str(v).lower() if isinstance(v,bool) else v)" "$1" 2>/dev/null || true; }

# sha256 of stdin; sha256sum on Linux (the add-on), shasum on macOS (dev/tests).
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi; }

if [ ! -f "$OPTIONS" ]; then
  echo "[furbo] no options.json; is this running as a Home Assistant add-on?" >&2
  exit 1
fi

export FURBO_EMAIL FURBO_PASSWORD
FURBO_EMAIL="$(opt email)"
FURBO_PASSWORD="$(opt password)"
MFA_CODE="$(opt mfa_code)"
FURBO_QUALITY="$(opt quality)"; FURBO_QUALITY="${FURBO_QUALITY:-1080p}"
# The chosen stream quality lives in a file the on-demand go2rtc stream reads.
# Seed it from the add-on option on first run; the Home Assistant select then
# writes it and that choice persists across restarts.
[ -f "$DATA/quality" ] || echo "$FURBO_QUALITY" > "$DATA/quality"
export API_TOKEN; API_TOKEN="$(opt api_token)"
LOG_LEVEL="$(opt log_level)"; export GO2RTC_LOG="${LOG_LEVEL:-info}"
# Exported so serve, stream.sh and talk.sh all target the same camera. Required
# when the account has more than one camera.
export FURBO_DEVICE; FURBO_DEVICE="$(opt device_id)"
RESET_SESSION="$(opt reset_session)"

if [ -z "$API_TOKEN" ]; then
  echo "[furbo] the 'api_token' option is required: set it to a long random" >&2
  echo "[furbo] string (the Furbo integration uses the same value). Refusing to" >&2
  echo "[furbo] start an unauthenticated API on the network." >&2
  exit 1
fi

# RTSP is protected with a credential DERIVED from the api_token, not the token
# itself, so a leaked stream URL grants only video, never the control API. The
# integration derives the same value; keep this in step with discovery.py.
export RTSP_PASSWORD
RTSP_PASSWORD="$(printf 'furbo-rtsp:%s' "$API_TOKEN" | sha256 | cut -c1-32)"

# Reset the stored session on request, so a user can re-authenticate (expired
# session, changed password, wrong account) without reinstalling the add-on.
# Never discard a pending MFA challenge the user is in the middle of completing.
if [ "$RESET_SESSION" = "true" ]; then
  if [ -n "$MFA_CODE" ] && [ -f "$PENDING" ]; then
    echo "[furbo] reset_session is on but an emailed code is being submitted;" >&2
    echo "[furbo] keeping the pending login. Turn 'reset_session' off." >&2
  else
    echo "[furbo] reset_session is on: clearing the stored session." >&2
    rm -f "$FURBO_SESSION_FILE" "$PENDING"
    echo "[furbo] cleared. Turn 'reset_session' off again; a fresh login follows." >&2
  fi
fi

# --- ensure a cloud session (P2P credentials are fetched fresh each session) --
# email/password are only needed to log in, so they are required here, inside
# the no-session branch — a saved session keeps working after the password
# option is blanked.
if [ ! -f "$FURBO_SESSION_FILE" ]; then
  if [ -z "$FURBO_EMAIL" ] || [ -z "$FURBO_PASSWORD" ]; then
    echo "[furbo] set the 'email' and 'password' options, then restart." >&2
    exit 1
  fi
  if [ -n "$MFA_CODE" ] && [ -f "$PENDING" ]; then
    echo "[furbo] finishing login with the emailed code..." >&2
    "$PY" /app/furbo_p2p.py login --code "$MFA_CODE"
    echo "[furbo] logged in. Clear the 'mfa_code' option so it is not reused." >&2
  else
    echo "[furbo] no session yet — requesting a verification code by email..." >&2
    "$PY" /app/furbo_p2p.py login --send-only || true
    echo "[furbo] ==============================================================" >&2
    echo "[furbo] A code was emailed to $FURBO_EMAIL." >&2
    echo "[furbo] Put it in the add-on's 'mfa_code' option and restart the add-on." >&2
    echo "[furbo] ==============================================================" >&2
    # Idle so the add-on stays 'started' and the logs remain visible.
    sleep infinity
  fi
fi

# --- run go2rtc (video) and the HTTP bridge (state + control) -----------------
# go2rtc's own API is bound to loopback (see go2rtc.yaml); only RTSP/WebRTC and
# the token-protected HTTP API below are reachable off the host.
# One video stream per camera on the account, so the config depends on what
# the login found and cannot be shipped static in the image.
echo "[furbo] writing the go2rtc config for this account's cameras" >&2
"$PY" /app/furbo_p2p.py go2rtc-config --output "$DATA/go2rtc.yaml"

echo "[furbo] starting go2rtc (RTSP :8554, WebRTC :8555, API on loopback)" >&2
"$GO2RTC" -config "$DATA/go2rtc.yaml" &
GO2RTC_PID=$!

echo "[furbo] starting HTTP API on :8791 (bearer auth required)" >&2
"$PY" /app/furbo_p2p.py serve \
  --host 0.0.0.0 --port 8791 --interval 30 \
  --token "$API_TOKEN" &
BRIDGE_PID=$!

# Deliberately not `exec`: exec would replace this shell and discard the trap,
# which is why Supervisor had to kill the add-on with SIGTERM (exit 143) and
# the camera was left holding an orphaned P2P session. Staying alive lets the
# bridge shut that session down first.
term() {
  echo "[furbo] stopping" >&2
  kill -TERM "$BRIDGE_PID" 2>/dev/null || true
  kill -TERM "$GO2RTC_PID" 2>/dev/null || true
  wait "$BRIDGE_PID" 2>/dev/null || true
  exit 0
}
trap term SIGTERM SIGINT

# If either process dies on its own, stop the other and let Supervisor restart.
wait -n
term
