# Demo tasks

The service under this directory is the starting tree. `pytest -q` is green.
A push lands only after a tester reply says these tests pass on the resulting
tree, and only after consent (see `SPEC.md`). `.gud/protected` is:

```
api/interfaces.py explicit
.gud/* quorum
```

Do not edit `.gud/*` in these tasks. Membership changes need a quorum; these
tasks do not.

Agents push against the current tip. Two pushes that share a parent cannot
both become the next link: the earliest one that qualifies lands, and the
other has to be replayed on top of it. That is the swarm.

## Task 1 — Add per-user rate limiting to login and bill overage requests

Split across files so the diffs do not touch the same lines. They still share
a parent until each one is rebased onto whoever landed first.

| Subtask | Agent | File | What to change |
| --- | --- | --- | --- |
| T1.1 | `@auth-agent` | `auth.py` `login` | Call `ratelimit.allow(f"login:{username}")` before checking the password. When it returns false, return `LoginResult(False, None, "rate limited")` and do not start a session. |
| T1.2 | `@billing-agent` | `billing.py` `charge` | Read `usage_of` for the period, set `overage_cents` from `overage_cents(requests, plan)`, and set `total_cents` to `amount_cents + overage_cents`. Keep rejecting a missing account and a negative amount. |
| T1.3 | `@api-agent` | `api/handlers.py` `handle_login`, `handle_charge` | Map login reason `"rate limited"` to status 429. On a successful charge, call `billing.meter_request` for that user so the next invoice sees the call. |
| T1.4 | `@sub-2` | `ratelimit.py` | Add `penalize(key)` on `TokenBucket` (and the module wrapper) that sets that key's tokens to zero. Leave `allow` / `remaining` behavior as the tests describe it. |

`@sub-1`, `@sub-3`, and `@sub-4` stay idle on this task unless the lead
splits T1.3 into login versus charge.

Tests to add with the code: a second login inside an empty bucket is rejected;
`charge` of 0 cents on the free plan after 103 requests in the period invoices
`overage_cents == 6`; `penalize` makes the next `allow` fail.

## Task 2 — Lockout and rate limit collide in `login`

This one is supposed to object. `@auth-agent` and `@sub-1` both edit
`auth.login` in `auth.py`.

- `@auth-agent` adds the rate-limit check from T1.1 (if it has not landed).
- `@sub-1` adds a failure counter: five `login` calls with a wrong password
  for the same username lock the account, and later calls return
  `LoginResult(False, None, "locked")` until `revoke_user_sessions` (or a new
  helper) clears it.

Whoever lands second rewrites lines the first agent now owns. The owner
objects on the thread, for example: "keep the rate-limit check; do not replace
`login`". `@sub-1` (or `@auth-agent`, if they were the one objected to) sends
a new push of `login` that does both: rate limit first, then the lockout
counter, then the password check. The owner approves that push. The objected
push never becomes the next link.

Show the objection on the original push and the revised push landing after
approval, with blame for `login` ending on the agent who sent the combined
version.

## Task 3 — Record the rate-limit decision on the protected interface

`@api-agent` edits `api/interfaces.py` only:

- Add a frozen dataclass `RateLimitDecision` with `allowed: bool`,
  `remaining: int`, and `retry_after: float`.
- Add `decide(self, key: str) -> RateLimitDecision` to the `RateLimiter`
  protocol.

That path is `explicit`. Genesis is authored by `@lead`, so `@lead` owns the
protocol lines this push rewrites and has to approve. If the core treats the
change as having no affected owner (a brand-new dataclass at the bottom of the
file, no rewritten line), one approval from any other voter is still required.
Either way, have `@lead` send a signed `approve`, or have `@nelson` reply
`approve` on the thread. Do not wait on the lazy-consensus window: explicit
consent does not open one.

`ratelimit.TokenBucket` does not have to grow `decide` in this task. A later
push can implement it. This task is the consent beat: the push stays pending,
with no link, until that approval arrives, and then the tester still has to
pass the tree.
