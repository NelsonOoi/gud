"""Leaderless tasks and claims (§16): opening, claims, leases, overlap, self-splitting, derived done,
no-change, bug reports, fix-task offers, escalation events, aliases."""

from gud.core.replay import Replayer
from gud.core.tasks import aliases, fix_task_id
from gud.core.types import TaskState
from tests.v1.kit import Kit, events_of, link_of, status

L = 30 * 60_000


def base(**kw):
    k = Kit(**kw)
    g = k.genesis({"src/a.py": "a = 1\n", "src/b.py": "b = 1\n", "web/x.css": "x\n"}, author="@alice")
    return k, g


# ---------------------------------------------------------------- opening (§16.1)

def test_signed_task_opens_with_alias_and_event():
    k, g = base()
    t1 = k.task("@carol", "first")
    t2 = k.task("@nelson", "second", accept=["tests/test_x.py"])
    chain, events = k.run()
    a, b = chain.tasks[t1.id], chain.tasks[t2.id]
    assert (a.alias, b.alias) == ("T1", "T2")
    assert a.requester == "@nelson" and b.requester == "@nelson"     # sponsor / human author
    assert b.accept_paths == ("tests/test_x.py",)
    opened = events_of(events, "task.opened")
    assert [e.data["alias"] for e in opened] == ["T1", "T2"]
    assert "@carol" not in opened[0].concerns and "@alice" in opened[0].concerns


def test_task_from_tester_or_forged_is_ignored():
    k, g = base()
    t = k.task("@t1", "tester task")
    f = k.task("@carol", "forged", key=k.key("@mallory"))
    chain, _ = k.run()
    assert t.id not in chain.tasks and f.id not in chain.tasks


def test_verified_human_email_opens_task():
    k, g = base()
    m = k.unsigned("@dana", "Please add a pricing page.", subject="Re: [gud] Pricing page")
    bad = k.unsigned("@dana", "Spoofed", subject="fake", dmarc=False)
    chain, _ = k.run()
    t = chain.tasks[m.id]
    assert t.title == "Pricing page" and t.requester == "@dana" and t.description.startswith("Please")
    assert bad.id not in chain.tasks


# ---------------------------------------------------------------- claims (§16.3)

def test_earliest_claim_wins_and_later_is_lost():
    k, g = base()
    t = k.task("@nelson", "work")
    c1 = k.claim("@alice", t, ["src/**"])
    c2 = k.claim("@bob", t, ["src/**"])
    chain, events = k.run()
    assert chain.tasks[t.id].claim == c1.id and chain.claims[c1.id].status == "active"
    assert chain.claims[c2.id].status == "void"
    lost = events_of(events, "claim.lost")
    assert lost and lost[0].data["active_claim"] == c1.id and lost[0].concerns == ("@bob",)
    assert events_of(events, "claim.won")[0].data["holder"] == "@alice"


def test_claim_by_tester_is_ignored():
    k, g = base()
    t = k.task("@nelson", "work")
    c = k.claim("@t1", t)
    chain, _ = k.run()
    assert c.id not in chain.claims


def test_lease_lapses_and_others_can_claim():
    k, g = base()
    t = k.task("@nelson", "work")
    c1 = k.claim("@alice", t)
    chain, events = k.advance(L + 1)
    assert chain.claims[c1.id].status == "lapsed"
    lapsed = events_of(events, "claim.lapsed")
    assert lapsed and lapsed[0].at == c1.logged_at + L and "@alice" in lapsed[0].concerns
    c2 = k.claim("@bob", t)
    chain, _ = k.run()
    assert chain.tasks[t.id].claim == c2.id


def test_lease_renewed_by_comment_and_push_on_task():
    k, g = base()
    t = k.task("@nelson", "work")
    c1 = k.claim("@alice", t, ["src/**"])
    k.tick(L - 60_000)
    k.comment("@alice", t, "progress", task=t.id)
    k.tick(L - 60_000)
    k.push("@alice", g, {"src/a.py": "a = 2\n"}, task=t.id)
    chain, _ = k.advance(L - 60_000)
    assert chain.claims[c1.id].status in ("active", "done")


def test_comment_from_non_holder_does_not_renew():
    k, g = base()
    t = k.task("@nelson", "work")
    c1 = k.claim("@alice", t)
    k.tick(L - 60_000)
    k.comment("@bob", t, "hi", task=t.id)
    chain, _ = k.advance(61_000)
    assert chain.claims[c1.id].status == "lapsed"


def test_release_and_scope_update():
    k, g = base()
    t = k.task("@nelson", "work")
    c1 = k.claim("@alice", t, ["src/**"])
    u = k.claim("@alice", t, ["src/a.py"], revises=c1)
    chain, _ = k.run()
    assert chain.claims[c1.id].files == ("src/a.py",) and chain.claims[c1.id].latest == u.id
    k.claim("@bob", t, ["src/**"], revises=c1, release=True)       # not the holder: ignored link
    chain, _ = k.run()
    assert chain.tasks[t.id].claim == c1.id
    k.claim("@alice", t, ["src/**"], revises=u, release=True)
    chain, _ = k.run()
    assert chain.claims[c1.id].status == "released" and chain.tasks[t.id].claim is None
    c3 = k.claim("@bob", t)
    chain, _ = k.run()
    assert chain.tasks[t.id].claim == c3.id


def test_overlap_detected_between_claims():
    k, g = base()
    t1, t2, t3 = k.task("@nelson", "a"), k.task("@nelson", "b"), k.task("@nelson", "c")
    c1 = k.claim("@alice", t1, ["src/**"])
    c2 = k.claim("@bob", t2, ["src/a.py"])
    c3 = k.claim("@carol", t3, ["web/**"])
    chain, events = k.run()
    overlaps = events_of(events, "claim.overlap")
    assert len(overlaps) == 1
    assert overlaps[0].data["claims"] == sorted([c1.id, c2.id])
    assert set(overlaps[0].concerns) == {"@alice", "@bob"}


def test_scope_exceeded_when_push_touches_anothers_claim():
    k, g = base()
    t1 = k.task("@nelson", "a")
    c1 = k.claim("@bob", t1, ["web/**"])
    p = k.push("@alice", g, {"web/x.css": "y\n"})
    k.approve("@nelson", p)
    chain, events = k.run()
    ex = events_of(events, "scope.exceeded")
    assert ex and ex[0].data["claim"] == c1.id and set(ex[0].concerns) == {"@alice", "@bob"}


# ---------------------------------------------------------------- splitting (§16.5)

def test_split_counts_only_while_holding_parent_claim():
    k, g = base()
    t = k.task("@nelson", "big")
    bad = k.subtask("@bob", t, "not mine")
    k.claim("@alice", t)
    s1 = k.subtask("@alice", t, "part one", files=["src/a.py"])
    s2 = k.subtask("@alice", t, "part two")
    chain, events = k.run()
    assert bad.id not in chain.tasks
    assert chain.tasks[s1.id].alias == "T1.1" and chain.tasks[s2.id].alias == "T1.2"
    assert chain.tasks[t.id].children == (s1.id, s2.id)
    assert [e.data["alias"] for e in events_of(events, "subtask.posted")] == ["T1.1", "T1.2"]
    chain, _ = k.advance(L + 1)                                  # claim lapses
    late = k.subtask("@alice", t, "too late")
    chain, _ = k.run()
    assert late.id not in chain.tasks and s1.id in chain.tasks   # earlier split stays valid
    c = k.claim("@bob", s1)
    s11 = k.subtask("@bob", s1, "nested")
    chain, _ = k.run()
    assert chain.tasks[s11.id].alias == "T1.1.1"


# ---------------------------------------------------------------- derived done (§16.6)

def test_task_done_via_subtasks_and_requester_acceptance():
    k, g = base()
    t = k.task("@nelson", "big")
    k.claim("@alice", t)
    s1 = k.subtask("@alice", t, "a")
    s2 = k.subtask("@alice", t, "b")
    k.claim("@bob", s1, ["src/a.py"])
    k.claim("@carol", s2, ["src/b.py"])
    p1 = k.push("@bob", g, {"src/a.py": "a = 2\n"}, task=s1.id)
    p2 = k.push("@carol", g, {"src/b.py": "b = 2\n"}, task=s2.id)
    k.approve("@alice", p1)
    k.approve("@alice", p2)
    chain, events = k.run()
    assert chain.tasks[s1.id].status == "done" and chain.tasks[s2.id].status == "done"
    assert chain.tasks[t.id].status == "work_done"
    ready = events_of(events, "task.ready")
    assert ready and ready[-1].data["task"] == t.id and ready[-1].data["needs"] == "accept"
    assert ready[-1].concerns == ("@nelson",)
    k.approve("@nelson", t)                                     # signed requester acceptance
    chain, events = k.run()
    assert chain.tasks[t.id].status == "done"
    done = events_of(events, "task.done")
    assert [e.data["task"] for e in done] == [s1.id, s2.id, t.id]
    assert set(done[-1].concerns) >= {"@nelson"}


def test_acceptance_from_non_requester_does_not_count():
    k, g = base()
    t = k.task("@nelson", "x")
    c = k.claim("@alice", t)
    p = k.push("@alice", g, {"src/c.py": "c\n"}, task=t.id)
    chain, _ = k.run()
    k.claim("@alice", t, revises=c, done=True)
    k.approve("@dana", t)
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "work_done"


def test_task_with_criteria_done_on_accept_pass():
    k, g = base(test="command pytest\n")
    t = k.task("@nelson", "x", accept=["tests/test_x.py"])
    c = k.claim("@alice", t)
    p = k.push("@alice", g, {"src/c.py": "c\n"}, task=t.id)
    chain, _ = k.run()
    k.claim("@alice", t, revises=c, done=True)
    chain, events = k.run()
    assert chain.tasks[t.id].status == "work_done"
    assert events_of(events, "task.ready")[0].data["needs"] == "criteria"
    assert "@t1" in events_of(events, "task.ready")[0].concerns
    link = link_of(chain, p)
    k.test("accept", t, link.tree_hash, "fail", link=link.state)
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "work_done"
    k.test("accept", t, link.tree_hash, "pass", link=link.state)
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "done"


def test_root_task_is_not_done_on_its_first_link():
    """The eval repro (todo-stack gud1-cc t2): an `accept` command that already passes on the seed, and a
    README-only link with Gud-Task = T1. T1 stays claimed until its holder declares it done; an accept
    pass on a link before the declaration does not count."""
    k, g = base(test="command pytest\n")
    t = k.task("@nelson", "todo stack", cmds=["python -m pytest -q"])
    c = k.claim("@alice", t, ["**"])
    readme = k.push("@alice", g, {"README.md": "# todo\n"}, task=t.id)
    chain, events = k.run()
    assert status(chain, readme) == "linked"
    assert chain.tasks[t.id].status == "claimed" and not events_of(events, "task.ready")
    l1 = link_of(chain, readme)
    k.test("accept", t, l1.tree_hash, "pass", link=l1.state)
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "claimed"
    lib = k.push("@bob", l1.state, {"src/todolib.py": "def add(): ...\n"}, task=t.id)   # others' links: no effect
    chain, _ = k.run()
    assert status(chain, lib) == "linked" and chain.tasks[t.id].status == "claimed"
    decl = k.claim("@alice", t, ["**"], revises=c, done=True)
    chain, events = k.run()
    task = chain.tasks[t.id]
    assert task.status == "work_done" and task.declared == decl.id and task.declared_by == "@alice"
    assert task.work_done_link == link_of(chain, lib).state
    ready = events_of(events, "task.ready")
    assert ready and ready[-1].data["needs"] == "criteria" and ready[-1].data["link"] == task.work_done_link
    k.test("accept", t, l1.tree_hash, "pass", link=l1.state)       # an older link still does not count
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "work_done"
    l2 = link_of(chain, lib)
    k.test("accept", t, l2.tree_hash, "pass", link=l2.state)
    chain, events = k.run()
    assert chain.tasks[t.id].status == "done"
    done = events_of(events, "task.done")
    assert done and {"@alice", "@bob", "@carol"} <= set(done[-1].concerns)     # every agent hears of it
    assert chain.claims[c.id].status == "done"


def test_done_declaration_rules():
    k, g = base()
    t1, t2, t3, t4 = (k.task("@nelson", n) for n in ("one", "two", "three", "four"))
    c1 = k.claim("@alice", t1, ["src/a.py"])
    k.claim("@bob", t1, ["src/a.py"], revises=c1, done=True)       # not the holder: ignored
    c2 = k.claim("@bob", t2, ["src/b.py"])
    k.push("@bob", g, {"src/b.py": "b = 2\n"}, task=t2.id)        # rewrites @alice's line: a consent window
    k.claim("@bob", t2, ["src/b.py"], revises=c2, done=True)       # sent while that push is still live
    c3 = k.claim("@carol", t3, ["web/**"])
    k.claim("@carol", t3, ["web/**"], revises=c3, release=True, done=True)   # release --done
    c4 = k.claim("@alice", t4, ["**"])
    k.subtask("@alice", t4, "part")
    k.claim("@alice", t4, ["**"], revises=c4, done=True)            # has subtasks: their done decides
    chain, _ = k.run()
    assert chain.tasks[t1.id].status == "claimed" and chain.tasks[t1.id].declared is None
    assert chain.tasks[t2.id].declared is None
    assert chain.tasks[t3.id].declared is not None and chain.claims[c3.id].status == "released"
    assert chain.tasks[t3.id].status == "work_done"                  # requester @nelson still accepts it
    assert chain.tasks[t4.id].status == "claimed" and chain.tasks[t4.id].declared is None


def test_subtask_still_done_on_a_link_and_parent_after_all_subtasks():
    k, g = base()
    t = k.task("@nelson", "big")
    k.claim("@alice", t)
    s1 = k.subtask("@alice", t, "a", files=["src/a.py"])
    k.claim("@bob", s1, ["src/a.py"])
    p = k.push("@bob", g, {"src/a.py": "a = 3\n"}, task=s1.id)
    k.approve("@alice", p)
    chain, events = k.run()
    assert chain.tasks[s1.id].status == "done" and chain.tasks[t.id].status == "work_done"
    assert not events_of(events, "claim.overlap")                     # parent ** vs its own subtask: the plan
    assert not [e for e in events_of(events, "scope.exceeded") if "@alice" in e.concerns]


def test_no_change_accepted_and_dropped_by_later_link():
    k, g = base()
    t = k.task("@nelson", "big")
    k.claim("@alice", t)
    s = k.subtask("@alice", t, "check a")
    s2 = k.subtask("@alice", t, "other")
    k.claim("@bob", s, ["src/a.py"])
    nc = k.no_change("@bob", s, g, "a.py already fine")
    chain, _ = k.run()
    assert chain.tasks[s.id].status == "done"
    assert chain.tasks[t.id].no_change == ((s.id, "a.py already fine"),)
    p = k.push("@carol", g, {"src/a.py": "a = 9\n"})
    k.approve("@alice", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"
    assert chain.tasks[s.id].status != "done" and chain.tasks[s.id].no_change == ()


def test_no_change_rejected_when_paths_changed_or_not_holder():
    k, g = base()
    t = k.task("@nelson", "x")
    k.claim("@bob", t, ["src/a.py"])
    p = k.push("@carol", g, {"src/a.py": "a = 9\n"})
    k.approve("@alice", p)
    k.no_change("@bob", t, g, "stale tip")                        # src/a.py differs at the tip
    k.no_change("@carol", t, p, "not holder")
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "claimed"


# ---------------------------------------------------------------- bugs, fix tasks, escalation

def test_bug_report_only_first_task_valid():
    k, g = base()
    bug = "ab" * 32
    t1 = k.task("@alice", "bug fix", bug=bug)
    t2 = k.task("@bob", "dup", bug=bug)
    t3 = k.task("@t1", "tester", bug="cd" * 32)
    chain, _ = k.run()
    assert chain.tasks[t1.id].bug == bug and t2.id not in chain.tasks and t3.id not in chain.tasks
    k.comment("@carol", "ef" * 32, "not a bug", verdict="reject")
    chain, events = k.run()
    assert events_of(events, "commented")[0].data["verdict"] == "reject"


def red(k, g):
    l1 = k.push("@alice", g, {"src/c.py": "c\n"})
    l2 = k.push("@bob", l1, {"src/d.py": "d\n"})
    chain, _ = k.run()
    k.test_tip(l1.id, l1.tree, "pass")
    k.test_tip(l2.id, l2.tree, "fail")
    chain, _ = k.run()
    return l1, l2, chain


def test_fix_task_offer_window():
    k, g = base(test="command pytest\n", config="fix-offer 5m\n")
    l1, l2, chain = red(k, g)
    fix = chain.tasks[fix_task_id(l2.id)]
    assert fix.offered_to == "@bob" and fix.requester == "@nelson" and fix.alias == "T1"
    c1 = k.claim("@carol", fix.id)
    chain, _ = k.run()
    assert chain.claims[c1.id].status == "void"
    k.tick(5 * 60_000)
    c2 = k.claim("@carol", fix.id)
    chain, _ = k.run()
    assert chain.claims[c2.id].status == "active"


def test_fix_task_cited_author_wins_during_offer():
    k, g = base(test="command pytest\n")
    l1, l2, chain = red(k, g)
    c = k.claim("@bob", fix_task_id(l2.id))
    chain, _ = k.run()
    assert chain.claims[c.id].status == "active"


def test_lease_thrash_and_comment_escalate():
    k, g = base(config="lease 1m\nlease-thrash 2\n")
    t = k.task("@carol", "x")
    k.claim("@alice", t)
    k.tick(61_000)
    k.claim("@bob", t)
    chain, events = k.advance(61_000)
    esc = [e for e in events_of(events, "escalated") if e.data["reason"] == "lease-thrash"]
    assert len(esc) == 1 and esc[0].concerns == ("@nelson",)
    k.comment("@carol", t, "out of budget", task=t.id, escalate="budget")
    chain, events = k.run()
    assert [e.data["reason"] for e in events_of(events, "escalated")][-1] == "budget"


def test_aliases_function():
    def ts(i, parent, at):
        return TaskState(i, "", "subtask" if parent else "task", "", "", None, None, parent, at, (), (), (), (),
                         None, "open", None, (), None, None, None, None, ())
    tasks = [ts("b", None, 2), ts("a", None, 1), ts("c", "a", 5), ts("d", "a", 3)]
    assert dict(aliases(tasks)) == {"a": "T1", "b": "T2", "d": "T1.1", "c": "T1.2"}


# ---------------------------------------------------------------- done by quiescence (§16.6)
#
# The gud1-cc re-run (evals/v1/runs/20261020-v1-fixes-report.md, "Still unresolved"): the work on an unsplit
# T1 landed, but nobody holding T1 declared it done, so a finished swarm left T1 open for good. A root task
# without subtasks is now also work-done once a link carries it and nothing (no claim, no open push, no
# activity) has happened on it for `task-quiet`.

Q = 5 * 60_000
QUIET = "lease 10m\ntask-quiet 5m\n"


def quiet_base(config: str = QUIET):
    k, g = base(test="command pytest\n", config=config)
    t = k.task("@nelson", "site", cmds=["python -m pytest -q"])
    return k, g, t


def at(k, when: int):
    """Replay with the log head (a tick) at exactly `when`."""
    k.now = when
    return k.run()


def accept_tip(k, chain, t, result: str = "pass"):
    return k.test("accept", t, chain.tip.tree_hash, result, link=chain.tip.state)


def test_quiet_done_when_others_landed_the_work_and_the_holder_released():
    """acme-site-shared re-run: @a2 claimed T1 for two pages, others pushed the rest with Gud-Task = T1, and
    @a2 released without done. Work is done `task-quiet` after the last activity (the release)."""
    k, g, t = quiet_base()
    c = k.claim("@alice", t, ["index.html"])
    p1 = k.push("@bob", g, {"nav.html": "nav\n"}, task=t.id)
    p2 = k.push("@carol", g, {"style.css": "css\n"}, task=t.id)
    rel = k.claim("@alice", t, ["index.html"], revises=c, release=True)     # "my part", no Gud-Done
    chain, _ = k.run()
    assert status(chain, p1) == "linked" and status(chain, p2) == "linked"
    task = chain.tasks[t.id]
    assert task.status == "open" and task.claim is None and task.quiet_until == rel.logged_at + Q
    r = Replayer(k.store)
    assert r.feed(k.emails, k.entries(), k.now).next_wakeup == rel.logged_at + Q   # the host ticks then
    chain, events = at(k, rel.logged_at + Q - 1)
    assert chain.tasks[t.id].status == "open" and not events_of(events, "task.ready")
    chain, events = at(k, rel.logged_at + Q)
    task = chain.tasks[t.id]
    assert task.status == "work_done" and task.work_done_at == rel.logged_at + Q
    assert task.work_done_by == "quiet" and task.work_done_link == chain.tip.state
    assert all(link.linked_at < task.work_done_at for link in chain.links)   # only after the work landed
    ready = events_of(events, "task.ready")
    assert ready and ready[-1].at == rel.logged_at + Q and ready[-1].data["needs"] == "criteria"
    accept = accept_tip(k, chain, t)
    chain, events = k.run()
    task = chain.tasks[t.id]
    assert task.status == "done" and task.done_at == accept.logged_at
    assert {"@alice", "@bob", "@carol"} <= set(events_of(events, "task.done")[-1].concerns)


def test_quiet_done_when_the_holder_claimed_narrowly_and_stopped():
    """refactor-guard / acme re-run: the holder claimed its own files, pushed them, and stopped without a
    declaration while another agent landed the rest. With the default `task-quiet` (= the lease), work is done
    one lease after the last link, after the holder's claim has lapsed."""
    k, g = base(test="command pytest\n")
    t = k.task("@nelson", "refactor", cmds=["python -m pytest -q"])
    c = k.claim("@alice", t, ["src/c.py"])
    mine = k.push("@alice", g, {"src/c.py": "c\n"}, task=t.id)
    k.tick(60_000)
    other = k.push("@bob", g, {"src/d.py": "d\n"}, task=t.id)
    chain, _ = k.run()
    last = link_of(chain, other).linked_at
    assert chain.claims[c.id].lapses_at == mine.logged_at + L
    assert chain.tasks[t.id].status == "claimed" and chain.tasks[t.id].quiet_until is None   # held by the claim
    chain, events = at(k, mine.logged_at + L)
    assert chain.claims[c.id].status == "lapsed" and chain.tasks[t.id].status == "open"
    assert chain.tasks[t.id].quiet_until == last + L
    chain, _ = at(k, last + L - 1)
    assert chain.tasks[t.id].status == "open"
    chain, events = at(k, last + L)
    task = chain.tasks[t.id]
    assert task.status == "work_done" and task.work_done_at == last + L and task.work_done_by == "quiet"
    accept_tip(k, chain, t)
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "done"


def test_quiet_done_at_the_lapse_when_the_lease_outlasts_task_quiet():
    """The holder claimed `**`, landed everything and stopped. `task-quiet` (5m) passes while its claim is
    still active; work is done exactly when the lease (10m) lapses."""
    k, g, t = quiet_base()
    c = k.claim("@alice", t, ["**"])
    p = k.push("@alice", g, {"index.html": "home\n"}, task=t.id)
    chain, _ = k.run()
    lapse = chain.claims[c.id].lapses_at
    assert lapse == p.logged_at + 10 * 60_000
    chain, _ = at(k, p.logged_at + Q + 60_000)
    assert chain.tasks[t.id].status == "claimed"
    k.now = lapse + 30_000                                    # the next tick comes later: replay times it anyway
    k.tick_entry()
    r = Replayer(k.store)
    delta = r.feed(k.emails, k.entries(), k.now)
    task = r.chain().tasks[t.id]
    assert task.status == "work_done" and task.work_done_at == lapse
    lapsed, ready = events_of(delta.events, "claim.lapsed"), events_of(delta.events, "task.ready")
    assert lapsed[0].at == lapse and ready[0].at == lapse


def test_activity_inside_task_quiet_resets_the_timer():
    k, g, t = quiet_base(QUIET + "window 20m\n")             # a consent window that outlasts task-quiet
    p = k.push("@bob", g, {"nav.html": "nav\n"}, task=t.id)                 # nobody ever claimed T1
    chain, _ = k.run()
    landed = link_of(chain, p).linked_at
    assert chain.tasks[t.id].quiet_until == landed + Q
    k.now = landed + 4 * 60_000
    k.comment("@carol", t, "is the footer missing?")                          # no Gud-Task: not activity
    chain, _ = k.run()
    assert chain.tasks[t.id].quiet_until == landed + Q
    talk = k.comment("@carol", t, "checking the footer", task=t.id)
    chain, _ = at(k, landed + Q)
    assert chain.tasks[t.id].status == "open" and chain.tasks[t.id].quiet_until == talk.logged_at + Q
    chain, _ = at(k, talk.logged_at + Q)
    assert chain.tasks[t.id].status == "work_done" and chain.tasks[t.id].work_done_at == talk.logged_at + Q
    # before acceptance, a claim is activity too: someone took T1 up again, so its work is no longer done
    c = k.claim("@carol", t, ["footer.html"])
    chain, _ = k.run()
    task = chain.tasks[t.id]
    assert chain.claims[c.id].status == "active" and task.status == "claimed" and task.work_done_at is None
    # a push carrying T1 holds quiescence back while it is open, however long
    k.claim("@carol", t, ["footer.html"], revises=c, release=True)
    chain, _ = k.run()
    pending = k.push("@bob", link_of(chain, p).state, {"src/a.py": "a = 5\n"}, task=t.id)   # @alice's line
    chain, _ = at(k, pending.logged_at + Q + 1)
    assert status(chain, pending) == "window" and chain.tasks[t.id].status == "open"
    veto = k.veto("@nelson", pending)
    chain, _ = k.run()
    assert status(chain, pending) == "vetoed"
    assert chain.tasks[t.id].status == "work_done" and chain.tasks[t.id].work_done_at == veto.logged_at


def test_red_tip_blocks_done_by_quiescence():
    k, g, t = quiet_base()
    p = k.push("@bob", g, {"nav.html": "nav\n"}, task=t.id)
    chain, _ = k.run()
    l1 = link_of(chain, p)
    k.test_tip(l1.state, k.tree(p), "fail")
    chain, _ = at(k, l1.linked_at + Q)
    task = chain.tasks[t.id]
    assert chain.red is not None and chain.red.red
    assert task.status == "work_done" and task.work_done_at == l1.linked_at + Q   # work done, but not done
    accept_tip(k, chain, t)                                                   # a pass while red does not count
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "work_done"
    fix = k.push("@carol", l1.state, {"fix.txt": "fixed\n"})
    chain, _ = k.run()
    l2 = link_of(chain, fix)
    k.test_tip(l2.state, k.tree(fix), "pass")
    chain, _ = k.run()
    assert not chain.red.red and chain.tasks[t.id].status == "work_done"
    accept = accept_tip(k, chain, t)
    chain, _ = k.run()
    task = chain.tasks[t.id]
    assert task.status == "done" and task.done_at == accept.logged_at and task.done_link == l2.state


def test_declaration_fast_path_is_unchanged():
    k, g, t = quiet_base()
    c = k.claim("@alice", t, ["**"])
    p = k.push("@alice", g, {"index.html": "home\n"}, task=t.id)
    decl = k.claim("@alice", t, ["**"], revises=c, done=True)
    chain, events = k.run()
    task = chain.tasks[t.id]
    assert task.status == "work_done" and task.work_done_at == decl.logged_at and task.work_done_by == "declared"
    assert task.quiet_until is None
    accept = accept_tip(k, chain, t)
    chain, _ = k.run()
    assert chain.tasks[t.id].status == "done" and chain.tasks[t.id].done_at == accept.logged_at
    assert chain.tasks[t.id].done_at < link_of(chain, p).linked_at + Q
    # a holder that keeps its claim alive is never overtaken by quiescence
    t2 = k.task("@nelson", "two", cmds=["true"])
    c2 = k.claim("@alice", t2, ["src/**"])
    k.push("@alice", chain.tip.state, {"src/z.py": "z\n"}, task=t2.id)
    for _ in range(4):
        k.tick(4 * 60_000)
        k.comment("@alice", c2, "still on it", task=t2.id)
    chain, _ = k.run()
    assert chain.tasks[t2.id].status == "claimed" and chain.claims[c2.id].status == "active"


def test_quiet_rule_skips_split_and_unlinked_tasks():
    k, g, t = quiet_base()
    bare = k.task("@nelson", "nobody touched it")
    c = k.claim("@alice", t, ["**"])
    s = k.subtask("@alice", t, "part", files=["a.html"])
    k.claim("@alice", t, ["**"], revises=c, release=True)
    k.claim("@bob", s, ["a.html"])
    k.push("@carol", g, {"b.html": "b\n"}, task=t.id)              # a link carrying the split parent
    chain, _ = at(k, k.now + 3 * Q)
    assert chain.tasks[bare.id].status == "open" and chain.tasks[bare.id].quiet_until is None
    assert chain.tasks[t.id].status == "open" and chain.tasks[t.id].quiet_until is None   # follows its subtask
    assert chain.tasks[s.id].status == "open"
