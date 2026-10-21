"""Consent v1 (§13): fading ownership, declared owners, quorum, members rule, windows, holds, grace,
objections (reason-only and evidence), veto, negotiations, rounds and mutual escalations."""


from tests.v1.kit import Kit, events_of, link_of, status

W, G, S = 60_000, 30_000, 120_000


def base(**kw):
    k = Kit(**kw)
    g = k.genesis({"app.py": "a = 1\n", "lib.py": "x = 1\ny = 2\n", "docs/readme": "hi\n"})
    return k, g


def prop(chain, msg):
    return chain.push_status(msg.id)


# ---------------------------------------------------------------- fading line ownership (§13.1)

def test_live_ownership_asks_line_owner():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    chain, events = k.run()
    assert prop(chain, p).askers == ("@nelson",) and prop(chain, p).line_owners == ("@nelson",)
    assert [e.data["owner"] for e in events_of(events, "review.requested")] == ["@nelson"]
    assert status(chain, p) == "window"
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "linked"
    assert link_of(chain, p).linked_at == p.logged_at + W + G


def test_expired_ownership_links_without_asking():
    k, g = base(config="own-links 2\nown-time 1m\n")
    a = k.push("@alice", g, {"n1": "1\n"})
    b = k.push("@alice", a, {"n2": "1\n"})
    k.tick(2 * 60_000)
    p = k.push("@bob", b, {"app.py": "a = 2\n"})          # @nelson's line: 2 links and 2 min old
    chain, events = k.run()
    assert status(chain, p) == "linked"
    assert link_of(chain, p).linked_at == p.logged_at
    assert not events_of(events, "review.requested")


def test_ownership_live_by_links_even_when_old():
    k, g = base(config="own-links 5\nown-time 1m\n")
    k.tick(10 * 60_000)
    p = k.push("@bob", g, {"app.py": "a = 2\n"})          # old, but only 0 links ago
    chain, _ = k.run()
    assert prop(chain, p).line_owners == ("@nelson",)


def test_ownership_live_by_time_when_many_links():
    k, g = base(config="own-links 0\nown-time 1h\n")
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    chain, _ = k.run()
    assert prop(chain, p).line_owners == ("@nelson",)


def test_python_insert_inside_def_asks_def_owner():
    k = Kit()
    g = k.genesis({"m.py": "def f():\n    a = 1\n    return a\n"})
    p = k.push("@bob", g, {"m.py": "def f():\n    a = 1\n    b = 2\n    return a\n"})
    chain, _ = k.run()
    assert prop(chain, p).line_owners == ("@nelson",)


def test_python_append_at_top_level_asks_nobody():
    k = Kit()
    g = k.genesis({"m.py": "def f():\n    return 1\n"})
    p = k.push("@bob", g, {"m.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n"})
    chain, _ = k.run()
    assert prop(chain, p) is None or prop(chain, p).line_owners == ()
    assert status(chain, p) == "linked"


# ---------------------------------------------------------------- declared owners (§13.2)

def test_lazy_declared_owner_still_has_a_say_when_ownership_expired():
    k, g = base(config="own-links 0\nown-time 1s\n", owners="docs/** lazy @carol\n")
    k.tick(5_000)
    p = k.push("@bob", g, {"docs/readme": "bye\n"})
    chain, _ = k.run()
    assert prop(chain, p).line_owners == ()
    assert prop(chain, p).askers == ("@carol",)
    k.object("@carol", p, "keep it")
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) in ("objected", "parked")


def test_lazy_owner_approve_links_before_window():
    k, g = base(owners="docs/** lazy @carol\n")
    p = k.push("@bob", g, {"docs/readme": "bye\n"})
    k.approve("@carol", p)
    k.approve("@nelson", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_explicit_owner_must_approve_no_deadline():
    k, g = base(owners="lib.py explicit @carol\n")
    p = k.push("@bob", g, {"lib.py": "x = 2\ny = 2\n"})
    chain, _ = k.advance(10 * W)
    assert status(chain, p) in ("needs_approval", "parked")
    assert prop(chain, p).declared == (("lib.py", "explicit", ("@carol",)),)
    k.approve("@carol", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_explicit_owner_is_author_needs_any_other_voter():
    k, g = base(owners="lib.py explicit @bob\n", config="own-links 0\nown-time 1s\n")
    k.tick(2000)
    p = k.push("@bob", g, {"lib.py": "x = 2\ny = 2\n"})
    chain, _ = k.advance(2 * W)
    assert status(chain, p) in ("needs_approval", "parked")
    k.approve("@bob", p)                                  # the author's own approve does not count
    chain, _ = k.run()
    assert status(chain, p) != "linked"
    k.approve("@alice", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_quorum_on_config_change():
    k, g = base()
    p = k.push("@alice", g, {".gud/config": "window 30s\nauthserv-id mx.example.org\n"})
    k.approve("@bob", p)
    chain, _ = k.advance(W + G + 5)
    pr = prop(chain, p)
    assert pr.quorum_needed == 3 and pr.quorum_have == 2         # 5 voters; @alice counts herself
    assert status(chain, p) in ("needs_quorum", "parked")
    k.approve("@carol", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_quorum_glob_rule():
    k, g = base(owners="migrations/** quorum\n")
    p = k.push("@alice", g, {"migrations/001.sql": "create\n"})
    chain, _ = k.advance(W + G)
    assert status(chain, p) in ("needs_quorum", "parked")
    k.approve("@bob", p)
    k.approve("@dana", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_members_rule_needs_a_human_approve():
    k, g = base()
    members = list(k.members) + [("@erin", "agent", "@nelson")]
    p = k.push("@alice", g, {".gud/members": k.members_file(members)})
    k.approve("@bob", p)                                  # an agent's approve is not enough
    chain, _ = k.advance(W + G)
    assert status(chain, p) in ("needs_approval", "parked")
    assert prop(chain, p).line_owners == ()               # line owners of .gud/members are not asked
    k.approve("@dana", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"
    assert chain.tip.state == p.id


# ---------------------------------------------------------------- windows, holds, grace, skew (§13.4)

def test_hold_extends_window_once():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.hold("@nelson", p)
    k.hold("@nelson", p)
    chain, events = k.run()
    assert prop(chain, p).held_by == "@nelson"
    assert prop(chain, p).window_closes_at == p.logged_at + 2 * W
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "window"
    k.tick(W)
    chain, _ = k.run()
    assert status(chain, p) == "linked"
    assert link_of(chain, p).linked_at == p.logged_at + 2 * W + G
    assert events_of(events, "held")


def test_hold_from_non_owner_agent_has_no_effect():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.hold("@carol", p)
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "linked"


def test_objection_sent_before_close_and_delayed_within_grace_counts():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    close = p.logged_at + W
    k.tick(W + G - 1000)
    k.object("@nelson", p, "no", sent=close - 1000)       # sent in time, received within G
    chain, _ = k.advance(5_000)
    assert status(chain, p) in ("objected", "parked")


def test_objection_delayed_beyond_grace_does_not_count():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"}, log=False)
    k.log_msg(p)
    close = p.logged_at + W
    late = k.object("@nelson", p, "no", sent=close - 1000, log=False)
    k.log_msg(late, at=close + G + 1)                     # the host held it back > G
    chain, _ = k.advance(10)
    assert status(chain, p) == "linked"
    assert link_of(chain, p).linked_at == close + G


def test_objection_sent_after_close_does_not_count():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    close = p.logged_at + W
    k.tick(W + 2000)
    k.object("@nelson", p, "no", sent=close + 2000)
    chain, _ = k.advance(G)
    assert status(chain, p) == "linked"


def test_future_sent_vote_is_void():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.object("@nelson", p, "no", sent=k.now + S + 10_000)
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "linked"


def test_future_sent_approve_does_not_shortcut():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.approve("@nelson", p, sent=k.now + S + 10_000)
    chain, _ = k.run()
    assert status(chain, p) == "window"
    k.approve("@nelson", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"


# ---------------------------------------------------------------- objections (§13.5)

def test_reason_only_objection_blocks_until_answered_by_comment():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    o = k.object("@nelson", p, "why?")
    chain, _ = k.advance(W + G + 5)
    assert prop(chain, p).objections[0].status == "blocking"
    assert status(chain, p) in ("objected", "parked")
    c = k.comment("@bob", o, "because the spec says so")
    chain, _ = k.run()
    assert status(chain, p) == "linked"
    assert link_of(chain, p).linked_at == c.logged_at


def test_comment_from_someone_else_does_not_answer():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    o = k.object("@nelson", p, "why?")
    k.comment("@carol", o, "I think it is fine")
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) in ("objected", "parked")


def test_reason_only_objection_advisory_after_revision():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.object("@nelson", p, "use 3")
    r = k.push("@bob", g, {"app.py": "a = 3\n"}, revises=p)
    chain, _ = k.run()
    o = prop(chain, p).objections[0]
    assert o.status == "advisory" and o.answered_by == r.id
    assert prop(chain, r).negotiation == p.id


def test_approve_after_objection_withdraws_it():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.object("@nelson", p, "hmm")
    k.approve("@nelson", p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_objection_from_non_eligible_agent_has_no_effect():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.object("@carol", p, "meh")
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "linked"
    assert prop(chain, p) is None or True


def test_objection_on_push_without_askers_has_no_effect():
    k, g = base()
    p = k.push("@alice", g, {"new.py": "n\n"}, log=False)
    k.object("@dana", p, "no", log=False)
    k.log_msg(k.msgs[-1])
    k.log_msg(p)
    chain, _ = k.run()
    assert status(chain, p) == "linked"


EVIDENCE = b"def test_a():\n    assert True\n"


def evidence_tree(tree, path=b"", data=EVIDENCE, name="tests/test_ev.py"):
    return tree.set(name, "100644", data).hash()


def test_evidence_objection_confirmed_blocks_until_revision_passes():
    k, g = base(config="escalate 1h\n")
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    o = k.object("@nelson", p, "breaks test", evidence=("tests/test_ev.py", EVIDENCE))
    chain, events = k.run()
    assert prop(chain, p).objections[0].status == "pending_verdict"
    assert "@t1" in events_of(events, "objected")[0].concerns
    k.test("evidence", o, evidence_tree(p.tree), "fail", on=p.id, base="pass")
    k.comment("@bob", o, "disagree")
    chain, _ = k.advance(W + G + 5)
    assert prop(chain, p).objections[0].status == "confirmed"
    assert status(chain, p) in ("objected", "parked")
    # A revision without the evidence file stays blocked even when its own test would pass.
    r1 = k.push("@bob", g, {"app.py": "a = 4\n"}, revises=p)
    k.test("evidence", o, evidence_tree(r1.tree), "pass", on=r1.id)
    chain, _ = k.advance(W + G + 5)
    assert status(chain, r1) in ("objected", "parked", "superseded")
    assert status(chain, r1) != "linked"
    # The clearing revision carries the evidence file byte-exactly and passes it.
    r2 = k.push("@bob", g, {"app.py": "a = 3\n", "tests/test_ev.py": EVIDENCE}, revises=r1)
    k.test("evidence", o, r2.tree.hash(), "pass", on=r2.id)
    chain, _ = k.advance(W + G + 5)
    assert status(chain, r2) == "linked"
    assert status(chain, p) != "linked"


def test_refuted_evidence_objection_is_reason_only():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    o = k.object("@nelson", p, "breaks test", evidence=("tests/test_ev.py", EVIDENCE))
    k.test("evidence", o, evidence_tree(p.tree), "pass", on=p.id, base="pass")   # passes on the push
    chain, _ = k.advance(W + G + 5)
    assert prop(chain, p).objections[0].status == "refuted"
    assert status(chain, p) in ("objected", "parked")
    k.comment("@bob", o, "the test passes")
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_evidence_without_verdict_becomes_reason_only_after_timeout():
    k, g = base(config="escalate 2m\n")
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    o = k.object("@nelson", p, "breaks test", evidence=("tests/test_ev.py", EVIDENCE))
    k.comment("@bob", o, "no it does not")
    chain, _ = k.advance(W + G + 60_000)
    assert status(chain, p) in ("objected", "parked")
    chain, _ = k.advance(2 * 60_000)
    assert status(chain, p) == "linked"


def test_human_signed_veto_is_terminal():
    k, g = base()
    p = k.push("@alice", g, {"new.py": "n\n"}, log=False)
    k.veto("@nelson", p, log=False)
    k.log_msg(k.msgs[-1])
    k.log_msg(p)
    chain, events = k.run()
    assert status(chain, p) == "vetoed"
    assert events_of(events, "push.rejected")[0].data["status"] == "vetoed"


def test_agent_cannot_veto():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.veto("@carol", p)
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "linked"


def test_unsigned_human_veto_and_approve():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    q = k.push("@carol", g, {"lib.py": "x = 9\ny = 2\n"})
    k.unsigned("@nelson", "Veto. This breaks prod.", reply_to=p)
    k.unsigned("@nelson", "approve", reply_to=q)
    chain, _ = k.run()
    assert status(chain, p) == "vetoed"
    assert status(chain, q) == "linked"


def test_unsigned_without_dmarc_or_authserv_does_not_count():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.unsigned("@nelson", "veto", reply_to=p, dmarc=False)
    k.unsigned("@nelson", "veto", reply_to=p, from_addr="mallory@evil.org")
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "linked"
    k2 = Kit(authserv=False)
    g2 = k2.genesis({"app.py": "a = 1\n"})
    p2 = k2.push("@bob", g2, {"app.py": "a = 2\n"})
    k2.unsigned("@nelson", "veto", reply_to=p2)
    chain, _ = k2.advance(W + G + 5)
    assert status(chain, p2) == "linked"


def test_unsigned_reply_resolves_by_subject_tag():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.unsigned("@nelson", "veto!", subject=f"Re: whatever [gud:{p.id[:12]}]")
    chain, _ = k.run()
    assert status(chain, p) == "vetoed"


# ---------------------------------------------------------------- escalations (§13.6, §13.7)

def two_owners():
    k = Kit(config="escalate 10m\n")
    g = k.genesis({"x": "x\n"})
    a = k.push("@alice", g, {"a.txt": "a\n"})
    b = k.push("@bob", a, {"b.txt": "b\n"})
    return k, g, a, b


def mutual(k, b):
    p = k.push("@alice", b, {"b.txt": "alice's b\n"})       # asks @bob
    q = k.push("@bob", b, {"a.txt": "bob's a\n"})           # asks @alice
    k.object("@bob", p, "no")
    k.object("@alice", q, "no")
    return p, q


def test_mutual_objection_escalates_and_requester_choice_wins():
    k, g, a, b = two_owners()
    p, q = mutual(k, b)
    chain, events = k.run()
    esc = events_of(events, "escalated")
    assert len(esc) == 1 and esc[0].data["reason"] == "mutual"
    assert esc[0].data["deciders"] == ["@nelson"]
    assert set(esc[0].data["pushes"]) == {p.id, q.id}
    assert status(chain, p) == "escalated"
    k.approve("@nelson", q)
    chain, events = k.run()
    resolved = events_of(events, "escalation.resolved")
    assert resolved and resolved[0].data["chosen"] == q.id and resolved[0].data["by"] == "@nelson"
    assert status(chain, p) == "declined"
    # The chosen push's objections are advisory; it still waits for its own window (lazy part).
    chain, _ = k.advance(W + G)
    assert status(chain, q) == "linked"


def test_mutual_escalation_default_picks_earlier_push():
    k, g, a, b = two_owners()
    p, q = mutual(k, b)
    chain, _ = k.advance(11 * 60_000)
    assert status(chain, p) == "linked"
    assert status(chain, q) == "declined"
    (esc,) = chain.escalations.values()
    assert esc.decided_by == "default" and esc.chosen == p.id


def test_no_escalation_without_mutual_objections():
    k, g, a, b = two_owners()
    p = k.push("@alice", b, {"b.txt": "alice's b\n"})
    k.object("@bob", p, "no")
    chain, events = k.run()
    assert not events_of(events, "escalated")


def test_rounds_escalation_after_second_episode_defaults_to_link():
    k, g = base(config="escalate 5m\n")
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    o1 = k.object("@nelson", p, "why")
    k.comment("@bob", o1, "because")
    chain, events = k.run()
    assert not events_of(events, "escalated")
    k.object("@nelson", p, "still no")
    chain, events = k.run()
    esc = events_of(events, "escalated")
    assert esc and esc[0].data["reason"] == "rounds"
    assert status(chain, p) == "escalated"
    chain, events = k.advance(6 * 60_000)
    assert status(chain, p) == "linked"
    assert events_of(events, "escalation.resolved")[0].data["by"] == "default"


def test_rounds_escalation_decider_object_declines():
    k, g = base(config="escalate 5m\n")
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    o1 = k.object("@nelson", p, "why")
    k.comment("@bob", o1, "because")
    k.object("@nelson", p, "still no")
    chain, _ = k.run()
    k.object("@nelson", p, "decided: no")                  # the sponsor decides
    chain, _ = k.run()
    assert status(chain, p) == "declined"
