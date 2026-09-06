#!/usr/bin/env bash
# Furbo Bridge startup:
#   1. read the add-on options,
#   2. make sure there is a valid cloud session (two-step MFA login),
#   3. start go2rtc (RTSP/WebRTC) and the HTTP state/control API.
set -euo pipefail

OPTIONS=/data/options.json
export FURBO_SESSION_FILE=/data/furbo_session.json

# --- read options (no bashio dependency; options.json is written by HA) -------
opt() { python3 -c "import json,sys;print(json.load(open('$OPTIONS')).get(sys.argv[1],''))" "$1" 2>/dev/null || true; }

if [ ! -f "$OPTIONS" ]; then
  echo "[furbo] no options.json; is this running as a Home Assistant add-on?" >&2
  exit 1
fi

export FURBO_EMAIL="$(opt email)"
export FURBO_PASSWORD="$(opt password)"
MFA_CODE="$(opt mfa_code)"
export FURBO_QUALITY="$(opt quality)"; FURBO_QUALITY="${FURBO_QUALITY:-1080p}"
API_TOKEN="$(opt api_token)"
LOG_LEVEL="$(opt log_level)"; export GO2RTC_LOG="${LOG_LEVEL:-info}"

if [ -z "$FURBO_EMAIL" ] || [ -z "$FURBO_PASSWORD" ]; then
  echo "[furbo] set the 'email' and 'password' options, then restart." >&2
  exit 1
fi

# --- ensure a cloud session (P2P credentials are fetched fresh each session) --
PENDING=/data/furbo_session.pending.json
if [ ! -f "$FURBO_SESSION_FILE" ]; then
  if [ -n "$MFA_CODE" ] && [ -f "$PENDING" ]; then
    echo "[furbo] finishing login with the emailed code..." >&2
    python3 /app/furbo_p2p.py login --code "$MFA_CODE"
    echo "[furbo] logged in. Clear the 'mfa_code' option so it is not reused." >&2
  else
    echo "[furbo] no session yet — requesting a verification code by email..." >&2
    python3 /app/furbo_p2p.py login --send-only || true
    echo "[furbo] ==============================================================" >&2
    echo "[furbo] A code was emailed to $FURBO_EMAIL." >&2
    echo "[furbo] Put it in the add-on's 'mfa_code' option and restart the add-on." >&2
    echo "[furbo] ==============================================================" >&2
    # Idle so the add-on stays 'started' and the logs remain visible.
    sleep infinity
  fi
fi

# --- run go2rtc (video) and the HTTP bridge (state + control) -----------------
echo "[furbo] starting go2rtc (RTSP :8554, WebRTC :8555, API :1984)" >&2
go2rtc -config /app/go2rtc.yaml &
GO2RTC_PID=$!

term() { echo "[furbo] stopping" >&2; kill "$GO2RTC_PID" 2>/dev/null || true; exit 0; }
trap term SIGTERM SIGINT

echo "[furbo] starting HTTP API on :8791 (auth $([ -n "$API_TOKEN" ] && echo on || echo off))" >&2
exec python3 /app/furbo_p2p.py serve \
  --host 0.0.0.0 --port 8791 --interval 30 \
  ${API_TOKEN:+--token "$API_TOKEN"}
