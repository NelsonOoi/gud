"""Determinism (§18): delivery order, batch splits, restarts and checkpoints never change the chain,
tips, blame or events; Replayer == replay()."""

import hashlib
import random

import pytest

from gud.core.objects import DiskStore
from gud.core.ops import Put
from gud.core.replay import Replayer, Snapshot, replay, replay_events
from tests.v1.kit import Kit

W, G = 60_000, 30_000


def rich_scenario() -> Kit:
    """Pushes, carries, votes, objections, evidence, an escalation, tasks, claims, blobs and a red tip."""
    k = Kit(config="escalate 5m\nlease 10m\n", test="command pytest -q\non-paths src/**\n")
    g = k.genesis({"app.py": "a = 1\nb = 1\n", "src/core.py": "x = 1\n", "web/a.css": "a\n", "web/b.css": "b\n"},
                  author="@alice")
    task = k.task("@nelson", "build it", accept=["tests/test_it.py"])
    k.claim("@alice", task, ["web/**"])
    sub = k.subtask("@alice", task, "styles", files=["web/**"])
    k.claim("@bob", sub, ["web/a.css"])
    k.claim("@carol", sub, ["web/**"])                        # lost
    p1 = k.push("@bob", g, {"web/a.css": "aa\n"}, task=sub.id)
    p2 = k.push("@carol", g, {"web/b.css": "bb\n"})            # carried onto p1
    p3 = k.push("@carol", g, {"app.py": "a = 2\nb = 1\n"})     # asks @alice
    o = k.object("@alice", p3, "use a = 3")
    k.comment("@carol", o, "ok")
    r3 = k.push("@carol", g, {"app.py": "a = 3\nb = 1\n"}, revises=p3)
    k.hold("@alice", r3)
    p4 = k.push("@alice", g, {"src/core.py": "x = 2\n"})       # on-paths: needs a test
    k.tick(5_000)
    big = hashlib.sha256(b"seed").digest() * 300
    k.store.put(big)
    blob_push = k.push("@bob", g, ops=[Put("assets/big.bin", "100644", hashlib.sha256(big).hexdigest(),
                                           len(big), None)])
    k.blob(big, 4096)
    k.tick(W + G + 1000)
    k.tick_entry()
    k.approve("@nelson", task)
    k.unsigned("@nelson", "approve", reply_to=r3)
    k.tick(W + 10_000)
    k.tick_entry()
    r = k.replayer()
    cand = r.chain().push_status(p4.id)                        # p4 has been carried onto r3's link
    k.test_push(p4.id, r.reader().candidate_tree(cand.id))
    k.tick(1_000)
    k.tick_entry()
    tip = k.replayer().chain().tip
    k.test_tip(tip.state, k.tree(tip.state), "fail")
    k.tick(2 * W)
    k.tick_entry()
    late = k.push("@carol", g, {"late.txt": "l\n"})
    k.comment("@carol", task, "status?", task=task.id)
    k.tick(3 * W)
    k.tick_entry()
    return k


@pytest.fixture(scope="module")
def scenario():
    k = rich_scenario()
    full = Replayer(k.store)
    delta = full.feed(k.emails, k.entries(), k.now)
    return k, full, delta


def fingerprint(r: Replayer):
    chain = r.chain()
    reader = r.reader()
    blame = {p: reader.blame(p) for p in sorted(reader.files())}
    return chain, r.snapshot().hash(), blame


def test_scenario_exercises_the_engine(scenario):
    k, full, delta = scenario
    chain = full.chain()
    types = {e.type for e in delta.events}
    assert len(chain.links) >= 6
    for t in ("linked", "carried", "objected", "commented", "claim.won", "claim.lost", "subtask.posted",
              "test.passed", "test.failed", "tip.red", "fix.opened", "held", "review.requested"):
        assert t in types, t
    assert chain.red.red


def test_shuffled_delivery_gives_identical_results(scenario):
    k, full, delta = scenario
    ref = fingerprint(full)
    for seed in range(5):
        emails = list(k.emails)
        random.Random(seed).shuffle(emails)
        r = Replayer(k.store)
        d = r.feed(emails, k.entries(), k.now)
        assert fingerprint(r) == ref
        assert [e.id for e in d.events] == [e.id for e in delta.events]
        assert d.events == delta.events


def test_replay_function_equals_replayer(scenario):
    k, full, delta = scenario
    assert replay(k.emails, k.entries(), k.now, store=k.store) == full.chain()
    assert replay_events(k.emails, k.entries(), k.now, store=k.store) == delta.events


@pytest.mark.parametrize("seed", range(6))
def test_random_batch_splits_equal_one_full_replay(scenario, seed):
    k, full, delta = scenario
    rng = random.Random(seed)
    entries = k.entries()
    emails = list(k.emails)
    rng.shuffle(emails)
    r = Replayer(k.store)
    events = []
    e_i = m_i = 0
    while e_i < len(entries) or m_i < len(emails):
        e_n = rng.randint(0, 4)
        m_n = rng.randint(0, 5)
        batch_entries = entries[e_i:e_i + e_n]
        batch_mail = emails[m_i:m_i + m_n]
        e_i += e_n
        m_i += m_n
        now = (batch_entries[-1].time if batch_entries else entries[max(e_i - 1, 0)].time) + rng.randint(-50, 5000)
        d = r.feed(batch_mail, batch_entries, now)
        events.extend(d.events)
        if rng.random() < 0.3:
            events.extend(r.advance(now + rng.randint(0, 100_000)).events)
    events.extend(r.feed([], entries, k.now).events)
    assert fingerprint(r) == fingerprint(full)
    assert sorted(e.id for e in events) == sorted(e.id for e in delta.events)
    assert len(events) == len(delta.events)


def test_restart_from_persisted_state_equals_continuous_run(scenario, tmp_path):
    k, full, delta = scenario
    entries = k.entries()
    store = DiskStore(tmp_path / "objects")
    for key in list(k.store._data):
        store.put(k.store.get(key))
    events = []
    cuts = [len(entries) // 4, len(entries) // 2, (3 * len(entries)) // 4, len(entries)]
    start = 0
    r = Replayer(store, state_dir=tmp_path / "state")
    for cut in cuts:
        d = r.feed(k.emails, entries[start:cut], entries[cut - 1].time)
        events.extend(d.events)
        r.save()
        r = Replayer.load(tmp_path / "state", store)
        start = cut
    events.extend(r.feed([], [], k.now).events)
    assert r.chain() == full.chain()
    assert r.snapshot().hash() == full.snapshot().hash()
    assert sorted(e.id for e in events) == sorted(e.id for e in delta.events)


def test_checkpoint_snapshot_resume(scenario):
    k, full, delta = scenario
    entries = k.entries()
    n = len(entries) // 2
    first = Replayer(k.store)
    first.feed(k.emails, entries[:n + 1], entries[n].time)
    snap = first.snapshot()
    data = snap.to_bytes()
    assert Snapshot.from_bytes(data).hash() == snap.hash()
    # Another member computing the same position gets the same snapshot hash.
    other = Replayer(k.store)
    other.feed(list(reversed(k.emails)), entries[:n + 1], entries[n].time)
    assert other.snapshot().hash() == snap.hash()
    later = [e for e in k.emails if e.gud_id not in {gid for _, gid, _ in first.log().mail_ids(0)}
             or e.gud_id not in snap.state["seen"]]
    resumed = Replayer.from_checkpoint(data, k.store)
    resumed.feed(later, entries, k.now)
    assert resumed.chain() == full.chain()
    assert resumed.snapshot().hash() == full.snapshot().hash()
    chain = replay(later, entries, k.now, store=k.store, checkpoint=data)
    assert chain == full.chain()


def test_checkpoint_messages_validity():
    k = Kit()
    g = k.genesis({"a": "1\n"})
    r = k.replayer()
    snap = r.snapshot()
    voters = ["@nelson", "@alice", "@bob", "@carol", "@dana"]
    for v in voters[:2]:
        k.checkpoint(v, snap.log_head, g.id, g.tree.hash(), snap.hash())
    r = k.replayer()
    assert r.valid_checkpoints() == []
    k.checkpoint(voters[2], snap.log_head, g.id, g.tree.hash(), snap.hash())
    k.checkpoint("@t1", snap.log_head, g.id, g.tree.hash(), snap.hash())      # testers do not count
    r = k.replayer()
    (valid,) = r.valid_checkpoints()
    assert valid["snapshot"] == snap.hash() and valid["signers"] == ["@alice", "@bob", "@nelson"]


def test_delta_reports_changes(scenario):
    k, full, delta = scenario
    assert [l.seq for l in delta.new_links] == list(range(len(full.chain().links)))
    assert delta.red is not None and delta.red.red
    assert delta.changed_tasks and delta.changed_claims
    linked = [p for p in delta.changed_proposals if p.status == "qualified"]
    assert {p.push for p in linked} >= {l.push for l in full.chain().links[1:]}
    assert delta.events == tuple(sorted(delta.events, key=lambda e: (e.at, __import__("gud.core.types", fromlist=["x"]).EVENT_TYPES.index(e.type), e.id)))
