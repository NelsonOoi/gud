"""End to end over the fake transport: `gud host serve`, `gud tester` and every member command as
separate processes sharing one FakeMailbox root (see scenario.py for the flow)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.v1.e2e.scenario import Scenario, Transport
from tests.v1.procs import gud, serve

DOMAIN = "example.org"


def fake_transport(tmp: Path) -> Transport:
    mail = tmp / "mail"

    def sent() -> int:
        return sum(1 for _ in (mail / "boxes").glob("*/*")) if (mail / "boxes").exists() else 0

    return Transport(name="fake", repo=f"repo@{DOMAIN}", address=lambda u: f"{u}@{DOMAIN}",
                     settings=lambda u: {"root": str(mail)}, count_sent=sent)


def test_e2e_fake(tmp_path: Path, capsys):
    sc = Scenario(tmp_path, fake_transport(tmp_path))
    try:
        result = sc.run_all()
    finally:
        sc.close()
    with capsys.disabled():
        print("\ne2e fake:", json.dumps(result, sort_keys=True))


def test_e2e_migration_to_a_new_address(tmp_path: Path):
    """Host migration with a new repo address (integration note 4), all through the CLI: the
    old host signs a handover naming the new key and address, exports; the new host imports and
    serves; members follow, record the new host line, and keep pushing to the new address."""
    t = fake_transport(tmp_path)
    sc = Scenario(tmp_path, t)
    moved = f"moved@{DOMAIN}"
    try:
        hoster = tmp_path / "homes" / "hoster"
        old_dir = tmp_path / "hostdir"
        made = gud(hoster, "host", "init", "--repo", t.repo, *t.args("repo"), "--dir", str(old_dir), handle="@hoster")
        server, url = serve(hoster, old_dir, tmp_path / "logs" / "host-old.log")
        sc.procs.append(server)
        sc.url, sc.host_key = url, made["host_key"]
        from tests.v1.e2e.scenario import Member
        sc.members["human"] = Member("@human", "human", tmp_path / "homes" / "human", tmp_path / "wc-human")
        sc.members["alice"] = Member("@alice", "agent", tmp_path / "homes" / "alice")
        human, alice = sc.members["human"], sc.members["alice"]
        gud(human.home, "keys", "new", "--address", t.address("human"), *t.args("human"), handle="@human")
        roster = tmp_path / "roster"
        roster.write_text(f"@human human {t.address('human')}\n@host host {t.repo}\n")
        sc.seed(human.wc)
        sc.run("human", "init", "--repo", t.repo, "--member-file", str(roster), "--host-url", url)
        j = gud(alice.home, "join", url, "--handle", "@alice", "--address", t.address("alice"), "--sponsor", "@human",
                *t.args("alice"))
        sc.wait(lambda: sc.run("human", "show", j["join"][:16], code=None).get("type") == "message", "join logged")
        admit = sc.run("human", "admit", j["join"][:16])
        sc.wait(lambda: sc.status("human", admit["push"]) == "linked", "admission")
        alice.wc = tmp_path / "wc-alice"
        gud(alice.home, "clone", url, str(alice.wc), handle="@alice", cwd=tmp_path)
        before = sc.run("alice", "log", "-n", "100")["links"]

        # the new host makes its key; the old host stops, signs the handover and exports
        hoster2 = tmp_path / "homes" / "hoster2"
        new_dir = tmp_path / "hostdir-new"
        key = gud(hoster2, "host", "key", "--dir", str(new_dir), handle="@hoster2")["host_key"]
        server.stop()
        h = gud(hoster, "host", "handover", key, "--new-repo", moved, "--dir", str(old_dir), handle="@hoster")
        assert h["repo"] == moved and h["next"] == key
        archive = gud(hoster, "export-archive", "--host", "--dir", str(old_dir), str(tmp_path / "archive"),
                      handle="@hoster")
        assert archive["missing"] == []
        imp = gud(hoster2, "import-archive", archive["archive"], "--dir", str(new_dir), *t.args("x"),
                  handle="@hoster2")
        assert imp["repo"] == moved and imp["host_key"] == key
        server2, url2 = serve(hoster2, new_dir, tmp_path / "logs" / "host-new.log")
        sc.procs.append(server2)
        sc.url, sc.host_key = url2, key

        # members follow; the host line records the new key and address; work goes on
        for who in ("alice", "human"):
            f = sc.run(who, "host", "follow", url2)
            assert f["repo"] == moved and f["repos"] == [t.repo, moved] and f["host_key"] == key
        line = sc.run("human", "host", "line", key, "--address", moved)
        sc.wait(lambda: sc.status("human", line["push"]) == "linked", "the host line push links")
        sc.sync("alice")
        sc.write("alice", "after.txt", "after the move\n")
        p = sc.push("alice", "after the move")
        sc.wait(lambda: sc.status("alice", p["push"]) == "linked", "a push after the move links")
        after = sc.run("human", "log", "-n", "100")["links"]
        assert [l["state"] for l in after[-len(before):]] == [l["state"] for l in before]   # history kept
        boxes = {d.name for d in (tmp_path / "mail" / "boxes").iterdir()}
        assert any("moved" in b for b in boxes), boxes                                     # mail went to moved@
        for who in ("alice", "human"):
            v = sc.run(who, "verify", "--from-genesis")
            assert v["ok"], v
    except BaseException:
        import sys
        sys.stderr.write(sc.logs())
        raise
    finally:
        sc.close()

