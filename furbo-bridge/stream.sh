#!/usr/bin/env bash
# go2rtc exec wrapper: stream the Furbo at the chosen quality and remux to RTSP.
#
# go2rtc runs this on demand (only while a viewer is connected) and substitutes
# {output} with an internal RTSP endpoint. The quality is read fresh each run,
# so the Home Assistant "Video quality" select takes effect on the next view.
# go2rtc runs the command in its own process group and kills the group on stop,
# so the piped ffmpeg and stream both exit with it.
quality="$(cat /data/quality 2>/dev/null || echo 1080p)"
exec python3 /app/furbo_p2p.py stream --quality "$quality" \
  | exec ffmpeg -hide_banner -loglevel error -fflags nobuffer \
      -f h264 -i - -c copy -rtsp_transport tcp -f rtsp "$1"
