#!/usr/bin/env bash
# go2rtc two-way audio backchannel: receive the microphone audio go2rtc sends
# on stdin, transcode it to G.711 mu-law 16 kHz mono (what the camera wants),
# and push it to the camera's speaker via furbo_p2p.py talk.
#
# go2rtc runs this only while a client has two-way audio open, and kills the
# process group when it stops.
exec ffmpeg -hide_banner -loglevel error -f s16le -ar 16000 -ac 1 -i - \
    -ar 16000 -ac 1 -f mulaw - \
  | exec python3 /app/furbo_p2p.py talk --frame 320
