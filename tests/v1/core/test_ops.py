import difflib
import hashlib
import random

import pytest

from gud.core import envelope as E
from gud.core.objects import BlameMap, BlameRun, MemoryStore, Tree
from gud.core.ops import (
    Delete, ModeOp, OpError, Patch, Put, Rename, apply_ops, apply_patch, blob_refs, diff_trees, genesis_blame,
    invert_ops, is_text, make_patch, merge_file, ops_to_records, records_to_ops, render_unified, split_lines,
    three_way, touched_lines, touched_paths, update_blame,
)
from gud.core.types import Envelope, Record


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Blobs:
    def __init__(self, store=None, extra=()):
        self.store = store
        self.extra = {_sha(b): b for b in extra}

    def blob(self, sha):
        if sha in self.extra:
            return self.extra[sha]
        if self.store is not None and self.store.has(sha):
            return self.store.get(sha)
        return None


NO_BLOBS = Blobs()


# ---------------------------------------------------------------- lines and patches

def test_split_lines_and_is_text():
    assert split_lines(b"") == []
    assert split_lines(b"a\nb") == [b"a\n", b"b"]
    assert split_lines(b"a\r\n\n") == [b"a\r\n", b"\n"]
    assert split_lines(b"\0\r") == [b"\0\r"]
    assert is_text("héllo\n".encode()) and is_text(b"")
    assert not is_text(b"a\0b") and not is_text(b"\xff")


def test_patch_apply_basic_and_no_newline():
    old = b"a\nb\nc\n"
    assert apply_patch(old, b"@@ -2 +2 @@\n-b\n+B\n") == b"a\nB\nc\n"
    assert apply_patch(old, b"@@ -3,1 +3,1 @@ ctx text ignored\n-c\n+c\n\\ No newline at end of file\n") == b"a\nb\nc"
    assert apply_patch(b"x", b"@@ -1 +1 @@\n-x\n\\ No newline\n+x\n") == b"x\n"
    assert apply_patch(b"", b"@@ -0,0 +1,2 @@\n+1\n+2\n") == b"1\n2\n"
    assert apply_patch(old, b"@@ -0,0 +1 @@\n+0\n@@ -3,0 +5 @@\n+d\n") == b"0\na\nb\nc\nd\n"
    assert apply_patch(old, b"@@ -1,3 +0,0 @@\n-a\n-b\n-c\n") == b""
    crlf = b"a\r\nb\r\n"
    assert apply_patch(crlf, b"@@ -2 +2 @@\n-b\r\n+c\r\n") == b"a\r\nc\r\n"
    binary = b"\0\xff\n\x01\n"
    assert apply_patch(binary, b"@@ -2 +2 @@\n-\x01\n+\x02\n") == b"\0\xff\n\x02\n"


@pytest.mark.parametrize("old, patch, why", [
    (b"a\nb\n", b"@@ -2 +2 @@\n-x\n+y\n", "content mismatch, no fuzz"),
    (b"a\nb\n", b"@@ -1 +1 @@\n-b\n+y\n", "offset mismatch, no fuzz"),
    (b"a\nb\n", b"@@ -2 +2 @@\n-b\n+y", "missing final LF"),
    (b"a\nb\n", b"", "empty patch"),
    (b"a\nb\n", b"-b\n+y\n", "no hunk header"),
    (b"a\nb\n", b"@@ -2,2 +2 @@\n-b\n+y\n", "old count mismatch"),
    (b"a\nb\n", b"@@ -2 +2,2 @@\n-b\n+y\n", "new count mismatch"),
    (b"a\nb\n", b"@@ -2 +3 @@\n-b\n+y\n", "new start mismatch"),
    (b"a\nb\n", b"@@ -2 +2 @@\n-b\n+y\n@@ -1 +1 @@\n-a\n+z\n", "decreasing hunks"),
    (b"a\nb\nc\n", b"@@ -1,2 +1,2 @@\n a\n-b\n+y\n@@ -2,1 +2,1 @@\n-b\n+z\n", "overlapping hunks"),
    (b"a\n", b"@@ -2 +2 @@\n-b\n+y\n", "past end of file"),
    (b"a\n", b"@@ -0 +1 @@\n-a\n+y\n", "old start 0 with old lines"),
    (b"a\nb\n", b"@@ -2 +2 @@\n-b\n\n+y\n", "empty line is not an hline"),
    (b"a\nb\n", b"@@ -2 +2 @@\n-b\n*y\n", "bad line prefix"),
    (b"a\nb\n", b"@@ -2 +2 @@\n\\ No newline\n-b\n+y\n", "nonl without line"),
    (b"a\nb", b"@@ -2 +2 @@\n-b\n\\ x\n\\ y\n+c\n", "double nonl"),
    (b"a\nb\n", b"@@ -1 +1 @@\n-a\n+x\n\\ No newline\n", "LF-less line before end"),
    (b"a\nb", b"@@ -2 +2 @@\n-b\n+c\n", "old last line lacks LF"),
    (b"a\nb\n", b"@@ -x +2 @@\n-b\n+y\n", "bad header"),
])
def test_patch_rejects(old, patch, why):
    with pytest.raises(OpError):
        apply_patch(old, patch)


def _random_lines(rng, n):
    pool = [b"a\n", b"b\n", b"c\r\n", b"\n", b"\0x\n", b"\xff\n", b"   \n", b"d"]
    lines = [rng.choice(pool[:-1]) for _ in range(n)]
    if rng.random() < 0.3:
        lines.append(rng.choice([b"tail", b"\0", b"x\r"]))
    return b"".join(lines)


def test_make_patch_apply_round_trip_is_byte_exact():
    rng = random.Random(4)
    for _ in range(500):
        old = _random_lines(rng, rng.randint(0, 30))
        lines = split_lines(old)
        for _ in range(rng.randint(0, 5)):
            i = rng.randint(0, len(lines))
            choice = rng.random()
            if choice < 0.4 and lines:
                del lines[min(i, len(lines) - 1)]
            elif choice < 0.8:
                lines.insert(i, rng.choice([b"new\n", b"\r\n", b"\0\n"]))
            elif lines:
                lines[min(i, len(lines) - 1)] = b"changed\n"
        new = b"".join(line if line.endswith(b"\n") or k == len(lines) - 1 else line + b"\n"
                       for k, line in enumerate(lines))
        for context in (0, 1, 3):
            patch = make_patch(old, new, context)
            if old == new:
                assert patch == b""
                continue
            assert apply_patch(old, patch) == new


# ---------------------------------------------------------------- records <-> ops

def test_records_round_trip():
    ops = (Rename("a b", "c%d"), Delete("x"), Put("p", "120000", _sha(b"t"), 1, b"t"),
           Put("big", "100644", "f" * 64, 9, None), Put("empty", "100644", _sha(b""), 0, b""),
           Patch("q", b"@@ -1 +1 @@\n-a\n+b\n"), ModeOp("q", "100755"))
    records = ops_to_records(ops)
    assert records_to_ops(records) == ops
    assert records_to_ops((Record("text", (), b"intent"),) + records) == ops
    env = Envelope((("Gud-Version", "1"),), records)
    assert records_to_ops(E.decode(E.encode(env)).records) == ops
    assert touched_paths(ops) == ("a b", "big", "c%d", "empty", "p", "q", "x")
    assert blob_refs(ops) == ("f" * 64,)


@pytest.mark.parametrize("record", [
    Record("frob", ("a",), b""),
    Record("patch", ("a",), b""),
    Record("patch", ("a", "b"), b"x"),
    Record("put", ("a", "100600", "f" * 64, "1"), b"x"),
    Record("put", ("a", "100644", "F" * 64, "1"), b"x"),
    Record("put", ("a", "100644", "f" * 64, "01"), b"x"),
    Record("put", ("a", "100644", "f" * 64, "2"), b"x"),
    Record("put", ("a", "100644", "f" * 64, "0"), b"x"),
    Record("delete", ("a",), b"x"),
    Record("delete", ("../a",), b""),
    Record("delete", (".gud/BASE",), b""),
    Record("rename", ("a",), b""),
    Record("rename", ("a", ".git/x"), b""),
    Record("mode", ("a", "120000"), b""),
])
def test_records_to_ops_rejects(record):
    with pytest.raises(OpError):
        records_to_ops([record])


# ---------------------------------------------------------------- applying ops

def _tree(store, files):
    return Tree.from_files(store, files)


def test_apply_ops_each_kind():
    store = MemoryStore()
    tree = _tree(store, {"a.txt": ("100644", b"1\n2\n"), "bin": ("100644", b"\0\1"), "s": ("120000", b"a.txt")})
    out = apply_ops(tree, [
        Patch("a.txt", b"@@ -2 +2 @@\n-2\n+two\n"),
        Rename("a.txt", "docs/a.txt"),
        ModeOp("bin", "100755"),
        Put("s", "120000", _sha(b"docs/a.txt"), 10, b"docs/a.txt"),
        Put("new", "100644", _sha(b"blobbed"), 7, None),
        Delete("bin"),
    ], Blobs(extra=[b"blobbed"]))
    assert out.to_files() == {"docs/a.txt": ("100644", b"1\ntwo\n"), "s": ("120000", b"docs/a.txt"),
                              "new": ("100644", b"blobbed")}


@pytest.mark.parametrize("ops, why", [
    ([Patch("nope", b"@@ -1 +1 @@\n-a\n+b\n")], "patch missing path"),
    ([Patch("s", b"@@ -1 +1 @@\n-a.txt\n+b\n")], "patch a symlink"),
    ([Patch("a.txt", b"@@ -1 +1 @@\n-x\n+b\n")], "patch mismatch"),
    ([Put("a.txt", "100644", _sha(b"x"), 2, b"x")], "size mismatch"),
    ([Put("a.txt", "100644", "0" * 64, 1, b"x")], "sha mismatch"),
    ([Put("a.txt/sub", "100644", _sha(b"x"), 1, b"x")], "parent is a file"),
    ([Put("d", "100644", _sha(b"x"), 1, b"x")], "path is a directory"),
    ([Put("n", "100644", "e" * 64, 5, None)], "pending blob"),
    ([Put("n", "120000", _sha(b"a\0"), 2, b"a\0")], "symlink with NUL"),
    ([Delete("nope")], "delete missing"),
    ([Rename("nope", "x")], "rename missing source"),
    ([Rename("a.txt", "d/e")], "rename onto existing"),
    ([Rename("a.txt", "d/e/f")], "rename under a file"),
    ([ModeOp("a.txt", "100644")], "mode unchanged"),
    ([ModeOp("s", "100755")], "mode of symlink"),
    ([ModeOp("nope", "100755")], "mode of missing"),
    ([Delete("a.txt"), Delete("a.txt")], "second op fails"),
    ([Put(".gud/local/x", "100644", _sha(b"x"), 1, b"x")], "reserved path"),
])
def test_apply_ops_rejects(ops, why):
    store = MemoryStore()
    tree = _tree(store, {"a.txt": ("100644", b"a\n"), "s": ("120000", b"a.txt"), "d/e": ("100644", b"")})
    with pytest.raises(OpError) as info:
        apply_ops(tree, ops, NO_BLOBS)
    assert "op " in str(info.value)


def test_file_to_directory_and_back():
    store = MemoryStore()
    a = _tree(store, {"x": ("100644", b"file\n"), "y/z": ("100644", b"deep\n")})
    b = _tree(store, {"x/inner": ("100644", b"file\n"), "y": ("100644", b"flat\n")})
    for old, new in ((a, b), (b, a)):
        ops = diff_trees(old, new, inline_budget=1 << 20)
        assert apply_ops(old, ops, NO_BLOBS) == new


# ---------------------------------------------------------------- diff_trees round trips

def _random_files(rng):
    files = {}
    for _ in range(rng.randint(0, 25)):
        path = "/".join(rng.choice(["a", "b", "c", "dir", "é", "x y"]) for _ in range(rng.randint(1, 3)))
        segs = path.split("/")
        if any("/".join(segs[:i]) in files for i in range(1, len(segs))):
            continue
        if any(p.startswith(path + "/") for p in files):
            continue
        files[path] = _random_content(rng)
    return files


def _random_content(rng):
    kind = rng.random()
    if kind < 0.1:
        return ("120000", rng.choice([b"a", b"../b", b"x y"]))
    if kind < 0.25:
        return ("100644", bytes(rng.randrange(256) for _ in range(rng.randint(0, 40))))
    return (rng.choice(["100644", "100755"]), _random_lines(rng, rng.randint(0, 20)))


def _mutate(rng, files):
    out = dict(files)
    for _ in range(rng.randint(0, 8)):
        if not out or rng.random() < 0.2:
            path = rng.choice(["a", "b", "new", "dir/new", "c/c", "é"])
            segs = path.split("/")
            if not any("/".join(segs[:i]) in out for i in range(1, len(segs))) and \
                    not any(p.startswith(path + "/") for p in out):
                out[path] = _random_content(rng)
            continue
        path = rng.choice(sorted(out))
        mode, data = out[path]
        choice = rng.random()
        if choice < 0.2:
            del out[path]
        elif choice < 0.35:
            dst = rng.choice(["moved", "m/" + path.replace("/", "_"), path + "2"])
            segs = dst.split("/")
            del out[path]
            if dst not in out and not any("/".join(segs[:i]) in out for i in range(1, len(segs))) and \
                    not any(p.startswith(dst + "/") for p in out):
                out[dst] = (mode, data)
            else:
                out[path] = (mode, data)
        elif choice < 0.5 and mode != "120000":
            out[path] = ("100755" if mode == "100644" else "100644", data)
        elif mode != "120000":
            lines = split_lines(data)
            i = rng.randint(0, len(lines))
            lines.insert(i, b"inserted\n")
            if len(lines) > 2 and rng.random() < 0.5:
                del lines[0]
            new = b"".join(line if line.endswith(b"\n") or k == len(lines) - 1 else line + b"\n"
                           for k, line in enumerate(lines))
            out[path] = (mode, new)
        else:
            out[path] = (mode, data + b"x")
    return out


def test_diff_trees_apply_round_trips_on_random_trees():
    rng = random.Random(7)
    store = MemoryStore()
    for _ in range(400):
        old_files = _random_files(rng)
        new_files = _mutate(rng, old_files)
        old, new = Tree.from_files(store, old_files), Tree.from_files(store, new_files)
        budget = rng.choice([0, 10, 100, 1 << 20])
        ops = diff_trees(old, new, inline_budget=budget)
        if old == new:
            assert ops == ()
        # through the wire and back
        decoded = records_to_ops(E.decode(E.encode(Envelope((("Gud-Version", "1"),), ops_to_records(ops)))).records)
        assert decoded == ops
        blobs = Blobs(extra=[new.read(p) for p in new.paths()])
        result = apply_ops(old, decoded, blobs)
        assert result == new and result.hash() == new.hash()
        if budget == 0:
            assert all(op.data is None or op.size == 0 for op in ops if isinstance(op, Put))
            assert not any(isinstance(op, Patch) for op in ops)


def test_diff_trees_canonical_choices():
    store = MemoryStore()
    text = b"".join(b"line number %d of a longer file\n" % i for i in range(40))
    old = _tree(store, {"a": ("100644", text), "bin": ("100644", b"\0\0"),
                        "m": ("100644", b"m"), "gone": ("100644", b"g"), "src": ("100644", b"moved\n")})
    new = _tree(store, {"a": ("100644", text.replace(b"of a longer file\n", b"changed\n", 1)), "bin": ("100644", b"\0\1"),
                        "m": ("100755", b"m"), "dst/src": ("100644", b"moved\n")})
    ops = diff_trees(old, new, inline_budget=1 << 20)
    assert ops[0] == Rename("src", "dst/src")
    assert ops[1] == Delete("gone")
    assert isinstance(ops[2], Patch) and ops[2].path == "a"
    assert ops[3] == Put("bin", "100644", _sha(b"\0\1"), 2, b"\0\1")
    assert ops[4] == ModeOp("m", "100755")
    preview = render_unified(old, new, ops)
    assert "rename from src" in preview and "Binary files a/bin and b/bin differ" in preview
    assert "-line number 0 of a longer file" in preview and "+line number 0 changed" in preview
    assert "old mode 100644" in preview
    short = render_unified(old, new, ops, max_lines=3)
    assert short.splitlines()[-1].startswith("[... ")


# ---------------------------------------------------------------- three-way merge

def _files(tree):
    return {p: d for p, (_, d) in tree.to_files().items()}


def test_three_way_clean_merges():
    store = MemoryStore()
    base = _tree(store, {"f": ("100644", b"1\n2\n3\n4\n5\n6\n"), "keep": ("100644", b"k"), "del": ("100644", b"d")})
    mine = _tree(store, {"f": ("100644", b"one\n2\n3\n4\n5\n6\n"), "keep": ("100644", b"k"), "mine": ("100644", b"m")})
    theirs = _tree(store, {"f": ("100644", b"1\n2\n3\n4\n5\nsix\n"), "keep": ("100755", b"k"), "del": ("100644", b"d"),
                           "theirs": ("100644", b"t")})
    result = three_way(base, mine, theirs)
    assert result.conflicts == ()
    assert result.tree.to_files() == {"f": ("100644", b"one\n2\n3\n4\n5\nsix\n"), "keep": ("100755", b"k"),
                                      "mine": ("100644", b"m"), "theirs": ("100644", b"t")}


def test_three_way_identical_edits_merge_once():
    store = MemoryStore()
    base = _tree(store, {"f": ("100644", b"1\n2\n3\n4\n5\n6\n")})
    mine = _tree(store, {"f": ("100644", b"one\n2\n3\n4\n5\n6\n")})
    theirs = _tree(store, {"f": ("100644", b"one\n2\n3\n4\n5\nsix\n")})
    assert _files(three_way(base, mine, theirs).tree) == {"f": b"one\n2\n3\n4\n5\nsix\n"}


@pytest.mark.parametrize("b, m, t, fragment", [
    ({"f": b"1\n2\n3\n"}, {"f": b"1\nX\n3\n"}, {"f": b"1\nY\n3\n"}, "tip changed line 2"),
    ({}, {"f": b"a"}, {"f": b"b"}, "both added"),
    ({"f": b"a\n"}, {}, {"f": b"b\n"}, "you deleted it"),
    ({"f": b"a\n"}, {"f": b"c\n"}, {}, "tip deleted it"),
    ({"f": b"\0a"}, {"f": b"\0b"}, {"f": b"\0c"}, "binary"),
    ({"f": b"1\n2\n"}, {"f": b"0\n1\n2\n"}, {"f": b"z\n1\n2\n"}, "insert at line 1"),
])
def test_three_way_conflicts(b, m, t, fragment):
    store = MemoryStore()
    mk = lambda d: _tree(store, {k: ("100644", v) for k, v in d.items()})
    theirs = mk(t)
    result = three_way(mk(b), mk(m), theirs)
    assert len(result.conflicts) == 1 and result.conflicts[0].startswith("f: ")
    assert fragment in result.conflicts[0]
    assert result.tree == theirs


def test_three_way_paths_restriction_and_mode_conflict():
    store = MemoryStore()
    base = _tree(store, {"a": ("100644", b"a"), "b": ("100644", b"b")})
    mine = _tree(store, {"a": ("100644", b"A"), "b": ("100755", b"b")})
    theirs = _tree(store, {"a": ("100644", b"a"), "b": ("100644", b"B")})
    only_a = three_way(base, mine, theirs, paths=["a"])
    assert only_a.tree.to_files() == {"a": ("100644", b"A"), "b": ("100644", b"B")}
    both = three_way(base, mine, theirs)
    assert both.tree.to_files() == {"a": ("100644", b"A"), "b": ("100755", b"B")}
    clash = three_way(base, _tree(store, {"a": ("100755", b"a"), "b": ("100644", b"b")}),
                      _tree(store, {"a": ("120000", b"a"), "b": ("100644", b"b")}))
    assert clash.conflicts and "mode changed on both sides" in clash.conflicts[0]


def test_three_way_shape_conflict_is_reported():
    store = MemoryStore()
    base = _tree(store, {"x": ("100644", b"")})
    mine = _tree(store, {"x": ("100644", b""), "d/f": ("100644", b"mine")})
    theirs = _tree(store, {"x": ("100644", b""), "d": ("100644", b"theirs")})
    result = three_way(base, mine, theirs)
    assert result.conflicts and result.conflicts[0].startswith("d/f: ")
    assert result.tree == theirs


def test_merge_file():
    assert merge_file(b"a\n", b"a\n", b"b\n") == b"b\n"
    assert merge_file(b"a\n", b"b\n", b"a\n") == b"b\n"
    assert merge_file(None, b"x", b"x") == b"x"
    assert merge_file(b"a", None, b"a") is None
    with pytest.raises(OpError):
        merge_file(b"1\n", b"2\n", b"3\n")


def test_invert_ops():
    store = MemoryStore()
    before = _tree(store, {"f": ("100644", b"1\n2\n3\n4\n5\n"), "old": ("100644", b"o")})
    after = _tree(store, {"f": ("100644", b"1\nTWO\n3\n4\n5\n"), "new": ("100644", b"n")})
    later = _tree(store, {"f": ("100644", b"1\nTWO\n3\n4\nFIVE\n"), "new": ("100644", b"n"), "z": ("100644", b"z")})
    result, conflicts = invert_ops(before, after, later)
    assert conflicts == ()
    assert result.to_files() == {"f": ("100644", b"1\n2\n3\n4\nFIVE\n"), "old": ("100644", b"o"),
                                 "z": ("100644", b"z")}
    clash = _tree(store, {"f": ("100644", b"1\nTHREE\n3\n4\n5\n"), "new": ("100644", b"n")})
    _, conflicts = invert_ops(before, after, clash)
    assert conflicts == ("f",)


# ---------------------------------------------------------------- touched lines

def test_touched_lines():
    store = MemoryStore()
    before = _tree(store, {"p.py": ("100644", b"def f():\n    a = 1\n    return a\n"),
                           "t.txt": ("100644", b"1\n2\n3\n4\n"), "bin": ("100644", b"\0"),
                           "r": ("100644", b"r"), "m": ("100644", b"m"), "d": ("100644", b"d")})
    ops = [Patch("p.py", b"@@ -2,0 +3 @@\n+    b = 2\n"),
           Put("t.txt", "100644", _sha(b"1\nX\n3\n"), 6, b"1\nX\n3\n"),
           Put("bin", "100644", _sha(b"\1"), 1, b"\1"),
           Rename("r", "r2"), ModeOp("m", "100755"), Delete("d"), Put("fresh", "100644", _sha(b"f"), 1, b"f")]
    after = apply_ops(before, ops, NO_BLOBS)
    touched = touched_lines(before, after, ops)
    assert touched.removed == {"t.txt": (1, 3)}
    assert touched.inserted_at == {"p.py": (2,)}
    assert touched.whole_file == ("bin", "d", "m", "r", "r2")
    rewrite = [Patch("t.txt", b"@@ -2,2 +2,3 @@\n-2\n-3\n+two\n+three\n+extra\n")]
    touched = touched_lines(before, apply_ops(before, rewrite, NO_BLOBS), rewrite)
    assert touched.removed == {"t.txt": (1, 2)} and touched.inserted_at == {}


# ---------------------------------------------------------------- blame

def test_genesis_and_update_blame():
    store = MemoryStore()
    before = _tree(store, {"a.txt": ("100644", b"1\n2\n3\n"), "bin": ("100644", b"\0"), "s": ("120000", b"a.txt"),
                           "mv": ("100644", b"x\ny\n"), "mode": ("100644", b"q\n"), "empty": ("100644", b""),
                           "gone": ("100644", b"g\n")})
    blame0 = genesis_blame(before, "@g")
    assert blame0.get("a.txt") == (BlameRun("@g", 0, 3),)
    assert blame0.get("bin") == (BlameRun("@g", 0, 1),)
    assert blame0.get("s") == (BlameRun("@g", 0, 1),)
    assert blame0.get("empty") == () and "empty" in blame0
    ops = [Patch("a.txt", b"@@ -2 +2,2 @@\n-2\n+two\n+2.5\n"), Rename("mv", "moved/mv"), ModeOp("mode", "100755"),
           Put("bin", "100644", _sha(b"\1"), 1, b"\1"), Delete("gone"), Put("new.txt", "100644", _sha(b"n\nm"), 3, b"n\nm"),
           Put("s", "120000", _sha(b"bin"), 3, b"bin")]
    after = apply_ops(before, ops, NO_BLOBS)
    blame1 = update_blame(before, blame0, after, ops, "@b", 4)
    assert list(blame1.lines("a.txt")) == [("@g", 0), ("@b", 4), ("@b", 4), ("@g", 0)]
    assert blame1.get("moved/mv") == (BlameRun("@g", 0, 2),) and blame1.get("mv") == ()
    assert blame1.get("mode") == (BlameRun("@g", 0, 1),)
    assert blame1.get("bin") == (BlameRun("@b", 4, 1),)
    assert blame1.get("s") == (BlameRun("@b", 4, 1),)
    assert blame1.get("new.txt") == (BlameRun("@b", 4, 2),)
    assert "gone" not in blame1
    assert sorted(blame1.paths()) == sorted(after.paths())


def test_update_blame_uses_difflib_alignment():
    rng = random.Random(9)
    store = MemoryStore()
    for _ in range(100):
        old = _random_lines(rng, rng.randint(0, 15))
        new_lines = split_lines(old)
        for _ in range(3):
            i = rng.randint(0, len(new_lines))
            new_lines.insert(i, b"x\n")
            if new_lines and rng.random() < 0.5:
                del new_lines[rng.randrange(len(new_lines))]
        new = b"".join(l if l.endswith(b"\n") or k == len(new_lines) - 1 else l + b"\n" for k, l in enumerate(new_lines))
        if not (is_text(old) and is_text(new)) or old == new:
            continue
        before = _tree(store, {"f": ("100644", old)})
        after = _tree(store, {"f": ("100644", new)})
        old_blame = [("@o%d" % i, i) for i in range(len(split_lines(old)))]
        blame0 = BlameMap.empty(store).set("f", [BlameRun(o, s, 1) for o, s in old_blame])
        ops = [Put("f", "100644", _sha(new), len(new), new)]
        got = list(update_blame(before, blame0, after, ops, "@new", 99).lines("f"))
        expected = []
        sm = difflib.SequenceMatcher(None, split_lines(old), split_lines(new), autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            expected += old_blame[i1:i2] if tag == "equal" else [("@new", 99)] * (j2 - j1)
        assert got == expected


def test_update_blame_rename_chain_and_swap():
    store = MemoryStore()
    before = _tree(store, {"a": ("100644", b"A\n"), "b": ("100644", b"B\n")})
    blame0 = BlameMap.empty(store).set("a", [BlameRun("@x", 1, 1)]).set("b", [BlameRun("@y", 2, 1)])
    ops = [Rename("a", "tmp"), Rename("b", "a"), Rename("tmp", "b")]
    after = apply_ops(before, ops, NO_BLOBS)
    blame1 = update_blame(before, blame0, after, ops, "@z", 3)
    assert blame1.get("a") == (BlameRun("@y", 2, 1),) and blame1.get("b") == (BlameRun("@x", 1, 1),)
    assert "tmp" not in blame1
    recreate = [Delete("a"), Put("a", "100644", _sha(b"A\n"), 2, b"A\n")]
    blame2 = update_blame(before, blame0, apply_ops(before, recreate, NO_BLOBS), recreate, "@z", 3)
    assert blame2.get("a") == (BlameRun("@z", 3, 1),)
