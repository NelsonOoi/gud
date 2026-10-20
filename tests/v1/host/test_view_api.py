"""The UI view API of the v1 host (`gud.host_view`, `/api/v1/view/*`) against a real host replaying a
scenario with a negotiation that escalates, a carried push, a red tip with its fix task, an
evidence objection, overlapping claims and a join request (`tests/v1/host/view_world.py`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gud.core import envelope as E
from gud.events.dispatch import parse_sse
from gud.host_view import EventFeed, load_signers
from gud.host_v1 import sign_request
from tests.v1.events.harness import World
from tests.v1.host.view_world import EVIDENCE, G, W, Story, build

LOOP = frozenset({"testclient"})
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def story(tmp_path_factory) -> Story:
    s = build(tmp_path_factory.mktemp("view"))
    yield s
    s.w.close()


@pytest.fixture(scope="module")
def c(story) -> TestClient:
    return TestClient(story.w.host.app(loopback_hosts=LOOP, ui_dist=False))


def get(c: TestClient, path: str, code: int = 200) -> dict:
    r = c.get(path)
    assert r.status_code == code, (path, r.status_code, r.text[:500])
    return r.json()


def by_push(rows: list[dict], push: str) -> dict:
    return next(r for r in rows if r["push"] == push)


# ---------------------------------------------------------------- state and chain

def test_state(c, story):
    s = get(c, "/api/v1/view/state")
    chain = story.w.chain()
    assert s["repo"] == "repo@example.org" and s["transport"] == "fake"
    assert s["tip"]["seq"] == 3 and s["tip"]["state"] == chain.tip.state
    assert s["log_head"]["seq"] == len(story.w.intake.log) - 1 and s["processed"]["seq"] == s["log_head"]["seq"]
    assert s["t"] == chain.t and s["config"]["window"] == W and s["config"]["grace"] == G
    assert s["test_policy"]["command"] is None
    red = s["red"]
    assert red["red"] and red["red_link_seq"] == 2 and red["last_green_seq"] == 1
    assert red["first_bad_seq"] == 2 and red["first_bad_author"] == "@alice"
    fix = red["fix_task"]
    assert fix["alias"] == "T3" and fix["offered_to"] == "@alice" and fix["status"] == "open"
    assert fix["offer_until"] == red["red_since"] + s["config"]["fix_offer"]
    assert s["counts"] == {"links": 4, "proposals_live": 3, "tasks_open": 3, "claims_active": 2,
                           "escalations_open": 1}
    assert s["ui"] == {"signers": [], "human": None, "console": None, "actions": False}


def test_chain_links(c, story):
    body = get(c, "/api/v1/view/chain")
    links = body["links"]
    assert [l["seq"] for l in links] == [3, 2, 1, 0] and body["total"] == 4
    genesis, carol, docs, bob = links[3], links[2], links[1], links[0]
    assert genesis["author"] == "@alice" and genesis["parent"] is None and genesis["verdict"] == "pass"
    assert "logo.png" in genesis["diffstat"]["binary"]
    assert carol["push"] == story.p_carol and carol["task_alias"] == "T2" and carol["verdict"] == "pass"
    assert carol["red_role"] == "last_green" and carol["carried_from"] is None
    # alice's push named genesis as its parent and was carried onto #1 (§14.4)
    assert docs["push"] == story.p_docs and docs["carried_from"] == story.genesis and docs["carried_from_seq"] == 0
    assert docs["state"] != docs["push"] and docs["parent"] == carol["state"]
    assert docs["verdict"] == "fail" and docs["red_role"] == "first_bad"
    # docs.md +2, notes.txt moved (-1 +1); the mode change and the binary count no lines
    assert (docs["diffstat"]["added"], docs["diffstat"]["removed"], docs["diffstat"]["binary"]) == (3, 1, ["logo.png"])
    assert bob["push"] == story.p_bob and bob["carried_from"] == story.genesis and bob["verdict"] is None
    assert bob["title"] == "faster f" and bob["tree_hash"] == story.w.chain().tip.tree_hash
    page = get(c, "/api/v1/view/chain?limit=2&before=2")
    assert [l["seq"] for l in page["links"]] == [1, 0] and page["next_before"] is None


# ---------------------------------------------------------------- proposals and escalations

def test_proposals_negotiation_and_escalation(c, story):
    rows = get(c, "/api/v1/view/proposals")["proposals"]
    assert {r["push"] for r in rows} >= {story.q_bob, story.r_bob, story.p_dana}
    q, r = by_push(rows, story.q_bob), by_push(rows, story.r_bob)
    assert r["revises"] == story.q_bob and r["negotiation"] == story.q_bob == q["negotiation"]
    assert r["status"] == "escalated" and r["escalation"] == q["escalation"] is not None
    assert [x["push"] for x in r["revisions"]] == [story.q_bob, story.r_bob]
    assert r["rounds"] == 2 and r["plan"]["askers"] == ["@alice"] and r["plan"]["line_owners"] == ["@alice"]
    first = q["objections"][0]
    assert first["id"] == story.obj2 and first["kind"] == "reason" and first["status"] == "advisory"
    assert first["answered_by"] == story.r_bob           # answered by the revision (§13.5)
    again = r["objections"][0]
    assert again["id"] == story.obj3 and again["blocking"] and again["episode"] == 2
    t = r["timers"]
    assert t["window_closes_at"] == t["window_open"] + W and t["grace_until"] == t["window_closes_at"] + G
    assert t["expires_at"] == r["received_at"] + 7 * 86_400_000
    assert q["race"] == 0 and q["blocks_tip"] and not r["blocks_tip"]
    assert q["parent_seq"] == 3 and not q["carried"] and q["task_alias"] == "T1"


def test_evidence_objection_is_confirmed(c, story):
    d = by_push(get(c, "/api/v1/view/proposals")["proposals"], story.p_dana)
    assert d["status"] == "objected" and d["plan"]["askers"] == ["@carol"]
    ob = d["objections"][0]
    assert ob["id"] == story.obj_ev and ob["kind"] == "evidence" and ob["status"] == "confirmed" and ob["blocking"]
    assert ob["evidence"] == {"path": "tests/test_lib.py", "sha256": E.sha256_hex(EVIDENCE), "size": len(EVIDENCE)}


def test_escalations(c, story):
    escs = get(c, "/api/v1/view/escalations")["escalations"]
    assert len(escs) == 1
    e = escs[0]
    assert e["reason"] == "rounds" and e["status"] == "open" and e["deciders"] == ["@nelson"]
    assert e["deadline"] == e["opened_at"] + 3_600_000
    assert e["pushes"][0]["root"] == story.q_bob and e["pushes"][0]["push"] == story.r_bob
    assert e["default"]["chosen"] == story.q_bob and e["default"]["declined"] == []


def test_carried_proposal(tmp_path):
    w = World(tmp_path)
    try:
        g = w.genesis({"a.py": "a = 1\n", "b.py": "b = 1\n"}, author="@alice")
        pa = w.push("@bob", g, {"a.py": "a = 2\n"})
        pb = w.push("@carol", g, {"b.py": "b = 2\n"})
        w.approve("@alice", pa)                  # every asker approved: bob links now
        w.advance(10)
        c = TestClient(w.host.app(loopback_hosts=LOOP, ui_dist=False))
        assert w.chain().tip.push == pa.id
        row = by_push(get(c, "/api/v1/view/proposals")["proposals"], pb.id)
        assert row["carried"] and row["named_parent_seq"] == 0 and row["parent_seq"] == 1
        assert row["id"] != pb.id and row["status"] == "window"
        assert row["timers"]["window_open"] == row["received_at"]          # no new asker: same window (§14.3)
        assert get(c, f"/api/v1/view/diff/{row['id']}")["files"][0]["path"] == "b.py"
    finally:
        w.close()


# ---------------------------------------------------------------- tasks, claims, members

def test_tasks_claims_and_overlaps(c, story):
    body = get(c, "/api/v1/view/tasks")
    tasks = {t["alias"]: t for t in body["tasks"]}
    assert set(tasks) == {"T1", "T2", "T3"}
    t1, t2, fix = tasks["T1"], tasks["T2"], tasks["T3"]
    assert t1["title"] == "Speed up f" and t1["description"] == "f is slow" and t1["requester"] == "@nelson"
    assert t1["holder"] == "@bob" and t1["status"] == "work_done" and t1["waiting"] == "acceptance"
    assert [l["seq"] for l in t1["links"]] == [3]
    claim = t1["claims"][0]
    assert claim["id"] == story.c_bob and claim["files"] == ["app.py"] and claim["status"] == "active"
    assert claim["lapses_at"] == claim["last_activity"] + 1_800_000 and claim["overlaps"] == [story.c_carol]
    assert t2["claims"][0]["files"] == ["app.py", "lib.py"]
    assert body["overlaps"] == [{"claims": [story.c_bob, story.c_carol], "holders": ["@bob", "@carol"],
                                 "tasks": [story.t_speed, story.t_tidy], "globs": ["app.py"], "paths": ["app.py"]}]
    assert fix["kind"] == "fix" and fix["cited_seq"] == 2 and fix["cited_author"] == "@alice"
    assert fix["offered_to"] == "@alice" and fix["status"] == "open"


def test_members_and_joins(c, story):
    body = get(c, "/api/v1/view/members")
    m = {x["handle"]: x for x in body["members"]}
    assert set(m) == {"@alice", "@bob", "@carol", "@dana", "@host", "@nelson", "@t1"}
    alice = m["@alice"]
    assert alice["role"] == "agent" and alice["sponsor"] == "@nelson" and alice["votes"]
    assert alice["key"] == story.w.kit.pub("@alice") and alice["key_short"] == alice["key"][8:20]
    assert alice["lines"] == alice["lines_live"] > 0 and m["@bob"]["lines"] == 1
    assert m["@t1"]["role"] == "tester" and not m["@t1"]["votes"] and m["@host"]["role"] == "host"
    assert alice["delivery"]["channel"] == "email" and alice["delivery"]["journal_head"] > 0
    assert body["removed"] == []
    join = body["joins"][0]
    assert join["id"] == story.join and join["handle"] == "@erin" and join["status"] == "pending"
    assert join["role"] == "agent" and join["sponsor"] == "@nelson"


# ---------------------------------------------------------------- files, blame, diffs, threads

def test_tree_file_and_blame(c, story):
    tree = get(c, "/api/v1/view/tree")
    assert tree["at"]["seq"] == 3
    files = {f["path"]: f for f in tree["files"]}
    assert files["app.py"]["owners"] == {"@alice": 1, "@bob": 1} and files["app.py"]["lines"] == 2
    assert files["logo.png"]["binary"] and files["logo.png"]["lines"] is None
    assert files["run.sh"]["mode"] == "100755" and "notes.txt" not in files and "docs/notes.txt" in files
    old = get(c, f"/api/v1/view/tree?at={story.genesis[:10]}")
    assert old["at"]["seq"] == 0 and "notes.txt" in {f["path"] for f in old["files"]}
    proposed = get(c, f"/api/v1/view/tree?at={story.r_bob}")
    assert proposed["at"]["kind"] == "proposal"
    f = get(c, "/api/v1/view/file?path=app.py")
    assert f["kind"] == "text" and f["text"] == "def f():\n    return 2\n" and f["lines"] == 2
    assert get(c, "/api/v1/view/file?path=logo.png")["kind"] == "binary"
    get(c, "/api/v1/view/file?path=nope.txt", 404)
    blame = get(c, "/api/v1/view/blame?path=app.py")
    lines = blame["lines"]
    assert [(l["owner"], l["seq"], l["text"]) for l in lines] == [("@alice", 0, "def f():"), ("@bob", 3, "    return 2")]
    assert all(l["live"] and l["member"] and l["voter"] for l in lines)
    assert lines[1]["intent"] == "faster f" and lines[0]["links_left"] == 50 - 3
    binary = get(c, "/api/v1/view/blame?path=logo.png")
    assert binary["binary"] and len(binary["lines"]) == 1 and binary["lines"][0]["seq"] == 2
    get(c, f"/api/v1/view/blame?path=app.py&at={story.r_bob}", 400)      # a proposal has no blame


def test_blame_shows_expired_ownership(tmp_path):
    w = World(tmp_path, config="authserv-id fake.gud.invalid\nown-links 1\nown-time 1s\n")
    try:
        g = w.genesis({"a.py": "a = 1\n", "b.py": "b = 1\n"}, author="@alice")
        w.push("@alice", g, {"b.py": "b = 2\n"})
        w.net.advance(5_000)
        w.intake.tick(force=True)             # replay time follows the log (§11.3)
        assert len(w.chain().links) == 2
        c = TestClient(w.host.app(loopback_hosts=LOOP, ui_dist=False))
        line = get(c, "/api/v1/view/blame?path=a.py")["lines"][0]
        assert line["owner"] == "@alice" and line["seq"] == 0 and not line["live"] and line["links_left"] == 0
        assert get(c, "/api/v1/view/blame?path=b.py")["lines"][0]["live"]
        files = {f["path"]: f for f in get(c, "/api/v1/view/tree")["files"]}
        assert files["a.py"]["owners"] == {"@alice": 1} and files["a.py"]["live_owners"] == {}
        m = {x["handle"]: x for x in get(c, "/api/v1/view/members")["members"]}
        assert m["@alice"]["lines_live"] < m["@alice"]["lines"]
    finally:
        w.close()


def test_link_diff_render_ops(c, story):
    d = get(c, f"/api/v1/view/diff/{story.p_docs[:8]}")
    assert d["kind"] == "link" and d["seq"] == 2 and d["carried_from"] == story.genesis and d["available"]
    assert {o["op"] for o in d["ops"]} == {"rename", "put", "mode"}
    files = {f["path"]: f for f in d["files"]}
    assert files["docs/notes.txt"]["op"] == "rename" and files["docs/notes.txt"]["src"] == "notes.txt"
    assert files["docs.md"]["op"] == "modify" and files["docs.md"]["added"] == 2 and "+more" in files["docs.md"]["hunks"]
    logo = files["logo.png"]
    assert logo["binary"] and logo["hunks"] == "" and logo["placeholder"].startswith("binary file")
    assert files["run.sh"]["op"] == "mode" and files["run.sh"]["new_mode"] == "100755"
    assert "Binary files a/logo.png and b/logo.png differ" in d["unified"]
    assert "rename from notes.txt" in d["unified"] and "new mode 100755" in d["unified"]
    p = get(c, f"/api/v1/view/diff/{story.p_dana}")
    assert p["kind"] == "proposal" and p["status"] == "objected"
    assert p["files"] == [{**p["files"][0], "op": "modify", "path": "lib.py", "added": 1, "removed": 1}]
    g = get(c, "/api/v1/view/diff/" + story.genesis)
    assert {f["op"] for f in g["files"]} == {"add"}
    get(c, "/api/v1/view/diff/" + "f" * 64, 404)


def test_thread(c, story):
    th = get(c, f"/api/v1/view/thread/{story.r_bob}")
    assert th["head"]["type"] == "proposal" and th["head"]["status"] == "escalated"
    ids = [m["id"] for m in th["messages"]]
    assert ids == [story.q_bob, story.obj2, story.r_bob, story.obj3]        # the whole negotiation, log order
    assert th["messages"][2]["headers"]["revises"] == story.q_bob and th["messages"][3]["kind"] == "object"
    link = get(c, f"/api/v1/view/thread/{story.p_bob}")
    assert link["head"]["type"] == "link" and link["head"]["seq"] == 3
    kinds = [m["kind"] for m in link["messages"]]
    assert kinds[0] == "push" and "object" in kinds and "hold" in kinds and "comment" in kinds
    ev = get(c, f"/api/v1/view/thread/{story.p_dana}")
    assert ev["messages"][1]["evidence"]["path"] == "tests/test_lib.py"
    task = get(c, "/api/v1/view/thread/T1")
    assert task["head"]["type"] == "task" and {m["kind"] for m in task["messages"]} >= {"task", "claim"}
    get(c, "/api/v1/view/thread/zz", 400)


# ---------------------------------------------------------------- host, events, stream

def test_host_log_and_audit(c, story):
    h = get(c, "/api/v1/view/host?limit=5")
    log = story.w.intake.log
    assert h["head"]["seq"] == len(log) - 1 and h["entries"] == len(log) and len(h["tail"]) == 5
    assert h["tail"][0]["seq"] == h["head"]["seq"] and h["tail"][0]["kind"] == "join"
    assert h["counts"]["open"] == 1 and h["counts"]["mail"] == h["intake"]["logged"]
    assert h["key"] == story.w.kit.pub("@host") and not h["handed_over"] and h["missing_mail"] == []
    a = h["audit"]
    assert a["ok"] and a["exit_status"] == 0 and a["log_ok"] and a["mail_entries"] == h["counts"]["mail"]
    assert a["seen_mismatches"] == 0 and a["missing_mail"] == 0


def test_events_feed_and_stream(c, story):
    first = get(c, "/api/v1/view/events?limit=2")
    assert [e["n"] for e in first["events"]] == [1, 2] and first["events"][0]["type"] == "linked"
    allev = get(c, "/api/v1/view/events?limit=5000")["events"]
    types = {e["type"] for e in allev}
    assert {"linked", "carried", "objected", "escalated", "tip.red", "claim.overlap", "fix.opened",
            "task.opened", "claim.won"} <= types
    assert len({e["id"] for e in allev}) == len(allev)
    r = c.get("/api/v1/view/stream?follow=0&after=3")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    frames = list(parse_sse(r.text.splitlines()))
    assert frames[0][0] == 4 and len(frames) == len(allev) - 3
    r = c.get("/api/v1/view/stream?follow=0", headers={"Last-Event-ID": str(len(allev) - 1)})
    assert [f[0] for f in parse_sse(r.text.splitlines())] == [len(allev)]


def test_stream_follows_new_events_and_log_head(tmp_path):
    w = World(tmp_path)
    try:
        g = w.genesis({"a.py": "a = 1\n"}, author="@alice")
        c = TestClient(w.host.app(loopback_hosts=LOOP, ui_dist=False, sse_keepalive_s=0.05))
        head = get(c, "/api/v1/view/events")["head"]
        import threading

        def later():
            import time
            time.sleep(0.3)
            w.push("@bob", g, {"a.py": "a = 2\n"})

        threading.Thread(target=later, daemon=True).start()
        r = c.get(f"/api/v1/view/stream?after={head}&max=2")
        assert "event: push.received" in r.text and "event: head" in r.text
    finally:
        w.close()


def test_feed_persists_and_backfills_from_journals(story, tmp_path):
    host = story.w.host
    persisted = EventFeed(host.home / "host" / "ui-events.jsonl")
    assert persisted.head() == host.feed.head() > 0
    rebuilt = EventFeed(None, host.dispatcher.journal)       # a host whose feed file is gone
    ids = {e.id for _, e in rebuilt.since(0, 10_000)}
    assert ids and ids <= {e.id for _, e in host.feed.since(0, 10_000)}


# ---------------------------------------------------------------- access and actions

def test_access_is_the_mirrors(story):
    w = story.w
    remote = TestClient(w.host.app(loopback_hosts=frozenset(), ui_dist=False))
    for path in ("/api/v1/view/state", "/api/v1/view/chain", "/api/v1/view/proposals", "/api/v1/view/tasks",
                 "/api/v1/view/members", "/api/v1/view/host", "/api/v1/view/stream?follow=0"):
        assert remote.get(path).status_code == 401, path
        h = sign_request("@bob", w.kit.key("@bob"), "GET", path, b"", w.net.clock())
        assert remote.get(path, headers=h).status_code == 200, path
    body = b'{"title": "x"}'
    h = sign_request("@nelson", w.kit.key("@nelson"), "POST", "/api/v1/view/act/task", body, w.net.clock())
    assert remote.post("/api/v1/view/act/task", content=body, headers=h).status_code == 403   # loopback only


def test_actions_sign_as_the_configured_human(tmp_path):
    w = World(tmp_path)
    try:
        g = w.genesis({"a.py": "a = 1\n"}, author="@alice")
        home = tmp_path / "nelson-home"
        (home / "members" / "@nelson").mkdir(parents=True)
        (home / "members" / "@nelson" / "key").write_text(
            "ed25519:" + __import__("base64").b64encode(w.kit.key("@nelson")).decode() + "\n")
        (home / "members" / "@stranger").mkdir(parents=True)
        (home / "members" / "@stranger" / "key").write_text("ed25519:" + "A" * 43 + "=\n")
        signers = load_signers([home])
        assert set(signers) == {"@nelson", "@stranger"}
        c = TestClient(w.host.app(loopback_hosts=LOOP, ui_dist=False, ui_signers=signers))
        ui = get(c, "/api/v1/view/state")["ui"]
        assert ui == {"signers": [{"handle": "@nelson", "role": "human"}], "human": "@nelson", "console": None,
                      "actions": True}
        r = c.post("/api/v1/view/act/task", json={"title": "Write docs", "description": "please",
                                                   "accept_cmds": ["true"]})
        assert r.status_code == 200, r.text
        made = r.json()
        assert made["logged"] and made["as"] == "@nelson" and "task.opened" in made["events"]
        t = next(t for t in get(c, "/api/v1/view/tasks")["tasks"] if t["id"] == made["id"])
        assert t["title"] == "Write docs" and t["accept_cmds"] == ["true"] and t["author"] == "@nelson"
        p = w.push("@bob", g, {"a.py": "a = 2\n"})
        r = c.post("/api/v1/view/act/vote", json={"kind": "hold", "target": p.id[:10]})
        assert r.status_code == 200 and r.json()["kind"] == "hold"
        r = c.post("/api/v1/view/act/comment", json={"target": p.id, "text": "why 2?"})
        assert r.status_code == 200 and r.json()["kind"] == "comment"
        r = c.post("/api/v1/view/act/vote", json={"kind": "veto", "target": p.id, "text": "not now"})
        assert r.status_code == 200
        row = next(x for x in get(c, "/api/v1/view/proposals")["proposals"] if x["push"] == p.id)
        assert row["status"] == "vetoed" and row["dead"]
        th = get(c, f"/api/v1/view/thread/{p.id}")
        assert [m["kind"] for m in th["messages"]] == ["push", "hold", "comment", "object"]
        assert th["messages"][3]["text"] == "veto not now"
        assert c.post("/api/v1/view/act/vote", json={"kind": "object", "target": p.id}).status_code == 400
        assert c.post("/api/v1/view/act/vote", json={"kind": "approve", "target": p.id,
                                                     "as": "@stranger"}).status_code == 403
        assert c.post("/api/v1/view/act/vote", json={"kind": "nod", "target": p.id}).status_code == 400
        assert c.post("/api/v1/view/act/vote", json={"kind": "veto", "target": made["id"]}).status_code == 400
        # admit a join request: a members push citing it (members rule: the human author's own push)
        erin = w.kit.key("@erin")
        j = w.kit.sign("join", "@erin", {"Gud-Role": "agent", "Gud-Address": "erin@example.org",
                                         "Gud-Key": E.public_of(erin), "Gud-Sponsor": "@nelson"}, (), key=erin, log=False)
        w.send(j)
        assert get(c, "/api/v1/view/members")["joins"][0]["status"] == "pending"
        r = c.post("/api/v1/view/act/admit", json={"join": j.id})
        assert r.status_code == 200, r.text
        assert r.json()["member"]["handle"] == "@erin"
        w.advance(1_000)
        joins = get(c, "/api/v1/view/members")["joins"]
        assert joins[0]["status"] == "member"
        assert "@erin" in {m["handle"] for m in get(c, "/api/v1/view/members")["members"]}
        assert get(c, "/api/v1/view/needs")["member"] == "@nelson"
    finally:
        w.close()


def test_link_thread_holds_its_whole_negotiation(tmp_path):
    w = World(tmp_path)
    try:
        g = w.genesis({"a.py": "a = 1\n"}, author="@alice")
        p = w.push("@bob", g, {"a.py": "a = 2\n"}, intent="two")
        o = w.object("@alice", p, "no")
        r = w.push("@bob", g, {"a.py": "a = 3\n"}, revises=p, intent="three")
        a = w.approve("@alice", r)
        w.advance(W + G + 1_000)
        tip = w.chain().tip
        assert tip.push in (p.id, r.id) and tip.seq == 1
        c = TestClient(w.host.app(loopback_hosts=LOOP, ui_dist=False))
        for linked_or_not in (p.id, r.id):
            th = get(c, f"/api/v1/view/thread/{linked_or_not}")
            assert th["head"]["type"] in ("link", "proposal")
            assert [m["id"] for m in th["messages"]] == [p.id, o.id, r.id, a.id]
    finally:
        w.close()


def test_actions_need_a_signer(story):
    c = TestClient(story.w.host.app(loopback_hosts=LOOP, ui_dist=False))
    r = c.post("/api/v1/view/act/task", json={"title": "x"})
    assert r.status_code == 409 and "--ui-home" in r.json()["detail"]


def test_needs_for_the_human(story):
    c = TestClient(story.w.host.app(loopback_hosts=LOOP, ui_dist=False))
    items = get(c, "/api/v1/view/needs?member=@nelson")["items"]
    kinds = {i["type"] for i in items}
    assert {"escalation", "accept"} <= kinds
    assert next(i for i in items if i["type"] == "escalation")["pushes"] == [story.q_bob]


# ---------------------------------------------------------------- the built UI at /

def test_serves_a_ui_build(story, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><script type="module" src="/assets/app-1.js"></script>')
    (dist / "assets" / "app-1.js").write_text("console.log('gud')")
    (dist / "favicon.svg").write_text("<svg/>")
    c = TestClient(story.w.host.app(loopback_hosts=LOOP, ui_dist=dist))
    r = c.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "app-1.js" in r.text
    js = c.get("/assets/app-1.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "immutable" in js.headers["cache-control"]
    assert c.get("/favicon.svg").headers["content-type"].startswith("image/svg")
    assert "app-1.js" in c.get("/tasks").text                               # client-side routes get index.html
    assert c.get("/assets/missing.js").status_code == 404
    assert c.get("/../../etc/passwd").status_code in (404, 200) and "root:" not in c.get("/../../etc/passwd").text
    assert c.get("/api/v1/nope").status_code == 404
    assert c.get("/api/v1/info").json()["repo"] == "repo@example.org"         # the mirror still wins
    bare = TestClient(story.w.host.app(loopback_hosts=LOOP, ui_dist=tmp_path / "missing"))
    assert "npm run build" in bare.get("/").text


@pytest.mark.skipif(not (ROOT / "ui" / "dist" / "index.html").is_file(), reason="ui/ is not built (npm run build)")
def test_serves_the_real_ui_build(story):
    dist = ROOT / "ui" / "dist"
    c = TestClient(story.w.host.app(loopback_hosts=LOOP, ui_dist=dist))
    index = c.get("/")
    assert index.status_code == 200 and '<div id="root">' in index.text
    import re
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.text)
    assert assets, index.text
    for path in assets:
        r = c.get(path)
        assert r.status_code == 200 and len(r.content) > 0, path
