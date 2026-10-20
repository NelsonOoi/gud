"""Harness for tester tests: a `Kit` repo, a `Tester` (@t1) wired to it through `LocalClient`, and a
tiny test suite in the tree (`check.sh` fails when any `*.txt` file contains `BAD`)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from gud.core.replay import Replayer
from gud.runners.local import LocalRunner
from gud.tester_v1 import LocalClient, Tester, TesterIdentity
from tests.v1.kit import Kit

PY = sys.executable

CHECK = (
    "#!/bin/sh\n"
    "for f in *.txt; do\n"
    "  if grep -q BAD \"$f\"; then echo \"FAIL $f\"; exit 1; fi\n"
    "done\n"
    "echo ok\n"
)

FILES = {"check.sh": ("100755", CHECK), "a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n", "d.txt": "d\n"}


def repo(test: str, files: dict[str, Any] | None = None, **kw) -> tuple[Kit, Any]:
    """A kit whose genesis (by @alice, so her own later pushes need no consent) holds FILES."""
    k = Kit(test=test, **kw)
    g = k.genesis(dict(FILES if files is None else files), author="@alice")
    return k, g


class CountingRunner(LocalRunner):
    """LocalRunner that records the trees and argument lists it ran."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, files, command, *, image=None, timeout_ms, extra_args=()):
        self.calls.append((command, tuple(extra_args)))
        return super().run(files, command, image=image, timeout_ms=timeout_ms, extra_args=extra_args)


def make_tester(k: Kit, *, runner=None, parallel: int = 1, state_dir: Path | None = None,
           replayer: Replayer | None = None, send=None) -> Tester:
    r = replayer if replayer is not None else Replayer(k.store)

    def fetch():
        return list(k.emails), k.entries()

    def deliver(env, summary, text):
        k.raw_envelope(env)

    client = LocalClient(r, fetch, send or deliver, now=lambda: k.now)
    return Tester(client, runner if runner is not None else CountingRunner(),
                  identity=TesterIdentity("@t1", k.key("@t1")), parallel=parallel,
                  clock=lambda: k.now / 1000, state_dir=state_dir)


def chain_tests(k: Kit, scope: str | None = None) -> list:
    out = []
    for e in k.emails:
        env = e.envelope
        if env is not None and env.kind == "test" and (scope is None or env.get("Gud-Scope") == scope):
            out.append(env)
    return out
