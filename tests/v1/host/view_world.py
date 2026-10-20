"""A real host (`tests.v1.events.harness.World`) driven through a scenario that exercises every UI
view: claims that overlap, a carried push, a negotiation that escalates (rounds), a hold, an
evidence objection the tester confirms, a red tip with `first_bad` and its fix task, a binary
file, a rename and a mode change, and a pending join request. Every message is signed with the
kit keys and goes through the host's real intake (mail → log → replay → journal)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gud.core import envelope as E
from tests.v1.events.harness import World

W, G = 60_000, 30_000
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(64))
EVIDENCE = b"from lib import x\n\n\ndef test_x():\n    assert x == 2\n"


@dataclass
class Story:
    w: World
    genesis: str
    t_speed: str        # task T1 (claimed by @bob)
    t_tidy: str         # task T2 (claimed by @carol; overlaps @bob's claim on app.py)
    c_bob: str
    c_carol: str
    p_carol: str        # lib.py change: links first (#1)
    p_bob: str          # app.py change: objected, held, answered by a comment, links as #3
    p_docs: str         # @alice's docs/logo/rename/mode push on genesis: carried onto #1, links as #2
    q_bob: str          # app.py signature change on #3: objected (episode 1)
    r_bob: str          # its revision, objected again (episode 2): rounds escalation
    obj1: str
    obj2: str
    obj3: str
    p_dana: str         # lib.py change by @dana: evidence objection by @carol, confirmed
    obj_ev: str
    join: str


def build(tmp: Path) -> Story:
    w = World(tmp)
    k = w.kit
    g = w.genesis({
        "app.py": "def f():\n    return 1\n",
        "lib.py": "x = 1\n",
        "docs.md": "# docs\n",
        "logo.png": PNG,
        "notes.txt": "notes\n",
        "run.sh": "#!/bin/sh\necho hi\n",
    }, author="@alice")
    t_speed = w.task("@nelson", "Speed up f", text="f is slow")
    t_tidy = w.task("@nelson", "Tidy lib")
    c_bob = w.claim("@bob", t_speed, ["app.py"])
    c_carol = w.claim("@carol", t_tidy, ["app.py", "lib.py"])
    w.test_tip(g.id, k.tree(g), "pass")
    p_carol = w.push("@carol", g, {"lib.py": "x = 2\n"}, task=t_tidy.id, intent="lib: x is 2")
    p_bob = w.push("@bob", g, {"app.py": "def f():\n    return 2\n"}, task=t_speed.id, intent="faster f")
    obj1 = w.object("@alice", p_bob, "returning 2 changes behaviour")
    w.hold("@nelson", p_bob)
    docs_tree = k.tree(g).set("docs.md", "100644", b"# docs\n\nmore\n").set("logo.png", "100644", PNG[::-1])
    docs_tree = docs_tree.delete("notes.txt").set("docs/notes.txt", "100644", b"notes\n")
    docs_tree = docs_tree.delete("run.sh").set("run.sh", "100755", b"#!/bin/sh\necho hi\n")
    from gud.core.ops import diff_trees
    ops = diff_trees(k.tree(g), docs_tree, inline_budget=1 << 30)
    p_docs = w.push("@alice", g, ops=ops, intent="docs, logo, notes moved, run.sh executable")
    w.advance(W + G + 1_000)        # carol's window closes: she links as #1
    assert w.chain().tip.push == p_carol.id
    w.advance(W + 1_000)            # bob's held, objected push parks; alice's (carried onto #1) links as #2
    chain = w.chain()
    assert [l.push for l in chain.links[1:]] == [p_carol.id, p_docs.id], [l.intent for l in chain.links]
    # bob answers alice's objection with a comment: it becomes advisory and his push links (#3)
    w.comment("@bob", obj1, "it is meant to; see the task")
    w.advance(1_000)
    chain = w.chain()
    assert chain.tip.push == p_bob.id, [l.intent for l in chain.links]
    for link in chain.links:          # the kit builds pushes on trees it knows
        k.trees[link.state] = w.host.replayer.reader().tree(link.state)
    tip = chain.tip.state
    # a negotiation that escalates: alice objects, bob revises, alice objects again (two episodes)
    q_bob = w.push("@bob", tip, {"app.py": "def f(n=1):\n    return 2\n"}, task=t_speed.id, intent="f takes n")
    obj2 = w.object("@alice", q_bob, "the signature change breaks callers")
    r_bob = w.push("@bob", tip, {"app.py": "def f(n=1, *, fast=True):\n    return 2\n"}, task=t_speed.id,
                   revises=q_bob, intent="f takes n, keyword only")
    obj3 = w.object("@alice", r_bob, "still breaks callers")
    # red tip: #1 passes, #2 fails: first_bad is #2 (alice), a fix task is offered to her
    l1, l2 = chain.links[1], chain.links[2]
    w.test_tip(l1.state, w.host.replayer.reader().tree(l1.state), "pass")
    w.test_tip(l2.state, w.host.replayer.reader().tree(l2.state), "fail")
    # an evidence objection, confirmed by the tester
    p_dana = w.push("@dana", tip, {"lib.py": "x = 3\n"}, intent="lib: x is 3")
    obj_ev = w.object("@carol", p_dana, "x must stay 2", evidence=("tests/test_lib.py", EVIDENCE))
    reader = w.host.replayer.reader()
    cand = w.chain().push_status(p_dana.id)
    with_file = reader.candidate_tree(cand.id).set("tests/test_lib.py", "100644", EVIDENCE)
    w.test("evidence", obj_ev.id, with_file.hash(), "fail", on=cand.id, base="pass")
    # a join request from an outside agent
    erin = k.key("@erin")
    jmsg = k.sign("join", "@erin", {"Gud-Role": "agent", "Gud-Address": "erin@example.org",
                                    "Gud-Key": E.public_of(erin), "Gud-Sponsor": "@nelson"},
                  (), key=erin, log=False)
    w.send(jmsg)
    w.advance(2_000)
    return Story(w, g.id, t_speed.id, t_tidy.id, c_bob.id, c_carol.id, p_carol.id, p_bob.id, p_docs.id, q_bob.id,
                 r_bob.id, obj1.id, obj2.id, obj3.id, p_dana.id, obj_ev.id, jmsg.id)
