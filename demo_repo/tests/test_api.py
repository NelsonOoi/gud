"""Handlers, plus the token bucket they do not call yet."""

from __future__ import annotations

import api.handlers as handlers
import auth
import billing
import ratelimit


def test_login_whoami_and_logout(alice):
    denied = handlers.handle_login({"username": "alice", "password": "no"})
    assert denied["status"] == 401
    assert denied["body"]["error"] == "invalid credentials"
    ok = handlers.handle_login({"username": "alice", "password": "correct horse"})
    assert ok["status"] == 200
    token = ok["body"]["token"]
    who = handlers.handle_whoami(token)
    assert who["status"] == 200
    assert who["body"]["username"] == "alice"
    assert who["body"]["email"] == "alice@example.com"
    assert who["body"]["plan"] == "pro"
    assert handlers.handle_whoami(None)["status"] == 401
    assert handlers.handle_whoami("nope")["status"] == 401
    assert handlers.handle_logout(token)["status"] == 200
    assert handlers.handle_whoami(token)["status"] == 401
    assert handlers.handle_logout(token)["status"] == 401


def test_sessions_do_not_cross(alice, bob):
    alice_token = handlers.handle_login({"username": "alice", "password": "correct horse"})["body"]["token"]
    bob_token = handlers.handle_login({"username": "bob", "password": "battery staple"})["body"]["token"]
    assert handlers.handle_whoami(alice_token)["body"]["plan"] == "pro"
    assert handlers.handle_whoami(bob_token)["body"]["plan"] == "free"
    assert auth.check_session(alice_token).username == "alice"
    assert auth.check_session(bob_token).username == "bob"


def test_charge_and_usage(alice_token):
    period = billing.current_period()
    billing.meter_request("alice", period=period, n=4)
    missing = handlers.handle_charge(None, {"amount_cents": 1})
    assert missing["status"] == 401
    bad = handlers.handle_charge(alice_token, {"amount_cents": "5"})
    assert bad["status"] == 400
    assert handlers.handle_charge(alice_token, {"amount_cents": True})["status"] == 400
    charged = handlers.handle_charge(alice_token, {"amount_cents": 500})
    assert charged["status"] == 200
    invoice = charged["body"]["invoice"]
    assert invoice["base_cents"] == 500
    assert invoice["overage_cents"] == 0
    assert invoice["total_cents"] == 500
    usage = handlers.handle_usage(alice_token)
    assert usage["status"] == 200
    assert usage["body"]["requests"] == 4
    assert usage["body"]["included"] == 10_000
    assert usage["body"]["overage_cents"] == 0
    assert handlers.handle_usage(None)["status"] == 401


def test_token_bucket_refills_per_key():
    bucket = ratelimit.TokenBucket(rate=1, capacity=2)
    assert bucket.allow("alice", now=0)
    assert bucket.allow("alice", now=0)
    assert not bucket.allow("alice", now=0)
    assert bucket.remaining("alice", now=0) == 0
    assert bucket.allow("alice", now=1)
    assert bucket.allow("bob", now=0)
    assert bucket.remaining("bob", now=0) == 1


def test_module_bucket_configure_and_reset():
    ratelimit.configure(rate=1, capacity=1)
    assert ratelimit.allow("k", now=10)
    assert not ratelimit.allow("k", now=10)
    assert ratelimit.remaining("k", now=10) == 0
    ratelimit.reset()
    assert ratelimit.allow("k", now=10)
    assert ratelimit.remaining("other", now=10) == 1
