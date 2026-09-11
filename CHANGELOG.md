# Changelog

Release notes for the **Furbo integration**. HACS shows the matching section
when it offers an update, so every version that reaches users has an entry
here. The Furbo Bridge add-on has its own at
[`furbo-bridge/CHANGELOG.md`](furbo-bridge/CHANGELOG.md) and its own version
numbers: the two are released independently and their numbers do not line up.

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
