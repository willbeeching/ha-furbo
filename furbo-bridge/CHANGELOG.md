# Changelog

Home Assistant shows this file when an update is available, so every version
that ships to users gets an entry here.

## 1.3.5

- **Audio now works on cameras that send AAC without a header.** 1.3.4 carried
  sound only when a frame arrived already wrapped in ADTS. A camera has no
  reason to wrap it: the P2P SDK marks frame boundaries itself, so the frames
  come through bare, and a bare AAC frame is undecodable on its own. The bridge
  now builds the header the camera left off, from the sample rate and channel
  count the camera's own frame header reports.
- **Nothing is claimed that a decoder has not accepted.** Before the transport
  stream's tables are written, a fraction of a second of audio is put through
  ffmpeg, which the add-on image already carries. If it decodes cleanly the
  track is declared; if not, the stream carries video only. Silence, a ramp and
  random noise are all rejected, so an accepted sample is evidence rather than
  the absence of a complaint. This is what replaces the codec id, which is
  defined inside TUTK's native library and cannot be read here.
- **The answer is remembered for the session**, so only the first viewer after
  a restart waits for the check, and only when `audio` is on.
- **An unrecognised format is now reported properly.** The log gives the codec
  id, sample rate, bit depth, channel count and frame sizes, and at `debug` a
  base64 sample of the audio itself. That is what identifying a new format
  takes, so please include it when you report one.
- **The audio is re-encoded on the way out to RTSP, because it has to be.**
  ffmpeg's RTSP output refuses AAC that arrived in ADTS framing ("AAC with no
  global headers is currently not supported"): the packetizer needs the codec
  config that ADTS does not carry. Re-encoding 16 kHz mono costs almost
  nothing, and the alternative is no sound. The picture is still copied
  untouched.
- Frames held while the format is being settled are no longer dropped if the
  sound ends first. The first frames are the ones carrying the parameter set a
  decoder opens on, so losing them cost the start of the picture.

## 1.3.4

- **Fixes a regression in 1.3.3 that broke live video when `audio` was on.**
  1.3.3 declared the camera's audio to be AAC, because that is what the phone
  app decodes. At least one camera sends something else, and a transport
  stream's tables are a promise about its payload: ffmpeg could not parse the
  track, the output header failed along with it, and **the picture went down
  with the sound**. Live view restarted every few seconds. Anyone who left
  `audio` off, which is the default, was never affected.
- **Audio is now carried only when its format can be proved from the data.**
  ADTS says what it is in its first bits; anything else gets no audio track
  and the video streams on its own. The codec id in the frame header is not
  usable for this: it is defined inside TUTK's native library, an FB0030
  reports 135, and the only id this project has established is the 137 its
  speaker accepts in the other direction.
- **If your camera's audio is not carried, the log says so and prints the
  first bytes of a frame.** Those bytes are what identifying the codec needs,
  so please do report them along with your camera model.
- A test now covers the exact failure: audio the bridge cannot name must cost
  you the sound and never the picture.

## 1.3.3

- **The camera's microphone can now be carried on the live stream.** Off by
  default; turn on the new `audio` option to enable it. The camera has always
  had a microphone, and this add-on has always asked it only for pictures.
  **Superseded by 1.3.4: as shipped this broke live video on cameras whose
  audio is not AAC. Update rather than enabling `audio` on this version.**
- **Sound and picture stay together, because the bridge now muxes them.** The
  two arrive from the camera as separate queues of bare frames, and neither
  carries a container timestamp. Handed to ffmpeg as two pipes they landed
  three seconds apart, and no combination of its timestamp flags closed the
  gap: two clockless streams have no common zero. The camera stamps both, on
  one clock, so the bridge now writes a transport stream carrying those stamps
  and serves it on one connection. Measured end to end, an offset asked for is
  the offset that comes back out, to the millisecond.
- The audio format is read from the camera rather than assumed. The first
  frame of each stream logs its codec id, sample rate, bit depth, channel
  count and whether it opens with an AAC sync word, because nothing published
  says what a given Furbo's microphone sends.
- Video-only streaming is untouched. Leaving the option off takes exactly the
  path it did in 1.3.2, down to the ffmpeg command.

## 1.3.2

- **The live stream starts in well under a second instead of three.** ffmpeg
  will not publish anything until it knows the frame size, and it learns that
  from the stream's parameter set. The bridge handed it whatever the camera was
  sending at that instant, so ffmpeg fell back to reading up to five seconds of
  video hunting for one. Home Assistant's own camera card buffers into HLS and
  waits it out, which is why this looked fine there. HomeKit gives up at around
  five seconds and reconnects, so an Apple Home viewer got a few frames, a
  dropped producer, and a restart, over and over: one frame every ten seconds
  rather than a stream. Refs #7.

  The reader now opens the stream on a frame carrying the parameter set, so the
  answer is in the first bytes, and `stream.sh` no longer lets ffmpeg spend
  seconds looking for it. Measured on a synthetic 720p feed: about 3.1s before,
  about 0.43s after.
- Frames before that first parameter set are dropped rather than sent. A
  decoder cannot use them, and a viewer joining mid-picture was being handed
  bytes it could only discard.
- A camera that sends no parameter set at all still streams. The wait is
  bounded by both a timeout and a frame count, because either alone leaves a
  hole: a short stream can end while the reader is still dropping every frame,
  which would serve nothing.

## 1.3.1

- **The add-on log keeps hours of history instead of four minutes.** Every
  camera command and reply was printed unconditionally, and the integration
  polls about fifteen of them every thirty seconds, so the log rolled over
  before anyone could read it. Going to look at why a login failed the night
  before turned up nothing but hex. Those per-frame lines now print only when
  `log_level` is set to `debug` or `trace`; failures, connection state and the
  login path are unchanged at `info`.

## 1.3.0

- **The add-on stops asking for a verification code every time its session
  lapses.** Furbo's login response carries an `MfaAuthCode`: proof that this
  client has already passed a verification. The phone app keeps that and
  presents it on every later login, which is exactly why the app never asks
  you to verify twice. The add-on read the same response, took the account id
  and the token, and threw the proof away, so each login looked like a
  first-ever login on an account with two-step verification and earned a fresh
  emailed code.

  It is now kept and presented. On an account with two-step verification this
  is the difference between a bridge that can get back in by itself and one
  that needs a person every time its token dies.
- A stored proof the cloud no longer accepts (retired, or a changed password)
  is not a dead end: the login is retried once without it, and the emailed
  code is then asked for as before rather than the bridge appearing broken.
- The proof is cleared by `reset_session`, unlike the device id. A reset is
  what you do when the account has changed under you, and a proof earned
  against the old one is worth nothing.

## 1.2.12

- **FB002 video no longer stalls a few seconds after the stream starts.**
  1.2.6 set `auth_type = 1` on the legacy path, from a matrix run against real
  hardware. It did authenticate, but the video then dried up (`-20012` until
  the bridge gave up) and the camera sometimes closed the session. The Furbo
  app never assigns that field for any camera, so it sends 0, and an A/B on
  the same FB002 confirms it: 0 authenticates just as well and the stream
  keeps running. The bridge now leaves it at 0 everywhere, as the app does.
  Found, tested and confirmed on the hardware by @scotthalldumarey in #3.
- Nothing changes for a camera the cloud issues P2P credentials for. That path
  already sent 0.

## 1.2.11

- **The security upgrade in 1.2.10 is now four named packages rather than
  everything.** 1.2.10 ran a blanket `apt-get upgrade`, which Docker's own
  guidance is against: it makes two builds of the same file produce different
  images, and it changes packages nothing in the build asked about. The four
  the scan actually named are upgraded by name instead, each recorded in the
  Dockerfile with the CVEs behind it. Same packages patched, a build you can
  read.
- Correcting 1.2.10's note: it was not only perl. The scan found twelve fixed
  advisories across `gzip`, `libpcre2-8-0`, `libsqlite3-0` and `perl-base`.

## 1.2.10

- **The image picks up Debian's security fixes at build time.** The base image
  is pinned by digest, so its contents are fixed, and Debian publishes patches
  for packages inside it faster than the image is rebuilt. The build installed
  what it needed and inherited everything else unpatched, which meant shipping
  known-patchable packages -- perl, in this instance. It now upgrades first.

## 1.2.9

- The login response's `DeviceBindingLimit` is logged by value at debug level.
  Each login binds a `MobileId` against that limit, so the number decides
  whether two clients on one account can coexist or evict each other.

## 1.2.8

- **`log_level` now reaches the login, not just the running bridge.** 1.2.6
  wired the option into the server and stopped there, so every command run by
  the start-up script -- the login among them -- still ran at Python's default
  level. Setting the option to debug therefore printed nothing from the one
  step anybody turns it up to watch. The line that records which fields the
  cloud returns at login can now actually appear, which is what is needed to
  find out whether Furbo hands back anything that would let a session be
  renewed without a person.

## 1.2.7

- **A half-complete P2P response is refused rather than guessed at.** Making
  the three modern credentials optional for FB002 support in 1.2.6 left the
  step after it still assuming they were all there. A null `P2PAccountKey`
  became the literal string `"None"` and failed to authenticate for no visible
  reason; a missing one raised a bare `KeyError`; and a missing `AuthKey`
  alongside otherwise complete credentials silently took the legacy path,
  moving a camera that should be using DTLS onto the unencrypted one. Two
  shapes are now accepted, all three credentials or none of them, and anything
  between is refused with the missing field named.

## 1.2.6

- **Older `FB002` cameras can connect.** The cloud answers with `AuthKey`,
  `P2PAccountId` and `P2PAccountKey` all null for these, which the add-on
  treated as a broken response and gave up on before it ever reached the
  camera. They authenticate from the device record instead: the account id and
  the device's own `P2PAccessToken`, over the plain parallel connect. Which
  path is taken follows from the cloud sending no `AuthKey`, not from the
  model, so a camera that works today cannot be moved onto it.

  Worked out and verified on real FB002 hardware by
  [@scotthalldumarey](https://github.com/scotthalldumarey) in #3, down to the
  exact `auth_type`. I have no FB002 and have not tested this myself, so it
  ships on their verification rather than mine.

## 1.2.5

- **The add-on's `log_level` option now reaches the add-on.** It was wired to
  go2rtc alone, while the bridge's own logger stayed pinned at info, so
  setting it to debug produced nothing from the part you were trying to
  debug.
- The `furbo_p2p.py diary` command has been removed. It asked two hosts that
  turned out not to serve the diary, so it never worked. The integration
  reports the same thing as a Doggie Diary sensor, and downloads the videos,
  with no container shell needed. See #5.
- The build retries its downloads of the TUTK library and go2rtc. Both come
  from third-party hosts that rate-limit anonymous traffic, and a 429 has
  failed the build before.

## 1.2.4

- **The add-on no longer says a code was emailed when none was.** The login
  that requests one had its failure swallowed and the "a code was emailed"
  message printed regardless, so a login the cloud refused sent people to
  watch an inbox that nothing was ever going to arrive in. What actually
  happened is now read off the disk: a session means it logged straight in, a
  pending login means a code really is on its way, and neither means the login
  failed and says so. Reported in #4.
- **`reset_session` resets once, not once per restart.** Left on by accident
  it threw the session away every time the add-on started, so each restart
  went round the emailed-code loop again and looked like the add-on refusing
  to start. It now resets, says so, and then leaves the new session alone
  until the option is turned off and on again. Completing an emailed code
  with the option still on counts as that reset, so the session the code just
  earned survives the next restart -- which is the path the recovery
  instructions put you on.
- **An `mfa_code` that arrives with no login waiting for it is called out.**
  It used to fall through to requesting a new code, which silently made the
  code you had just typed useless -- and the next restart then checked that
  stale code against the new login and reported it as wrong. It now says the
  code cannot be used and to expect a fresh one.

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
