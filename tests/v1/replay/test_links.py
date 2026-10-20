"""Genesis, intrinsic checks, signatures (no lead exception), race order, parking, orphans, expiry (§9, §12)."""


import pytest

from gud.core import envelope as E
from gud.core.ops import Patch
from gud.core.types import Record
from tests.v1.kit import Kit, events_of, link_of, status

W, G = 60_000, 30_000


def base(**kw):
    k = Kit(**kw)
    g = k.genesis({"app.py": "a = 1\n", "lib.py": "x = 1\ny = 2\n"})
    return k, g


def test_genesis_links_at_received_time():
    k, g = base()
    chain, events = k.run()
    assert [l.seq for l in chain.links] == [0]
    tip = chain.tip
    assert tip.state == g.id and tip.push == g.id and tip.linked_at == g.logged_at == tip.received_at
    assert tip.author == "@nelson" and tip.parent is None
    assert tip.tree_hash == g.tree.hash()
    assert events_of(events, "linked")[0].data["link"] == g.id


def test_genesis_needs_host_key_of_the_log():
    k = Kit()
    members = k.members_file().replace(k.pub("@host").encode(), k.pub("@other-host").encode())
    bad = k.genesis({".gud/members": members})
    chain, _ = k.run()
    assert chain.links == ()
    assert status(chain, bad) == "bad_sig"


def test_push_without_owners_links_immediately():
    k, g = base()
    p = k.push("@alice", g, {"new.py": "n = 1\n"})
    chain, _ = k.run()
    link = link_of(chain, p)
    assert link is not None and link.seq == 1 and link.linked_at == p.logged_at
    assert link.files == ("new.py",) and link.parent == g.id and link.carried_from is None


def test_malformed_push_is_dead():
    k, g = base()
    env = E.build("push", author="@alice", headers={"Gud-Parent": g.id, "Gud-Tree": "0" * 64},
                  records=(Record("text", (), b"x"),), sent=k.now // 1000, seen=k.seen())  # no ops
    msg = k.raw_envelope(E.sign(env, k.key("@alice")))
    chain, events = k.run()
    assert status(chain, msg) == "malformed"
    assert events_of(events, "push.rejected")[0].data["status"] == "malformed"


@pytest.mark.parametrize("role_handle", ["@alice", "@nelson", "@t1", "@carol"])
def test_forged_signature_from_any_role_is_rejected(role_handle):
    """No lead exception: every signed kind verifies against .gud/members (§9)."""
    k, g = base()
    p = k.push(role_handle, g, {"new.py": "n = 1\n"}, key=k.key("@mallory"))
    chain, events = k.run()
    assert status(chain, p) == "bad_sig"
    assert chain.tip.state == g.id


def test_forged_approve_and_test_do_not_count():
    k, g = base(test="command pytest\non-paths app.py\n")
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.approve("@nelson", p, key=k.key("@mallory"))
    k.test_push(p.id, p.tree, tester="@t1", key=k.key("@mallory"))
    chain, _ = k.run()
    prop = chain.push_status(p.id)
    assert prop.approvals == () and prop.status == "testing"


def test_non_member_push_is_bad_sig():
    k, g = base()
    p = k.push("@zed", g, {"new.py": "n\n"})
    chain, _ = k.run()
    assert status(chain, p) == "bad_sig"


def test_conflict_when_tree_hash_differs():
    k, g = base()
    p = k.push("@alice", g, {"new.py": "n = 1\n"}, tree_hash="1" * 64)
    chain, _ = k.run()
    assert status(chain, p) == "conflict"


def test_conflict_when_patch_does_not_apply():
    k, g = base()
    p = k.push("@alice", g, ops=[Patch("app.py", b"@@ -1 +1 @@\n-zzz\n+a = 2\n")], tree_hash="2" * 64)
    chain, _ = k.run()
    assert status(chain, p) == "conflict"


def test_bad_config_push_does_not_link():
    k, g = base()
    p = k.push("@alice", g, {".gud/test": "every-link maybe\n"})
    chain, _ = k.run()
    assert status(chain, p) == "bad_config"


def test_race_order_is_log_order():
    k, g = base()
    p1 = k.push("@alice", g, {"a1": "1\n"})
    p2 = k.push("@bob", g, {"b1": "1\n"})
    chain, _ = k.run()
    assert [l.push for l in chain.links[1:]] == [p1.id, p2.id]
    assert chain.links[2].state == p2.carried(p1)


def test_pending_first_candidate_holds_later_until_parked():
    k, g = base()
    slow = k.push("@bob", g, {"app.py": "a = 2\n"})          # asks @nelson: waits W + G
    fast = k.push("@alice", g, {"new.py": "n\n"})            # no askers
    chain, _ = k.run()
    assert chain.tip.state == g.id
    assert status(chain, fast) == "window" or chain.push_status(fast.id).status == "qualified"
    k.tick(W + G + 5)
    k.tick_entry()
    chain, _ = k.run()
    assert status(chain, slow) == "linked"
    assert status(chain, fast) == "linked"


def test_parked_candidate_stops_holding_back_others():
    k, g = base()
    blocked = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.object("@nelson", blocked, "please no")
    later = k.push("@alice", g, {"new.py": "n\n"})
    chain, _ = k.run()
    assert chain.tip.state == g.id
    chain, _ = k.advance(W + G + 10)
    assert status(chain, later) == "linked"
    assert status(chain, blocked) in ("objected", "parked")
    assert chain.push_status(blocked.id).status == "parked"


def test_orphan_becomes_native_when_parent_links():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})            # waits on @nelson
    child = k.push("@bob", p, {"app.py": "a = 3\n"})        # stacked on p
    chain, _ = k.run()
    assert status(chain, child) == "orphan"
    chain, _ = k.advance(W + G + 10)
    assert status(chain, p) == "linked" and status(chain, child) == "linked"
    assert link_of(chain, child).parent == p.id and link_of(chain, child).carried_from is None


def test_orphan_goes_stale_when_parent_dies():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    child = k.push("@bob", p, {"app.py": "a = 3\n"})
    k.veto("@nelson", p)
    chain, events = k.run()
    assert status(chain, p) == "vetoed"
    assert status(chain, child) == "stale"


def test_unknown_parent_is_stale():
    k, g = base()
    p = k.push("@alice", g, {"n": "1\n"})
    q = k.push("@alice", p, {"m": "1\n"}, log=False)
    r = k.push("@alice", q, {"o": "1\n"})
    chain, _ = k.run()
    assert status(chain, r) == "stale"


def test_push_expires():
    k, g = base(config="expire 2m\n")
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.object("@nelson", p, "no")
    chain, _ = k.advance(3 * 60_000)
    assert status(chain, p) == "expired"


def test_author_removed_before_carry_is_bad_sig():
    k, g = base()
    slow = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.object("@nelson", slow, "wait")
    members = [m for m in k.members if m[0] != "@bob"]
    rm = k.push("@nelson", g, {".gud/members": k.members_file(members)})
    k.approve("@dana", rm)
    chain, _ = k.advance(W + G + 10)
    assert status(chain, rm) == "linked"
    assert status(chain, slow) == "bad_sig"


def test_replay_time_capped_at_log_head():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    # A far-future local clock does not close windows the log has not reached.
    r = k.replayer(now=k.now + 10 * W)
    assert r.chain().tip.state == g.id
    assert r.chain().t == k.log.head.time


def test_missing_mail_caps_replay_time():
    k, g = base()
    p = k.push("@alice", g, {"n": "1\n"})
    q = k.push("@alice", p, {"m": "1\n"})
    emails = [e for e in k.emails if e.gud_id != p.id]
    from gud.core.replay import Replayer
    r = Replayer(k.store)
    delta = r.feed(emails, k.entries(), k.now)
    assert r.chain().tip.state == g.id
    assert delta.missing_mail == (p.id,)
    assert r.chain().t == p.logged_at - 1
    delta = r.feed([e for e in k.emails if e.gud_id == p.id], [], k.now)
    assert r.chain().tip.push == q.id
    assert [l.push for l in delta.new_links] == [p.id, q.id]


def test_unlogged_mail_is_invisible():
    k, g = base()
    p = k.push("@alice", g, {"n": "1\n"}, log=False)
    chain, _ = k.run()
    assert chain.tip.state == g.id and status(chain, p) == "unknown"


def _blob_push(k, g, data):
    import hashlib
    from gud.core.ops import Put
    k.store.put(data)
    return k.push("@alice", g, ops=[Put("big.bin", "100644", hashlib.sha256(data).hexdigest(), len(data), None)])


def test_push_waits_for_blobs_then_links_at_completion():
    k, g = base()
    data = bytes(range(256)) * 40
    p = _blob_push(k, g, data)
    k.blob(data, 4000, only=[0, 1])
    chain, _ = k.run()
    assert status(chain, p) == "pending_blobs"
    assert chain.push_status(p.id).pending_blobs == (__import__("hashlib").sha256(data).hexdigest(),)
    (last,) = k.blob(data, 4000, only=[2])
    chain, events = k.run()
    assert status(chain, p) == "linked" and link_of(chain, p).linked_at == last.logged_at
    assert k.replayer().reader().files()["big.bin"][1] == data


def test_blob_push_asks_owners_once_blobs_complete():
    import hashlib
    from gud.core.ops import Put
    k, g = base()
    data = bytes(range(256)) * 10
    k.store.put(data)
    p = k.push("@bob", g, ops=[Put("app.py", "100644", hashlib.sha256(data).hexdigest(), len(data), None)])
    chain, events = k.run()
    assert status(chain, p) == "pending_blobs" and not events_of(events, "review.requested")
    k.blob(data, 1000)
    chain, events = k.run()
    assert [e.data["owner"] for e in events_of(events, "review.requested")] == ["@nelson"]
    assert status(chain, p) == "window"


def test_blob_chunk_disagreeing_with_first_is_ignored():
    k, g = base()
    data = bytes(range(256)) * 40
    p = _blob_push(k, g, data)
    k.blob(data, 4000, only=[0])
    k.blob(data, 5000, only=[1])        # different chunk size: ignored
    chain, _ = k.run()
    assert status(chain, p) == "pending_blobs"


def test_duplicate_delivery_counts_once():
    k, g = base()
    p = k.push("@alice", g, {"n": "1\n"})
    from gud.core.replay import replay
    chain = replay(k.emails + [p.email, p.email], k.entries(), k.now, store=k.store)
    assert [l.push for l in chain.links] == [g.id, p.id]
