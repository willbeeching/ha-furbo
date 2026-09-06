# Furbo Bridge

Furbo cameras have no local RTSP or ONVIF; every live view goes through
ThroughTek's proprietary TUTK/Kalay P2P protocol, which needs the vendor SDK
and real host networking and so cannot run inside Home Assistant. This add-on
runs that P2P session **outside** Home Assistant, on the LAN, and exposes it in
two forms the Furbo integration and Home Assistant can consume:

- **RTSP / WebRTC** live video, via go2rtc — point the camera entity at it.
- An **HTTP JSON API** for state and controls (volume, night vision, barking
  sensitivity, tracking, pan, treat toss) — the integration polls and drives it.

The TUTK library and go2rtc are downloaded when the add-on image is built;
nothing proprietary is stored in the repository.

## Setup

1. **Install** the add-on (this repository must be added under Add-on Store →
   ⋮ → Repositories first).
2. In **Configuration**, set your Furbo **email** and **password**, pick a
   video **quality**, and set an **api_token**. Generate a long random value —
   for example `openssl rand -hex 32` — and use the **same** value as the
   integration's bridge token. The add-on refuses to start without one, because
   the HTTP API can toss treats and change camera settings.
3. **Start** the add-on and open the **Log**. On the first start it emails a
   verification code and prints:
   *"A code was emailed to … Put it in the 'mfa_code' option and restart."*
4. Put that code in **mfa_code**, **restart**, and watch the log for
   *"starting HTTP API on :8791"*. Clear **mfa_code** afterwards so it is not
   reused. The session is saved to the add-on's storage and survives restarts.

## Wiring the integration to it

The add-on is reachable from Home Assistant at the add-on's own hostname on
the Supervisor network. In the Furbo integration's options, per camera set:

- **Stream URL:** `rtsp://furbo:<api_token>@<addon-hostname>:8554/furbo`
  (RTSP is password-protected; the username is `furbo` and the password is your
  `api_token`)
- **Bridge URL:** `http://<addon-hostname>:8791`
- **Bridge token:** the same `api_token` you set above

When the integration discovers the add-on automatically it fills all three in
for you, including the RTSP credentials.

`<addon-hostname>` is shown in the add-on's info; for a locally-built add-on it
is typically `local-furbo_bridge` (or `<repo-slug>-furbo_bridge`). Because the
add-on uses host networking, the Home Assistant host's own IP with the same
ports also works.

## Options

| Option | Meaning |
| --- | --- |
| `email` / `password` | Furbo account credentials (used only to log in and fetch P2P credentials). |
| `mfa_code` | The emailed verification code, needed once to complete login. Clear it afterwards. |
| `device_id` | The Furbo cloud device id this add-on serves. **Required only if your account has more than one camera** — the add-on lists the ids in its log and refuses to guess. One add-on instance per camera. |
| `reset_session` | Turn on once to discard the stored session and log in again (expired session, changed password, wrong account), then turn it back off. |
| `quality` | `1080p`, `720p` or `360p`. This camera serves 1080p or 360p; 720p falls back to 360p. |
| `api_token` | **Required.** Bearer token the HTTP API requires; the add-on will not start without it. Use a long random value and set the same value in the integration. |
| `log_level` | go2rtc log verbosity. |

## Notes and limits

- **Add-ons need Home Assistant OS or Supervised.** On Home Assistant Container
  or Core, run `furbo_p2p.py serve` and go2rtc yourself and point the
  integration at them.
- **Concurrent sessions:** the state API holds one P2P session; go2rtc opens a
  second only while someone is watching. Whether this camera is happy with two
  simultaneous sessions is the main thing to confirm on your LAN — if live view
  disturbs state polling, that is why.
- **1080p** sends a brief 360p preview frame before switching up; give the
  stream a few seconds to reach full resolution.
