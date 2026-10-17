"""HTTP-shaped handlers over auth and billing.

Each function returns ``{"status": int, "body": dict}``. They do not apply
rate limits or bill overage; those land as separate pushes (see TASKS.md).
"""

from __future__ import annotations

import auth
import billing
from api.interfaces import Invoice


def handle_login(payload: dict) -> dict:
    """Check username and password. 200 with a token, or 401."""
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    result = auth.login(username, password)
    if not result.ok or result.session is None:
        return _error(401, result.reason or "invalid credentials")
    return _ok({"token": result.session.token, "username": result.session.username})


def handle_whoami(token: str | None) -> dict:
    """The user behind `token`, or 401."""
    session = auth.check_session(token)
    if session is None:
        return _error(401, "no session")
    user = auth.get_user(session.username)
    if user is None:
        return _error(401, "no session")
    return _ok({"username": user.username, "email": user.email, "plan": user.plan})


def handle_logout(token: str | None) -> dict:
    """Drop `token`. 401 when it was not a live session."""
    if not token or auth.check_session(token) is None:
        return _error(401, "no session")
    auth.logout(token)
    return _ok({"logged_out": True})


def handle_charge(token: str | None, payload: dict) -> dict:
    """Invoice `amount_cents` to the session's user. Does not add plan overage."""
    session = auth.check_session(token)
    if session is None:
        return _error(401, "no session")
    amount = payload.get("amount_cents")
    # bool is an int, and it is not an amount.
    if type(amount) is not int:
        return _error(400, "amount_cents must be an integer")
    try:
        invoice = billing.charge(session.username, amount)
    except KeyError:
        return _error(404, "no billing account")
    except ValueError as exc:
        return _error(400, str(exc))
    return _ok({"invoice": invoice_view(invoice)})


def handle_usage(token: str | None) -> dict:
    """Metered requests for the session's user, plus what overage would cost."""
    session = auth.check_session(token)
    if session is None:
        return _error(401, "no session")
    usage = billing.usage_of(session.username)
    plan = billing.plan_for(session.username)
    return _ok({
        "username": usage.username,
        "period": usage.period,
        "requests": usage.requests,
        "included": plan.included_requests,
        "overage_cents": billing.overage_cents(usage.requests, plan),
    })


def invoice_view(invoice: Invoice) -> dict:
    """A JSON-ready invoice. Handlers and tests both use this."""
    return {
        "username": invoice.username,
        "period": invoice.period,
        "base_cents": invoice.base_cents,
        "overage_cents": invoice.overage_cents,
        "total_cents": invoice.total_cents,
    }


def _ok(body: dict) -> dict:
    return {"status": 200, "body": body}


def _error(status: int, reason: str) -> dict:
    return {"status": status, "body": {"error": reason}}
