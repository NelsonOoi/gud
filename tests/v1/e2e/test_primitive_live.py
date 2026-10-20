"""Live smoke over Primitive (opt-in: GUD_LIVE_PRIMITIVE=1). The credentials come from the
environment (PRIMITIVE_API_KEY, PRIMITIVE_EMAIL) or from the file named by GUD_LIVE_ENV_FILE
(default: the .env next to the checkout); the key is passed to processes in the environment and
transport.toml files name it (`api_key_env`), so it is never written to disk or printed.

Flow (a fresh repo address `repo-e2e-<stamp>@<domain>`, at most GUD_LIVE_MAX_EMAILS = 60 sends):
genesis from a seed; @alice, @bob and the tester @box1 join and are admitted; @human pushes a
new file (consent alone); @bob edits @human's line (approved); @alice adds a file; @human edits
@bob's line, @bob objects with a failing test, the tester confirms it, @human revises with the
test, it links; verify everywhere. Emails are counted from the Primitive mailboxes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.v1.e2e.scenario import Scenario, Transport, smoke_flow

ROOT = Path(__file__).resolve().parents[3]


def _credentials() -> tuple[str, str]:
    key, email = os.environ.get("PRIMITIVE_API_KEY"), os.environ.get("PRIMITIVE_EMAIL")
    env_file = Path(os.environ.get("GUD_LIVE_ENV_FILE") or ROOT.parent / ".env")
    if not (key and email):
        for parent in [env_file, *[p / ".env" for p in ROOT.parents]]:
            if parent.is_file():
                values = {}
                for line in parent.read_text().splitlines():
                    k, sep, v = line.partition("=")
                    if sep and not k.strip().startswith("#"):
                        values[k.strip()] = v.strip().strip("'\"")
                key, email = key or values.get("PRIMITIVE_API_KEY"), email or values.get("PRIMITIVE_EMAIL")
                if key and email:
                    break
    if not (key and email):
        pytest.skip("no Primitive credentials (PRIMITIVE_API_KEY, PRIMITIVE_EMAIL)")
    return key, email


def _count(api_key: str, addresses: list[str]) -> dict[str, int]:
    from gud.transport.primitive import PrimitiveMailbox
    out = {}
    for addr in addresses:
        box = PrimitiveMailbox(api_key, addr)
        n, cursor = 0, None
        while True:
            items, cursor = box.list(since=cursor, limit=500)
            n += len(items)
            if not items:
                break
        out[addr] = n
        box.close()
    return out


@pytest.mark.skipif(os.environ.get("GUD_LIVE_PRIMITIVE") != "1", reason="set GUD_LIVE_PRIMITIVE=1 for the live run")
def test_e2e_primitive_live(tmp_path: Path, capsys):
    key, email = _credentials()
    domain = email.rpartition("@")[2]
    stamp = time.strftime("%Y%m%d%H%M%S")

    def address(user: str) -> str:
        return f"{'repo' if user == 'repo' else user}-e2e-{stamp}@{domain}"

    t = Transport(name="primitive", repo=address("repo"), address=address,
                  settings=lambda u: {"api_key_env": "PRIMITIVE_API_KEY", "subscribe_wait": "10"},
                  window="20s", grace="8s", slow=3.0, on_tip="links=3 time=10m",
                  env={"PRIMITIVE_API_KEY": key, "GUD_WAIT": "90"})
    sc = Scenario(tmp_path, t)
    started = time.monotonic()
    try:
        try:
            smoke_flow(sc)
        except BaseException:
            import sys
            sys.stderr.write(sc.logs())
            raise
    finally:
        sc.close()
    counts = _count(key, [t.repo] + [address(u) for u in ("human", "alice", "bob", "box1")])
    sent = sum(counts.values())
    result = {**sc.timings, "notes": sc.notes, "repo": t.repo, "emails": counts, "emails_total": sent,
              "wall_s": round(time.monotonic() - started, 1)}
    with capsys.disabled():
        print("\ne2e primitive (live):", json.dumps(result, sort_keys=True))
    assert sent <= int(os.environ.get("GUD_LIVE_MAX_EMAILS", "60")), counts
