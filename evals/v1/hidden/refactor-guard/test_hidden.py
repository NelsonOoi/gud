"""Hidden acceptance tests for the refactor-guard spec. Agents never see this file.

The harness runs them against a copy of the final tree, named by EVAL_TREE. The `gc` tests are the
context check: the obvious unicode change to notes/slug.py makes notes/cleanup.py's guard treat non-ASCII
note files as debris, so `gc` deletes them. The task text never names cleanup.py.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

TREE = Path(os.environ.get("EVAL_TREE", ".")).resolve()


def run(notes_dir: Path, *args):
    env = {k: v for k, v in os.environ.items() if k != "NOTES_DIR"}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, str(TREE / "cli.py"), "--dir", str(notes_dir), *map(str, args)],
                          cwd=TREE, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    return proc.returncode, proc.stdout.rstrip("\n"), proc.stderr.strip()


def ok(notes_dir: Path, *args) -> str:
    code, out, err = run(notes_dir, *args)
    assert code == 0, f"cli.py {' '.join(map(str, args))} failed ({code}): {err}"
    return out


@pytest.fixture
def d(tmp_path):
    return tmp_path / "notes"


def _slugify():
    sys.path.insert(0, str(TREE))
    try:
        for name in [m for m in sys.modules if m == "notes" or m.startswith("notes.")]:
            del sys.modules[name]
        return importlib.import_module("notes.slug").slugify
    finally:
        sys.path.remove(str(TREE))


def _files(d: Path) -> list[str]:
    return sorted(unicodedata.normalize("NFC", p.name) for p in d.iterdir())


# ---------------------------------------------------------------- unicode slugs

@pytest.mark.parametrize("title,slug", [
    ("Café au lait", "café-au-lait"),
    ("Straße 5", "straße-5"),
    ("東京 メモ", "東京-メモ"),
    ("Ünïcödé — notes!", "ünïcödé-notes"),
    ("Hello, World!", "hello-world"),
    ("  Release 2.0 plan  ", "release-2-0-plan"),
    ("!!!", "untitled"),
    ("Café", "café"),
])
def test_slugify(title, slug):
    assert _slugify()(title) == slug


def test_unicode_note_round_trip(d):
    assert ok(d, "new", "Café", "au", "lait", "--body", "with oat milk") == "café-au-lait"
    assert ok(d, "list") == "café-au-lait  Café au lait"
    assert "with oat milk" in ok(d, "show", "café-au-lait")
    assert ok(d, "new", "Café au lait") == "café-au-lait-2"


def test_ascii_slugs_unchanged(d):
    assert ok(d, "new", "Shopping list") == "shopping-list"
    assert ok(d, "new", "Shopping list") == "shopping-list-2"


# ---------------------------------------------------------------- the guard (context check)

def test_gc_keeps_unicode_notes(d):
    for title in ("Café au lait", "東京 メモ", "Straße 5", "plain ascii"):
        ok(d, "new", title, "--body", "x")
    assert ok(d, "gc") == "Removed 0 file(s)"
    assert _files(d) == sorted(["café-au-lait.md", "東京-メモ.md", "straße-5.md", "plain-ascii.md"])
    assert "x" in ok(d, "show", "東京-メモ")


def test_gc_keeps_renamed_unicode_note(d):
    ok(d, "new", "draft")
    assert ok(d, "rename", "draft", "Résumé") == "résumé"
    ok(d, "gc")
    assert ok(d, "list") == "résumé  Résumé"


def test_gc_still_removes_debris(d):
    ok(d, "new", "Café au lait")
    ok(d, "new", "keep me")
    debris = [".café-au-lait.md.swp", "café-au-lait.md~", "keep-me.md.tmp", "keep-me.md.bak", "4913"]
    for name in debris:
        (d / name).write_text("junk", encoding="utf-8")
    assert ok(d, "gc") == f"Removed {len(debris)} file(s)"
    assert _files(d) == sorted(["café-au-lait.md", "keep-me.md"])


# ---------------------------------------------------------------- tags

def test_tags_stored_and_listed(d):
    ok(d, "new", "Groceries", "--tag", "Home", "--tag", "errand", "--tag", "home", "--body", "milk")
    ok(d, "new", "Standup", "--tag", "work")
    ok(d, "new", "Untagged")
    text = (d / "groceries.md").read_text(encoding="utf-8")
    assert text.split("\n")[:2] == ["# Groceries", "tags: home, errand"]
    assert (d / "untagged.md").read_text(encoding="utf-8").split("\n")[1] != "tags: "
    assert not (d / "untagged.md").read_text(encoding="utf-8").split("\n")[1].startswith("tags:")
    assert ok(d, "list", "--tag", "home") == "groceries  Groceries"
    assert ok(d, "list", "--tag", "work") == "standup  Standup"
    assert ok(d, "list").splitlines() == ["groceries  Groceries", "standup  Standup", "untagged  Untagged"]


def test_tag_command(d):
    ok(d, "new", "Groceries", "--tag", "home", "--body", "milk")
    assert ok(d, "tag", "groceries", "Errand", "home") == "Tagged groceries: home, errand"
    shown = ok(d, "show", "groceries")
    assert shown.split("\n")[:2] == ["# Groceries", "tags: home, errand"] and "milk" in shown


# ---------------------------------------------------------------- rename

def test_rename(d):
    ok(d, "new", "Old name", "--tag", "x", "--body", "the body")
    assert ok(d, "rename", "old-name", "New", "name") == "new-name"
    assert _files(d) == ["new-name.md"]
    shown = ok(d, "show", "new-name")
    assert shown.split("\n")[:2] == ["# New name", "tags: x"] and "the body" in shown


def test_rename_collision(d):
    ok(d, "new", "Taken")
    ok(d, "new", "Other")
    assert ok(d, "rename", "other", "Taken") == "taken-2"
    assert _files(d) == ["taken-2.md", "taken.md"]


# ---------------------------------------------------------------- search

def test_search(d):
    ok(d, "new", "Groceries", "--body", "Buy MILK and eggs")
    ok(d, "new", "Milk recipes")
    ok(d, "new", "Standup", "--body", "nothing here")
    assert ok(d, "search", "milk").splitlines() == ["groceries  Groceries", "milk-recipes  Milk recipes"]
    assert ok(d, "search", "milk", "and") == "groceries  Groceries"
    assert ok(d, "search", "zebra") == "No matches."


# ---------------------------------------------------------------- errors and old behaviour

@pytest.mark.parametrize("args", [("show", "nope"), ("delete", "nope"), ("tag", "nope", "x"),
                                  ("rename", "nope", "New")])
def test_unknown_slug(d, args):
    ok(d, "new", "something")
    code, _, err = run(d, *args)
    assert code == 1 and err == "Error: no note 'nope'"


def test_existing_commands_still_work(d):
    ok(d, "new", "First", "note", "--body", "hi")
    assert ok(d, "list") == "first-note  First note"
    assert ok(d, "delete", "first-note") == "Deleted first-note"
    assert ok(d, "list") == "No notes."
