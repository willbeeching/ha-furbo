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
  # One stream carrying both tracks. The bridge muxes them using the camera's
  # own timestamps, which is the only clock the two share: handed to ffmpeg as
  # two separate pipes they land three seconds apart, and no combination of its
  # timestamp flags closes that, because neither pipe says how it relates to
  # the other. A transport stream has somewhere to put the answer.
  #
  # The video is copied; the audio has to be re-encoded, cheap as it is at
  # 16 kHz mono. ffmpeg's RTSP output cannot carry AAC that arrived in ADTS
  # framing: "AAC with no global headers is currently not supported", because
  # the RTP packetizer needs the codec config that ADTS does not carry and
  # aac_adtstoasc does not supply in time. Re-encoding produces it, and the
  # alternative is no sound at all.
  exec curl -sS -N --fail-with-body \
      -H "Authorization: Bearer ${API_TOKEN}" \
      "${URL%/stream}/av" \
    | exec ffmpeg -hide_banner -loglevel error -fflags nobuffer \
        "${PROBE[@]}" -f mpegts -i - \
        -c:v copy -c:a aac -rtsp_transport tcp -f rtsp "$1"
fi

exec curl -sS -N --fail-with-body \
    -H "Authorization: Bearer ${API_TOKEN}" \
    "$URL" \
  | exec ffmpeg -hide_banner -loglevel error -fflags nobuffer \
      "${PROBE[@]}" \
      -f h264 -i - -c copy -rtsp_transport tcp -f rtsp "$1"
