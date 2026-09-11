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
nothing proprietary is stored in this repository. The TUTK P2P library is
fetched at build time from the [docker-wyze-bridge](https://github.com/mrlt8/docker-wyze-bridge)
project (the same SDK it uses), and the image is built on your own machine — so
this project neither stores nor distributes any ThroughTek binary. If you later
publish a pre-built image containing the SDK, that is redistribution and is your
responsibility to clear with the vendor.

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

- **Stream URL:** `rtsp://furbo:<rtsp-password>@<addon-hostname>:8554/furbo`
  (RTSP is password-protected; the username is `furbo`)
- **Bridge URL:** `http://<addon-hostname>:8791`
- **Bridge token:** the same `api_token` you set above

When the integration discovers the add-on automatically it fills all three in
for you, including the RTSP password — you normally never type it.

The **RTSP password is not the `api_token`**: it is derived from it (so a leaked
stream URL cannot drive the control API). Auto-discovery fills it in for you; for
a manual setup, compute it yourself from your `api_token`:

```sh
printf 'furbo-rtsp:%s' "<api_token>" | sha256sum | cut -c1-32
```

`<addon-hostname>` is shown in the add-on's info; for a locally-built add-on it
is typically `local-furbo_bridge` (or `<repo-slug>-furbo_bridge`). Because the
add-on uses host networking, the Home Assistant host's own IP with the same
ports also works.

## Options

| Option | Meaning |
| --- | --- |
| `email` / `password` | Furbo account credentials (used only to log in and fetch P2P credentials). |
| `mfa_code` | The emailed verification code, needed once to complete login. Clear it afterwards. |
| `device_id` | The Furbo cloud device id this add-on serves. **Required only if your account has more than one camera** — the add-on lists the ids in its log and refuses to guess. See the multi-camera note below. |
| `reset_session` | Turn on once to discard the stored session and log in again (expired session, changed password, wrong account), then turn it back off. It resets once, not once per restart, so forgetting to turn it off does not keep throwing the new session away; toggle it off and on to reset again. |
| `quality` | `1080p`, `720p` or `360p`. This camera serves 1080p or 360p; 720p falls back to 360p. |
| `api_token` | **Required.** Bearer token the HTTP API requires; the add-on will not start without it. Use a long random value and set the same value in the integration. |
| `log_level` | go2rtc log verbosity. |

## Notes and limits

- **One camera per add-on installation.** This packaged add-on serves a single
  camera; Home Assistant's Add-on Store does not let you install a second copy
  of the same add-on. If your Furbo account has several cameras, the integration
  still shows all of them from the cloud, but only the one you set as `device_id`
  gets live video and P2P controls through this add-on. To bridge more than one
  camera, run additional `furbo_p2p.py serve` + go2rtc instances yourself (see
  the Container/Core note below), each with its own `--device` and port, and add
  their URLs to the integration per camera. Multiple cameras in one add-on is a
  possible future enhancement.
- **Add-ons need Home Assistant OS or Supervised.** On Home Assistant Container
  or Core, run `furbo_p2p.py serve` and go2rtc yourself and point the
  integration at them.
- **One P2P session, always.** State, controls and video all share the single
  session the add-on holds. Video is read from it over `GET /api/stream`, which
  go2rtc consumes on demand. Earlier versions let go2rtc open a second session:
  because the camera's P2P credential is reissued on every cloud fetch, the two
  sessions invalidated each other's key and both failed to authenticate
  (`avClientStartEx -20011`), which could wedge video and controls for hours.
  Talkback did the same thing, opening its own session while the microphone
  was open, so it is not wired up in this version. `talk.sh` is still in the
  image and returns once it reads from the shared session and that has been
  tested against a camera: microphone, video and controls at once, repeated
  microphone start and stop without interrupting video, clean recovery across
  a disconnect, and two cameras staying independent. Note what this does and
  does not buy: it removes the one competing session we know about, not every
  way a session can be invalidated. The cloud reissuing the credential can
  still do it, which is why the bridge logs in again by itself.
- **Spotting a bad connection:** `GET /api/status` reports `session_mode`.
  `LAN` or `P2P` is a direct path; `relay` means the traffic is going out to a
  Kalay relay, which makes commands slow and can stop video starting at all.
  The add-on logs the mode when it connects and warns if a poll runs long.
- **LAN mode needs one broadcast domain.** The SDK finds the camera by sending
  a UDP broadcast, so it reports `LAN` only when the add-on and the camera sit
  on the same subnet. It cannot be pointed at an address instead: the library
  exposes no connect-by-IP, only the broadcast-based `IOTC_Lan_Search`. With
  the camera on another VLAN you have two routes. Allow UDP in both directions
  between the Home Assistant host and the camera, which lets the session come
  up as `P2P`, a direct path that is nearly as quick as `LAN`. Or carry the
  search broadcast across with a UDP broadcast relay. Still seeing `relay`
  after either change means UDP between the two hosts is being dropped.
- **One add-on, every camera.** With `device_id` blank the bridge serves every
  camera on the account, each with its own P2P session, controls and video
  slot, so watching one does not stop another. Set `device_id` to a single id,
  or a comma-separated list, to serve only some of them. `GET /api/cameras`
  lists what is served and names each camera's stream.
- **The API works two ways.** Every operation exists at
  `/api/cameras/<device_id>/...` and unscoped at `/api/...`, where it acts on
  the first camera. The unscoped paths are what a single-camera bridge has
  always exposed, so an older integration keeps working. Each camera's video
  is published by go2rtc as `furbo_<device_id>`, and the first camera also
  keeps the plain `furbo` name.
- **A dead cloud token recovers on its own, usually.** Anything can invalidate
  it: the phone app signing in, a password change, or the token ageing out.
  The add-on logs in again with the device identity it already used, which the
  cloud normally accepts without a code. When it does ask for a code, the
  add-on says so, reports `needs_login` on `GET /api/status`, and backs off
  instead of retrying into the rate limiter. Recover by setting a fresh
  `mfa_code` with `reset_session` on, then turning `reset_session` back off.
- **The integration borrows this add-on's cloud token.** Furbo's login returns
  a token that lasts about a day and nothing to renew it with, so an
  integration on its own has to ask you to sign in again. The add-on holds the
  password, so it can log in again unattended, and serves the token it is
  using on `GET /api/cloud-token`, checking it with the cloud first and
  logging in again if it has died. When the cloud refuses the integration's
  token it takes the add-on's and carries on, and only asks for a sign-in when
  there is no add-on to ask or the add-on is locked out too. The integration
  refuses a token for a different Furbo account, so a bridge signed in
  elsewhere cannot quietly repoint it.
- **One login at a time.** Every camera holds its own session and reconnects
  in a thread of its own, so a dead token is noticed in several places at the
  same moment. Logins are serialized, and a caller that finds another has
  already replaced the token uses that one rather than asking the cloud for a
  second, which is what the rate limiter answers with a lockout. A caller left
  waiting for about a minute is told to come back instead of being held.
- **Renewal is slower than a status read.** Checking the token, and logging in
  when it has died, is up to three cloud calls at twenty seconds each, so
  `GET /api/cloud-token` can take far longer than the rest of the API. The
  integration allows for that and treats a renewal it did not get in time as
  something to retry on the next poll, not as a reason to ask for a sign-in.
  A cloud that could not be reached answers `503` and is waited out the same
  way; a renewal that needs a person, because the cloud wants an emailed code
  or refuses the stored email and password, answers `409` and is acted on
  straight away.
- **The full poll waits on replies, not on a timer.** Each of the thirteen
  settings reads is awaited individually and moves on the moment the camera
  answers, so the sweep costs one round trip per setting. It used to sleep a
  fixed 0.4s after each command, which made the sweep take ten seconds even on
  a LAN session where every reply arrived in a few milliseconds.
- **Writes do not re-read the camera.** A setting change updates the cached
  state from what it wrote and returns. Reading every setting back costs one
  round trip each, which on a relayed session took longer than Home Assistant
  waits, so a write that the camera had accepted was reported as a timeout. A
  setting the camera refuses is left at its cached value, and the five-minute
  full poll still picks up changes made from the Furbo app.
- **1080p** sends a brief 360p preview frame before switching up; give the
  stream a few seconds to reach full resolution.
