"""SES adapter: SigV4, SNS signature verification, receipt-rule intake, S3 catch-up."""

from __future__ import annotations

import json
import time

import pytest

from gud.transport import auth, mime, registry
from gud.transport.base import Outgoing, TransportError
from gud.transport.ses import (
    AUTHSERV_ID, SNS_CONTENT_MAX, AwsCredentials, SesError, SesMailbox, SesQuotaError, SnsVerificationError,
    check_sns_url, sigv4_headers, sns_string_to_sign,
)

from .ses_mock import ACCESS_KEY, REGION, SECRET_KEY, TOPIC, MockAws, make_cert

CREDS = AwsCredentials(ACCESS_KEY, SECRET_KEY)


def _repo(mock: MockAws, tmp_path, **kw) -> SesMailbox:
    kw.setdefault("topic_arns", (TOPIC,))
    kw.setdefault("inbound_bucket", mock.bucket)
    kw.setdefault("inbound_prefix", mock.prefix)
    return SesMailbox(REGION, CREDS, f"repo@{mock.receiving_domain}", client=mock.client(), store=tmp_path / "spool",
                      sleep=lambda s: None, **kw)


def _agent(mock: MockAws, tmp_path) -> SesMailbox:
    return SesMailbox(REGION, CREDS, f"agent@{mock.receiving_domain}", client=mock.client(),
                      store=tmp_path / "agent-spool", sleep=lambda s: None)


def _send(agent: SesMailbox, to: str, subject: str) -> None:
    agent.send(Outgoing(from_addr=agent.address, to=(to,), subject=subject, text="hello"))


def _capture(mock: MockAws) -> list:
    got: list = []
    mock.sns_sink = lambda headers, body: got.append((headers, body))
    return got


def _wait_for(got: list, n: int = 1) -> None:
    deadline = time.monotonic() + 3
    while len(got) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(got) >= n


def test_sigv4_matches_the_aws_test_suite() -> None:
    creds = AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    vanilla = sigv4_headers("GET", "https://example.amazonaws.com/", region="us-east-1", service="service",
                            credentials=creds, amz_date="20150830T123600Z")
    assert vanilla["Authorization"].endswith(
        "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")
    iam = sigv4_headers("GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08", region="us-east-1",
                        service="iam", credentials=creds, amz_date="20150830T123600Z",
                        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
    assert iam["Authorization"].endswith(
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")
    token = sigv4_headers("GET", "https://b.s3.us-east-1.amazonaws.com/k", region="us-east-1", service="s3",
                          credentials=AwsCredentials("A", "S", "session"))
    assert token["X-Amz-Security-Token"] == "session" and "x-amz-security-token" in token["Authorization"]


def test_send_reports_rewritten_message_id_and_synthesized_auth(tmp_path) -> None:
    mock = MockAws()            # no SNS sink: the message is read by S3 catch-up
    repo, agent = _repo(mock, tmp_path), _agent(mock, tmp_path)
    sent =agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), subject="s", text="t",
                               message_id="<mine@ses.test>"))
    assert sent.message_id == f"<{sent.provider_id}@email.amazonses.com>"
    assert repo.catch_up() == 1
    item = repo.list()[0][0]
    parsed = mime.parse(item.raw_mime)
    assert parsed.message_id == sent.message_id, "SES rewrote the Message-ID as reported"
    assert parsed.auth_results[0] == f"{AUTHSERV_ID}; dmarc=none header.from=ses.test", "catch-up has no verdict"
    eu = SesMailbox("eu-west-1", CREDS, "a@b.c", client=mock.client(), store=tmp_path / "eu")
    assert eu.ses_endpoint == "https://email.eu-west-1.amazonaws.com"


def test_sns_signature_v1_and_v2_and_tampering(tmp_path) -> None:
    for version in ("1", "2"):
        mock = MockAws(signature_version=version)
        got = _capture(mock)
        repo, agent = _repo(mock, tmp_path / version), _agent(mock, tmp_path / version)
        _send(agent, repo.address, f"v{version}")
        _wait_for(got)
        headers, body = got[0]
        now = int(time.time() * 1000)
        assert repo.verify(headers, body, now)
        items = repo.accept_webhook(headers, body)
        assert len(items) == 1 and items[0].cursor == "1"
        assert mime.parse(items[0].raw_mime).auth_results[0] == f"{AUTHSERV_ID}; dmarc=pass header.from=ses.test"
        assert repo.accept_webhook(headers, body) == [], "a retried notification is stored once"
        msg = json.loads(body)
        tampered = dict(msg, Message=msg["Message"].replace("PASS", "FAIL"))
        assert not repo.verify(headers, json.dumps(tampered).encode(), now)
        assert not repo.verify({**headers, "x-amz-sns-message-type": "SubscriptionConfirmation"}, body, now)
        with pytest.raises(SnsVerificationError):
            repo.accept_webhook(headers, json.dumps(dict(msg, SignatureVersion="3")).encode())


@pytest.mark.parametrize("url", [
    "http://sns.us-east-1.amazonaws.com/SimpleNotificationService-x.pem",          # not HTTPS
    "https://sns.us-east-1.amazonaws.com.evil.example/SimpleNotificationService-x.pem",
    "https://evil.example/sns.us-east-1.amazonaws.com/x.pem",
    "https://sns.us-east-1.amazonaws.com@evil.example/x.pem",                      # userinfo trick
    "https://sns.us-east-1.amazonaws.com:8443/x.pem",
    "https://sns.us-east-1.amazonaws.com/x.txt",
    "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-x.pem",         # not the topic's region
    "https://s3.amazonaws.com/sns.us-east-1.amazonaws.com/x.pem",
])
def test_forged_signing_cert_url_is_refused_without_fetching(tmp_path, url) -> None:
    mock = MockAws()
    repo = _repo(mock, tmp_path)
    forged_key, forged_pem = make_cert()
    mock.extra_certs[url] = forged_pem
    msg = mock.sns_message("Notification", "{}", cert_url=url, key=forged_key)
    before = len(mock.requests)
    assert not repo.verify(mock.headers_for(msg), json.dumps(msg).encode(), 0)
    assert len(mock.requests) == before, "a forged certificate URL is never fetched"
    with pytest.raises(SnsVerificationError):
        check_sns_url(url, region=REGION)


def test_wrong_key_wrong_topic_wrong_subject_and_pinning(tmp_path) -> None:
    mock = MockAws()
    repo = _repo(mock, tmp_path)
    good = mock.sns_message("Notification", "{}")
    assert repo.verify({}, json.dumps(good).encode(), 0)
    other_key, _ = make_cert()
    assert not repo.verify({}, json.dumps(mock.sns_message("Notification", "{}", key=other_key)).encode(), 0)
    other_topic = mock.sns_message("Notification", "{}", topic=f"arn:aws:sns:{REGION}:999999999999:attacker")
    assert not repo.verify({}, json.dumps(other_topic).encode(), 0), "a topic in another account is not ours"
    # A certificate that is not an SNS certificate, even at an allow-listed URL, is refused.
    url = f"https://sns.{REGION}.amazonaws.com/SimpleNotificationService-other.pem"
    key, pem = make_cert("attacker.example")
    mock.extra_certs[url] = pem
    assert not repo.verify({}, json.dumps(mock.sns_message("Notification", "{}", cert_url=url, key=key)).encode(), 0)
    # Pinned: once fetched, a URL's certificate does not change.
    new_key, new_pem = make_cert()
    mock.cert_pem = new_pem
    assert repo.verify({}, json.dumps(mock.sns_message("Notification", "{}")).encode(), 0)
    assert not repo.verify({}, json.dumps(mock.sns_message("Notification", "{}", key=new_key)).encode(), 0)
    assert sns_string_to_sign({"Type": "Notification", "Message": "m", "MessageId": "i", "Timestamp": "t",
                               "TopicArn": "a"}) == b"Message\nm\nMessageId\ni\nTimestamp\nt\nTopicArn\na\nType\nNotification\n"


def test_subscription_confirmation(tmp_path) -> None:
    mock = MockAws()
    repo = _repo(mock, tmp_path)
    msg = mock.subscription_confirmation()
    assert repo.accept_webhook(mock.headers_for(msg), json.dumps(msg).encode()) == []
    assert mock.confirmed and mock.confirmed[0][0] == TOPIC and repo.confirmed == [TOPIC]
    forged = dict(msg, SubscribeURL="https://evil.example/?Action=ConfirmSubscription")
    with pytest.raises(SnsVerificationError):
        repo.accept_webhook({}, json.dumps(forged).encode())   # signature covers SubscribeURL
    with pytest.raises(SnsVerificationError):
        repo.confirm(forged)                                    # and the host rule holds anyway


def test_sns_action_inline_content_and_its_150_kb_cap(tmp_path) -> None:
    mock = MockAws(action="sns")
    got = _capture(mock)
    repo = _repo(mock, tmp_path, inbound_bucket=None)
    agent = _agent(mock, tmp_path)
    assert repo.capabilities().max_message_bytes == SNS_CONTENT_MAX
    _send(agent, repo.address, "small")
    _wait_for(got)
    assert len(repo.accept_webhook(*got[0])) == 1
    agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), subject="big", envelope=b"x" * 200_000))
    time.sleep(0.2)
    assert len(got) == 1, "SES bounces mail over 150 KB for an SNS action"
    assert not mock.objects


def test_catch_up_dedupes_with_the_webhook_and_resumes(tmp_path) -> None:
    mock = MockAws(max_keys=2)
    got = _capture(mock)
    repo, agent = _repo(mock, tmp_path), _agent(mock, tmp_path)
    for i in range(5):
        _send(agent, repo.address, f"m{i}")
    _wait_for(got, 5)
    repo.accept_webhook(*got[1])                     # one arrived by webhook, the rest were missed
    assert repo.catch_up() == 4 and repo.catch_up() == 0
    items, cursor = repo.list()
    assert len(items) == 5 and len({i.provider_id for i in items}) == 5
    for headers, body in got:
        assert repo.accept_webhook(headers, body) == [], "webhook after catch-up writes nothing"
    fresh = _repo(mock, tmp_path)
    assert fresh.list(since=items[2].cursor)[0] == items[3:]
    assert fresh.list(since=cursor) == ([], cursor)


def test_dmarc_verdicts_and_recipient_filter(tmp_path) -> None:
    mock = MockAws()
    got = _capture(mock)
    repo = _repo(mock, tmp_path)
    forged = (b"Authentication-Results: " + auth.synthesize(AUTHSERV_ID, "pass", "example.org").encode() + b"\r\n"
              + mime.build(Outgoing(from_addr="human@example.org", to=(repo.address,), subject="forged", text="approve\n")))
    mock.inject(forged, repo.address, "fail")
    mock.inject(mime.build(Outgoing(from_addr="x@example.org", to=("other@ses.test",), subject="o", text="t")),
                "other@ses.test", "pass")
    _wait_for(got, 2)
    items = repo.accept_webhook(*got[0]) + repo.accept_webhook(*got[1])
    assert len(items) == 1, "a notification for another recipient is not ours"
    parsed = mime.parse(items[0].raw_mime)
    assert parsed.auth_results[0] == f"{AUTHSERV_ID}; dmarc=fail header.from=example.org"
    assert not auth.sender_verified(parsed.auth_results, parsed.from_addr, AUTHSERV_ID)


def test_errors_quota_and_config(tmp_path) -> None:
    mock = MockAws()
    agent = _agent(mock, tmp_path)
    with pytest.raises(SesError) as exc:
        agent.send(Outgoing(from_addr="human@example.org", to=("repo@ses.test",), subject="s", text="t"))
    assert exc.value.code == "MessageRejected"
    mock.fail_next = [429, 503]
    _send(agent, "repo@ses.test", "retried")
    mock.fail_next = [429] * 4
    with pytest.raises(SesQuotaError):
        _send(agent, "repo@ses.test", "throttled")
    assert agent.capabilities().send_quota_per_day == 50_000
    mock.max24 = -1.0
    assert _agent(mock, tmp_path).capabilities().send_quota_per_day is None
    wrong = SesMailbox(REGION, AwsCredentials(ACCESS_KEY, "wrong"), "agent@ses.test", client=mock.client(),
                       store=tmp_path / "w", sleep=lambda s: None)
    with pytest.raises(SesError) as exc:
        _send(wrong, "repo@ses.test", "bad signature")
    assert exc.value.code == "SignatureDoesNotMatch"
    with pytest.raises(TransportError):
        registry.open_mailbox("ses", {"address": "a@b"})
    box = registry.open_mailbox("ses", {"region": "us-west-2", "address": "a@b.c", "access_key_id": "A",
                                        "secret_access_key": "S", "topic_arns": "arn:aws:sns:us-west-2:1:t",
                                        "store": str(tmp_path / "cfg"), "send_quota_per_day": "200"})
    assert isinstance(box, SesMailbox) and box.topic_arns == ("arn:aws:sns:us-west-2:1:t",)
    caps = box.capabilities()   # no network: the quota is configured
    assert caps.max_message_bytes == SNS_CONTENT_MAX and caps.send_quota_per_day == 200
