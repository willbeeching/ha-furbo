# Furbo for Home Assistant

A custom integration that brings a Furbo dog camera into Home Assistant. Over
Furbo's cloud API it exposes the camera's smart-alert settings as switches and
the pet activity the cloud reports (subscription state, the day's detected
events, and hourly activity counts) as sensors. Live video is shown through a
camera entity fed by a stream URL you configure, because Furbo's video runs
over a proprietary P2P protocol that needs a bridge on the camera's LAN (see
[Live video](#live-video)).

Everything the cloud side exposes is proven against the live Furbo API. The
P2P bridge that produces the stream lives in a separate development branch,
kept out of this integration package. The bridge itself is verified on real
hardware (1920x1080 H.264 at 25 fps from a Furbo 360); the last hop, go2rtc
into this camera entity, has not yet been run end to end.

## Supported devices

Verified on a **Furbo 360 (product id `FB0030`, firmware 108)** on 2026-09-05.
Other Furbo models that use the same `product.furbo.co` account API are
expected to work but are unverified; the model name shown falls back to the
raw product id for anything not in the known list. Furbo Mini and camera
generations that predate the smart-alert cloud API are **not** supported.
Requires an active Furbo account; a Furbo Nanny subscription is not required
for the alert switches but is required for the event and activity sensors to
contain data.

## What you get

Per camera (one Home Assistant device, linked under a "Furbo account" hub):

- **Camera** entity with live streaming, created when a stream URL is set for
  that camera in the options (see [Live video](#live-video)). Snapshots are
  taken from the stream.
- **Smart-alert switches** (configuration category): barking, crying, person,
  activity and the other alerts your camera reports. Toggling one writes the
  new value to the Furbo cloud.
- **Last event** sensor: a timestamp of the most recent detected event, with
  the caption ("standing in the doorway"), action and location as attributes.
- **Subscription days left** and **Subscription status** (diagnostic).

Per account (the hub device), when calendar polling is enabled:

- **Notable events today**, with the written daily summary as an attribute.
- **Barking events today** and **Activity events today** counts.

There are no custom actions, events, device triggers or device conditions.
Use Home Assistant's standard state and numeric-state triggers/conditions on
these entities.

## Prerequisites

- Home Assistant 2025.2.0 or newer.
- A Furbo account (the same email and password you use in the Furbo app).
- Access to the email inbox for that account, to read the verification code.

## Installation

### HACS (recommended)

1. In HACS, add `https://github.com/willbeeching/ha-furbo` as a custom
   repository of category **Integration**.
2. Install "Furbo" and restart Home Assistant.

### Manual

1. Copy `custom_components/furbo` into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.

## Setup

1. Settings, Devices & services, Add integration, search for **Furbo**.
2. Enter the **Email** and **Password** for your Furbo account. The
   integration signs in to confirm they work before creating the entry.
3. If your account has two-step verification on (it is on by default), Furbo
   emails a **Verification code**. Enter it to finish. A new code is emailed on
   every sign-in, including reauthentication.

Your password is **not** stored. The entry keeps only the account id, the
session token, a generated mobile id and your email. Furbo issues no refresh
token, so when the session expires the integration asks for your password again
during reauthentication; it is used for that one sign-in and discarded. Nothing
credential-related is logged or included in diagnostics.

## Options

Settings, Devices & services, Furbo, Configure. The first page covers polling;
one further page per camera asks for its stream URL.

- **Update interval (seconds)**: how often the cloud is polled. Default 300,
  minimum 60. The event calendar rejects repeat calls to the same endpoint
  within about ten seconds, so intervals below that would be rate-limited.
- **Poll the pet calendar**: when off, the account-level event and activity
  sensors are not created and only the camera settings and subscription are
  polled. Default on.
- **Stream URL** (per camera): an `rtsp://`, `rtsps://`, `http://` or
  `https://` URL that Home Assistant can play. Leave empty for no camera
  entity. Changing it reloads the integration.

## Live video

Furbo does not offer RTSP or any other open stream. The app receives video over
ThroughTek's TUTK P2P protocol with a DTLS-protected channel, which needs the
vendor's native SDK, UDP access and a presence on the camera's LAN. None of
that can run inside a Home Assistant integration, so the video path is:

```
Furbo camera  --TUTK P2P (LAN, UDP)-->  furbo_p2p.py bridge  --H.264-->  go2rtc  --RTSP/WebRTC-->  Home Assistant
```

1. Run the bridge on a Linux machine on the same LAN as the camera. It is
   `furbo_p2p.py` on the `claude/furbo-home-assistant-integration-m2ojey`
   branch of this repository; the README there explains the protocol and
   how to run it (Docker with host networking, or bare Linux). It needs the
   TUTK 4.2 library (the copy that docker-wyze-bridge ships works) and a
   Furbo login of its own.
2. Publish the bridge output through [go2rtc](https://github.com/AlexxIT/go2rtc),
   which is bundled with Home Assistant OS and available as an add-on:

   ```yaml
   streams:
     furbo:
       - "exec:python3 /config/furbo_p2p.py stream --quality 1080p#killsignal=2"
   ```

   Ask for 1080p: on the shipping firmware the other quality slots return
   the camera's 640x360 profile, and the first 1080p keyframe follows a
   single low-resolution preview frame a few seconds in.

   go2rtc then serves `rtsp://<go2rtc host>:8554/furbo`.
3. Enter that RTSP URL as the camera's stream URL in the Furbo options. The
   camera entity appears after the reload. With Home Assistant's own go2rtc
   integration enabled, the dashboard plays it over WebRTC with low latency.

Status: the bridge speaks the protocol as the Furbo app does (license key,
`IOTC_Connect_ByUIDEx` with the device auth key, DTLS `PSK-AES128-CBC-SHA256`,
the V3 control opcodes) and is verified live on a Furbo 360: 1080p video,
device state, pan and treat toss. The camera entity is tested against a
mocked stream URL. What remains unproven is only the combination: the bridge
under go2rtc feeding this entity on a running Home Assistant.

The bridge also exposes controls the cloud does not (pan, treat toss, volume,
night vision, camera power). Bringing those into Home Assistant needs the
bridge to offer them over HTTP or MQTT, since the integration cannot load the
TUTK library itself; that is the next design step and is not in this release.

## How data is updated

One coordinator poll per interval fetches, in order: the device list, the
subscription state, each camera's alert settings, and (when calendar polling
is on) the day's activity report, daily summary and notable events. Every
entity is served from that single response. There is no push channel on the
cloud API, so this is straight polling; the calendar's rate limit is respected
by the fixed 60-second interval floor and a bounded retry that waits out an
`80002` response.

## Offline, reconnect, auth expiry and stale devices

- If the cloud is unreachable, entities become unavailable and recover on the
  next successful poll. The failure and the recovery are each logged once.
- If the stored session token is rejected, Home Assistant starts a
  reauthentication flow that reuses your stored email and a fresh emailed code.
- If a camera disappears from the account, its entities become unavailable.
  Automatic removal from the device registry is not yet implemented (see
  Limitations).

## Limitations

- **Live video needs a bridge.** The stream is TUTK P2P/DTLS and cannot be
  decoded by Home Assistant itself; see [Live video](#live-video).
- **No treat tossing.** The cloud `control_device` endpoint returns success
  for any action string, so it does not prove a treat was tossed; the app
  tosses over P2P. Treat control is therefore deliberately not exposed here.
- Newly added cameras appear after the next reload, not instantly.
- Cameras removed from the account are not auto-removed from the registry.
- Only Furbo models on the `product.furbo.co` account API are supported.

## Troubleshooting

- **"Wrong email or password"**: the credentials were rejected at sign-in.
  Re-check them in the Furbo app.
- **"That code was rejected"**: the emailed code was wrong or expired. Start
  the flow again to get a new one; too many attempts triggers a Furbo cooldown
  of several minutes.
- **Event/activity sensors are empty or unknown**: the account has no Furbo
  Nanny subscription, or "Poll the pet calendar" is off, or there were no
  detected events yet today.
- **The integration keeps asking to reauthenticate**: Furbo expired the
  session token. Complete the reauth flow with a fresh emailed code.
- Download **Diagnostics** from the integration to share a redacted snapshot
  when reporting a problem.

## Use cases

- Turn barking or crying alerts off during known-noisy hours with a schedule
  automation on the alert switches, and back on afterwards.
- Notify when the "Last event" timestamp updates while you are out, using the
  caption attribute in the message.
- Track "Barking events today" on a dashboard to spot separation-anxiety
  trends over time.

## Removing the integration

Delete the integration from Settings, Devices & services. That removes all its
entities and devices and forgets the stored session token. Nothing is changed on
the camera or in your Furbo account; alert settings you changed keep whatever
value they had last. To fully revoke access, change your Furbo password in the
app, which invalidates the stored session.

## What the tests prove, and what needs a real account

Automated tests (`tests/`, run on Home Assistant 2025.2 and the latest stable)
cover the config/options/reauth/reconfigure flows, setup and teardown, offline
and recovery, multi-account, the entity states (camera included), diagnostics
redaction, and the
API client against a mocked socket. They mock the network at the HTTP boundary.
They do **not** prove the live cloud contract; that was verified by hand on
2026-09-05 against a real Furbo 360 and is recorded in
[`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md). The camera entity is
tested against a mocked stream URL; the P2P bridge that produces a real stream
is outside this package and verified separately on the research branch.

## Quality self-assessment

This is a custom integration; Home Assistant has not formally graded it, so the
manifest claims no tier. `custom_components/furbo/quality_scale.yaml` records an
honest self-assessment against the official rules, validated in CI.

---
