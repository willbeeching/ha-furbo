# Hardware and live-service verification matrix

Behaviour that automated tests cannot prove, recorded against real hardware and
the live Furbo cloud. Negative results are kept deliberately.

| Date | Model / firmware | Area | Method | Expected | Observed |
| --- | --- | --- | --- | --- | --- |
| 2026-09-05 | FB0030 / 108 | Cloud login + MFA | Live login with emailed code | Token issued | Verified; MFA emailed on every login |
| 2026-09-05 | FB0030 / 108 | Device list | `/v2/account/{id}/device` | One camera returned | Verified (Hallway) |
| 2026-09-05 | FB0030 / 108 | Alert settings read | `/v5/device/alert-setting` | 40 flags + cooldowns | Verified |
| 2026-09-05 | FB0030 / 108 | Alert settings write | `/v5/device/alert-setting/update` (no-op to same value) | Value persists on re-read | Verified; unknown names rejected `12001` |
| 2026-09-05 | FB0030 / 108 | Subscription | `/v3/service/license` | Plan + days left | Verified (NST Standard, 24 days) |
| 2026-09-05 | FB0030 / 108 | Notable events | `pet-gpt.furbo.co/v1/calendar/notable-events/get` | Events with captions | Verified; host rate-limits repeat calls (~10s) with `80002` |
| 2026-09-05 | FB0030 / 108 | Daily summary | `.../daily-summary/get` | Written summary | Verified |
| 2026-09-05 | FB0030 / 108 | Activity report | `.../v2/calendar/activity-report/get` | Hourly counts per type | Verified (Barking, DogMoveAbove10Sec) |
| 2026-09-05 | FB0030 / 108 | Cloud treat toss | `/v3/account/control_device` | Proof a treat tossed | **Negative:** returns `Success` for any action string; not a proof, so not exposed |
| 2026-09-05 | FB0030 / 108 | Live video (P2P) | `furbo_p2p.py` in a sandbox | H.264 frames | **Blocked:** sandbox has no IPv6 and filters UDP; verified only up to the connect call. Needs a LAN host |

## Still to verify on real hardware or a live account

- Token lifetime and the reauth flow firing on a genuinely expired token.
- Alert switch toggling actually changing camera behaviour (only echo verified).
- Multiple cameras on one account (only single-camera verified).
- A camera leaving the account (stale-device handling).
- Live video, two-way audio and treat toss over P2P end to end.
