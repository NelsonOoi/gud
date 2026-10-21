"""`.gudignore` semantics and the pre-push secret scan (SPEC §22)."""

from __future__ import annotations

import pytest

from gud.wc.ignore import IgnoreRules
from gud.wc.secrets import PATTERNS, scan

RULES = IgnoreRules.parse("""\
# comment
*.log
!keep.log
build/
/top.txt
docs/*.tmp
**/cache/**
a/**/z
\\#hash
trailing\\\x20\x20
[abc].bin
""")


@pytest.mark.parametrize("path,is_dir,expected", [
    ("x.log", False, True),
    ("deep/dir/x.log", False, True),            # no slash: matches at any depth
    ("keep.log", False, False),                 # negation
    ("sub/keep.log", False, False),
    ("build", True, True),
    ("build", False, False),                    # trailing slash: directories only
    ("build/out.o", False, True),               # under an ignored directory
    ("src/build/out.o", False, True),
    ("top.txt", False, True),
    ("sub/top.txt", False, False),              # anchored
    ("docs/a.tmp", False, True),
    ("docs/x/a.tmp", False, False),             # * does not cross /
    ("x/cache/y", False, True),
    ("cache/y", False, True),
    ("a/z", False, True),
    ("a/b/c/z", False, True),
    ("#hash", False, True),
    ("trailing ", False, True),
    ("a.bin", False, True),
    ("d.bin", False, False),
    ("src/main.py", False, False),
])
def test_patterns(path, is_dir, expected):
    assert RULES.ignored(path, is_dir) is expected


@pytest.mark.parametrize("path", [".git", ".git/config", "sub/.GIT/x", ".gud/BASE", ".gud/local", ".gud/local/index.json"])
def test_builtins_always_ignored(path):
    assert IgnoreRules.parse("!.git\n!.gud/BASE\n!.gud/local/\n").ignored(path)
    assert IgnoreRules().ignored(path)


def test_tracked_gud_files_not_ignored():
    assert not IgnoreRules().ignored(".gud/members")
    assert not IgnoreRules().ignored(".gud/config")


def test_excluded_directory_cannot_be_reincluded():
    rules = IgnoreRules.parse("logs/\n!logs/important.txt\n")
    assert rules.ignored("logs/important.txt")
    rules = IgnoreRules.parse("logs/*\n!logs/important.txt\n")
    assert not rules.ignored("logs/important.txt")
    assert rules.ignored("logs/other.txt")


def test_load_prefers_disk_then_tree(tmp_path):
    from gud.core.objects import MemoryStore, Tree
    tree = Tree.empty(MemoryStore()).set(".gudignore", "100644", b"*.a\n")
    assert IgnoreRules.load(tmp_path, tree).ignored("x.a")
    (tmp_path / ".gudignore").write_text("*.b\n")
    rules = IgnoreRules.load(tmp_path, tree)
    assert rules.ignored("x.b") and not rules.ignored("x.a")


AWS = b"aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"


@pytest.mark.parametrize("data,name", [
    (AWS, "aws-access-key-id"),
    (b"-----BEGIN RSA PRIVATE KEY-----\nMII\n", "pem-private-key"),
    (b"x\n-----BEGIN OPENSSH PRIVATE KEY-----\n", "openssh-private-key"),
    (b"ed25519:" + b"A" * 43 + b"=\n", "gud-private-key"),
    (b"token ghp_" + b"a" * 36, "github-token"),
    (b"github_pat_11ABC", "github-pat"),
    (b"xoxb-123-456", "slack-token"),
    (b"key sk-ant-api03-zzz", "anthropic-key"),
    (b"OPENAI=sk-" + b"x" * 24, "api-key"),
])
def test_secret_patterns(data, name):
    hits = scan({"f": data})
    assert name in {h.pattern for h in hits}


def test_hits_have_lines_and_allow():
    hits = scan({"a.txt": b"one\ntwo\n" + AWS, "ok.txt": b"nothing here\n"})
    assert [(h.path, h.line, h.pattern) for h in hits] == [("a.txt", 3, "aws-access-key-id")]
    assert scan({"a.txt": AWS}, allow=["a.txt"]) == []


def test_members_file_public_keys_are_not_secrets():
    members = b"@a agent a@x.org ed25519:" + b"A" * 43 + b"= -\n@host host r@x.org ed25519:" + b"B" * 43 + b"= -\n"
    assert scan({".gud/members": members}) == []


def test_pattern_list_covers_spec():
    names = {n for n, _ in PATTERNS}
    assert {"pem-private-key", "openssh-private-key", "gud-private-key", "aws-access-key-id", "github-token",
            "github-pat", "slack-token", "anthropic-key", "api-key"} <= names
