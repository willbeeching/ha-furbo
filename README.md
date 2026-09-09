# Furbo for Home Assistant

[![CI](https://github.com/willbeeching/ha-furbo/actions/workflows/ci.yml/badge.svg)](https://github.com/willbeeching/ha-furbo/actions/workflows/ci.yml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-furbo)](https://github.com/willbeeching/ha-furbo/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20AI%20tokens-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/willbeeching)

A Home Assistant custom integration for **Furbo** dog cameras — live 1080p
video, treat tossing, camera pan, device settings, and the smart-alert and
pet-activity data from your Furbo account.

> **Live video needs the companion add-on.** Furbo has no local RTSP; its
> video runs over a proprietary protocol that can't run inside Home Assistant.
> The **Furbo Bridge** add-on (in this same repository) runs it on your
> network and hands Home Assistant a normal stream. Install the integration
> for the account features; add the add-on to get video, live camera state,
> pan and the treat controls. See [Live video](#live-video).

## Features

**With the integration alone** (Furbo cloud account):

- **Smart-alert switches** — barking, crying, person, activity and the other
  alerts your camera reports, plus a **notification-frequency** select for the
  main ones (barking, person, activity)
- **Pet activity sensors** — today's notable events with the daily summary,
  plus barking and activity counts
- **Last event** — timestamp, caption, action and location of the most recent
  detection
- **Subscription** status and days left

**With the Furbo Bridge add-on** as well:

- **Camera** — live video (WebRTC / RTSP), with snapshots
- **Video quality** — 1080p / 720p / 360p select
- **Controls** — camera power, speaker volume, night vision, barking
  sensitivity, auto pet tracking, auto zoom, voice control, treat size,
  the on/off schedule and Calm My Pet
- **Pan** left / right, **toss treat** and **play treat sound**
- **Talkback** — speak to your pet from the camera's live view (via go2rtc).
  This is one-way (your microphone to the camera speaker); the camera's own
  audio is not yet carried back in the stream
- **Treat toss sound** shown read-only (the app picks presets by sound file)

## Requirements

| | |
|---|---|
| Home Assistant | 2025.2.0 or newer |
| A Furbo account | email + password (a Furbo Nanny subscription is only needed for the event/activity sensors) |
| For video | the **Furbo Bridge** add-on (Home Assistant OS or Supervised) |

## Installation

### 1. Integration (HACS)

1. In HACS → three-dots menu → **Custom repositories**
2. Add `https://github.com/willbeeching/ha-furbo` as an **Integration**
3. Install **Furbo**, then restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Furbo**, and sign in
   with your Furbo email, password and the code Furbo emails you

That gives you the alert switches and activity sensors. For video and the
camera controls, add the bridge:

### 2. Furbo Bridge add-on (for video)

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and add
   `https://github.com/willbeeching/ha-furbo`
2. Install **Furbo Bridge**, open its **Configuration**, and set your Furbo
   email, password and an **api_token** (any long random string)
3. **Start** it and follow its log to enter the emailed code once — see the
   [add-on docs](furbo-bridge/DOCS.md)

The integration finds the running add-on automatically: open the Furbo
integration's **Configure** and the camera's stream URL, bridge URL and token
are already filled in — just submit.

## Live video

Furbo offers no RTSP or ONVIF stream. Its app receives video over a
proprietary peer-to-peer protocol that needs a helper on the camera's network,
which is what the Furbo Bridge add-on provides:

```
Furbo camera  ──(P2P, LAN)──▶  Furbo Bridge add-on  ──(RTSP/WebRTC)──▶  Home Assistant
```

The add-on runs the P2P session and republishes the video with
[go2rtc](https://github.com/AlexxIT/go2rtc), so the camera entity plays it
like any other stream. It also exposes the live camera state and controls that
the integration's pan, treat and settings entities use.

On **Home Assistant Container or Core** (no add-on support) you can run the
bridge yourself — see [`furbo-bridge/`](furbo-bridge/) — and enter its URLs in
the integration's options by hand.

## Removing it

**Settings → Devices & Services → Furbo → ⋮ → Delete** removes the entry and
all its entities. If you added the bridge, uninstall the add-on too, and remove
both repositories from HACS and the Add-on Store. Anything you changed on the
camera (volume, night mode, alert settings) stays as you set it.

## Supported devices

Developed and verified on a **Furbo 360 (`FB0030`)**. Other Furbo models on the
same account API are expected to work but are unverified.

**More than one camera** on the account works: the integration creates a device
and entities for each, and one Furbo Bridge add-on serves them all, holding a
separate session per camera. Leave the add-on's `device_id` blank for every
camera, or set it (a single id, or several separated by commas) to serve only
some. Multi-camera support has been built and tested against the add-on's own
test suite, but not yet against two physical cameras, so reports are welcome.

## Support

This was reverse-engineered and vibe-coded over many late nights, and the AI tokens don't pay for
themselves. If this integration ever saved you reaching for your phone to check on the dog,
consider [buying me some AI tokens](https://buymeacoffee.com/willbeeching) ☕🤖. Entirely optional —
bug reports and stars are appreciated just as much.
