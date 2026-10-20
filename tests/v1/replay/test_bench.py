"""Scale micro-benchmark (scripted, replay and storage only): 10k links on a 2,000-file tree.

Run with `pytest -m slow tests/v1/replay/test_bench.py -s`. Deselected by default.
"""

import gc
import time
import tracemalloc

import pytest

from gud.core.ops import make_patch, Patch
from gud.core.replay import Replayer
from tests.v1.kit import Kit

FILES = 2_000
LINKS = 10_000
BATCH = 500


def build(k: Kit):
    paths = [f"src/d{i % 40:02d}/f{i:04d}.py" for i in range(FILES)]
    files = {paths[i]: f"# file {i}\nvalue = 0\n" for i in range(FILES)}
    g = k.genesis(files, author="@alice")
    counters = [0] * FILES
    parent = g
    for n in range(LINKS):
        i = (n * 7919) % FILES
        old = f"# file {i}\nvalue = {counters[i]}\n".encode()
        counters[i] += 1
        new = f"# file {i}\nvalue = {counters[i]}\n".encode()
        parent = k.push("@alice", parent, ops=[Patch(paths[i], make_patch(old, new))], intent=f"bump {n}")
        k.tick(1_000)
    return g, parent, paths, counters


@pytest.mark.slow
def test_10k_links_on_2000_files_incremental_bounded_memory():
    k = Kit()
    t0 = time.perf_counter()
    g, last, paths, counters = build(k)
    build_s = time.perf_counter() - t0
    entries = k.entries()
    emails = k.emails
    by_id = {e.gud_id: e for e in emails}

    gc.collect()
    tracemalloc.start()
    base_mem = tracemalloc.get_traced_memory()[0]
    r = Replayer(k.store)
    t1 = time.perf_counter()
    for start in range(0, len(entries), BATCH):
        chunk = entries[start:start + BATCH]
        mail = [by_id[e.fields[0][1]] for e in chunk if e.type == "mail"]
        r.feed(mail, chunk, chunk[-1].time)
    feed_s = time.perf_counter() - t1
    gc.collect()
    retained = tracemalloc.get_traced_memory()[0] - base_mem
    tracemalloc.stop()

    chain = r.chain()
    assert len(chain.links) == LINKS + 1
    assert chain.tip.state == last.id
    assert chain.tip.tree_hash == last.tree.hash()

    # One more email, fed incrementally.
    i = 3
    old = f"# file {i}\nvalue = {counters[i]}\n".encode()
    new = f"# file {i}\nvalue = {counters[i] + 1}\n".encode()
    extra = k.push("@alice", last, ops=[Patch(paths[i], make_patch(old, new))])
    t2 = time.perf_counter()
    delta = r.feed([extra.email], [k.entries()[-1]], k.now)
    one_s = time.perf_counter() - t2
    assert [l.push for l in delta.new_links] == [extra.id]

    unshared = (LINKS + 1) * FILES * 100          # what a tree dict per link would cost (very conservatively)
    print(f"\nbuild {build_s:.1f}s, replay {LINKS} links in {feed_s:.1f}s "
          f"({1000 * feed_s / LINKS:.2f} ms/link), retained {retained / 2**20:.1f} MiB "
          f"(unshared trees would be >= {unshared / 2**20:.0f} MiB), one-email feed {1000 * one_s:.1f} ms")
    assert one_s < 1.0
    assert retained < 400 * 2**20
    assert retained < unshared / 4
