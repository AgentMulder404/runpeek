# Unified accounting validation

Validated locally on macOS, Python 3.12, 2026-09-10 (America/Los_Angeles).

- Full suite: 162 tests. Existing agent/SDK tests plus 34 new test cases.
- Ruff and strict mypy pass.
- sdist and wheel build; installed-wheel command smoke check performed separately.
- Synthetic HTTP demo: two devices observing one request plus a billing observation,
  repeated delivery, one charge; actual $1.50 replaces both $2.00 estimates.
- Tenant separation, role restrictions, revoked credentials, pairing single-use,
  request limits, field allowlist, invalid numeric values, immutable event conflicts,
  quotas, deletion/replay tombstones, offline backoff and acknowledgement loss tested.
- SDK tests cover context propagation, bounded-queue drops, failed storage and flush counters.
- Regression coverage: ambiguous copied histories, nested listing and attribution,
  explicit child labels, zero-cost shares, full hashed trace IDs, legacy recovery,
  missing-source rollback, and reassignment back to the original work item.
- Onboarding CLI tested with isolated agent fixtures; pause/resume and shared IDs verified.

Measured on this machine (not a performance guarantee):

| Measurement | Result |
|---|---:|
| SDK enqueue median | 0.009 ms |
| SDK enqueue p95 | 0.0115 ms |
| 10,000 events, including persistence | 1.09 s |
| Dropped/invalid/failed/unflushed events | 0 |
| 10,000 nested-session assignment resolution | 0.0049 s |
| SQL queries for that ancestry resolution | 2 |
| Model calls/tokens used by tracking | 0 |

Not validated: public internet deployment, OS keychain interaction on all supported
operating systems, real multi-machine TLS/certificate operations, managed encryption or
backup deletion, browser SSO, unavailable platform telemetry, or production adversarial
load. The receiver is a private pilot, not a security-certified production service.

The demo and benchmark use synthetic accounting and call no model provider. Do not
interpret their example amounts as actual expenditure.
