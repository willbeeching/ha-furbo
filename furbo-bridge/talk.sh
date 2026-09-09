#!/usr/bin/env bash
# Talkback backchannel: take the microphone audio go2rtc sends on stdin,
# transcode it to G.711 mu-law 16 kHz mono (what the camera speaker wants), and
# push it to the camera via furbo_p2p.py talk.
#
# go2rtc's exec backchannel (v1.9.9) offers the sender PCMA/8000 and PCM, and a
# WebRTC browser negotiates PCMA (G.711 A-law, 8 kHz mono). So the bytes on
# stdin are raw A-law at 8 kHz, which is what ffmpeg is told to expect here.
#
# go2rtc runs this only while a client has the microphone open and kills the
# process group when it stops.
exec ffmpeg -hide_banner -loglevel error -f alaw -ar 8000 -ac 1 -i - \
    -ar 16000 -ac 1 -f mulaw - \
  | python3 /app/furbo_p2p.py talk --frame 320 ${1:+--device "$1"}
