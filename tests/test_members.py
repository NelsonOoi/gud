import base64

import pytest

from gud import sig
from gud.config import Config
from gud.keys import create_identity, human_identity, load_identity, rotate
from gud.memberfile import MEMBERS, by_address, format_members, members_of, sending_address
from gud.members import (
    membership_history, pending_membership, propose_add, propose_remove, welcome, with_member,
    without_member,
)
from gud.notify import EmailNotifier, Notifier, notify_requester
from gud.testkit import REPO, World, parsed
from gud.types import LinkState, Member, ProposalState
from gud.wire import parse_body, short


def _cfg(tmp_path) -> Config:
    return Config(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None, instabox_api_key=None,
        armature_api_key=None, openrouter_key=None, agent_model="test",
    )


def _member(handle, role="agent", address=None, key="ed25519:AA", sponsor="@nelson") -> Member:
    return Member(handle, role, address or f"{handle.lstrip('@')}@gud.local", key, sponsor)


def _tip(pushed, seq=0) -> LinkState:
    return LinkState(
        message_id=pushed.id, seq=seq, author="@lead", state=pushed.state, tree_hash=pushed.tree_hash,
        tree=pushed.tree, blame={}, members=members_of(pushed.tree), received_at=pushed.email.received_at,
        linked_at=pushed.email.received_at, subject=pushed.email.subject,
    )


def _link(mid, seq, author, members, at, received=None) -> LinkState:
    return LinkState(
        message_id=mid, seq=seq, author=author, state=f"{seq:064x}", tree_hash=f"{seq + 1:064x}",
        tree={MEMBERS: format_members(members)}, blame={}, members=dict(members),
        received_at=received if received is not None else at - 5, linked_at=at, subject=mid,
    )


def test_off_domain_human_is_the_handle_on_the_agent_domain():
    members = {
        "@lead": _member("@lead", role="lead"),
        "@nelson": Member("@nelson", "human", "nelsonwmooi@gmail.com", None, None),
    }
    assert by_address(members, "nelsonwmooi@gmail.com").handle == "@nelson"
    assert by_address(members, "Nelson@gud.local").handle == "@nelson"
    assert by_address(members, "nelson@gmail.com") is None
    assert by_address(members, "other@gud.local") is None
    assert sending_address(members["@nelson"], "gud.local") == "nelson@gud.local"
    assert sending_address(members["@lead"], "gud.local") == "lead@gud.local"

    on_domain = {
        **members,
        "@nelson": Member("@nelson", "human", "pat@gud.local", None, None),
    }
    assert by_address(on_domain, "nelson@gud.local") is None
    assert by_address(on_domain, "pat@gud.local").handle == "@nelson"
    assert sending_address(on_domain["@nelson"], "gud.local") == "pat@gud.local"


def test_with_member_round_trip_header_and_duplicates():
    original = {
        "@a": _member("@a", key="ed25519:AA"),
        "@b": _member("@b", role="lead", key="ed25519:BB"),
    }
    tree = {MEMBERS: format_members(original), "app.py": "x = 1\n"}
    before = tree[MEMBERS]
    extra = Member("@c", "agent", "C@Gud.Local", "ed25519:CC", "@nelson")
    added = with_member(tree, extra)
    assert tree[MEMBERS] == before
    assert added["app.py"] == "x = 1\n"
    assert added[MEMBERS].startswith("# handle role address public_key sponsor")
    got = members_of(added)
    assert set(got) == {"@a", "@b", "@c"}
    assert got["@c"].address == "c@gud.local"
    assert got["@c"].role == "agent"
    assert added[MEMBERS] == format_members(got)

    removed = without_member(added, "c")
    assert set(members_of(removed)) == {"@a", "@b"}
    assert removed[MEMBERS] == format_members(members_of(removed))
    assert added[MEMBERS] != removed[MEMBERS]

    custom = "# roster\n# handle role address public_key sponsor\n@a agent a@gud.local ed25519:AA @nelson\n"
    kept = with_member({MEMBERS: custom}, _member("@b", key="ed25519:BB"))
    assert kept[MEMBERS].startswith("# roster\n")
    assert "# handle role address public_key sponsor" in kept[MEMBERS]
    assert "@b agent b@gud.local ed25519:BB @nelson" in kept[MEMBERS]
    assert members_of(kept)["@a"].address == "a@gud.local"

    with pytest.raises(ValueError, match="handle"):
        with_member(added, Member("@c", "agent", "other@gud.local", "ed25519:DD", None))
    with pytest.raises(ValueError, match="address"):
        with_member(tree, Member("@z", "agent", "A@gud.local", "ed25519:DD", None))
    with pytest.raises(ValueError, match="unknown"):
        without_member(tree, "@missing")


def test_keys_perms_no_overwrite_and_not_in_tree_or_mailbox(tmp_path):
    cfg = _cfg(tmp_path)
    w = World.create(tmp_path / "repo")
    genesis = w.genesis({"app.py": "x = 1\n"})
    ident, public = create_identity(cfg, "alice")
    path = cfg.key_path("@alice")
    assert path == cfg.home / "keys" / "alice.key"
    assert path.stat().st_mode & 0o777 == 0o600
    assert ident.handle == "@alice"
    assert ident.address == "alice@gud.local"
    assert sig.public_of(ident.private_raw) == public
    secret = path.read_text().strip()
    assert secret == base64.b64encode(ident.private_raw).decode()

    loaded = load_identity(cfg, "@alice")
    assert loaded.private_raw == ident.private_raw
    overridden = load_identity(cfg, "alice", address="alice@other.test")
    assert overridden.address == "alice@other.test"
    assert overridden.private_raw == ident.private_raw

    text = path.read_text()
    with pytest.raises(FileExistsError, match="overwrite=True"):
        create_identity(cfg, "@alice")
    assert path.read_text() == text

    replaced, public2 = create_identity(cfg, "@alice", overwrite=True)
    assert public2 != public
    assert path.stat().st_mode & 0o777 == 0o600
    rotated, public3 = rotate(cfg, "alice")
    assert public3 != public2
    assert load_identity(cfg, "@alice").private_raw == rotated.private_raw
    assert sig.public_of(rotated.private_raw) == public3

    human = human_identity("nelson", "Nelson@Example.com")
    assert human.handle == "@nelson" and human.private_raw is None
    assert human.address == "Nelson@Example.com"
    assert not (cfg.home / "keys" / "nelson.key").exists()

    secret = path.read_text().strip()
    roster = with_member({}, Member("@alice", "agent", ident.address, public3, "@nelson"))
    assert secret not in roster[MEMBERS]
    assert secret not in (tmp_path / "repo" / "mailbox.jsonl").read_text()
    assert secret not in genesis.email.body
    assert path.parent == cfg.keys_dir


def test_propose_add_and_remove_shape(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "x = 1\n"})
    tip = _tip(g)
    _raw, public = sig.generate()
    dave = Member("@dave", "agent", "dave@gud.local", public, "@nelson")
    before = len(w.repo_mail())
    email = propose_add(w.mailbox, w.ids["@lead"], tip, dave, reason="because we need them")
    body = parsed(email)
    assert email.subject == "[gud] members: add @dave"
    assert email.to == (REPO,)
    assert "dave@gud.local" not in email.cc
    assert "alice@gud.local" in email.cc
    assert "carol@gud.local" in email.cc
    assert "tester@gud.local" in email.cc
    assert "nelson@gud.local" not in email.cc
    assert email.in_reply_to == g.id
    assert body.kind == "push"
    assert body.get("Gud-Parent") == g.id
    assert body.get("Gud-Author") == "@lead"
    assert "because we need them" in body.text
    assert "sponsored by @nelson" in body.text
    assert "@dave" in body.text
    assert any(line.startswith("+") and "dave@gud.local" in line for line in body.diff.splitlines())
    assert "diff --git a/.gud/members b/.gud/members" in body.diff
    assert sig.verify(w.members["@lead"].public_key, body.trailers, body.diff)
    assert base64.b64encode(_raw).decode() not in email.body

    with pytest.raises(ValueError, match="handle"):
        propose_add(w.mailbox, w.ids["@lead"], tip, Member("@alice", "agent", "new@gud.local", public, None))
    assert len(w.repo_mail()) == before + 1

    removal = propose_remove(w.mailbox, w.ids["@lead"], tip, "carol", reason="rotated out")
    removed = parsed(removal)
    assert removal.subject == "[gud] members: remove @carol"
    assert "rotated out" in removed.text
    assert "carol@gud.local" in removal.cc
    assert any(line.startswith("-") and "@carol" in line and "carol@gud.local" in line
               for line in removed.diff.splitlines())
    assert sig.verify(w.members["@lead"].public_key, removed.trailers, removed.diff)

    with pytest.raises(ValueError, match="unknown"):
        propose_remove(w.mailbox, w.ids["@lead"], tip, "@nobody")


def test_welcome_and_notify_email_shape(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "x = 1\n"})
    _raw, public = sig.generate()
    dave = Member("@dave", "agent", "dave@gud.local", public, "@nelson")
    tree = with_member(g.tree, dave)
    tip = LinkState(
        message_id="<landed@gud.local>", seq=2, author="@lead", state="ab" * 32, tree_hash="cd" * 32,
        tree=tree, blame={}, members=members_of(tree), received_at=10, linked_at=12, subject="add dave",
    )
    welcome_email = welcome(w.mailbox, w.ids["@lead"], dave, tip)
    welcome_body = parse_body(welcome_email.body)
    assert welcome_email.subject == "[gud] welcome @dave"
    assert welcome_email.to == ("dave@gud.local",)
    assert welcome_email.cc == (REPO,)
    assert welcome_body.kind == "notify"
    assert welcome_body.get("Gud-Target") == tip.message_id
    assert welcome_body.get("Gud-Author") == "@lead"
    assert "Gud-Sig" not in welcome_body.trailers
    assert tip.message_id in welcome_email.body
    assert tip.state in welcome_email.body
    assert tip.tree_hash in welcome_email.body
    assert "seq: 2" in welcome_email.body
    assert "@alice" in welcome_email.body and "@dave" in welcome_email.body
    assert "genesis" in welcome_email.body.lower()
    assert "replay" in welcome_email.body.lower()

    notifier = EmailNotifier(w.mailbox, w.ids["@console"])
    assert isinstance(notifier, Notifier)
    done = notifier.notify_requester(
        w.members["@nelson"], "T7", "Ship the parser", _tip(g, seq=4), "parser landed", [g.id, "https://gud.local/T7"],
    )
    direct = notify_requester(
        w.mailbox, w.ids["@console"], w.members["@nelson"], "T7", "Ship the parser", _tip(g, seq=4),
        "parser landed", [g.id],
    )
    for email in (done, direct):
        body = parse_body(email.body)
        assert email.subject == "[gud] T7 done: Ship the parser"
        assert email.to == ("nelson@gud.local",)
        assert email.cc == (REPO,)
        assert email.in_reply_to == g.id
        assert body.kind == "notify"
        assert body.get("Gud-Task") == "T7"
        assert body.get("Gud-Target") == g.id
        assert body.get("Gud-Author") == "@console"
        assert "Gud-Sig" not in body.trailers
        assert short(g.state) in email.body
        assert g.id in email.body
        assert "parser landed" in email.body


def test_membership_history_and_pending(tmp_path):
    alice = _member("@alice")
    bob = _member("@bob", role="agent", key="ed25519:BB")
    carol = _member("@carol", key="ed25519:CC")
    first = {"@alice": alice, "@bob": bob}
    second = {**first, "@carol": carol}
    third = {
        "@alice": _member("@alice", role="lead", key="ed25519:AA"),
        "@carol": carol,
    }
    links = [
        _link("<g@gud.local>", 0, "@lead", first, at=100, received=90),
        _link("<same@gud.local>", 1, "@alice", dict(first), at=200),
        _link("<add@gud.local>", 2, "@alice", second, at=300),
        _link("<edit@gud.local>", 3, "@lead", third, at=400),
    ]
    history = membership_history(links)
    assert [row["link"] for row in history] == ["<g@gud.local>", "<add@gud.local>", "<edit@gud.local>"]
    assert history[0]["added"] == ["@alice", "@bob"]
    assert history[0]["removed"] == [] and history[0]["changed"] == []
    assert history[0]["seq"] == 0 and history[0]["author"] == "@lead" and history[0]["at"] == 100
    assert history[1]["added"] == ["@carol"] and history[1]["at"] == 300
    assert history[2]["removed"] == ["@bob"] and history[2]["changed"] == ["@alice"]
    assert history[2]["added"] == [] and history[2]["at"] == 400

    w = World.create(tmp_path)
    g = w.genesis({"app.py": "x = 1\n"})
    edit = w.edit("@alice", g, "app.py", "x = 2\n", intent="touch app")
    _raw, public = sig.generate()
    add = propose_add(w.mailbox, w.ids["@lead"], _tip(g), Member("@dave", "agent", "dave@gud.local", public, "@nelson"))
    proposals = {}
    for email, status in ((edit.email, "window"), (add, "needs_quorum")):
        proposals[email.message_id] = ProposalState(
            message_id=email.message_id, parent=g.id, author="@lead", subject=email.subject,
            received_at=email.received_at, status=status, protected_rule="quorum" if email is add else None,
            quorum_needed=3 if email is add else None, quorum_have=1 if email is add else None,
        )

    class _Chain:
        pass

    chain = _Chain()
    chain.proposals = proposals
    chain.emails = w.repo_mail()
    pending = pending_membership(chain)
    assert [p.message_id for p in pending] == [add.message_id]
    assert pending[0].quorum_needed == 3 and pending[0].quorum_have == 1
    assert pending[0].status == "needs_quorum"
