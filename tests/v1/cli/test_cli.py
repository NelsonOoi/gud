"""`gud` smoke tests: every command runs as a subprocess with `--json` over the fake transport
(one shared mail root, one GUD_HOME per member), twice: in local host mode, and against the real
host (`gud host serve`, gud.host) as a separate process."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REPO = "repo@example.org"


def gud(home: Path, *args: str, cwd: Path | None = None, code: int = 0, handle: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("GUD_HOME", "GUD_HANDLE", "GUD_TRANSPORT")}
    env["PYTHONPATH"] = str(ROOT)
    env["GUD_WAIT"] = "5"
    Path(home).mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "gud.cli", "--home", str(home), "--json"]
    if handle:
        argv += ["--handle", handle]
    proc = subprocess.run(argv + list(args), cwd=cwd or home, capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == code, f"gud {' '.join(args)} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert lines, f"no JSON output from gud {' '.join(args)}: {proc.stderr}"
    return json.loads(lines[-1])


@dataclass
class Repo:
    tmp: Path
    mail: Path
    homes: dict
    dirs: dict
    host_url: str
    mode: str = "local"

    def run(self, who: str, *args: str, code: int = 0, cwd: Path | None = None) -> dict:
        return gud(self.homes[who], *args, cwd=cwd or self.dirs.get(who) or self.homes[who], code=code)


@pytest.fixture(scope="module", params=["local", "served"])
def repo(request, tmp_path_factory) -> Repo:
    from tests.v1 import procs
    tmp = tmp_path_factory.mktemp(f"cli-{request.param}")
    mail = tmp / "mail"
    init_args: list[str] = []
    if request.param == "served":
        hoster = tmp / "homes" / "hoster"
        made = gud(hoster, "host", "init", "--repo", REPO, "--transport", "fake", "--set", f"root={mail}",
                   "--dir", str(tmp / "hostdir"), handle="@hoster")
        server, url = procs.serve(hoster, tmp / "hostdir", tmp / "serve.log")
        request.addfinalizer(server.stop)
        assert made["host_key"].startswith("ed25519:")
        init_args = ["--host-url", url]
    homes = {who: tmp / "homes" / who for who in ("nelson", "alice", "bob")}
    pubs = {}
    for who in homes:
        out = gud(homes[who], "keys", "new", "--address", f"{who}@example.org", "--transport", "fake",
                  "--set", f"root={mail}", handle=f"@{who}")
        assert out["created"] and out["public_key"].startswith("ed25519:") and out["transport"] == "fake"
        pubs[who] = out["public_key"]
    roster = tmp / "roster"
    roster.write_text(f"@nelson human nelson@example.org\n"
                      f"@alice agent alice@example.org {pubs['alice']} @nelson\n"
                      f"@bob agent bob@example.org {pubs['bob']} @nelson\n"
                      f"@host host {REPO}\n")
    wn = tmp / "wn"
    wn.mkdir()
    (wn / "README").write_text("hello\nworld\n")
    (wn / "app.py").write_text("x = 1\ny = 2\n")
    out = gud(homes["nelson"], "init", "--repo", REPO, "--member-file", str(roster), *init_args, cwd=wn)
    assert out["files"] == 7 and out["dir"] == str(wn.resolve())      # incl. the default .gudignore
    assert out["host_url"].startswith("local:" if request.param == "local" else "http://")
    host_url = out["host_url"]
    dirs = {"nelson": wn}
    for who in ("alice", "bob"):
        res = gud(homes[who], "clone", host_url, str(tmp / f"w{who}"))
        assert res["base"] == out["genesis"] and res["from"] == "genesis"
        dirs[who] = tmp / f"w{who}"
    return Repo(tmp, mail, homes, dirs, host_url, request.param)


def test_keys_show(repo: Repo):
    out = repo.run("alice", "keys", "show")
    assert out["handle"] == "@alice" and out["matches_tip"] and out["member"]["role"] == "agent"


def test_status_diff_push_log_blame_show(repo: Repo):
    w = repo.dirs["alice"]
    (w / "notes.txt").write_text("alice's notes\n")
    st = repo.run("alice", "status")
    assert st["added"] == ["notes.txt"] and not st["clean"]
    assert repo.run("alice", "status", "README")["added"] == []
    d = repo.run("alice", "diff")
    assert d["files"] == ["notes.txt"] and "+alice's notes" in d["diff"]
    p = repo.run("alice", "push", "-m", "add notes")
    assert p["status"] == "linked" and p["ops"] == 1 and p["base"] == p["push"]
    assert repo.run("alice", "status")["clean"]
    log = repo.run("alice", "log", "-n", "2")
    assert log["links"][0]["push"] == p["push"] and log["links"][0]["intent"] == "add notes"
    blame = repo.run("alice", "blame", "notes.txt")
    assert blame["lines"] == [{"line": 1, "owner": "@alice", "seq": log["links"][0]["seq"], "text": "alice's notes\n"}]
    shown = repo.run("bob", "show", p["push"][:12])
    assert shown["type"] == "link" and shown["link"]["author"] == "@alice"
    synced = repo.run("bob", "sync")
    assert synced["ok"] and synced["written"] == ["notes.txt"]
    assert (repo.dirs["bob"] / "notes.txt").read_text() == "alice's notes\n"


def test_review_and_votes(repo: Repo):
    w = repo.dirs["alice"]
    repo.run("alice", "sync")
    (w / "app.py").write_text("x = 10\ny = 2\n")            # rewrites @nelson's line: @nelson is asked
    p = repo.run("alice", "push", "-m", "x is ten")
    assert p["status"] == "window" and p["forecast"]["askers"] == ["@nelson"]
    review = repo.run("nelson", "review")
    item = next(i for i in review["items"] if i["push"] == p["push"])
    assert item["type"] == "review" and item["why"] == ["line"] and item["files"] == ["app.py"]
    held = repo.run("nelson", "hold", p["push"][:12], "-m", "looking")
    assert held["kind"] == "hold" and held["logged"]
    c = repo.run("nelson", "comment", p["push"][:12], "-m", "nice")
    assert c["kind"] == "comment" and c["Gud-Target"] == p["push"]
    a = repo.run("nelson", "approve", p["push"][:12], "-m", "ok")
    assert a["kind"] == "approve" and a["logged"]
    shown = repo.run("alice", "show", p["push"])
    assert shown["type"] == "link"
    assert not any(i.get("push") == p["push"] for i in repo.run("nelson", "review")["items"])

    # an objection with an evidence test file from the working copy, and a veto
    repo.run("alice", "sync")
    (w / "app.py").write_text("x = 10\ny = 20\n")
    q = repo.run("alice", "push", "-m", "y is twenty")
    test_file = repo.dirs["nelson"] / "tests" / "test_y.py"
    test_file.parent.mkdir(exist_ok=True)
    test_file.write_text("from app import y\n\ndef test_y():\n    assert y == 2\n")
    o = repo.run("nelson", "object", q["push"][:12], "-m", "y must stay 2", "--test", str(test_file))
    assert o["kind"] == "object" and o["logged"]
    obj = repo.run("alice", "show", o["id"])
    assert obj["type"] == "message" and obj["kind"] == "object" and obj["text"] == "y must stay 2"
    prop = repo.run("alice", "show", q["push"])
    assert prop["type"] == "proposal" and prop["proposal"]["objections"][0]["evidence"]["path"] == "tests/test_y.py"
    mine = repo.run("alice", "review")
    assert any(i["type"] == "objection" and i["push"] == q["push"] for i in mine["items"])
    v = repo.run("nelson", "veto", q["push"][:12], "-m", "no")
    assert v["kind"] == "object"
    assert repo.run("alice", "show", q["push"])["proposal"]["status"] == "vetoed"
    # put the working copy back on the tip for the next tests
    (w / "app.py").write_text("x = 10\ny = 2\n")
    assert repo.run("alice", "sync")["ok"]


def test_sync_conflict_and_refused_push(repo: Repo):
    for who in ("alice", "bob"):
        repo.run(who, "sync")
    (repo.dirs["alice"] / "README").write_text("HELLO\nworld\n")
    p = repo.run("alice", "push", "-m", "shout")
    repo.run("nelson", "approve", p["push"][:12])
    assert repo.run("alice", "show", p["push"])["type"] == "link"
    (repo.dirs["bob"] / "README").write_text("Hello!\nworld\n")
    refused = repo.run("bob", "push", "-m", "polite", code=1)
    assert "gud sync" in refused["error"]
    res = repo.run("bob", "sync", code=1)
    assert not res["ok"] and res["conflicts"][0].startswith("README: ") and res["markers"]
    assert (repo.dirs["bob"] / "README").read_text() == "Hello!\nworld\n"
    st = repo.run("bob", "status")
    assert st["conflicts"] == res["conflicts"] and st["behind"] >= 1
    (repo.dirs["bob"] / "README").write_text("HELLO\nworld\n")
    assert repo.run("bob", "sync")["ok"]


def test_secret_scan_cli(repo: Repo):
    repo.run("alice", "sync")
    (repo.dirs["alice"] / "creds.txt").write_text("AKIAIOSFODNN7EXAMPLE\n")
    out = repo.run("alice", "push", "-m", "creds", code=1)
    assert out["secrets"] == [{"path": "creds.txt", "line": 1, "pattern": "aws-access-key-id"}]
    ok = repo.run("alice", "push", "-m", "example creds", "--allow-secret", "creds.txt")
    assert ok["status"] == "linked"


def test_tasks_and_claims(repo: Repo):
    t = repo.run("alice", "task", "new", "Add a footer", "-m", "on every page", "--accept-cmd", "true")
    assert t["kind"] == "task" and t["Gud-Title"] == "Add a footer"
    listed = repo.run("bob", "tasks")
    task = next(x for x in listed["tasks"] if x["id"] == t["id"])
    alias = task["alias"]
    assert task["status"] == "open" and repo.run("bob", "task", "list", "--all")["tasks"]
    shown = repo.run("bob", "task", "show", alias)
    assert shown["type"] == "task" and shown["task"]["description"] == "on every page"
    claim = repo.run("bob", "claim", alias, "--files", "site/**")
    assert claim["kind"] == "claim" and claim["Gud-Files"] == "site/**"
    shown = repo.run("bob", "task", "show", alias)
    assert shown["task"]["status"] == "claimed" and shown["claims"][0]["holder"] == "@bob"
    upd = repo.run("bob", "claim", "update", claim["id"][:12], "--files", "site/footer/**")
    assert upd["Gud-Revises"] == claim["id"]
    sub = repo.run("bob", "subtask", alias, "Footer markup", "--files", "site/footer/**")
    assert sub["kind"] == "subtask"
    assert any(x["kind"] == "subtask" for x in repo.run("bob", "tasks")["tasks"])
    nc = repo.run("bob", "no-change", alias, "-m", "already there")
    assert nc["kind"] == "no-change"
    rel = repo.run("bob", "release", claim["id"][:12])
    assert rel["Gud-Release"] == "yes"
    shown = repo.run("bob", "show", claim["id"])
    assert shown["type"] == "claim"
    # §16.6 done declarations: `claim update --done` and `release --done`
    t2 = repo.run("alice", "task", "new", "Add a header", "-m", "on every page")
    t3 = repo.run("alice", "task", "new", "Add a sidebar", "-m", "on every page")
    a2 = next(x["alias"] for x in repo.run("bob", "tasks")["tasks"] if x["id"] == t2["id"])
    a3 = next(x["alias"] for x in repo.run("bob", "tasks")["tasks"] if x["id"] == t3["id"])
    repo.run("bob", "claim", a2, "--files", "site/header/**")
    done = repo.run("bob", "claim", "update", a2, "--done")
    assert done["Gud-Done"] == "yes" and done["Gud-Files"] == "site/header/**"
    assert repo.run("bob", "task", "show", a2)["task"]["status"] == "work_done"     # requester @nelson accepts
    repo.run("bob", "claim", a3, "--files", "site/side/**")
    rd = repo.run("bob", "release", a3, "--done")
    assert rd["Gud-Release"] == "yes" and rd["Gud-Done"] == "yes"


def test_verify_cli(repo: Repo):
    out = repo.run("bob", "verify")
    assert out["ok"] and out["differences"] == []
    assert repo.run("bob", "verify", "--from-genesis")["ok"]
    # outside a working copy it verifies the cache alone
    assert gud(repo.homes["nelson"], "verify", cwd=repo.tmp)["ok"]


def test_revert_and_checkpoint(repo: Repo):
    repo.run("alice", "sync")
    (repo.dirs["alice"] / "tmp.txt").write_text("temporary\n")
    p = repo.run("alice", "push", "-m", "temp")
    r = repo.run("alice", "revert", p["push"][:12], "-m", "undo temp")
    assert r["kind"] == "revert" and r["Gud-Revert"] == p["push"]
    cp = repo.run("nelson", "checkpoint")
    assert cp["kind"] == "checkpoint" and cp["Gud-Snapshot"]


def test_join_admit_clone(repo: Repo):
    home = repo.tmp / "homes" / "carol"
    out = gud(home, "join", repo.host_url, "--handle", "@carol", "--address", "carol@example.org", "--role", "agent",
              "--sponsor", "@nelson", "--reason", "I can help", "--transport", "fake", "--set", f"root={repo.mail}")
    assert out["handle"] == "@carol" and out["repo"] == REPO
    admit = repo.run("nelson", "admit", out["join"][:12])
    assert admit["member"]["handle"] == "@carol"
    assert repo.run("nelson", "show", admit["push"])["type"] == "link"
    cloned = gud(home, "clone", repo.host_url, str(repo.tmp / "wcarol"))
    assert cloned["base"] == cloned["tip"]
    assert gud(home, "keys", "show")["matches_tip"]


def test_doctor(repo: Repo):
    out = repo.run("alice", "doctor", "--timeout", "10")
    names = {c["name"]: c["ok"] for c in out["checks"]}
    assert out["ok"] and names == {"key": True, "transport": True, "mirror": True, "log": True, "membership": True}
    assert out["proposed"]["window_ms"] >= 2 * out["proposed"]["grace_ms"]


def test_host_init_and_trust(repo: Repo):
    home = repo.tmp / "homes" / "hoster"
    out = gud(home, "host", "init", "--repo", "other@example.org", "--transport", "fake", "--set",
              f"root={repo.tmp / 'mail2'}", handle="@hoster")
    assert out["host_url"].startswith("local:") and out["host_key"].startswith("ed25519:")
    assert out["probe"]["transport"] == "fake"
    trusted = repo.run("bob", "host", "trust", out["host_key"])
    assert out["host_key"] in trusted["trusted"]
    assert repo.run("bob", "verify")["ok"]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_import_from_git_and_mirror(repo: Repo, tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, env=env)
    (src / "hello.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "first"], check=True, env=env)
    home = tmp_path / "home"
    key = gud(home, "keys", "new", "--address", "dev@example.org", "--transport", "fake", "--set",
              f"root={tmp_path / 'mail'}", handle="@dev")
    roster = tmp_path / "roster"
    roster.write_text("@dev human dev@example.org\n@host host git-repo@example.org\n")
    out = gud(home, "init", "--repo", "git-repo@example.org", "--member-file", str(roster), "--from-git", str(src),
              str(tmp_path / "wc"))
    assert "gud import: git main at commit" in gud(home, "log")["links"][0]["intent"]
    assert (tmp_path / "wc" / "hello.py").read_text() == "print('hi')\n" and key["handle"] == "@dev"
    remote = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    m = gud(home, "mirror", "git", str(remote), cwd=tmp_path / "wc")
    assert m["pushed"] == 1 and out["genesis"]
    # `gud import` is the same as `init --from-git`
    home2 = tmp_path / "home2"
    gud(home2, "keys", "new", "--address", "dev@example.org", "--transport", "fake", "--set",
        f"root={tmp_path / 'mail3'}", handle="@dev")
    roster.write_text("@dev human dev@example.org\n@host host git-repo2@example.org\n")
    imp = gud(home2, "import", str(src), "--repo", "git-repo2@example.org", "--roster", str(roster))
    assert imp["dir"] is None and imp["files"] == 5


@pytest.mark.parametrize("command,module", [("agent", "gud.agent.runtime"), ("mcp", "gud.mcp")])
def test_agent_and_mcp_get_the_globals(tmp_path: Path, monkeypatch, command: str, module: str):
    """`gud agent` / `gud mcp` run the module's main with the global --home/--handle (and --wc
    inside a working copy, --json for mcp) followed by the command's own options."""
    from gud import cli
    seen: list = []
    mod = importlib.import_module(module)
    monkeypatch.setattr(mod, "main", lambda argv: seen.append(argv) or 0)
    wc = tmp_path / "wc"
    (wc / ".gud").mkdir(parents=True)
    (wc / ".gud" / "BASE").write_text("0" * 64 + "\n")
    code = cli.main(["--home", str(tmp_path / "h"), "--handle", "alice", "--json", "-C", str(wc), command,
                     "--model", "m", "--listen", ":9"])
    assert code == 0
    expected = ["--home", str((tmp_path / "h").resolve()), "--handle", "@alice", "--wc", str(wc.resolve())]
    expected += (["--json"] if command == "mcp" else []) + ["--model", "m", "--listen", ":9"]
    assert seen == [expected]
    with pytest.raises(SystemExit):
        cli.main(["status", "--bogus"])                 # other commands still refuse unknown options


def test_errors_are_json(repo: Repo):
    out = repo.run("alice", "show", "zzzz", code=2)
    assert "cannot resolve" in out["error"]
    out = gud(repo.tmp / "homes" / "alice", "status", cwd=repo.tmp, code=2)
    assert "not inside a working copy" in out["error"]


def test_keys_rotate_and_promote(repo: Repo):
    old = repo.run("bob", "keys", "show")["public_key"]
    rot = repo.run("bob", "keys", "rotate")
    assert rot["public_key"] != old
    repo.run("nelson", "approve", rot["push"][:12])          # members rule: a human approves
    assert repo.run("nelson", "show", rot["push"])["type"] == "link"
    prom = repo.run("bob", "keys", "promote")
    assert prom["public_key"] == rot["public_key"]
    assert repo.run("bob", "keys", "show")["matches_tip"]


def test_tester_audit_and_export(repo: Repo):
    """The former stubs run the real modules: tester (one pass, as a member with a transport),
    audit (exit status 0 on an honest host) and export-archive (verifies)."""
    t = repo.run("alice", "tester", "--once")
    assert t["handle"] == "@alice" and t["sent"] == []            # no .gud/test command: nothing to run
    a = repo.run("bob", "audit", "--evidence", str(repo.tmp / "audit.json"))
    assert a["ok"] and a["exit_status"] == 0 and a["log_ok"] and a["sync_error"] is None
    assert json.loads((repo.tmp / "audit.json").read_text())["ok"]
    out = repo.run("bob", "export-archive", str(repo.tmp / f"archive-{repo.mode}"))
    assert out["missing"] == [] and out["messages"] >= 1
    from gud.ops.archive import verify_archive
    assert verify_archive(Path(out["archive"])).ok

