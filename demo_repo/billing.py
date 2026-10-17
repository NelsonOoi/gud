"""Plans, request metering, and invoices.

`overage_cents` knows what extra requests should cost. `charge` does not add
that figure yet; it invoices only the amount the caller passes (see TASKS.md).
"""

from __future__ import annotations

import time

from api.interfaces import Invoice, Plan, Usage

PLANS: dict[str, Plan] = {
    "free": Plan("free", monthly_cents=0, included_requests=100, overage_cents=2),
    "pro": Plan("pro", monthly_cents=2000, included_requests=10_000, overage_cents=1),
}

_accounts: dict[str, str] = {}
_usage: dict[tuple[str, str], int] = {}
_invoices: dict[str, list[Invoice]] = {}


def reset() -> None:
    """Drop accounts, meters, and invoices."""
    _accounts.clear()
    _usage.clear()
    _invoices.clear()


def open_account(username: str, plan: str = "free") -> Plan:
    """Attach `username` to a plan. `register_user` calls this."""
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan}")
    _accounts[username] = plan
    return PLANS[plan]


def plan_for(username: str) -> Plan:
    """The user's plan, or `free` when they have no account row."""
    return PLANS[_accounts.get(username, "free")]


def current_period(when: float | None = None) -> str:
    """UTC `YYYY-MM` for `when` (default: now)."""
    stamp = time.gmtime(time.time() if when is None else when)
    return f"{stamp.tm_year:04d}-{stamp.tm_mon:02d}"


def meter_request(username: str, period: str | None = None, n: int = 1) -> Usage:
    """Add `n` requests to the meter and return the new total."""
    if n < 1:
        raise ValueError("n must be >= 1")
    period = period or current_period()
    key = (username, period)
    _usage[key] = _usage.get(key, 0) + n
    return usage_of(username, period)


def usage_of(username: str, period: str | None = None) -> Usage:
    period = period or current_period()
    return Usage(username, period, _usage.get((username, period), 0))


def overage_cents(requests: int, plan: Plan) -> int:
    """Cents owed for requests past `plan.included_requests`. Zero when inside the plan."""
    extra = max(0, requests - plan.included_requests)
    return extra * plan.overage_cents


def charge(user: str, amount_cents: int, period: str | None = None) -> Invoice:
    """Record an invoice for `amount_cents`. Overage is not included yet.

    Raises `KeyError` when `user` has no account, `ValueError` when the amount is negative.
    """
    if amount_cents < 0:
        raise ValueError("amount_cents must be >= 0")
    if user not in _accounts:
        raise KeyError(user)
    period = period or current_period()
    invoice = Invoice(
        username=user, period=period, base_cents=amount_cents, overage_cents=0, total_cents=amount_cents,
    )
    _invoices.setdefault(user, []).append(invoice)
    return invoice


def list_invoices(user: str) -> list[Invoice]:
    """Invoices for `user`, oldest first. Empty when they have never been charged."""
    return list(_invoices.get(user, []))


def outstanding_cents(user: str) -> int:
    """Sum of `total_cents` across the user's invoices."""
    return sum(invoice.total_cents for invoice in _invoices.get(user, []))
