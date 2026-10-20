"""Mirror auth v1 (§21) has one implementation: members (gud.wc.remote) and the host
(gud.host_v1) both use gud.events.dispatch. Cross-check the two ends against each other."""

from __future__ import annotations

import math

from gud.core import envelope as E
from gud.core.config import parse_members
from gud.events import dispatch
from gud import host_v1
from gud.wc import remote

from tests.v1.kit import Kit


class _Http:
    """Captures what HttpMirror sends."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes, dict]] = []

    def request(self, method, url, content=None, headers=None):
        self.calls.append((method, url, content or b"", dict(headers or {})))

        class R:
            status_code = 200
            content = b"{}"
            text = "{}"

            @staticmethod
            def json():
                return {}
        return R()


def test_one_implementation():
    for name in ("format_time", "request_bytes", "sign_request", "verify_request"):
        assert getattr(remote, name) is getattr(dispatch, name), name
        assert getattr(host_v1, name) is getattr(dispatch, name), name
    assert (remote.HANDLE_HEADER, remote.TIME_HEADER, remote.SIG_HEADER) == \
        (dispatch.AUTH_HANDLE, dispatch.AUTH_TIME, dispatch.AUTH_SIG)
    assert remote.MAX_SKEW == dispatch.MAX_SKEW_S


def test_member_requests_verify_at_the_host_with_a_path_prefix():
    kit = Kit()
    members = parse_members(kit.members_file())
    http = _Http()
    now = 1_760_000_000.75
    mirror = remote.HttpMirror("https://host.example/gud/", "@alice", kit.key("@alice"), http=http,
                               clock=lambda: now)
    mirror.log(3, 10)
    mirror.delivery({"sse": True})
    (m1, url1, b1, h1), (m2, url2, b2, h2) = http.calls
    assert url1 == "https://host.example/gud/api/v1/log?since=3&limit=10"
    assert h1["X-Gud-Time"] == str(math.floor(now))          # whole seconds (wc note 7)
    # the host sees the prefixed path and verifies with the same code
    assert host_v1.verify_request(members, m1, "/gud/api/v1/log?since=3&limit=10", b1, h1, now) == "@alice"
    assert host_v1.verify_request(members, m2, "/gud/api/v1/delivery", b2, h2, now) == "@alice"
    assert host_v1.verify_request(members, m1, "/api/v1/log?since=3&limit=10", b1, h1, now) is None
    assert host_v1.verify_request(members, m2, "/gud/api/v1/delivery", b2 + b" ", h2, now) is None
    # and a host-side signature is accepted by the member-side verifier
    h = dispatch.sign_request("@bob", kit.key("@bob"), "GET", "/api/v1/mail/x", b"", now)
    assert remote.verify_request(members, "GET", "/api/v1/mail/x", b"", h, now + 299) == "@bob"
    assert remote.verify_request(members, "GET", "/api/v1/mail/x", b"", h, now + 301) is None
    assert E.verify_bytes(kit.pub("@bob"), remote.request_bytes("GET", "/api/v1/mail/x", h["X-Gud-Time"]),
                          h["X-Gud-Sig"])
