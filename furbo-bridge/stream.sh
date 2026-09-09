#!/usr/bin/env bash
# go2rtc exec wrapper: pull H.264 from the bridge's existing P2P session.
#
# This deliberately does NOT run `furbo_p2p.py stream`. Doing so opened a second
# P2P session, and each session fetches its own P2PAccountKey from the cloud,
# which reissues the key and invalidates the other session's. Both then failed
# to authenticate (avClientStartEx -20011) until one happened to win the race.
# Reading from the bridge keeps it to one session and one credential.
#
# go2rtc runs this on demand, only while a viewer is connected, and kills the
# process group when the last one leaves; the bridge then stops the video and
# keeps the session for the controls.
set -euo pipefail
# $1 is the RTSP output go2rtc wants; $2 is the camera to read, which is how
# one bridge serves several without them sharing a stream.
DEVICE="${2:-}"
if [ -n "$DEVICE" ]; then
  URL="http://127.0.0.1:${FURBO_API_PORT:-8791}/api/cameras/${DEVICE}/stream"
else
  URL="http://127.0.0.1:${FURBO_API_PORT:-8791}/api/stream"
fi
exec curl -sS -N --fail-with-body \
    -H "Authorization: Bearer ${API_TOKEN}" \
    "$URL" \
  | exec ffmpeg -hide_banner -loglevel error -fflags nobuffer \
      -f h264 -i - -c copy -rtsp_transport tcp -f rtsp "$1"
