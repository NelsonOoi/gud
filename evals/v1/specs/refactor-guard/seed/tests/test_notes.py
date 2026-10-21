import subprocess
import sys
from pathlib import Path

from notes.cleanup import prune
from notes.slug import slugify
from notes.store import NoteStore

ROOT = Path(__file__).resolve().parents[1]


def test_slugify_ascii():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  Release 2.0 plan  ") == "release-2-0-plan"
    assert slugify("!!!") == "untitled"


def test_store_create_read_list(tmp_path):
    store = NoteStore(tmp_path)
    note = store.create("Shopping list", "milk, eggs")
    assert note.slug == "shopping-list"
    assert store.create("Shopping list").slug == "shopping-list-2"
    assert store.read("shopping-list").body == "milk, eggs"
    assert [n.slug for n in store.list()] == ["shopping-list", "shopping-list-2"]


def test_prune_removes_debris(tmp_path):
    store = NoteStore(tmp_path)
    store.create("Keep me")
    for name in (".keep-me.md.swp", "keep-me.md~", "keep-me.md.tmp"):
        (tmp_path / name).write_text("x")
    assert prune(tmp_path) == [".keep-me.md.swp", "keep-me.md.tmp", "keep-me.md~"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep-me.md"]


def test_cli_new_and_list(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, str(ROOT / "cli.py"), "--dir", str(tmp_path), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    assert run("new", "First", "note", "--body", "hi") == "first-note"
    assert run("list") == "first-note  First note"
    assert run("gc") == "Removed 0 file(s)"
