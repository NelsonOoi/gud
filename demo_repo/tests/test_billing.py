"""Plans, metering, overage math, and invoices."""

from __future__ import annotations

import pytest

import billing


def test_open_account_and_unknown_plan():
    plan = billing.open_account("carol", "pro")
    assert plan.monthly_cents == 2000
    assert billing.plan_for("carol").included_requests == 10_000
    assert billing.plan_for("stranger").name == "free"
    with pytest.raises(ValueError):
        billing.open_account("carol", "enterprise")


def test_meter_is_per_period():
    billing.open_account("carol", "free")
    first = billing.meter_request("carol", period="2026-10", n=3)
    assert first.requests == 3
    assert billing.meter_request("carol", period="2026-10").requests == 4
    other = billing.usage_of("carol", period="2026-11")
    assert other.requests == 0
    assert billing.current_period(1_700_000_000) == "2023-11"


def test_overage_cents_inside_and_past_the_plan():
    free = billing.PLANS["free"]
    assert free.included_requests == 100
    assert billing.overage_cents(100, free) == 0
    assert billing.overage_cents(103, free) == 6
    assert billing.overage_cents(0, billing.PLANS["pro"]) == 0


def test_charge_records_the_amount_without_overage():
    billing.open_account("carol", "free")
    billing.meter_request("carol", period="2026-10", n=150)
    invoice = billing.charge("carol", 250, period="2026-10")
    assert invoice.base_cents == 250
    assert invoice.overage_cents == 0
    assert invoice.total_cents == 250
    assert billing.list_invoices("carol") == [invoice]
    assert billing.outstanding_cents("carol") == 250
    again = billing.charge("carol", 0, period="2026-10")
    assert billing.outstanding_cents("carol") == 250
    assert again.total_cents == 0


def test_charge_rejects_unknown_user_and_negative_amount():
    with pytest.raises(KeyError):
        billing.charge("ghost", 10, period="2026-10")
    billing.open_account("carol")
    with pytest.raises(ValueError):
        billing.charge("carol", -1)
    with pytest.raises(ValueError):
        billing.meter_request("carol", n=0)
