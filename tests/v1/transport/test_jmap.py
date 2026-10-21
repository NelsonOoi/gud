"""JMAP adapter behaviour beyond the shared conformance checks (against jmap_mock)."""

from __future__ import annotations

import pytest

from gud.transport import mime, registry
from gud.transport.base import MIME_OVERHEAD, Outgoing, TransportError
from gud.transport.jmap import (
    JmapError, JmapMailbox, JmapQuotaError, decode_cursor, encode_cursor, expand, iter_sse,
)

from .jmap_mock import MockJmap


def _box(mock: MockJmap, local: str, **kw) -> JmapMailbox:
    acc = mock.account(local)
    kw.setdefault("sleep", lambda s: None)
    return JmapMailbox(mock.session_url(), acc.token, acc.address, client=mock.client(),
                       authserv_id=mock.authserv_id, ping=1, **kw)


def _send(box: JmapMailbox, to: str, subject: str, **kw) -> None:
    box.send(Outgoing(from_addr=box.address, to=(to,), subject=subject, text="x", **kw))


def test_cursor_codec_and_templates() -> None:
    c = encode_cursor("q", "st|a~b", 1792512362000, ("M1", "M2"), True)
    assert decode_cursor(c) == ("q", "st|a~b", 1792512362000, ("M1", "M2"), True)
    assert decode_cursor(encode_cursor("c", "7")) == ("c", "7", None, (), False)
    for bad in ("", "x|1|||0", "c||||0", "c|1|abc||0", "c|1|||2"):
        with pytest.raises(TransportError):
            decode_cursor(bad)
    assert expand("https://h/d/{accountId}/{blobId}/{name}?type={type}", accountId="u1", blobId="G1",
                  name="m.eml", type="message/rfc822") == "https://h/d/u1/G1/m.eml?type=message%2Frfc822"
    events = list(iter_sse(iter(["event: state", 'data: {"a":', "data: 1}", "", ": comment", "event: ping",
                                 "data: {}", ""])))
    assert events == [("state", '{"a":\n1}'), ("ping", "{}")]


def test_same_second_batch_resumes_from_every_item() -> None:
    """receivedAt has one-second resolution: ties are kept in server order and every item's
    cursor resumes exactly after it, in a fresh instance."""
    mock = MockJmap(clock=lambda: 1_792_512_000.0)  # every delivery in the same second
    repo, agent = _box(mock, "repo"), _box(mock, "agent")
    start = repo.head()
    for i in range(5):
        _send(agent, repo.address, f"m{i}")
    seen, cursor = [], start
    while True:
        items, cursor = _box(mock, "repo").list(since=cursor, limit=2)
        if not items:
            break
        seen += [mime.parse(i.raw_mime).subject for i in items]
        rest = _box(mock, "repo").list(since=items[0].cursor)[0]
        assert [r.provider_id for r in rest[:len(items) - 1]] == [i.provider_id for i in items[1:]]
    assert seen == [f"m{i}" for i in range(5)]
    assert repo.list(since=cursor) == ([], cursor)
    # From the start of the mailbox (Email/query walk) the same five, then changes mode.
    walk, end = repo.list(since=None, limit=3)
    assert [mime.parse(i.raw_mime).subject for i in walk] == ["m0", "m1", "m2"]
    more, end = repo.list(since=end)
    assert [mime.parse(i.raw_mime).subject for i in more] == ["m3", "m4"]
    assert decode_cursor(end)[0] == "c"


def test_cannot_calculate_changes_falls_back_to_a_walk() -> None:
    mock = MockJmap()
    repo, agent = _box(mock, "repo"), _box(mock, "agent")
    _send(agent, repo.address, "old")
    items, cursor = repo.list(since=repo.head())
    assert items == []
    first, cursor = repo.list(since=encode_cursor("c", "0"))
    assert [mime.parse(i.raw_mime).subject for i in first] == ["old"]
    _send(agent, repo.address, "new")
    mock.min_state = 10_000  # every state is now too old to calculate changes from
    again, cursor2 = repo.list(since=cursor)
    subjects = [mime.parse(i.raw_mime).subject for i in again]
    assert "new" in subjects and subjects.count("new") == 1  # at least once; "old" may repeat
    mock.min_state = 0
    assert repo.list(since=cursor2)[0] == []


def test_junk_is_read_and_own_sends_are_not_listed() -> None:
    mock = MockJmap()
    repo, agent = _box(mock, "repo"), _box(mock, "agent")
    start, agent_start = repo.head(), agent.head()
    _send(agent, repo.address, "to inbox")
    raw = mime.build(Outgoing(from_addr="spam@example.org", to=(repo.address,), subject="to junk", text="approve\n"))
    mock.deliver(raw, repo.address, verdict="fail", junk=True)
    items, _ = repo.list(since=start)
    assert [mime.parse(i.raw_mime).subject for i in items] == ["to inbox", "to junk"]
    assert mime.parse(items[1].raw_mime).auth_results[0] == "mx.jmap.test; dmarc=fail header.from=example.org"
    assert agent.list(since=agent_start)[0] == [], "the Sent copy of an Email/import is not delivered mail"


def test_raw_send_is_unchanged_and_duplicate_import_still_submits() -> None:
    mock = MockJmap()
    repo, agent = _box(mock, "repo"), _box(mock, "agent")
    start = repo.head()
    raw = mime.build(Outgoing(from_addr=agent.address, to=(repo.address,), subject="same bytes", text="x",
                              message_id="<fixed-1@jmap.test>"))
    first, second = agent.send(raw), agent.send(raw)   # second import is alreadyExists
    assert first.provider_id != second.provider_id and first.message_id == "<fixed-1@jmap.test>"
    items, _ = repo.list(since=start)
    assert len(items) == 2
    assert all(i.raw_mime.endswith(raw) for i in items), "the MTA only prepends; the message is byte-identical"


def test_forbidden_from_and_size_limits() -> None:
    mock = MockJmap(max_size_upload=2_000_000, max_size_attachments=1_000_000)
    agent = _box(mock, "agent")
    caps = agent.capabilities()
    assert caps.max_message_bytes == min(2_000_000, 1_000_000 * 78 // 57 + MIME_OVERHEAD)
    with pytest.raises(JmapError) as exc:
        agent.send(Outgoing(from_addr="someone@example.org", to=("x@jmap.test",), subject="s", text="t"))
    assert exc.value.type == "forbiddenFrom"
    big = mime.build(Outgoing(from_addr=agent.address, to=("x@jmap.test",), envelope=b"\0" * 1_500_000))
    uploads = len([r for r in mock.requests if "upload" in r[1]])
    with pytest.raises(JmapError) as exc:
        agent.send(big)
    assert exc.value.type == "tooLarge"
    assert len([r for r in mock.requests if "upload" in r[1]]) == uploads, "refused before uploading"
    assert _box(mock, "agent", max_message_bytes=3_000_000).capabilities().max_message_bytes == 3_000_000


def test_retries_and_quota() -> None:
    mock = MockJmap()
    repo, agent = _box(mock, "repo"), _box(mock, "agent")
    agent.session()
    mock.fail_next = [503, 429]
    _send(agent, repo.address, "retried")
    mock.fail_next = [429] * 4
    with pytest.raises(JmapQuotaError):
        _send(agent, repo.address, "throttled")
    bad = JmapMailbox(mock.session_url(), "wrong-token", repo.address, client=mock.client(), sleep=lambda s: None)
    with pytest.raises(JmapError):
        bad.list()


def test_from_config() -> None:
    mock = MockJmap()
    with pytest.raises(TransportError):
        registry.open_mailbox("jmap", {"address": "a@b"})
    with pytest.raises(TransportError):
        registry.open_mailbox("jmap", {"session_url": "https://x/s", "address": "a@b"})
    box = registry.open_mailbox("jmap", {"session_url": "https://x/s", "address": "a@b", "token": "t",
                                         "authserv_id": "mx.example", "received_time_resolution_ms": "1"})
    assert isinstance(box, JmapMailbox) and box.authserv_id == "mx.example"
    assert box._resolution == 1
    assert mock.accounts == {}
