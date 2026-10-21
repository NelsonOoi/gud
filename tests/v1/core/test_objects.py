import hashlib
import random

import pytest

from gud.core.objects import (
    BlameMap, BlameRun, DiskStore, Entry, MemoryStore, ObjectError, Tree, check_tree_shape, tree_hash,
    valid_path,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_bytes(store: MemoryStore) -> int:
    return sum(len(v) for v in store._data.values())


def _manual_hash(files: dict) -> str:
    out = b""
    for path in sorted(files, key=lambda p: p.encode()):
        mode, data = files[path]
        out += path.encode() + b"\0" + mode.encode() + b"\0" + _sha(data).encode() + b"\n"
    return _sha(out)


# ---------------------------------------------------------------- stores

def test_memory_store():
    store = MemoryStore()
    key = store.put(b"hello")
    assert key == _sha(b"hello") and store.put(b"hello") == key
    assert store.get(key) == b"hello" and store.has(key)
    assert not store.has("0" * 64)
    with pytest.raises(KeyError):
        store.get("0" * 64)


def test_disk_store(tmp_path):
    store = DiskStore(tmp_path / "objects")
    key = store.put(b"\0binary\r\n")
    assert (tmp_path / "objects" / key[:2] / key[2:]).read_bytes() == b"\0binary\r\n"
    assert store.put(b"\0binary\r\n") == key
    assert store.get(key) == b"\0binary\r\n" and store.has(key)
    assert not store.has("f" * 64) and not store.has("../etc")
    with pytest.raises(KeyError):
        store.get("f" * 64)
    assert not list((tmp_path / "objects" / key[:2]).glob(".*tmp"))
    tree = Tree.from_files(store, {"a/b": ("100644", b"x"), "c": ("100755", b"y")})
    again = Tree.load(DiskStore(tmp_path / "objects"), tree.root)
    assert again.to_files() == tree.to_files() and again.hash() == tree.hash()


# ---------------------------------------------------------------- paths

@pytest.mark.parametrize("path", ["a", "a/b", "a.b/c d", "é/漢", ".gud/members", ".gudx", "a/.gitx", "x\ny",
                                  ".gud/BASEx", ".gud/localx"])
def test_valid_paths(path):
    assert valid_path(path)


@pytest.mark.parametrize("path", ["", "/a", "a/", "a//b", "./a", "a/../b", "..", ".", "a\0b", ".git", "a/.GIT/b",
                                  "x/.Git", ".gud/BASE", ".gud/local", ".gud/local/x", "\ud800"])
def test_invalid_paths(path):
    assert not valid_path(path)


# ---------------------------------------------------------------- tree basics

def test_tree_hash_matches_the_spec():
    store = MemoryStore()
    assert Tree.empty(store).hash() == _sha(b"")
    files = {"b": ("100644", b"1\n"), "a.txt": ("100755", b""), "a/x": ("120000", b"target"),
             "a/y/z": ("100644", b"\0\r\n"), "a-b": ("100644", b"q"), "é": ("100644", b"e")}
    tree = Tree.from_files(store, files)
    assert tree.hash() == _manual_hash(files)
    assert tree.hash() == tree_hash((p, e.mode, e.blob) for p, e in tree.items())
    # byte order: "a-b" < "a.txt" < "a/x" < "a/y/z" < "b" < "é"
    assert list(tree.paths()) == ["a-b", "a.txt", "a/x", "a/y/z", "b", "é"]


def test_tree_get_set_delete():
    store = MemoryStore()
    tree = Tree.empty(store).set("a/b/c", "100644", b"one")
    assert tree.get("a/b/c") == Entry("100644", _sha(b"one"), 3)
    assert tree.read("a/b/c") == b"one"
    assert "a/b/c" in tree and "a/b" not in tree and "a" not in tree
    assert tree.get("a/b") is None and tree.get("") is None
    with pytest.raises(KeyError):
        tree.read("nope")
    with pytest.raises(ObjectError):
        tree.set("a/b", "100644", b"file over dir")
    with pytest.raises(ObjectError):
        tree.set("a/b/c/d", "100644", b"dir under file")
    with pytest.raises(ObjectError):
        tree.set("x/../y", "100644", b"")
    with pytest.raises(ObjectError):
        tree.set("x", "100600", b"")
    with pytest.raises(ObjectError):
        tree.set("x", "120000", b"")
    with pytest.raises(ObjectError):
        tree.set("x", "120000", b"a\0b")
    with pytest.raises(KeyError):
        tree.delete("a/b")
    with pytest.raises(KeyError):
        tree.delete("zz/q")
    empty = tree.delete("a/b/c")
    assert len(empty) == 0 and empty.root == Tree.empty(store).root
    assert empty.hash() == _sha(b"")
    other = tree.set_entry("d", Entry("100755", _sha(b"one"), 3))
    assert other.read("d") == b"one"


def test_tree_is_immutable_and_equal_contents_share_a_root():
    store = MemoryStore()
    a = Tree.from_files(store, {"x": ("100644", b"1"), "y/z": ("100644", b"2")})
    b = a.set("x", "100644", b"9")
    assert a.read("x") == b"1" and b.read("x") == b"9"
    c = b.set("x", "100644", b"1")
    assert c.root == a.root and c == a and hash(c) == hash(a)
    assert a.set("x", "100644", b"1").root == a.root


def test_random_edit_sequences_reach_the_canonical_root():
    rng = random.Random(11)
    store = MemoryStore()
    for _ in range(12):
        files = {}
        tree = Tree.empty(store)
        for _ in range(rng.randint(50, 1200)):
            path = rng.choice(["", "d/", "d/e/", "f/"]) + f"n{rng.randrange(2000)}"
            if path in files and rng.random() < 0.4:
                tree = tree.delete(path)
                del files[path]
                continue
            data = bytes([rng.randrange(256)]) * rng.randint(0, 3)
            try:
                tree = tree.set(path, "100644", data)
            except ObjectError:
                continue
            files[path] = ("100644", data)
        fresh = Tree.from_files(store, files)
        assert tree.root == fresh.root
        assert tree.hash() == fresh.hash() == _manual_hash(files)
        assert tree.to_files() == files and len(tree) == len(files)


def test_changed_paths():
    store = MemoryStore()
    base = Tree.from_files(store, {f"d{i % 7}/f{i}": ("100644", b"%d" % i) for i in range(500)})
    other = base.set("d1/f1", "100644", b"new").delete("d2/f2").set("z/new", "100755", b"").set(
        "d3/f3", "100755", b"3")
    assert list(base.changed_paths(other)) == ["d1/f1", "d2/f2", "d3/f3", "z/new"]
    assert list(other.changed_paths(base)) == ["d1/f1", "d2/f2", "d3/f3", "z/new"]
    assert list(base.changed_paths(base)) == []
    swapped = Tree.from_files(store, {"a": ("100644", b"f")})
    as_dir = Tree.from_files(store, {"a/b": ("100644", b"f"), "a.x": ("100644", b"")})
    assert list(swapped.changed_paths(as_dir)) == ["a", "a.x", "a/b"]


def test_structural_sharing_keeps_versions_small():
    """1000 one-file-changed versions of a 2000-file tree ~ one tree + 1000 small node sets."""
    for layout in ("flat", "nested"):
        store = MemoryStore()
        if layout == "flat":
            files = {f"file{i:04d}.py": ("100644", b"content %d\n" % i) for i in range(2000)}
        else:
            files = {f"pkg{i % 20}/mod{i // 20 % 10}/file{i:04d}.py": ("100644", b"content %d\n" % i)
                     for i in range(2000)}
        tree = Tree.from_files(store, files)
        one_tree = _store_bytes(store) - sum(len(d) for _, d in files.values())
        before = _store_bytes(store)
        rng = random.Random(0)
        paths = sorted(files)
        roots = []
        for version in range(1000):
            tree = tree.set(rng.choice(paths), "100644", b"version %d\n" % version)
            roots.append(tree.root)
        per_version = (_store_bytes(store) - before - sum(len(b"version %d\n" % v) for v in range(1000))) / 1000
        assert len(set(roots)) == 1000
        assert per_version < 6_000, (layout, per_version)
        assert per_version < one_tree * 0.03, (layout, per_version, one_tree)
        total = _store_bytes(store)
        assert total < 2 * one_tree + 1000 * 6_000 + 100_000


def test_check_tree_shape():
    store = MemoryStore()
    tree = Tree.from_files(store, {"a/b": ("100644", b""), "c": ("120000", b"a/b")})
    assert check_tree_shape(tree) is None
    with pytest.raises(ObjectError):
        Tree.from_files(store, {"a": ("100644", b""), "a/b": ("100644", b"")})
    with pytest.raises(ObjectError):
        Tree.from_files(store, {".gud/BASE": ("100644", b"")})


def test_load_unknown_root():
    with pytest.raises(ObjectError):
        Tree.load(MemoryStore(), "1" * 64)
    assert len(Tree.load(MemoryStore(), Tree.empty(MemoryStore()).root)) == 0


# ---------------------------------------------------------------- blame

def test_blame_map():
    store = MemoryStore()
    blame = BlameMap.empty(store)
    runs = [BlameRun("@a", 0, 2), BlameRun("@a", 0, 1), BlameRun("@b", 3, 1), BlameRun("@c", 4, 0)]
    b1 = blame.set("src/x.py", runs)
    assert b1.get("src/x.py") == (BlameRun("@a", 0, 3), BlameRun("@b", 3, 1))
    assert list(b1.lines("src/x.py")) == [("@a", 0)] * 3 + [("@b", 3)]
    assert blame.get("src/x.py") == ()
    b2 = b1.rename("src/x.py", "lib/y.py")
    assert b2.get("src/x.py") == () and b2.get("lib/y.py") == b1.get("src/x.py")
    assert list(b2.paths()) == ["lib/y.py"]
    b3 = b2.delete("lib/y.py")
    assert b3.root == blame.root
    with pytest.raises(KeyError):
        b3.delete("lib/y.py")
    with pytest.raises(KeyError):
        b3.rename("nope", "x")
    again = BlameMap.load(store, b1.root)
    assert again.get("src/x.py") == b1.get("src/x.py")
    assert BlameMap._build(store, {"src/x.py": runs}).root == b1.root
