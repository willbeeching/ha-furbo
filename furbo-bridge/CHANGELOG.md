# Changelog

Home Assistant shows this file when an update is available, so every version
that ships to users gets an entry here.

## 1.1.0-beta.6

- The device identity now lives in its own file, so turning on
  `reset_session` clears the stored token without also changing who the
  bridge claims to be. Keeping both in one file meant a session reset earned
  another emailed code, which is the thing beta.5 set out to stop. An id
  already stored in the session file is picked up and kept.
- A settings read that is not recognised now costs 0.4s rather than 1.5s,
  which is what the fixed wait it replaced cost. The reply matching is still
  not right on a V3 camera, and this caps what that is worth until it is.
- Received replies log their opcode. The names alone were enough to mislead
  two attempts at the matching; the numbers say what the camera actually
  sends.

## 1.1.0-beta.5

- The add-on now keeps the device identity it logs in with. Furbo recognises a
  client by its MobileId, and a new one was minted on every login, so each
  login looked like a new phone signing in and earned a fresh emailed code.
- When the cloud rejects the stored token, the add-on logs in again by itself
  and carries on. It used to sit in a thirty-second failure loop until someone
  noticed.
- A login the cloud will not complete without a verification code is reported
  once and then backed off, up to fifteen minutes between attempts, rather
  than retrying into Furbo's rate limiter. `GET /api/status` reports this as
  `needs_login`.

## 1.1.0-beta.4

- Fixed the reply matching added in beta.3. A V3 camera answers some commands
  on the request opcode and others on the request plus one, and only the
  second form was recognised, so every settings read waited out its ceiling
  and the poll got slower rather than faster. Both forms are now accepted, and
  a read that really goes unanswered logs which opcodes did arrive.

## 1.1.0-beta.3

- The full settings poll now waits for each reply instead of sleeping a fixed
  0.4s after every command. It used to take about ten seconds even on a LAN
  session where each reply arrives in milliseconds. That sweep holds the
  worker lock, so it could also push a setting change past the timeout Home
  Assistant allows.

## 1.1.0-beta.2

- Changing a setting no longer re-reads every setting afterwards. The read-back
  cost one round trip per setting, which on a relayed session took longer than
  Home Assistant waits, so an automation could report a failure for a change
  the camera had already applied. A setting the camera refuses is left at its
  previous value, and the five-minute poll still picks up changes made from
  the Furbo app.
- Documented why LAN mode needs the camera on the same subnet as Home
  Assistant, and what to change if it is not.

## 1.1.0-beta.1

- Video is served from the P2P session the add-on already holds. go2rtc used
  to open a second session, and because the camera's P2P credential is
  reissued on every cloud fetch, the two sessions invalidated each other's key
  and both failed to authenticate. That could wedge video and controls for
  hours. Talkback still opens its own short-lived session.
- `GET /api/status` reports `session_mode`, so a relayed session (slow, and
  video may not start) can be told apart from a local one.

## 1.0.0

First stable release. Live video over RTSP and WebRTC, camera state and
controls over HTTP, and the pan, treat and settings entities the Furbo
integration drives.

## 0.1.7

- Brand images for the integration and the add-on.

## 0.1.6

- Stopped logging the derived RTSP password.
- Fixed the stream-URL migration for tokens containing reserved characters,
  which had left some cameras' streams broken.

## 0.1.5

- Clarified the TUTK non-redistribution position in the docs.

## 0.1.4

- Migrated stale RTSP stream URLs and documented how the password is derived.

## 0.1.3

- Patched aiohttp CVEs and fixed the session and password handling.
- Split the stream credential out from the API token.

## 0.1.2

- RTSP now requires authentication, talkback works, and the camera identity is
  checked against the account.

## 0.1.1

- Reduced what the add-on exposes and pinned the supply chain: every download
  is fetched from an immutable source and checked against a recorded hash.

## 0.1.0

First release of the Furbo Bridge add-on.
