"""A hard spending cap across eval runs (and concurrent run processes), kept in one JSON ledger file.

    ledger = CapLedger(Path("evals/v1/runs/spend.json"), cap_usd=30)
    grant = ledger.reserve("gud-cc-todo-stack-n4-t1", want=1.20)   # 0.0 when the cap is reached
    ... run with a run budget of `grant` (the supervisor kills every agent when the run reaches it) ...
    ledger.commit("gud-cc-todo-stack-n4-t1", spent=0.87)

A run reserves its whole budget before it starts and releases the unused part when it commits, so the sum
of committed spend and open reservations never exceeds the cap, however many runs are in flight. Every
change takes an exclusive `flock` on `<ledger>.lock`. A run that crashed without committing keeps its
reservation (it counts against the cap) until `release_stale` is called.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


class CapLedger:
    def __init__(self, path: Path, cap_usd: float, *, min_grant: float = 0.05):
        self.path = Path(path)
        self.cap = float(cap_usd)
        self.min_grant = float(min_grant)

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                data = json.loads(self.path.read_text()) if self.path.exists() else {}
                data.setdefault("runs", {})
                data.setdefault("reserved", {})
                yield data
                tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @staticmethod
    def _spent(data: dict) -> float:
        return sum(float(r.get("spent_usd") or 0) for r in data["runs"].values())

    def state(self) -> dict:
        with self._locked() as data:
            spent = self._spent(data)
            reserved = sum(float(v.get("usd") or 0) for v in data["reserved"].values())
            return {"cap_usd": self.cap, "spent_usd": round(spent, 4), "reserved_usd": round(reserved, 4),
                    "remaining_usd": round(self.cap - spent - reserved, 4), "runs": len(data["runs"])}

    def reserve(self, key: str, want: float) -> float:
        """Reserve up to `want` dollars for run `key`; returns the grant (0.0 when below `min_grant`)."""
        with self._locked() as data:
            data["cap_usd"] = self.cap
            spent = self._spent(data)
            reserved = sum(float(v.get("usd") or 0) for k, v in data["reserved"].items() if k != key)
            grant = round(min(float(want), self.cap - spent - reserved), 4)
            if grant < self.min_grant:
                return 0.0
            data["reserved"][key] = {"usd": grant, "at": time.time()}
            return grant

    def commit(self, key: str, spent: float, **info) -> None:
        with self._locked() as data:
            data["reserved"].pop(key, None)
            n = 1
            name = key
            while name in data["runs"]:
                n += 1
                name = f"{key}#{n}"
            data["runs"][name] = {"spent_usd": round(float(spent), 6), "at": time.time(), **info}

    def release_stale(self, older_than_s: float = 6 * 3600) -> list[str]:
        with self._locked() as data:
            now = time.time()
            stale = [k for k, v in data["reserved"].items() if now - float(v.get("at") or 0) > older_than_s]
            for k in stale:
                data["reserved"].pop(k)
            return stale
