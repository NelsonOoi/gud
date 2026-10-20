"""The webhook-fed local store used by the SES and Postmark adapters."""

from __future__ import annotations

import threading
import time

import pytest

from gud.transport.base import TransportError
from gud.transport.spool import Spool


def test_ingest_dedupes_by_provider_id_and_cursors_resume(tmp_path) -> None:
    spool = Spool(tmp_path / "s")
    assert spool.head() is None and spool.list() == ([], None)
    first = spool.ingest(b"raw-1", 1_000, "p1", source="webhook")
    assert first is not None and first.cursor == "1"
    assert spool.ingest(b"other bytes", 2_000, "p1") is None, "first copy wins"
    spool.ingest(b"raw-2", 900, "p2")
    items, cursor = spool.list()
    assert [(i.raw_mime, i.received_at, i.cursor) for i in items] == [(b"raw-1", 1_000, "1"), (b"raw-2", 900, "2")]
    other = Spool(tmp_path / "s")               # another process sees the same store
    assert other.list(since="1")[0] == items[1:] and other.head() == cursor == "2"
    assert other.ids() == {"p1", "p2"} and other.has("p2")
    with pytest.raises(TransportError):
        spool.list(since="x")
    with pytest.raises(TransportError):
        spool.ingest(b"x", 1, "")


def test_wait_wakes_on_ingest_and_runs_catch_up(tmp_path) -> None:
    spool = Spool(tmp_path / "s")
    threading.Timer(0.2, lambda: spool.ingest(b"late", 5, "p")).start()
    t0 = time.monotonic()
    assert [i.raw_mime for i in spool.wait(since=None, timeout=5)] == [b"late"]
    assert time.monotonic() - t0 < 2
    calls = []
    assert spool.wait(since="1", timeout=0.35, on_idle=lambda: calls.append(1), idle_every=0.1) == []
    assert len(calls) >= 2
