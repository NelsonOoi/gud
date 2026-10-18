"""Join requests, propose, welcome, and audit. No network."""

import time
from urllib.parse import quote

from starlette.testclient import TestClient

from gud import compose, sig
from gud.api import Ctx, create_app
from gud.compose import Identity
from gud.config import Config
from gud.joins import audit, join_body, join_requests, welcome_new_members
from gud.replay import replay
from gud.testkit import REPO, Pushed, World
from gud.types import Email, LinkState, Member, ProposalState
from gud.wire import parse_body, render_body

HOST = "http://gud.example:8787"


def _cfg(tmp_path) -> Config:
    return Config(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None, instabox_api_key=None,
        armature_api_key=None, openrouter_key=None, agent_model="test",
    )


def _pushed(email) -> Pushed:
    body = parse_body(email.body)
    return Pushed(email, body.get("Gud-State") or "", body.get("Gud-Tree") or "", {})


def _propose(client, message_id: str):
    return client.post("/api/joins/" + quote(message_id, safe="") + "/propose")


def _email(mid, body, at, frm, to=(REPO,), cc=(), verified=True):
    return Email(mid, frm, tuple(to), tuple(cc), "s", body, at, verified, None, (), None)


def _chain(emails, proposals=None, links=None):
    links = list(links or [])

    class _Chain:
        tip = links[-1] if links else None

    chain = _Chain()
    chain.emails = emails
    chain.proposals = proposals or {}
    chain.links = links
    chain.by_id = {link.message_id: link for link in links}
    return chain


def _link(mid, members, seq=0):
    return LinkState(
        message_id=mid, seq=seq, author="@lead", state="ab" * 32, tree_hash="cd" * 32,
        tree={}, blame={}, members=dict(members), received_at=1.0, linked_at=1.0,
    )


def test_join_body_round_trip():
    raw, public = sig.generate()
    ident = Identity("@x", "X@Their.Domain", raw)
    body = join_body(ident, "agent", "X@Their.Domain", public, sponsor="@nelson", reason="please")
    parsed = parse_body(body)
    assert parsed.kind == "join"
    assert parsed.get("Gud-Author") == "@x"
    assert parsed.get("Gud-Role") == "agent"
    assert parsed.get("Gud-Address") == "X@Their.Domain"
    assert parsed.get("Gud-Key") == public
    assert parsed.get("Gud-Sponsor") == "@nelson"
    assert parsed.get("Gud-Reason") == "please"
    assert parsed.text == "please"
    assert sig.verify(public, parsed.trailers, parsed.diff)
    assert sig.public_of(raw) == public
    assert compose.join_body(
        ident, "agent", "X@Their.Domain", public, sponsor="@nelson", reason="please",
    ) == body
    bare = parse_body(join_body(ident, "lead", "x@their.domain", public))
    assert bare.get("Gud-Sponsor") is None
    assert "Gud-Reason" not in bare.trailers
    assert sig.verify(public, bare.trailers, bare.diff)
    _other_raw, other = sig.generate()
    try:
        join_body(ident, "agent", "x@their.domain", other)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_invalid_join_is_not_valid():
    raw, public = sig.generate()
    ident = Identity("@x", "x@their.domain", raw)
    good = join_body(ident, "agent", "x@their.domain", public, reason="in")
    ok = _email("<ok@gud.local>", good, 1, "x@their.domain")
    mismatched = _email("<from@gud.local>", good, 2, "other@their.domain")
    quiet = _email("<quiet@gud.local>", good, 3, "x@their.domain", verified=False)
    _other_raw, other = sig.generate()
    trailers = {
        "Gud-Kind": "join", "Gud-Author": "@x", "Gud-Role": "agent",
        "Gud-Address": "x@their.domain", "Gud-Key": other,
    }
    trailers["Gud-Sig"] = sig.sign(raw, trailers, "")
    wrong_key = _email("<key@gud.local>", render_body("no", trailers), 4, "x@their.domain")
    tip = _link("<g@gud.local>", {"@lead": Member("@lead", "lead", "lead@gud.local", "ed25519:AA", None)})
    rows = {row["id"]: row for row in join_requests(_chain([ok, mismatched, quiet, wrong_key], links=[tip]))}
    assert rows["<ok@gud.local>"]["valid"] is True
    assert rows["<ok@gud.local>"]["status"] == "pending"
    assert rows["<ok@gud.local>"]["address"] == "x@their.domain"
    assert rows["<ok@gud.local>"]["push_id"] is None
    assert rows["<from@gud.local>"]["valid"] is False
    assert rows["<quiet@gud.local>"]["valid"] is False
    assert rows["<key@gud.local>"]["valid"] is False
    assert sig.verify(other, parse_body(wrong_key.body).trailers, "") is False


def test_join_status_rules():
    raw, public = sig.generate()
    ident = Identity("@new", "new@their.domain", raw)
    body = join_body(ident, "agent", "new@their.domain", public, reason="hire me")
    join = _email("<join@gud.local>", body, 10, "new@their.domain")
    open_push = _email("<open@gud.local>", render_body("add", {
        "Gud-Kind": "push", "Gud-Author": "@lead", "Gud-Join": join.message_id,
    }), 11, "lead@gud.local")
    dead_push = _email("<dead@gud.local>", render_body("add", {
        "Gud-Kind": "push", "Gud-Author": "@lead", "Gud-Join": join.message_id,
    }), 9, "lead@gud.local")
    base = {"@lead": Member("@lead", "lead", "lead@gud.local", "ed25519:AA", None)}
    tip = _link("<g@gud.local>", base)

    pending = join_requests(_chain([join], links=[tip]))
    assert pending[0]["status"] == "pending" and pending[0]["push_id"] is None

    proposed = join_requests(_chain(
        [join, open_push],
        proposals={open_push.message_id: ProposalState(
            open_push.message_id, "<g@gud.local>", "@lead", "add", 11, "needs_quorum",
        )},
        links=[tip],
    ))
    assert proposed[0]["status"] == "proposed" and proposed[0]["push_id"] == open_push.message_id

    rejected = join_requests(_chain(
        [join, dead_push],
        proposals={dead_push.message_id: ProposalState(
            dead_push.message_id, "<g@gud.local>", "@lead", "add", 9, "superseded",
        )},
        links=[tip],
    ))
    assert rejected[0]["status"] == "rejected" and rejected[0]["push_id"] == dead_push.message_id

    failed = join_requests(_chain(
        [join, dead_push],
        proposals={dead_push.message_id: ProposalState(
            dead_push.message_id, "<g@gud.local>", "@lead", "add", 9, "tests_failed",
        )},
        links=[tip],
    ))
    assert failed[0]["status"] == "rejected"

    both = join_requests(_chain(
        [join, dead_push, open_push],
        proposals={
            dead_push.message_id: ProposalState(
                dead_push.message_id, "<g@gud.local>", "@lead", "add", 9, "objected",
            ),
            open_push.message_id: ProposalState(
                open_push.message_id, "<g@gud.local>", "@lead", "add", 11, "testing",
            ),
        },
        links=[tip],
    ))
    assert both[0]["status"] == "proposed" and both[0]["push_id"] == open_push.message_id

    admitted = _link(open_push.message_id, {
        **base, "@new": Member("@new", "agent", "new@their.domain", public, "@nelson"),
    }, seq=1)
    member = join_requests(_chain([join, open_push], links=[tip, admitted]))
    assert member[0]["status"] == "member"
    assert member[0]["push_id"] == open_push.message_id
    assert [row["id"] for row in join_requests(_chain([join, _email(
        "<later@gud.local>", body, 20, "new@their.domain",
    )], links=[tip]))] == ["<later@gud.local>", "<join@gud.local>"]


def test_join_lands_member_can_push_and_welcome_is_once(tmp_path):
    world = World.create(tmp_path / "repo")
    world.genesis({"app.py": "x = 1\n"})
    cfg = _cfg(tmp_path)
    sig.save_private(cfg.key_path("@console"), world.ids["@console"].private_raw)
    ctx = Ctx(
        cfg, mailbox=world.mailbox, clock=time.time, auto_welcome=True, host_url=HOST,
        start_welcome_thread=False,
    )
    app = create_app(ctx)
    local = TestClient(app)
    remote = TestClient(app, client=("203.0.113.5", 1234))

    raw, public = sig.generate()
    sneaky = Identity("@sneaky", "sneaky@their.domain", raw)
    bad_body = join_body(sneaky, "agent", "sneaky@their.domain", public, reason="nope")
    bad = world.mailbox.send(
        from_addr="other@their.domain", to=REPO, subject="[gud] join @sneaky", body=bad_body,
    )
    listed = local.get("/api/joins").json()
    bad_row = next(row for row in listed if row["id"] == bad.message_id)
    assert bad_row["valid"] is False
    assert _propose(local, bad.message_id).status_code == 409

    alice_body = join_body(
        world.ids["@alice"], "agent", world.members["@alice"].address,
        world.members["@alice"].public_key, reason="again",
    )
    alice_join = world.mailbox.send(
        from_addr=world.ids["@alice"].address, to=REPO, subject="[gud] join @alice", body=alice_body,
    )
    alice_row = next(row for row in local.get("/api/joins").json() if row["id"] == alice_join.message_id)
    assert alice_row["valid"] is True and alice_row["status"] == "member"
    assert _propose(local, alice_join.message_id).status_code == 409

    alias_raw, alias_pub = sig.generate()
    alias = Identity("@alias", world.members["@alice"].address, alias_raw)
    alias_body = join_body(alias, "agent", world.members["@alice"].address, alias_pub, reason="same inbox")
    alias_join = world.mailbox.send(
        from_addr=alias.address, to=REPO, subject="[gud] join @alias", body=alias_body,
    )
    alias_row = next(row for row in local.get("/api/joins").json() if row["id"] == alias_join.message_id)
    assert alias_row["valid"] is True and alias_row["status"] == "pending"
    denied = _propose(local, alias_join.message_id)
    assert denied.status_code == 409

    out_raw, out_public = sig.generate()
    outsider = Identity("@outsider", "outsider@their.domain", out_raw)
    out_body = join_body(outsider, "agent", "outsider@their.domain", out_public, sponsor="@nelson", reason="let me in")
    join_email = world.mailbox.send(
        from_addr=outsider.address, to=REPO, subject="[gud] join @outsider", body=out_body,
    )
    rows = local.get("/api/joins").json()
    assert rows[0]["id"] == join_email.message_id
    row = rows[0]
    assert row["valid"] is True and row["status"] == "pending"
    assert row["handle"] == "@outsider" and row["role"] == "agent"
    assert row["address"] == "outsider@their.domain" and row["public_key"] == out_public
    assert row["sponsor"] == "@nelson" and row["reason"] == "let me in"
    assert remote.post("/api/joins/" + quote(join_email.message_id, safe="") + "/propose").status_code == 403
    assert _propose(local, "<missing@gud.local>").status_code == 404

    proposed = _propose(local, join_email.message_id)
    assert proposed.status_code == 200, proposed.text
    push_id = proposed.json()["push_id"]
    push = next(email for email in world.repo_mail() if email.message_id == push_id)
    push_body = parse_body(push.body)
    assert push_body.kind == "push"
    assert push_body.get("Gud-Join") == join_email.message_id
    assert push_body.get("Gud-Author") == "@console"
    assert sig.verify(world.members["@console"].public_key, push_body.trailers, push_body.diff)
    assert "outsider@their.domain" in push_body.diff
    after = next(item for item in local.get("/api/joins").json() if item["id"] == join_email.message_id)
    assert after["status"] == "proposed" and after["push_id"] == push_id
    assert _propose(local, join_email.message_id).status_code == 409

    add_push = _pushed(push)
    world.approve("@lead", add_push)
    world.approve("@alice", add_push)
    world.test(add_push)
    still = replay(world.repo_mail(), 60, time.time())
    assert still.tip is None or still.tip.message_id != push_id
    assert still.proposals[push_id].status == "needs_approval"
    approved = local.post("/api/joins/" + quote(join_email.message_id, safe="") + "/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["push_id"] == push_id
    human_replies = [
        email for email in world.repo_mail()
        if email.in_reply_to == push_id and email.from_addr == world.members["@nelson"].address
    ]
    assert human_replies and human_replies[-1].body.lstrip().lower().startswith("approve")
    landed = next(item for item in local.get("/api/joins").json() if item["id"] == join_email.message_id)
    assert landed["status"] == "member" and landed["push_id"] == push_id
    chain = replay(world.repo_mail(), 60, time.time())
    assert chain.tip is not None and chain.tip.message_id == push_id, (
        f"join did not land; proposals="
        f"{[(p.subject, p.status, p.reason, p.quorum_have, p.quorum_needed) for p in chain.proposals.values()]}"
    )
    assert chain.tip.members["@outsider"].public_key == out_public
    assert chain.tip.members["@outsider"].address == "outsider@their.domain"

    welcomes = [email for email in world.repo_mail() if email.subject.startswith("[gud] welcome")]
    assert len(welcomes) == 1
    welcome = welcomes[0]
    welcome_body = parse_body(welcome.body)
    assert welcome.to == ("outsider@their.domain",)
    assert welcome.cc == (REPO,)
    assert welcome_body.kind == "notify"
    assert welcome_body.get("Gud-Target") == push_id
    assert HOST in welcome.body
    assert push_id in welcome.body
    assert chain.tip.state in welcome.body
    assert chain.tip.tree_hash in welcome.body
    assert f"seq: {chain.tip.seq}" in welcome.body
    assert "checkpoint" in welcome.body.lower()
    local.get("/api/joins")
    welcome_new_members(replay(world.repo_mail(), 60, time.time()), world.mailbox, world.ids["@console"], HOST, set())
    assert len([email for email in world.repo_mail() if email.subject.startswith("[gud] welcome")]) == 1

    tree = dict(chain.tip.tree)
    tree["out.py"] = "out = 1\n"
    body, _state, _tree = compose.push_body(
        outsider, parent=chain.tip.message_id, parent_state=chain.tip.state,
        parent_tree=chain.tip.tree, new_tree=tree, intent="outsider lands",
    )
    pushed = compose.send(
        world.mailbox, outsider, chain.tip.members, "[gud] outsider", body, in_reply_to=chain.tip.message_id,
    )
    world.test(_pushed(pushed))
    chain = replay(world.repo_mail(), 60, time.time())
    assert chain.tip is not None and chain.tip.message_id == pushed.message_id, (
        f"outsider did not land; proposals="
        f"{[(p.subject, p.status, p.reason) for p in chain.proposals.values()]}"
    )
    assert "out.py" in chain.tip.tree


def test_audit_detects_missing_altered_and_retimed():
    def gud(text):
        return f"{text}\n\nGud-Kind: push\nGud-Author: @m\n"

    own = [
        _email("<missing@gud.local>", gud("one"), 10, "m@their.domain"),
        _email("<altered@gud.local>", gud("two"), 20, "m@their.domain"),
        _email("<retimed@gud.local>", gud("three"), 30, "m@their.domain"),
        _email("<both@gud.local>", gud("four"), 40, "m@their.domain"),
        _email("<ok@gud.local>", gud("five"), 50, "m@their.domain"),
        _email("<edge@gud.local>", gud("six"), 60, "m@their.domain"),
        _email("<human@gud.local>", "just a reply\n", 70, "m@their.domain"),
        _email("<elsewhere@gud.local>", gud("seven"), 80, "m@their.domain", to=("other@their.domain",)),
        _email("<cc@gud.local>", gud("eight"), 90, "m@their.domain", to=("m@their.domain",), cc=(REPO,)),
    ]
    mirror = [
        _email("<altered@gud.local>", gud("TWO"), 20, "m@their.domain"),
        _email("<retimed@gud.local>", gud("three"), 30 + 121, "m@their.domain"),
        _email("<both@gud.local>", gud("FOUR"), 40 + 200, "m@their.domain"),
        _email("<ok@gud.local>", gud("five"), 50 + 10, "m@their.domain"),
        _email("<edge@gud.local>", gud("six"), 60 + 120, "m@their.domain"),
        _email("<human@gud.local>", "just a reply\n", 70, "m@their.domain"),
    ]
    report = audit(mirror, own, "Repo@Gud.Local", 120)
    assert [row["message_id"] for row in report["missing"]] == ["<missing@gud.local>", "<cc@gud.local>"]
    assert [row["message_id"] for row in report["altered"]] == ["<altered@gud.local>", "<both@gud.local>"]
    assert [row["message_id"] for row in report["retimed"]] == ["<retimed@gud.local>", "<both@gud.local>"]
    assert report["retimed"][0]["mirror_received_at"] == 151
    assert "<ok@gud.local>" not in {row["message_id"] for row in report["missing"] + report["altered"] + report["retimed"]}
    assert "<edge@gud.local>" not in {row["message_id"] for row in report["retimed"]}
    assert "<human@gud.local>" not in {row["message_id"] for row in report["missing"]}
    assert "<elsewhere@gud.local>" not in {row["message_id"] for row in report["missing"]}
    clean = audit(own, own, REPO, 120)
    assert clean == {"missing": [], "altered": [], "retimed": []}
