# Changelog

Home Assistant shows this file when an update is available, so every version
that ships to users gets an entry here.

## 1.2.3

- A new `furbo_p2p.py diary` command reports what the account's Doggie Diary
  contains, for working out whether the daily video can be fetched without the
  phone app. It prints field names, dates and counts, never the links: those
  are signed URLs to video of someone's home. Nothing else uses it yet and
  nothing else changes.

## 1.2.2

- **A camera switched off no longer loses its video until the add-on is
  restarted.** Only the first frame was ever on a timer, so a stream that had
  been running and then stopped -- which is what switching a camera off looks
  like, the session staying up with nothing to send -- left the reader waiting
  for ever. It held the camera's single video slot, so every later request was
  refused as busy. Video that goes quiet for ten seconds now gives up and hands
  the slot back, and switching a camera off ends its stream at once rather than
  waiting that out. Reported in #2.

Also in this release, diagnostics that change nothing about how the add-on
behaves:

- A login now records which fields the cloud sent back, by name, at debug
  level. Never the values: those are the credentials. The client keeps two
  of them and discards the rest, so "the cloud gives us nothing to renew a
  token with" has only ever described the client, and nobody had checked
  what the response actually carries. If there is something to refresh
  with, renewing the way the phone app does is a better answer than logging
  in again every day, and this is how we find out.
- When the add-on refuses to hand over a cloud token because only a person
  can fix the login, its log now says why. It used to tell the caller and
  go quiet, so the log showed "logging in again" and then nothing, and a
  wrong password looked exactly like the cloud wanting an emailed code.

## 1.2.0

**Several cameras on one account.** The add-on used to refuse to guess between
cameras and ask for a `device_id`, which left a second camera unreachable.
With `device_id` blank it now serves every camera on the account, each with
its own session, controls and video slot, so watching one does not stop
another and a camera that is slow or logged out does not hold up the rest.
Set `device_id` to pin it to one camera, or to a comma-separated few.
`GET /api/cameras` lists what is served. Every operation also exists at
`/api/cameras/<device_id>/...`, and each camera's video is published as
`furbo_<device_id>`.

Nothing changes for an account with one camera: the stream keeps its name,
the API keeps its existing paths, and an existing setup carries on untouched.

**No more daily sign-in prompt.** Furbo's login hands out a token that lasts
about a day with nothing to renew it from, so the integration had to ask you
for an emailed code roughly every day. The add-on keeps the account password
and can log in unattended, so it now serves the account's current token on
`GET /api/cloud-token`, checking it with the cloud first and logging in again
if it has died. The integration takes that instead of prompting you. It
refuses a token for a different Furbo account, so a bridge signed in
elsewhere cannot quietly repoint it. Needs the matching integration update.

**Logins cannot run away with themselves.** Only one runs at a time, a caller
that finds another has already refreshed the token uses that session, and one
waiting behind another gives up after about a minute rather than being held
forever. A login the cloud refuses stops the add-on asking until someone
fixes it and says what to fix, instead of being retried on every video
request: a burst of refused logins is what gets an account locked out. A
cloud that could not be reached is still retried.

**Talkback is off in this version.** It opened a second P2P session to the
camera while the microphone was open, and because the camera reissues its P2P
credential on every fetch, that session and the bridge's own could invalidate
each other and take video and controls with them. It returns once it runs
over the shared session and that has been tested against real hardware.
Removing it takes away the one competing session we know about, not every way
a session can be invalidated.

**Also fixed.** The camera SDK is started and stopped once for the whole
add-on rather than once per camera: it is global to the process, so a second
camera used to fail to start it, and whichever camera reconnected first shut
it down for the others. A video stream now always ends — when a viewer fell
too far behind, the reader's end-of-stream signal could be discarded, leaving
the request waiting for a frame that would never arrive and holding that
camera's video slot open.

## 1.1.0

Live video is served from the single P2P session the add-on already holds,
rather than a second one that fought with it for the camera's credential.
Settings reads and writes are several times quicker, and a cloud token the
camera's servers reject is recovered without anyone having to intervene.

Everything below was released as a beta on the way here and is included.

- Video comes from the bridge's own session. go2rtc used to open a second
  one, and because the camera's P2P credential is reissued on every cloud
  fetch, the two invalidated each other's key and both failed to
  authenticate, which could wedge video and controls for hours.
- Changing a setting no longer re-reads every setting afterwards, and the
  full poll waits on each reply instead of a fixed delay. A write went from
  about thirteen seconds to two, a full poll from ten to two.
- A rejected cloud token is retried with a fresh login using the device
  identity the bridge already registered, which the cloud normally accepts
  without emailing a code. When it does ask for one, the add-on says so once
  and backs off rather than retrying into the rate limiter.
- The device identity is kept separately from the session, so clearing a dead
  token no longer changes who the bridge claims to be.
- Video that never arrives is given up on and reported, instead of holding
  the single stream slot while every retry is turned away.
- The add-on tells the camera to stop video before shutting down.
- `GET /api/status` reports `session_mode` and `needs_login`.

## 1.1.0-beta.12

- The add-on now tells the camera to stop video before it shuts down, and
  clears any stream the camera still believes it is serving before starting a
  new one. Shutting down closed the session without stopping the stream, so
  the camera went on holding it for a client that had gone and answered the
  next start request with no data at all until its own timeout expired. That
  is why video came back on its own a few minutes after a restart, and why
  restarting repeatedly kept it broken.

## 1.1.0-beta.11

- The camera's video status notification is decoded and logged in full. It is
  the camera's own account of why it is or is not sending video, and the
  generic payload log truncated it at 48 bytes, which cut off the status field
  itself.

## 1.1.0-beta.10

- Fixed video. Frames were being sliced to the length the SDK call returns,
  but this build reports the frame length separately and returns zero on
  success, so every frame came out empty. The stream carried no bytes while
  reporting no error, and because an empty read still counted as a frame, the
  checks added in beta.8 and beta.9 stayed silent. The frame length now comes
  from the SDK's own field, and a successful read of nothing is treated as
  what it is.

## 1.1.0-beta.9

- The give-up added in beta.8 only covered one of the ways a frame can fail to
  arrive, and it was the wrong one. Frames that come back lost or incomplete
  were discarded in a tight loop with no logging and no pause, which is what
  the camera has actually been doing: the stream ran for as long as the viewer
  waited and produced nothing, while the loop starved the controls sharing
  that channel. The give-up now covers every outcome, the loop pauses, and a
  stream that yields no frames reports which codes it saw and how large a
  frame the camera was trying to send.

## 1.1.0-beta.8

- A camera that accepts the start command and then sends no video is now
  treated as a failure after eight seconds instead of being waited on
  indefinitely. The reader used to hold the single stream slot for as long as
  the viewer waited, so go2rtc timed out and every retry was turned away as
  busy, which looked like a hang rather than a refusal.
- The video opcodes are named in the log. `IPCAM_START` used to appear as a
  bare `0x1ff` with no way to tell whether the camera answered it.

## 1.1.0-beta.7

- Fixed the slow settings poll, this time from the evidence rather than a
  guess. Sending a command cleared the receive queue immediately afterwards,
  and because the send call blocks long enough for the camera to answer, that
  cleanup was swallowing the command's own reply. The value still reached the
  camera state, so nothing looked broken, but the code waiting for that reply
  waited out its full timeout. The queue is now cleared before the command
  goes out.

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
