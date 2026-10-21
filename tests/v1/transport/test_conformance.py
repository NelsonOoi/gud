"""§20.2 conformance, parametrised over transports.

- fake, fake_network: always.
- imap_smtp: against a local GreenMail (skipped when java or the jar is missing). GreenMail adds
  no Authentication-Results; the harness plays the receiving MTA for the human-verification
  path by prepending `Authentication-Results: mx.gud.test; dmarc=<verdict> ...` (the pinned
  authserv-id), the way Fastmail/Gmail/M365 MXes do.
- primitive_mock: PrimitiveMailbox against tests/v1/transport/primitive_mock.py.
- primitive_live: only with GUD_LIVE_PRIMITIVE=1 (credentials from PRIMITIVE_API_KEY /
  PRIMITIVE_EMAIL or the env file named by GUD_LIVE_ENV_FILE); at most 40 emails, to a fresh
  `repo-conf-<stamp>@<domain>`.
- jmap_mock, ses_mock, postmark_mock, gmail_mock: each adapter against its local mock of the
  provider API (jmap_mock.py, ses_mock.py, postmark_mock.py, gmail_mock.py).
- jmap_live, ses_live, postmark_live, gmail_live: only with GUD_LIVE_JMAP / GUD_LIVE_SES /
  GUD_LIVE_POSTMARK / GUD_LIVE_GMAIL = 1 and the settings each builder names; never run here.

Set GUD_CONF_REPORT=<path> to write the measurements as JSON.
"""

from __future__ import annotations

import imaplib
import json
import os
import smtplib
import time
from pathlib import Path

import pytest

from gud.transport import auth, mime
from gud.transport.fake import FAKE_AUTHSERV_ID, Faults, FakeMailbox, FakeNetwork
from gud.transport.gmail import AUTHSERV_ID as GMAIL_AUTHSERV_ID, GmailMailbox, OAuthClient
from gud.transport.imap_smtp import ImapSmtpMailbox
from gud.transport.jmap import JmapMailbox
from gud.transport.postmark import WEBHOOK_IPS as PM_WEBHOOK_IPS, PostmarkMailbox
from gud.transport.primitive import AUTHSERV_ID, PrimitiveMailbox
from gud.transport.ses import AUTHSERV_ID as SES_AUTHSERV_ID, AwsCredentials, SesMailbox

from . import greenmail
from .conformance import ALL_CHECKS, Harness
from .gmail_mock import AUDIENCE as GMAIL_AUDIENCE, PUSH_SA as GMAIL_PUSH_SA, TOPIC as GMAIL_TOPIC, MockGmail
from .jmap_mock import MockJmap
from .postmark_mock import HOOK_PASSWORD as PM_HOOK_PASSWORD, HOOK_USER as PM_HOOK_USER, MockPostmark
from .primitive_mock import SECRET, MockPrimitive
from .ses_mock import ACCESS_KEY as SES_ACCESS_KEY, REGION as SES_REGION, SECRET_KEY as SES_SECRET_KEY, \
    TOPIC as SES_TOPIC, MockAws

MIB = 1024 * 1024
MTA_AUTHSERV = "mx.gud.test"
TRANSPORTS = ["fake", "fake_network", "imap_smtp", "primitive_mock", "primitive_live",
              "jmap_mock", "jmap_live", "ses_mock", "ses_live", "postmark_mock", "postmark_live",
              "gmail_mock", "gmail_live"]
RESULTS: dict[str, dict] = {}


def _from_domain(raw: bytes) -> str:
    return mime.parse(raw).from_addr.rsplit("@", 1)[-1].lower()


def _mta_header(authserv: str, raw: bytes, verdict: str) -> bytes:
    return auth.prepend(raw, auth.synthesize(authserv, verdict, _from_domain(raw)))


def _fake(tmp: Path) -> Harness:
    root = tmp / "fakemail"
    repo = FakeMailbox(root, "repo@gud.test")
    sender = FakeMailbox(root, "alice@gud.test")
    return Harness(
        name="fake", repo=repo, sender=sender, human_address="human@example.org",
        authserv_id=FAKE_AUTHSERV_ID, reopen=lambda: FakeMailbox(root, "repo@gud.test"),
        wake_bound_s=5.0, delivery_timeout_s=10.0,
        deliver_external=lambda raw, verdict: repo.deliver(_mta_header(FAKE_AUTHSERV_ID, raw, verdict)),
        expected_caps={"name": "fake", "max_message_bytes": 10 * MIB, "push": "none",
                       "auth_results": "header", "authserv_id": FAKE_AUTHSERV_ID,
                       "received_time_resolution_ms": 1, "preserves_message_id": True},
    )


def _fake_network() -> Harness:
    net = FakeNetwork(faults=Faults(delay_ms=200))
    repo = net.mailbox("repo@gud.test")
    return Harness(
        name="fake_network", repo=repo, sender=net.mailbox("alice@gud.test"),
        human_address="human@example.org", authserv_id=FAKE_AUTHSERV_ID,
        reopen=lambda: net.mailbox("repo@gud.test"), wake_bound_s=5.0, delivery_timeout_s=10.0,
        deliver_external=lambda raw, verdict: repo.deliver(_mta_header(FAKE_AUTHSERV_ID, raw, verdict)),
        sleep=lambda s: net.advance(int(s * 1000)), monotonic=lambda: net.now_ms / 1000.0, wall=net.clock,
        virtual=True, expected_caps={"name": "fake", "push": "none"},
    )


def _imap(request) -> Harness:
    gm = request.getfixturevalue("greenmail_server")

    def mk(user: str) -> ImapSmtpMailbox:
        return ImapSmtpMailbox(gm.host, gm.imap_port, gm.host, gm.smtp_port, user, greenmail.PASSWORD,
                               gm.address(user), imap_tls="none", smtp_tls="none", idle_poll=5.0,
                               authserv_id=MTA_AUTHSERV)

    conn = imaplib.IMAP4(gm.host, gm.imap_port)
    conn.login("repo", greenmail.PASSWORD)
    conn.create("Junk")
    conn.logout()
    repo = mk("repo")

    def external(raw: bytes, verdict: str) -> None:
        with smtplib.SMTP(gm.host, gm.smtp_port) as smtp:
            smtp.sendmail(mime.parse(raw).from_addr, [gm.address("repo")], _mta_header(MTA_AUTHSERV, raw, verdict))

    return Harness(
        name="imap_smtp", repo=repo, sender=mk("alice"), human_address=gm.address("human"),
        authserv_id=MTA_AUTHSERV, reopen=lambda: mk("repo"), wake_bound_s=30.0, delivery_timeout_s=30.0,
        deliver_external=external, disconnect=repo._drop_idle_connections, adds_auth_results=False,
        expected_caps={"name": "imap_smtp", "push": "idle", "auth_results": "header",
                       "received_time_resolution_ms": 1000, "max_message_bytes": 10 * MIB},
    )


def _primitive_mock() -> Harness:
    mock = MockPrimitive()

    def mk(local: str) -> PrimitiveMailbox:
        return PrimitiveMailbox("test-key", f"{local}@{mock.domain}", base_url="https://mock.primitive/v1",
                                client=mock.client(), webhook_secret=SECRET, subscribe_wait=2)

    repo = mk("repo")

    def external(raw: bytes, verdict: str) -> None:
        mock.inject(raw, repo.address, auth={"dmarc": verdict, "dmarcFromDomain": _from_domain(raw)})

    h = Harness(
        name="primitive_mock", repo=repo, sender=mk("agent"), human_address="human@example.org",
        authserv_id=AUTHSERV_ID, reopen=lambda: mk("repo"), wake_bound_s=5.0, delivery_timeout_s=10.0,
        deliver_external=external,
        expected_caps={"name": "primitive", "push": "webhook", "auth_results": "synthesized",
                       "authserv_id": AUTHSERV_ID, "send_quota_per_day": 10_000,
                       "preserves_message_id": False, "max_message_bytes": 41_943_040},
    )
    h.results["mock"] = mock
    return h


def _live_credentials() -> tuple[str, str]:
    key, email = os.environ.get("PRIMITIVE_API_KEY"), os.environ.get("PRIMITIVE_EMAIL")
    env_file = os.environ.get("GUD_LIVE_ENV_FILE")
    if (not key or not email) and env_file and Path(env_file).is_file():
        values = {}
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
        key, email = key or values.get("PRIMITIVE_API_KEY"), email or values.get("PRIMITIVE_EMAIL")
    if not key or not email:
        pytest.skip("live Primitive credentials not found")
    return key, email


def _primitive_live() -> Harness:
    if os.environ.get("GUD_LIVE_PRIMITIVE") != "1":
        pytest.skip("set GUD_LIVE_PRIMITIVE=1 to run against the live Primitive account")
    key, email = _live_credentials()
    domain = email.rsplit("@", 1)[1]
    stamp = time.strftime("%Y%m%d%H%M%S")
    repo_addr, agent_addr = f"repo-conf-{stamp}@{domain}", f"agent-conf-{stamp}@{domain}"

    def mk(addr: str) -> PrimitiveMailbox:
        return PrimitiveMailbox(key, addr, subscribe_wait=10)

    return Harness(
        name="primitive_live", repo=mk(repo_addr), sender=mk(agent_addr), human_address="human@example.com",
        authserv_id=AUTHSERV_ID, reopen=lambda: mk(repo_addr), wake_bound_s=5.0, delivery_timeout_s=90.0,
        max_sends=int(os.environ.get("GUD_LIVE_MAX_SENDS", "40")),
        expected_caps={"name": "primitive", "push": "webhook", "auth_results": "synthesized",
                       "authserv_id": AUTHSERV_ID, "preserves_message_id": False},
    )


def _jmap_mock() -> Harness:
    mock = MockJmap()

    def mk(local: str) -> JmapMailbox:
        acc = mock.account(local)
        return JmapMailbox(mock.session_url(), acc.token, acc.address, client=mock.client(),
                           authserv_id=mock.authserv_id, ping=1)

    repo = mk("repo")
    return Harness(
        name="jmap_mock", repo=repo, sender=mk("agent"), human_address="human@example.org",
        authserv_id=mock.authserv_id, reopen=lambda: mk("repo"), wake_bound_s=5.0, delivery_timeout_s=10.0,
        deliver_external=lambda raw, verdict: mock.inject(raw, repo.address, verdict),
        disconnect=mock.drop_streams,
        expected_caps={"name": "jmap", "push": "eventsource", "auth_results": "header",
                       "authserv_id": mock.authserv_id, "max_message_bytes": 50_000_000,
                       "preserves_message_id": True, "received_time_resolution_ms": 1000},
    )


def _jmap_live() -> Harness:
    """GUD_LIVE_JMAP=1 with GUD_LIVE_JMAP_SESSION_URL, _TOKEN (repo account), _ADDRESS, _AUTHSERV_ID,
    and _SENDER_TOKEN / _SENDER_ADDRESS for a second account (or alias identity) that sends."""
    if os.environ.get("GUD_LIVE_JMAP") != "1":
        pytest.skip("set GUD_LIVE_JMAP=1 to run against a live JMAP account")
    env = {k: os.environ.get(f"GUD_LIVE_JMAP_{k}") for k in
           ("SESSION_URL", "TOKEN", "ADDRESS", "AUTHSERV_ID", "SENDER_TOKEN", "SENDER_ADDRESS", "HUMAN")}
    if not all(env[k] for k in ("SESSION_URL", "TOKEN", "ADDRESS", "SENDER_TOKEN", "SENDER_ADDRESS")):
        pytest.skip("live JMAP credentials not set (GUD_LIVE_JMAP_SESSION_URL, _TOKEN, _ADDRESS, ...)")

    def mk(token: str, address: str) -> JmapMailbox:
        return JmapMailbox(env["SESSION_URL"], token, address, authserv_id=env["AUTHSERV_ID"])

    return Harness(
        name="jmap_live", repo=mk(env["TOKEN"], env["ADDRESS"]), sender=mk(env["SENDER_TOKEN"], env["SENDER_ADDRESS"]),
        human_address=env["HUMAN"] or "human@example.com", authserv_id=env["AUTHSERV_ID"],
        reopen=lambda: mk(env["TOKEN"], env["ADDRESS"]), wake_bound_s=5.0, delivery_timeout_s=90.0,
        max_sends=int(os.environ.get("GUD_LIVE_MAX_SENDS", "40")),
        expected_caps={"name": "jmap", "push": "eventsource", "auth_results": "header"},
    )


def _ses_mock(tmp: Path) -> Harness:
    mock = MockAws()
    creds = AwsCredentials(SES_ACCESS_KEY, SES_SECRET_KEY)

    def mk(local: str, *, inbound: bool) -> SesMailbox:
        kw = {"topic_arns": (SES_TOPIC,), "inbound_bucket": mock.bucket, "inbound_prefix": mock.prefix} if inbound else {}
        return SesMailbox(SES_REGION, creds, f"{local}@{mock.receiving_domain}", client=mock.client(),
                          store=tmp / f"spool-{local}", catchup_interval=2.0, **kw)

    repo = mk("repo", inbound=True)
    mock.sns_sink = lambda headers, body: repo.accept_webhook(headers, body)
    return Harness(
        name="ses_mock", repo=repo, sender=mk("agent", inbound=False), human_address="human@example.org",
        authserv_id=SES_AUTHSERV_ID, reopen=lambda: mk("repo", inbound=True), wake_bound_s=5.0,
        delivery_timeout_s=10.0, deliver_external=lambda raw, verdict: mock.inject(raw, repo.address, verdict),
        expected_caps={"name": "ses", "push": "webhook", "auth_results": "synthesized",
                       "authserv_id": SES_AUTHSERV_ID, "send_quota_per_day": 50_000,
                       "preserves_message_id": False, "max_message_bytes": 40_000_000,
                       "received_time_resolution_ms": 1},
    )


def _ses_live(tmp: Path) -> Harness:
    """GUD_LIVE_SES=1 with AWS_* credentials and GUD_LIVE_SES_REGION, _REPO (receiving address),
    _SENDER (verified identity), _BUCKET, _PREFIX, _TOPIC. No public URL is assumed: the repo
    reads by S3 catch-up every 2 s (the SNS webhook path is covered by the mock only)."""
    if os.environ.get("GUD_LIVE_SES") != "1":
        pytest.skip("set GUD_LIVE_SES=1 to run against a live SES account")
    env = {k: os.environ.get(f"GUD_LIVE_SES_{k}") for k in ("REGION", "REPO", "SENDER", "BUCKET", "PREFIX", "TOPIC")}
    if not all(env[k] for k in ("REGION", "REPO", "SENDER", "BUCKET")):
        pytest.skip("live SES settings not set (GUD_LIVE_SES_REGION, _REPO, _SENDER, _BUCKET)")
    creds = AwsCredentials.from_env()

    def mk(address: str, inbound: bool) -> SesMailbox:
        kw = {"topic_arns": tuple(t for t in [env["TOPIC"]] if t), "inbound_bucket": env["BUCKET"],
              "inbound_prefix": env["PREFIX"] or ""} if inbound else {}
        return SesMailbox(env["REGION"], creds, address, store=tmp / f"spool-{address}", catchup_interval=2.0, **kw)

    return Harness(
        name="ses_live", repo=mk(env["REPO"], True), sender=mk(env["SENDER"], False), human_address="human@example.com",
        authserv_id=SES_AUTHSERV_ID, reopen=lambda: mk(env["REPO"], True), wake_bound_s=5.0, delivery_timeout_s=120.0,
        max_sends=int(os.environ.get("GUD_LIVE_MAX_SENDS", "40")),
        expected_caps={"name": "ses", "push": "webhook", "auth_results": "synthesized", "authserv_id": SES_AUTHSERV_ID},
    )


def _postmark_mock(tmp: Path) -> Harness:
    mock = MockPostmark()

    def mk(local: str) -> PostmarkMailbox:
        return PostmarkMailbox(f"pm-{local}", f"{local}@{mock.inbound_domain}", base_url="https://mock.postmark",
                               client=mock.client(), store=tmp / f"spool-{local}", webhook_username=PM_HOOK_USER,
                               webhook_password=PM_HOOK_PASSWORD, allowed_ips=PM_WEBHOOK_IPS)

    repo = mk("repo")
    mock.webhook_sink = lambda headers, body: repo.accept_webhook(headers, body)
    # No deliver_external: Postmark reports no DMARC verdict, so no unsigned mail can verify and
    # there is no positive control (the forged-A-R case is a unit test).
    return Harness(
        name="postmark_mock", repo=repo, sender=mk("agent"), human_address="human@example.org",
        authserv_id=None, reopen=lambda: mk("repo"), wake_bound_s=5.0, delivery_timeout_s=10.0,
        adds_auth_results=False,
        expected_caps={"name": "postmark", "push": "webhook", "auth_results": "none", "authserv_id": None,
                       "preserves_message_id": False, "max_message_bytes": 10_000_000,
                       "send_quota_per_day": None, "raw_mime": True},
    )


def _postmark_live(tmp: Path) -> Harness:
    """GUD_LIVE_POSTMARK=1 with GUD_LIVE_POSTMARK_TOKEN (server token), _SENDER (a confirmed
    Sender Signature address), _REPO (the inbound address of the same server). No public URL is
    assumed: the repo reads by message-search catch-up every 2 s."""
    if os.environ.get("GUD_LIVE_POSTMARK") != "1":
        pytest.skip("set GUD_LIVE_POSTMARK=1 to run against a live Postmark server")
    env = {k: os.environ.get(f"GUD_LIVE_POSTMARK_{k}") for k in ("TOKEN", "SENDER", "REPO")}
    if not all(env.values()):
        pytest.skip("live Postmark settings not set (GUD_LIVE_POSTMARK_TOKEN, _SENDER, _REPO)")

    def mk(address: str) -> PostmarkMailbox:
        return PostmarkMailbox(env["TOKEN"], address, store=tmp / f"spool-{address}", catchup_interval=2.0)

    return Harness(
        name="postmark_live", repo=mk(env["REPO"]), sender=mk(env["SENDER"]), human_address="human@example.com",
        authserv_id=None, reopen=lambda: mk(env["REPO"]), wake_bound_s=5.0, delivery_timeout_s=120.0,
        adds_auth_results=False, max_sends=int(os.environ.get("GUD_LIVE_MAX_SENDS", "40")),
        expected_caps={"name": "postmark", "push": "webhook", "raw_mime": True},
    )


def _gmail_mock(tmp: Path) -> Harness:
    mock = MockGmail()

    def mk(local: str) -> GmailMailbox:
        creds = mock.credentials(local)
        oauth = OAuthClient(creds["client_id"], creds["client_secret"], creds["refresh_token"])
        return GmailMailbox(None, f"{local}@{mock.domain}", oauth=oauth, client=mock.client(),
                            token_cache=tmp / f"{local}.token.json", pubsub_topic=GMAIL_TOPIC,
                            push_audience=GMAIL_AUDIENCE, push_service_account=GMAIL_PUSH_SA, poll_interval=10.0)

    repo = mk("gud-repo")
    mock.push_sink = lambda headers, body: repo.accept_webhook(headers, body)
    return Harness(
        name="gmail_mock", repo=repo, sender=mk("gud-agent"), human_address="human@example.org",
        authserv_id=GMAIL_AUTHSERV_ID, reopen=lambda: mk("gud-repo"), wake_bound_s=30.0, delivery_timeout_s=30.0,
        deliver_external=lambda raw, verdict: mock.inject(raw, repo.address, verdict),
        expected_caps={"name": "gmail", "push": "pubsub", "auth_results": "header", "authserv_id": "mx.google.com",
                       "send_quota_per_day": 500, "preserves_message_id": False,
                       "max_message_bytes": 25_000_000, "received_time_resolution_ms": 1},
    )


def _gmail_live(tmp: Path) -> Harness:
    """GUD_LIVE_GMAIL=1 with GUD_LIVE_GMAIL_REPO_CREDENTIALS / _REPO (the repo account's
    authorized_user JSON and address) and _SENDER_CREDENTIALS / _SENDER. No public URL is
    assumed: the subscription polls history.list every 5 s."""
    if os.environ.get("GUD_LIVE_GMAIL") != "1":
        pytest.skip("set GUD_LIVE_GMAIL=1 to run against live Gmail accounts")
    env = {k: os.environ.get(f"GUD_LIVE_GMAIL_{k}") for k in ("REPO_CREDENTIALS", "REPO", "SENDER_CREDENTIALS", "SENDER")}
    if not all(env.values()):
        pytest.skip("live Gmail settings not set (GUD_LIVE_GMAIL_REPO_CREDENTIALS, _REPO, _SENDER_CREDENTIALS, _SENDER)")

    def mk(creds: str, address: str) -> GmailMailbox:
        return GmailMailbox(creds, address, token_cache=tmp / f"{address}.token.json", poll_interval=5.0)

    return Harness(
        name="gmail_live", repo=mk(env["REPO_CREDENTIALS"], env["REPO"]),
        sender=mk(env["SENDER_CREDENTIALS"], env["SENDER"]), human_address="human@example.com",
        authserv_id=GMAIL_AUTHSERV_ID, reopen=lambda: mk(env["REPO_CREDENTIALS"], env["REPO"]),
        wake_bound_s=30.0, delivery_timeout_s=120.0, max_sends=int(os.environ.get("GUD_LIVE_MAX_SENDS", "40")),
        expected_caps={"name": "gmail", "auth_results": "header", "authserv_id": "mx.google.com"},
    )


@pytest.fixture(scope="module", params=TRANSPORTS)
def harness(request, tmp_path_factory):
    name = request.param
    tmp = tmp_path_factory.mktemp(name)
    builders = {
        "fake": lambda: _fake(tmp),
        "fake_network": _fake_network,
        "imap_smtp": lambda: _imap(request),
        "primitive_mock": _primitive_mock,
        "primitive_live": _primitive_live,
        "jmap_mock": _jmap_mock,
        "jmap_live": _jmap_live,
        "ses_mock": lambda: _ses_mock(tmp),
        "ses_live": lambda: _ses_live(tmp),
        "postmark_mock": lambda: _postmark_mock(tmp),
        "postmark_live": lambda: _postmark_live(tmp),
        "gmail_mock": lambda: _gmail_mock(tmp),
        "gmail_live": lambda: _gmail_live(tmp),
    }
    h = builders[name]()
    RESULTS[name] = {}
    yield h
    RESULTS[name]["emails_sent"] = h.sent_count
    for mb in (h.repo, h.sender):
        try:
            mb.close()
        except Exception:
            pass


@pytest.mark.parametrize("check", ALL_CHECKS, ids=[c.__name__.removeprefix("check_") for c in ALL_CHECKS])
def test_conformance(harness: Harness, check) -> None:
    started = time.monotonic()
    try:
        result = check(harness)
    except BaseException as exc:
        RESULTS[harness.name][check.__name__] = {"FAILED": f"{type(exc).__name__}: {str(exc)[:500]}"}
        raise
    result["_seconds"] = round(time.monotonic() - started, 2)
    RESULTS[harness.name][check.__name__] = result


def teardown_module(module) -> None:
    path = os.environ.get("GUD_CONF_REPORT")
    if path:
        Path(path).write_text(json.dumps(RESULTS, indent=1, default=str))
