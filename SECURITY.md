# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/willbeeching/ha-furbo/security/advisories/new)
rather than opening a public issue. You should receive an acknowledgement
within a few days.

## Handling of credentials

The integration stores only what setup needs: your Furbo account id, the
session (Cognito) token, a generated mobile id and your email. Your **password
is not stored**. Furbo issues no refresh token, so when the session expires the
integration starts a reauthentication flow and asks for your password again
(plus a fresh emailed code); it is used for that one sign-in and discarded.
A config entry created by an older build that did store the password is
migrated on load to remove it.

Credentials and tokens are never logged, never included in diagnostics, and
never written to test fixtures. Cloud API responses are validated at the client
boundary and their bodies are never placed in exception messages or logs, so a
token in a response cannot leak through an error. If you file an issue, do not
paste them; the downloadable diagnostics are already redacted.

## The Furbo Bridge add-on

The add-on logs in to your Furbo account once and keeps the resulting session
in the add-on's private `/data` storage (`furbo_session.json`); fresh P2P
credentials are fetched from the cloud each session and held only in memory.
Your password is used only for that first sign-in.

Note that Home Assistant's Supervisor stores every add-on option, including
`email`, `password` and `mfa_code`, in its own `options.json` for as long as
they are set — the add-on cannot erase them from there. Clear `mfa_code` once
login completes, and if you would rather the password not persist, you may
blank it after the first successful login (the saved session keeps working; set
it again only to re-authenticate). To start over without reinstalling — an
expired session, a changed password, or the wrong account — turn on the
**`reset_session`** option once: the add-on discards the stored session and logs
in again, then you turn the option back off.

Its HTTP API exposes the camera's controls (treat toss, pan, settings) and its
state, so it requires a bearer **`api_token`**:

- The add-on refuses to start without one, and every request without the exact
  token is rejected (the token is compared in constant time).
- Choose a long random value — e.g. `openssl rand -hex 32` or `python3 -c
  "import secrets; print(secrets.token_urlsafe(32))"` — and set the **same**
  value as the integration's bridge token.
- go2rtc's own admin API is bound to loopback and is not exposed on the network.

The RTSP/WebRTC video stream (ports 8554/8555) is reachable on your LAN by
design so Home Assistant can pull it; keep the add-on on a trusted network.

Never commit session files, captured video, or the vendor libraries. The
repository's `.gitignore` blocks `furbo_session*.json`, `p2p_auth*.json`,
`*.h264`, `*.so`/`*.dylib` and app packages so they cannot be added by
accident.

## If credentials may have been exposed

If a Furbo session token, P2P credential or password ever reaches a public
place (a commit, a pasted log, a screenshot): change your Furbo password, then
remove and re-add the integration and restart the add-on so a new session and
new P2P credentials are issued. Removing the exposed data from a git history
also requires rewriting and force-pushing that history and invalidating cached
views — see GitHub's guide on removing sensitive data.
