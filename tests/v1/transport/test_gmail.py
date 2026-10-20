"""Gmail adapter: OAuth token cache, Pub/Sub JWT verification, history cursors, quota."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gud.transport import mime, registry
from gud.transport.base import Outgoing, TransportError
from gud.transport.gmail import (
    AUTHSERV_ID, GmailAuthError, GmailError, GmailMailbox, GmailQuotaError, JwtError, OAuthClient,
    authorize_installed_app, decode_cursor, delivered, encode_cursor,
)

from .gmail_mock import AUDIENCE, CLIENT_ID, CLIENT_SECRET, PUSH_SA, TOPIC, MockGmail, b64url, sign_jwt


def _box(mock: MockGmail, local: str, tmp_path, **kw) -> GmailMailbox:
    creds = mock.credentials(local)
    kw.setdefault("sleep", lambda s: None)
    return GmailMailbox(None, f"{local}@{mock.domain}", oauth=OAuthClient(CLIENT_ID, CLIENT_SECRET, creds["refresh_token"]),
                        client=mock.client(), token_cache=tmp_path / f"{local}.token.json", pubsub_topic=TOPIC,
                        push_audience=AUDIENCE, push_service_account=PUSH_SA, **kw)


def _send(box: GmailMailbox, to: str, subject: str, **kw):
    return box.send(Outgoing(from_addr=box.address, to=(to,), subject=subject, text="x", **kw))


def test_token_refresh_cache_and_errors(tmp_path) -> None:
    mock = MockGmail()
    box = _box(mock, "repo", tmp_path)
    box.head()
    assert mock.token_requests == 1
    path = tmp_path / "repo.token.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    _box(mock, "repo", tmp_path).head()
    assert mock.token_requests == 1, "a new instance reuses the cached access token"
    mock.tokens.clear()                     # server-side revocation of the access token: 401 -> refresh once
    box.head()
    assert mock.token_requests == 2
    mock.token_ttl = 30                      # expires inside the 60 s margin: refreshed before use
    box.tokens.token(force=True)
    box.head()
    assert mock.token_requests == 4
    mock.account("repo").refresh_token = "rotated"     # the user revoked the old grant
    stale = GmailMailbox(None, "repo@gmail.com", oauth=OAuthClient(CLIENT_ID, CLIENT_SECRET, "refresh-repo"),
                         client=mock.client(), token_cache=tmp_path / "other.json")
    with pytest.raises(GmailAuthError, match="invalid_grant"):
        stale.head()
    (tmp_path / "au.json").write_text(json.dumps(mock.credentials("x")))
    assert OAuthClient.from_file(tmp_path / "au.json").refresh_token == "refresh-x"
    (tmp_path / "inst.json").write_text(json.dumps({"installed": {"client_id": "c", "client_secret": "s"},
                                                    "refresh_token": "r"}))
    assert OAuthClient.from_file(tmp_path / "inst.json") == OAuthClient("c", "s", "r")
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(GmailAuthError):
        OAuthClient.from_file(tmp_path / "bad.json")


def test_pubsub_jwt_positive_and_negative(tmp_path) -> None:
    mock = MockGmail()
    repo = _box(mock, "repo", tmp_path)
    body = mock.push_body(repo.address, 1234)
    ok = {"Authorization": f"Bearer {mock.push_token()}"}
    assert repo.verify(ok, body, 0)
    assert repo.accept_webhook(ok, body) == {"emailAddress": repo.address, "historyId": 1234}
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    good_claims = {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": PUSH_SA, "email_verified": True,
                   "iat": now, "exp": now + 600}
    bad_tokens = {
        "other key": mock.push_token(key=other_key),
        "wrong audience": mock.push_token(audience="https://evil.example/hook"),
        "wrong issuer": mock.push_token(iss="https://evil.example"),
        "expired": mock.push_token(exp_in=-3600),
        "wrong service account": mock.push_token(email="attacker@evil.iam.gserviceaccount.com"),
        "unknown kid": mock.push_token(kid="nope"),
        "alg none": b64url(json.dumps({"alg": "none", "kid": mock.kid}).encode()) + "."
        + b64url(json.dumps(good_claims).encode()) + ".",
        "alg HS256": sign_jwt(mock.key, good_claims, mock.kid, alg="HS256"),
        "unverified email": sign_jwt(mock.key, dict(good_claims, email_verified=False), mock.kid),
        "malformed": "not.a.jwt.at.all",
    }
    for label, token in bad_tokens.items():
        assert not repo.verify({"Authorization": f"Bearer {token}"}, body, 0), label
    assert not repo.verify({}, body, 0)
    with pytest.raises(JwtError):
        repo.accept_webhook({"Authorization": f"Bearer {bad_tokens['other key']}"}, body)
    no_push = GmailMailbox(None, "a@gmail.com", oauth=OAuthClient(CLIENT_ID, CLIENT_SECRET, "r"),
                           client=mock.client(), token_cache=tmp_path / "np.json")
    assert not no_push.verify(ok, body, 0), "no audience configured: no push verifies"
    jwks_fetches = [r for r in mock.requests if r[2] == "/oauth2/v3/certs"]
    assert len(jwks_fetches) <= 2, "JWKS cached (one refetch allowed for an unknown kid)"


def test_push_wakes_the_subscription(tmp_path) -> None:
    mock = MockGmail()
    repo, agent = _box(mock, "repo", tmp_path, poll_interval=60.0), _box(mock, "agent", tmp_path)
    mock.push_sink = lambda headers, body: repo.accept_webhook(headers, body)
    hits, event = [], threading.Event()
    sub = repo.subscribe(lambda r: (hits.append(r), event.set()))
    deadline = time.monotonic() + 3
    while not mock.account("repo").watched and time.monotonic() < deadline:
        time.sleep(0.01)
    t0 = time.monotonic()
    _send(agent, repo.address, "pushed")
    assert event.wait(5), "the Pub/Sub push woke the subscription (poll interval is 60 s)"
    sub.close()
    assert time.monotonic() - t0 < 2
    assert repo.watch_expiration and repo.watch_expiration > time.time() + 6 * 86400


def test_history_cursors_resume_and_404_fallback(tmp_path) -> None:
    mock = MockGmail()
    repo, agent = _box(mock, "repo", tmp_path), _box(mock, "agent", tmp_path)
    start = repo.head()
    for i in range(4):
        _send(agent, repo.address, f"m{i}")
    seen, cursor = [], start
    while True:
        items, cursor = _box(mock, "repo", tmp_path).list(since=cursor, limit=1)
        if not items:
            break
        seen.append(mime.parse(items[0].raw_mime).subject)
    assert seen == ["m0", "m1", "m2", "m3"]
    assert repo.list(since=cursor) == ([], cursor)
    assert agent.list(since=agent.head())[0] == []
    # history expired: walk from the cursor's last internalDate (at least once)
    _send(agent, repo.address, "after expiry")
    mock.min_history = 10**9
    items, cursor2 = repo.list(since=cursor)
    assert "after expiry" in [mime.parse(i.raw_mime).subject for i in items]
    mock.min_history = 0
    assert decode_cursor(cursor2)[0] in ("h", "s")
    for bad in ("", "x:1::", "h:abc::", "h:1:a:zz"):
        with pytest.raises(TransportError):
            decode_cursor(bad)
    assert decode_cursor(encode_cursor("s", "9", 5, ("a", "b"))) == ("s", 9, ("a", "b"), 5)


def test_walk_from_start_reads_spam_and_skips_own_sends(tmp_path) -> None:
    mock = MockGmail()
    repo, agent = _box(mock, "repo", tmp_path), _box(mock, "agent", tmp_path)
    _send(repo, agent.address, "repo's own send")
    _send(agent, repo.address, "inbox")
    mock.deliver(mime.build(Outgoing(from_addr="s@example.org", to=(repo.address,), subject="spam", text="x")),
                 repo.address, verdict="fail", spam=True)
    _send(repo, repo.address, "to self")
    items, cursor = repo.list(since=None, limit=2)
    more, cursor = repo.list(since=cursor)
    subjects = [mime.parse(i.raw_mime).subject for i in items + more]
    assert subjects == ["inbox", "spam", "to self"]
    assert all(a <= b for a, b in zip([i.received_at for i in items + more], [i.received_at for i in (items + more)[1:]]))
    top = mime.parse(items[0].raw_mime).auth_results[0]
    assert top.startswith(f"{AUTHSERV_ID};") and "dmarc=pass" in top
    assert decode_cursor(cursor)[0] == "h"
    assert delivered(["SENT", "INBOX"]) and not delivered(["SENT"]) and not delivered(["DRAFT", "INBOX"])
    assert delivered(["SPAM"]) and delivered(["Label_1"])


def test_message_id_and_from_rewrite_are_reported(tmp_path) -> None:
    mock = MockGmail()
    repo, agent = _box(mock, "repo", tmp_path), _box(mock, "agent", tmp_path)
    start = repo.head()
    sent = _send(agent, repo.address, "mid", message_id="<mine@gmail.com>")
    assert sent.message_id.endswith("@mail.gmail.com>") and sent.message_id != "<mine@gmail.com>"
    item = repo.list(since=start)[0][0]
    assert mime.parse(item.raw_mime).message_id == sent.message_id
    agent.send(Outgoing(from_addr="human@example.org", to=(repo.address,), subject="spoof", text="approve\n"))
    spoof = [i for i in repo.list(since=start)[0] if mime.parse(i.raw_mime).subject == "spoof"][0]
    assert mime.parse(spoof.raw_mime).from_addr == agent.address, "Gmail replaced the From it cannot send as"


def test_quota_and_size_limits(tmp_path) -> None:
    mock = MockGmail(send_limit=1)
    agent = _box(mock, "agent", tmp_path)
    _send(agent, "x@example.org", "one")
    with pytest.raises(GmailQuotaError, match="sending limit"):
        _send(agent, "x@example.org", "two")
    assert agent.capabilities().send_quota_per_day == 500
    work = GmailMailbox(None, "dev@company.example", oauth=OAuthClient("c", "s", "r"), token_cache=tmp_path / "w.json",
                        client=mock.client())
    assert work.capabilities().send_quota_per_day == 2000 and work.capabilities().push == "none"
    assert GmailMailbox(None, "dev@company.example", oauth=OAuthClient("c", "s", "r"), account_type="consumer",
                        token_cache=tmp_path / "w2.json", client=mock.client()).capabilities().send_quota_per_day == 500
    assert _box(mock, "agent", tmp_path, send_quota_per_day=100).capabilities().send_quota_per_day == 100
    with pytest.raises(GmailError, match="messageTooLarge"):
        agent.send(Outgoing(from_addr=agent.address, to=("x@example.org",), envelope=b"\0" * 19_000_000))
    mock2 = MockGmail()
    big_sender = _box(mock2, "agent", tmp_path / "b")
    repo2 = _box(mock2, "repo", tmp_path / "b")
    start = repo2.head()
    big_sender.send(Outgoing(from_addr=big_sender.address, to=(repo2.address,), subject="big", envelope=b"\1" * 3_500_000))
    assert any(r[2] == "/upload/gmail/v1/users/me/messages/send" for r in mock2.requests), "media upload for > 4 MB"
    assert len(repo2.list(since=start)[0]) == 1
    mock2.fail_next = [503, 500]
    _send(big_sender, "x@example.org", "retried")


def test_installed_app_flow(tmp_path) -> None:
    mock = MockGmail()
    mock.account("repo")
    opened = []

    def browser(url: str) -> None:
        opened.append(url)
        query = parse_qs(urlsplit(url).query)
        assert query["code_challenge_method"] == ["S256"] and query["access_type"] == ["offline"]
        redirect = query["redirect_uri"][0]
        threading.Thread(target=lambda: httpx.get(f"{redirect}?code=auth-code&state={query['state'][0]}"),
                         daemon=True).start()

    token = authorize_installed_app(CLIENT_ID, CLIENT_SECRET, open_browser=browser, client=mock.client(), timeout=10)
    assert token == "refresh-repo" and opened


def test_from_config(tmp_path) -> None:
    with pytest.raises(TransportError):
        registry.open_mailbox("gmail", {"address": "a@gmail.com"})
    box = registry.open_mailbox("gmail", {"address": "a@gmail.com", "client_id": "c", "client_secret": "s",
                                          "refresh_token": "r", "token_cache": str(tmp_path / "t.json"),
                                          "account_type": "workspace", "pubsub_topic": TOPIC})
    caps = box.capabilities()
    assert caps.push == "pubsub" and caps.send_quota_per_day == 2000 and caps.authserv_id == "mx.google.com"
