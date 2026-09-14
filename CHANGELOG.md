# Changelog

Release notes for the **Furbo integration**. HACS shows the matching section
when it offers an update, so every version that reaches users has an entry
here. The Furbo Bridge add-on has its own at
[`furbo-bridge/CHANGELOG.md`](furbo-bridge/CHANGELOG.md) and its own version
numbers: the two are released independently and their numbers do not line up.

## 1.6.1

- **Today's activity counts no longer start the day holding yesterday's.**
  The calendar is read at most hourly, and that hour was counted in elapsed
  time alone. A reading taken shortly before midnight therefore sat inside its
  hour well past midnight, serving yesterday's totals and summary under a name
  that says today. The cached figures now carry the date they counted, and
  midnight in your account's timezone ends it.
- A new day that cannot be fetched reads zero rather than yesterday's numbers.
  Carrying values over is right within a day and wrong across one. The hourly
  backoff is unaffected, so a rate-limited account still gets one attempt.
- **Signing back in around midnight no longer restores yesterday's counts.**
  The figures kept between fetches are now held with the date they counted,
  rather than read back off the last stored update. That stored update is only
  replaced when a whole refresh succeeds, so a refresh that read the calendar
  and then failed on something later left the previous day's numbers in place
  to be picked up and relabelled. A dead token also no longer spends the hour:
  it is not this host refusing us, so the refresh after renewal can ask
  again rather than sitting the hour out on nothing.
- **Signing back in through the add-on clears the add-on's warning.** The
  notice about it waiting for a verification code stayed up after a recovery
  that had visibly just worked, with nothing to press. A password sign-in
  still leaves it alone, because that says nothing about the add-on.

## 1.6.0

- **Signing in again no longer asks for your password and an emailed code.**
  When the Furbo Bridge add-on is set up and signed in to the same account,
  re-authentication now takes a current token from it and finishes without a
  form. The password prompt remains for anyone without the add-on, and for
  when it cannot answer.

  Why this matters more than it sounds. Furbo demands an emailed verification
  code for **every** password login on an account with two-step verification,
  including from a device it already knows, so a sign-in is the one step that
  can never be automated. Home Assistant was doing that login twice: once in
  the integration and once in the add-on, two separate sessions on one
  account, neither able to renew the other, each needing its own code when it
  lapsed. Now there is one, held by the add-on, and the integration follows it.
- A bridge signed in to a **different** Furbo account is refused and the
  password form is shown instead. Its token would authenticate, and every poll
  after it would read another account's cameras through entities and a device
  registry belonging to this one.

## 1.5.2

- **The calendar is asked for at most once an hour, instead of on every
  poll.** Its three calls go to a host that rate-limits repeat requests hard,
  which the code has said in a comment since the day it was written, and then
  it asked on every refresh anyway: better than thirty calls an hour at the
  default interval. Worse, a refusal was retried at the next poll, so a
  rate-limited account stayed rate-limited by the retrying. Found in a live
  log where that host answered `80002` on every single poll for seven hours.
  Today's counts do not change fast enough for any of that to be worth it.
- The hour is counted from the attempt rather than from a success, so being
  refused backs off too. This is what the Doggie Diary has always done; the
  calendar was simply never given the same treatment.

## 1.5.1

- **`furbo.get_events` did not work at all in 1.5.0.** Every call came back as
  `Furbo API error 400 (code 12001)`. The cloud requires the list of cameras
  and 1.5.0 treated it as optional, so a call that did not name one was
  refused. It now defaults to every camera on the account, and naming one
  still narrows it. Found by the reporter in #6 testing it the day it shipped.
- **Event names are checked before the call.** Furbo answers a name it does
  not recognise with the same bare `12001` it gives for a missing field, so a
  typo was indistinguishable from a bug. The field is now a dropdown of the
  names Furbo accepts.
- **`furbo.get_events` read a plain time in the wrong timezone.** A window
  written without an offset, which is how most people write one, was read in
  the timezone of the machine Home Assistant runs on rather than the timezone
  Home Assistant is set to. On a container those differ: asking for 19:00 from
  a London install fetched the clips from 20:00. Both ends of the window are
  now read as the local time the automation was written in.
- **A window with an offset on one end and not the other no longer fails.**
  Mixing the two raised an unhandled error instead of returning clips.
- The repair notice about the add-on waiting for a verification code is now
  cleared when the account is deleted. It was registered against the
  integration rather than the entry, so removing the account left a permanent
  warning about an add-on nothing was waiting on, with nothing to press.

## 1.5.0

- **New: two actions that hand back Furbo data for your own automations.**
  Asked for in #6, by someone who wants to build their own recap videos.
  - `furbo.get_events` returns the detected events in a window you choose,
    each with its own video clips and thumbnail. **The window is yours**, so
    unlike Furbo's own daily recap this is not limited to 07:00-19:00, and
    overnight activity is included if you ask for it.
  - `furbo.get_insight_report` returns the written daily report the app shows
    as a day's recap.

  Both return their answer to the caller rather than storing it. That is
  deliberate: an entity's attributes are written to the recorder, included in
  diagnostics downloads and pasted into issue reports, and neither of these
  has a fixed shape worth holding there. Your automation decides what to
  download, where to put it and how long to keep it, which is the part of
  "build my own recap" that is yours rather than the integration's.

  Downloading and keeping a clip library is deliberately not included. That is
  a media pipeline, not an integration, and these actions give an automation
  everything it needs to do it the way you want.

## 1.4.2

- **Home Assistant now tells you when the Furbo Bridge add-on needs a
  verification code.** The add-on cannot reach you, so when Furbo demands a
  code it can only say so into its own log, every thirty seconds, where nobody
  is looking. One installation sat like that for two days: the integration kept
  asking for a sign-in it could not explain, and the camera went down when its
  existing session finally dropped. A repair notice now appears with what to do,
  and clears by itself once the add-on can hand over a token again.

## 1.4.1

- **Signing in again no longer registers Home Assistant as a new device.**
  Furbo binds a `MobileId` per login and caps how many an account may hold, and
  reauthenticating minted a fresh one every time instead of reusing the one the
  entry already had. That spends the account's allowance on the same
  installation over and over, and something has to give way: the likely
  casualty is the Furbo Bridge add-on's own binding, which is exactly what
  leaves it unable to renew and asking for a verification code of its own.
  Reauth and reconfigure now reuse the identity the entry registered; a genuinely
  new entry still gets a new one.
- The login response's `DeviceBindingLimit` is logged by value at debug level,
  so how many bindings an account actually allows can be read rather than
  guessed at. The response carries no refresh token of any kind, which is now
  confirmed from a real login rather than assumed.

## 1.4.0

- **New: a Download Doggie Diary button** on the Furbo account device. Pressing
  it saves any of the week's daily timelapse videos that are not already on
  disk, into `furbo_diary` under Home Assistant's media folder, where they show
  up in the media browser and play in the UI. Point a daily automation at it and
  the videos arrive without the phone app, which is what #5 asked for.
  Days already saved are skipped, so pressing it twice costs nothing, and a day
  missed while Home Assistant was off is picked up on the next run because the
  report covers a rolling week.
  The report is read fresh on every press rather than reused: Furbo's links are
  presigned and expire, so they are worth having only at the moment they are
  used, and they never reach Home Assistant's stored state.
- Each account saves into its own folder under `furbo_diary`. Two accounts
  produce a video for the same date, and a shared folder would not merely mix
  them up: the second account's video would be skipped as already saved and
  never arrive.
- Camera actions serialize per camera rather than across the whole button
  platform, so a download cannot hold up a pan or a treat, and one camera's
  action no longer waits on another's.
- One diary download at a time per account. Two presses at once, a person and
  an automation say, would otherwise fetch the same day together and write the
  same partial file, and whichever finished first moved it out from under the
  other.

## 1.3.2

- **The Doggie Diary sensor stopped discarding the report it asked for.** The
  cloud answered with a real diary and the integration rejected it because
  `Weekday` arrives as a number where a string was expected. A sensor whose
  job is to describe an undocumented report has no business refusing one for
  holding a surprising type, so the diary's own fields now pass through as
  they arrive. Reported by the live response in #5.

## 1.3.1

- **The diary is asked for on the host that serves it.** 1.3.0 tried
  `product.furbo.co` and `pet-gpt.furbo.co`, and the diary is on neither. The
  Furbo app builds none of its hosts in, it reads them at startup from a
  config file, and that file names a third one: `event-handler.furbo.co`,
  which serves the app's unversioned endpoints. Refs #5.

## 1.3.0

- **New: a Doggie Diary days sensor** on the Furbo account device, under
  Diagnostic. It reports how many days the account's daily video report
  holds, which host answered and what each day carries, so the daily
  timelapse can be reached without the phone app. Asked for in #5.
  The signed links themselves are described, not included: they point at
  video of the inside of your home, and entity attributes are written to the
  recorder, included in diagnostics downloads and pasted into issue reports.
  It is fetched at most once an hour and follows the same option as the pet
  calendar, because both share a host that rate-limits repeat calls.

## 1.2.2

- **A camera switched off no longer loses its video until the add-on is
  restarted.** Reported in #2.
- The login response's field names are logged at debug level, for working out
  what the cloud actually returns.

## 1.2.1

- **Each camera streams its own video.** With more than one camera every
  camera entity pointed at the first one's stream. Reported in #1.
- **The integration asks the add-on again when it could not say which camera
  is which**, rather than settling for the wrong answer until the next
  restart.

## 1.2.0

Earlier releases are recorded in the
[commit history](https://github.com/willbeeching/ha-furbo/commits/main).
