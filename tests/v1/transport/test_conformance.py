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
from gud.transport.imap_smtp import ImapSmtpMailbox
from gud.transport.primitive import AUTHSERV_ID, PrimitiveMailbox

from . import greenmail
from .conformance import ALL_CHECKS, Harness
from .primitive_mock import SECRET, MockPrimitive

MIB = 1024 * 1024
MTA_AUTHSERV = "mx.gud.test"
TRANSPORTS = ["fake", "fake_network", "imap_smtp", "primitive_mock", "primitive_live"]
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
