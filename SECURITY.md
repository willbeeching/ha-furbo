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
never written to test fixtures. If you file an issue, do not paste them; the
downloadable diagnostics are already redacted.
