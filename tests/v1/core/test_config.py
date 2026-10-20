import pytest

from gud.core import envelope as E
from gud.core.config import (
    CONFIG, MEMBERS, OWNERS, TEST, ConfigError, Glob, OwnerRule, RepoConfig, RepoRules, TestPolicy, glob_match,
    globs_overlap, parse_config, parse_duration, parse_members, parse_owners, parse_test, render_members, rules_of,
)
from gud.core.objects import MemoryStore, Tree

KEY = E.public_of(bytes(range(32)))
KEY2 = E.public_of(bytes(range(1, 33)))

ROSTER = f"""# handle role address public_key sponsor
@auth-agent agent auth-agent@example.org {KEY} @nelson
@nelson\thuman   Nelson@Example.com - -   # trailing comment
@t1 tester t1@example.org {KEY} @nelson

@host host repo@example.org {KEY2} -
@ui console ui@example.org {KEY} -
""".encode()


# ---------------------------------------------------------------- globs

@pytest.mark.parametrize("pattern, path, ok", [
    ("*.css", "a.css", True),
    ("*.css", "x/a.css", False),
    ("src/**", "src/a/b.py", True),
    ("src/**", "src", True),
    ("src/**", "srcx/a", False),
    ("src/", "src/a", True),
    ("**/x", "x", True),
    ("**/x", "a/b/x", True),
    ("**/x", "a/xx", False),
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/y/b", True),
    ("a/**/b", "a/x/y/c", False),
    ("**", "anything/at/all", True),
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
    ("[ab]x", "bx", True),
    ("[!ab]x", "cx", True),
    ("[!ab]x", "ax", False),
    ("[!a]x", "/x", False),
    ("[a-c]", "b", True),
    ("[a-c]", "d", False),
    ("[]]", "]", True),
    ("\\*", "*", True),
    ("\\*", "a", False),
    ("a\\?", "a?", True),
    ("*", "a/b", False),
    ("a*b", "a/b", False),
    ("é*", "éé", True),
    ("migrations/**", "migrations/0001.sql", True),
])
def test_glob_match(pattern, path, ok):
    assert glob_match(pattern, path) is ok
    assert Glob(pattern).match(path) is ok


@pytest.mark.parametrize("pattern", ["", "[ab", "a\\", "/a", "a//b", "[z-a]"])
def test_bad_globs(pattern):
    with pytest.raises(ConfigError):
        Glob(pattern)


def test_globs_overlap():
    assert globs_overlap(["src/**"], ["src/**"], [])
    assert globs_overlap(["src/**"], ["src/auth/**"], [])          # literal read as path
    assert globs_overlap(["*.py"], ["a.py"], [])
    assert not globs_overlap(["src/**"], ["docs/**"], ["src/a", "docs/b"])
    assert globs_overlap(["*.py"], ["a*"], ["ab.py"])              # via a tree path
    assert not globs_overlap(["*.py"], ["a*"], ["b.py", "ab.txt"])
    assert globs_overlap(["[bad"], ["[bad"], [])
    assert not globs_overlap(["[bad"], ["**"], ["x"])


@pytest.mark.parametrize("text, ms", [("0", 0), ("10", 10_000), ("10s", 10_000), ("250ms", 250), ("10m", 600_000),
                                      ("24h", 86_400_000), ("7d", 604_800_000)])
def test_parse_duration(text, ms):
    assert parse_duration(text) == ms


@pytest.mark.parametrize("text", ["", "s", "1.5s", "-1s", "10 s", "10S", "1w", "ms"])
def test_bad_durations(text):
    with pytest.raises(ConfigError):
        parse_duration(text)


# ---------------------------------------------------------------- members

def test_parse_members():
    members = parse_members(ROSTER)
    assert members.get("@nelson").role == "human" and members.get("@nelson").public_key is None
    assert members.get("@auth-agent").sponsor == "@nelson"
    assert members.by_address("NELSON@example.COM").handle == "@nelson"
    assert members.by_address("Nelson <nelson@example.com>").handle == "@nelson"
    assert members.by_address("nobody@example.com") is None
    assert members.voters() == ("@auth-agent", "@nelson")
    assert members.humans() == ("@nelson",)
    assert members.host().handle == "@host"
    assert members.get("@ui").votes is False
    assert parse_members(render_members(members)) == members


@pytest.mark.parametrize("roster", [
    b"@h host h@x.org KEY -\n@l lead l@x.org KEY -\n",
    b"@h host h@x.org KEY -\n@h agent a@x.org KEY -\n",
    b"@h host h@x.org KEY -\n@a agent H@X.org KEY -\n",
    b"@h host h@x.org KEY -\n@a agent a@x.org - -\n",
    b"@h host h@x.org KEY -\n@a tester a@x.org - -\n",
    b"@h host h@x.org - -\n",
    b"@h host h@x.org KEY -\n@g host g@x.org KEY -\n",
    b"@a agent a@x.org KEY -\n",
    b"@h host h@x.org KEY\n",
    b"@h host h@x.org KEY - extra\n",
    b"h host h@x.org KEY -\n",
    b"@H host h@x.org KEY -\n",
    b"@h host not-an-address KEY -\n",
    b"@h host h@x.org ed25519:short -\n",
    b"@h host h@x.org KEY nelson\n",
    b"@h host h@x.org KEY -\r\n",
    b"\xff",
    b"",
])
def test_bad_members(roster):
    with pytest.raises(ConfigError):
        parse_members(roster.replace(b"KEY", KEY.encode()))


# ---------------------------------------------------------------- owners

def test_parse_owners():
    owners = parse_owners(b"# glob rule owners\nsrc/auth/** explicit @alice @bob\ndocs/** lazy @carol\n"
                          b"src/auth/legacy/** lazy @dave\nmigrations/** quorum @ignored\n")
    assert owners.rule_for("src/auth/x.py") == OwnerRule("src/auth/**", "explicit", ("@alice", "@bob"))
    assert owners.rule_for("src/auth/legacy/y.py").owners == ("@dave",)
    assert owners.rule_for("migrations/1.sql") == OwnerRule("migrations/**", "quorum", ())
    assert owners.rule_for("README") is None
    assert owners.builtin_for(OWNERS) == "quorum" and owners.builtin_for(CONFIG) == "quorum"
    assert owners.builtin_for(MEMBERS) == "members" and owners.builtin_for(TEST) is None
    assert parse_owners(b"").rules == ()


@pytest.mark.parametrize("data", [b"src/** lazy\n", b"src/** explicit\n", b"src/** strict @a\n", b"src/**\n",
                                  b"[x lazy @a\n", b"src/** lazy alice\n"])
def test_bad_owners(data):
    with pytest.raises(ConfigError):
        parse_owners(data)


# ---------------------------------------------------------------- test policy

def test_parse_test_defaults_and_values():
    assert parse_test(None) == TestPolicy(None, None, 600_000, (), 5, 600_000, False, 8, None)
    policy = parse_test(b"command pytest -q  tests/ # run it\nimage python:3.12-slim\ntimeout 2m\n"
                        b"on-paths migrations/**, src/auth/**\non-tip links=3 time=30s\nevery-link on\n"
                        b"stack-max 4\nauto-revert 30m\nfuture-key whatever\n")
    assert policy == TestPolicy("pytest -q  tests/", "python:3.12-slim", 120_000, ("migrations/**", "src/auth/**"),
                                3, 30_000, True, 4, 1_800_000)
    assert parse_test(b"on-tip off\nauto-revert off\n").on_tip_links is None
    assert parse_test(b"on-tip time=1m\n").on_tip_links is None


@pytest.mark.parametrize("data", [b"timeout soon\n", b"every-link yes\n", b"stack-max 0\n", b"stack-max -1\n",
                                  b"on-tip links=0 time=1m\n", b"on-tip links=2 links=3\n", b"on-tip sometimes\n",
                                  b"on-paths [bad\n", b"command\n", b"image a b\n", b"timeout 1m\ntimeout 2m\n",
                                  b"auto-revert never\n"])
def test_bad_test_policy(data):
    with pytest.raises(ConfigError):
        parse_test(data)


# ---------------------------------------------------------------- config

def test_parse_config():
    assert parse_config(None) == RepoConfig()
    cfg = parse_config(b"window 90s\ngrace 45000ms\nskew 5m\nown-links 0\nown-time 2d\nlease 1h\nescalate 30m\n"
                       b"expire 1d\ncarry line\nfix-offer 1m\nlease-thrash 5\ncheckpoint 10\n"
                       b"authserv-id mx.example.net\nunknown-key 1 2 3\n")
    assert cfg == RepoConfig(90_000, 45_000, 300_000, 0, 172_800_000, 3_600_000, 1_800_000, 86_400_000, "line",
                             60_000, 5, 10, "mx.example.net")


@pytest.mark.parametrize("data", [b"window\n", b"window 1 2\n", b"window fast\n", b"carry block\n",
                                  b"checkpoint 0\n", b"lease-thrash x\n", b"window 1s\nwindow 2s\n", b"\xff\n"])
def test_bad_config(data):
    with pytest.raises(ConfigError):
        parse_config(data)


# ---------------------------------------------------------------- rules bundle

def test_rules_of_and_encoding():
    store = MemoryStore()
    tree = Tree.from_files(store, {MEMBERS: ("100644", ROSTER), OWNERS: ("100644", b"docs/** lazy @nelson\n"),
                                   TEST: ("100644", b"command make test\n"), CONFIG: ("100644", b"window 30s\n")})
    rules = rules_of(tree)
    assert rules.config.window == 30_000 and rules.test.command == "make test"
    assert rules.owners.rule_for("docs/a").owners == ("@nelson",)
    data = rules.encode()
    assert RepoRules.decode(data) == rules and RepoRules.decode(data).encode() == data
    assert rules_of(Tree.from_files(store, {MEMBERS: ("100644", ROSTER)})).test == TestPolicy()
    with pytest.raises(ConfigError):
        rules_of(Tree.from_files(store, {"README": ("100644", b"")}))
    with pytest.raises(ConfigError):
        rules_of(Tree.from_files(store, {MEMBERS: ("120000", b"elsewhere")}))
    with pytest.raises(ConfigError):
        rules_of(tree.set(CONFIG, "100644", b"carry sideways\n"))
    with pytest.raises(ConfigError):
        RepoRules.decode(b"{}")
