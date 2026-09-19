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
# ffmpeg will not publish anything until it knows the frame size, and by
# default it reads up to five SECONDS of video looking for the parameter set
# that says so. Over a live feed that is five seconds of wall clock before the
# first frame reaches anyone. Home Assistant's own card buffers into HLS and
# waits it out; HomeKit gives up at around five and reconnects, which turned a
# smooth stream into one frame every ten seconds.
#
# The bridge now opens the stream on a parameter set, so the answer is in the
# first bytes and these limits are never reached. They stay generous rather
# than minimal on purpose: with -analyzeduration 0 a stream that somehow began
# mid-picture would fail outright ("dimensions not set") instead of merely
# starting slowly, and the bridge falling back to sending whatever arrives is
# exactly that case.
# $1 is the RTSP output go2rtc wants; $2 is the camera to read, which is how
# one bridge serves several without them sharing a stream.
DEVICE="${2:-}"
if [ -n "$DEVICE" ]; then
  URL="http://127.0.0.1:${FURBO_API_PORT:-8791}/api/cameras/${DEVICE}/stream"
else
  URL="http://127.0.0.1:${FURBO_API_PORT:-8791}/api/stream"
fi
PROBE=(-probesize 500000 -analyzeduration 1000000)

if [ "${FURBO_AUDIO:-false}" = "true" ]; then
  # Two tracks, so ffmpeg needs two inputs and the video can no longer be a
  # plain pipe on stdin: a named pipe carries it instead while curl fetches the
  # audio on a second connection. Both are bare elementary streams with no
  # container timestamps, so ffmpeg is told to stamp them on arrival; without
  # that it assumes a frame rate for the video and nothing at all for the
  # audio, and the two drift apart within seconds.
  FIFO="$(mktemp -u)"; mkfifo "$FIFO"
  trap 'rm -f "$FIFO"' EXIT
  curl -sS -N --fail-with-body \
      -H "Authorization: Bearer ${API_TOKEN}" \
      "$URL" > "$FIFO" &
  exec curl -sS -N --fail-with-body \
      -H "Authorization: Bearer ${API_TOKEN}" \
      "${URL%/stream}/audio" \
    | exec ffmpeg -hide_banner -loglevel error -fflags nobuffer \
        -use_wallclock_as_timestamps 1 "${PROBE[@]}" -f h264 -i "$FIFO" \
        -use_wallclock_as_timestamps 1 "${PROBE[@]}" -f aac -i - \
        -map 0:v:0 -map 1:a:0 -c copy -rtsp_transport tcp -f rtsp "$1"
fi

exec curl -sS -N --fail-with-body \
    -H "Authorization: Bearer ${API_TOKEN}" \
    "$URL" \
  | exec ffmpeg -hide_banner -loglevel error -fflags nobuffer \
      "${PROBE[@]}" \
      -f h264 -i - -c copy -rtsp_transport tcp -f rtsp "$1"
