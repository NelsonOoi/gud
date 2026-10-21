import pytest

from gud.core import envelope as E
from gud.core.config import CONFIG, MEMBERS, OWNERS, TEST, parse_members, rules_of
from gud.core.objects import MemoryStore, Tree
from gud.core.ops import Put, apply_ops, records_to_ops
from gud.core.types import ZERO, Seen
from gud.gitbridge import (
    DEFAULT_CONFIG, DEFAULT_OWNERS, DEFAULT_TEST, GitBridgeError, genesis_from_plan, import_git, parse_roster,
)

from .util import ALICE_KEY, ALICE_PRIV, BOB_KEY, FILES, HOST_KEY, ROSTER, git, make_git_repo, members_bytes

SEEN = Seen(0, "a" * 64)


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "roster.txt"
    path.write_text(ROSTER)
    return path


def test_import_is_byte_exact(tmp_path, roster):
    commit = make_git_repo(tmp_path / "src", FILES, submodule="vendor/lib")
    plan = import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)
    assert plan.source_commit == commit
    assert commit in plan.intent
    for path, (mode, data) in FILES.items():
        assert plan.files[path] == (mode, data), path
    assert set(plan.files) == set(FILES) | {MEMBERS, CONFIG, TEST, OWNERS}
    assert "vendor/lib" not in plan.files
    assert any("submodule vendor/lib" in w for w in plan.warnings)
    assert plan.files[MEMBERS] == ("100644", members_bytes())
    assert plan.files[CONFIG] == ("100644", DEFAULT_CONFIG)
    assert plan.files[TEST] == ("100644", DEFAULT_TEST)
    assert plan.files[OWNERS] == ("100644", DEFAULT_OWNERS)
    rules = rules_of(plan.tree(MemoryStore()))
    assert rules.members.host().public_key == HOST_KEY
    assert rules.test.command is None
    assert rules.config.window == 60_000 and rules.config.carry == "file"


def test_import_tree_hash_matches_git_contents(tmp_path, roster):
    make_git_repo(tmp_path / "src", FILES)
    plan = import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)
    store = MemoryStore()
    expected = Tree.from_files(store, {**FILES, **{p: plan.files[p] for p in (MEMBERS, CONFIG, TEST, OWNERS)}})
    assert plan.tree(store).hash() == expected.hash()


def test_import_bare_repo_and_other_ref(tmp_path, roster):
    make_git_repo(tmp_path / "src", FILES)
    git("checkout", "-q", "-b", "feature", cwd=tmp_path / "src")
    (tmp_path / "src" / "feature.txt").write_bytes(b"feature\n")
    git("add", "feature.txt", cwd=tmp_path / "src")
    git("commit", "-q", "-m", "feature", cwd=tmp_path / "src")
    git("tag", "v1", cwd=tmp_path / "src")
    git("clone", "-q", "--bare", str(tmp_path / "src"), str(tmp_path / "bare.git"))
    main = import_git(tmp_path / "bare.git", ref="main", roster=roster, host_key=HOST_KEY)
    feat = import_git(tmp_path / "bare.git", ref="v1", roster=roster, host_key=HOST_KEY)
    assert "feature.txt" not in main.files
    assert feat.files["feature.txt"] == ("100644", b"feature\n")
    assert feat.ref == "v1" and feat.source_commit in feat.intent


def test_import_keeps_gud_config_but_replaces_members(tmp_path, roster):
    files = dict(FILES)
    files[CONFIG] = ("100644", b"window 90s\n")
    files[MEMBERS] = ("100644", b"# old members\n")
    make_git_repo(tmp_path / "src", files)
    plan = import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)
    assert plan.files[CONFIG] == ("100644", b"window 90s\n")
    assert plan.files[MEMBERS] == ("100644", members_bytes())
    assert any(MEMBERS in w for w in plan.warnings)


def test_import_rejects_unparseable_config_from_git(tmp_path, roster):
    make_git_repo(tmp_path / "src", {**FILES, TEST: ("100644", b"stack-max zero\n")})
    with pytest.raises(GitBridgeError, match="does not parse"):
        import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)


def test_import_skips_reserved_paths(tmp_path, roster):
    make_git_repo(tmp_path / "src", {**FILES, ".gud/BASE": ("100644", b"x\n"),
                                     ".gud/local/handle": ("100644", b"@a\n")})
    plan = import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)
    assert ".gud/BASE" not in plan.files and ".gud/local/handle" not in plan.files
    assert sum("not a valid gud path" in w for w in plan.warnings) == 2


def test_import_errors(tmp_path, roster):
    with pytest.raises(GitBridgeError, match="not a git repository"):
        (tmp_path / "plain").mkdir()
        import_git(tmp_path / "plain", roster=roster, host_key=HOST_KEY)
    make_git_repo(tmp_path / "src", FILES)
    with pytest.raises(GitBridgeError, match="no commit at ref"):
        import_git(tmp_path / "src", ref="nope", roster=roster, host_key=HOST_KEY)
    with pytest.raises(GitBridgeError, match="not the top"):
        import_git(tmp_path / "src" / "a", roster=roster, host_key=HOST_KEY)
    with pytest.raises(GitBridgeError, match="no such directory"):
        import_git(tmp_path / "missing", roster=roster, host_key=HOST_KEY)


def test_import_empty_tree(tmp_path, roster):
    (tmp_path / "src").mkdir()
    git("init", "-q", str(tmp_path / "src"))
    git("commit", "-q", "--allow-empty", "-m", "empty", cwd=tmp_path / "src")
    plan = import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)
    assert set(plan.files) == {MEMBERS, CONFIG, TEST, OWNERS}


# ---------------------------------------------------------------- roster

def test_roster_fields():
    members = parse_members(parse_roster(ROSTER.encode(), HOST_KEY))
    assert members.get("@alice").public_key == ALICE_KEY and members.get("@alice").sponsor is None
    assert members.get("@bob").public_key == BOB_KEY and members.get("@bob").sponsor == "@alice"
    assert members.get("@carol").public_key is None
    assert members.host().public_key == HOST_KEY
    four = parse_members(parse_roster(b"@h host h@x.org\n@d human d@x.org @h\n@e human e@x.org -\n", HOST_KEY))
    assert four.get("@d").sponsor == "@h" and four.get("@d").public_key is None
    assert four.get("@e").sponsor is None


@pytest.mark.parametrize("text, match", [
    ("@h host h@x.org\n@a agent a@x.org\n", "needs a key"),
    ("@a human a@x.org\n", "exactly one host"),
    (f"@h host h@x.org {ALICE_KEY}\n", "host key differs"),
    ("@h host h@x.org\n@a human\n", "expected"),
    ("@h host h@x.org\n@a lead a@x.org\n", "bad role"),
])
def test_roster_errors(text, match):
    with pytest.raises(GitBridgeError, match=match):
        parse_roster(text.encode(), HOST_KEY)


# ---------------------------------------------------------------- genesis

def test_genesis_from_plan(tmp_path, roster):
    make_git_repo(tmp_path / "src", FILES)
    plan = import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)
    store = MemoryStore()
    env, blobs = genesis_from_plan(plan, "@alice", ALICE_PRIV, SEEN, store, 1 << 20, sent=1_700_000_000)
    assert blobs == []
    assert E.validate_kind(env) is None
    assert env.kind == "push" and env.get("Gud-Parent") == ZERO
    assert env.get("Gud-Tree") == plan.tree(store).hash()
    assert env.get("Gud-Seen") == SEEN.render() and env.get("Gud-Sent") == "1700000000"
    assert env.text == plan.intent
    assert E.verify(env, ALICE_KEY)
    assert E.decode(E.encode(env)) == env
    tree = apply_ops(Tree.empty(store), records_to_ops(env.records), None)
    assert tree.to_files() == dict(plan.files)


def test_genesis_large_files_become_blobs(tmp_path, roster):
    big = bytes(range(256)) * 64
    make_git_repo(tmp_path / "src", {**FILES, "big.bin": ("100644", big)})
    plan = import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)
    store = MemoryStore()
    env, blobs = genesis_from_plan(plan, "@alice", ALICE_PRIV, SEEN, store, 4096, sent=1)
    assert blobs == [big]
    refs = [op for op in records_to_ops(env.records) if isinstance(op, Put) and op.data is None]
    assert [op.path for op in refs] == ["big.bin"]

    class Blobs:
        def blob(self, sha):
            return big if sha == E.sha256_hex(big) else None
    assert apply_ops(Tree.empty(store), records_to_ops(env.records), Blobs()).hash() == env.get("Gud-Tree")


@pytest.mark.parametrize("author, priv, match", [
    ("@alice", bytes(range(5, 37)), "key does not match"),
    ("@nobody", ALICE_PRIV, "not a member"),
    ("@host", ALICE_PRIV, "not a member with a push role"),
    ("@carol", ALICE_PRIV, "key does not match"),
])
def test_genesis_author_checks(tmp_path, roster, author, priv, match):
    make_git_repo(tmp_path / "src", FILES)
    plan = import_git(tmp_path / "src", roster=roster, host_key=HOST_KEY)
    with pytest.raises(GitBridgeError, match=match):
        genesis_from_plan(plan, author, priv, SEEN, MemoryStore(), 1 << 20, sent=1)
