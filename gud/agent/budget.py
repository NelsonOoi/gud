"""Budgets for the reference agent (PLAN Phase 4 "Budgets"): dollars and tokens, per member and
per task. When one runs out the runtime escalates the work (`comment` with `Gud-Escalate:
budget`, SPEC §16.7) and stops spending on it.

Tokens counted are input + output + cache-creation tokens of each turn (cache reads are
discounted to a tenth: they are what keeps warm sessions cheap, and counting them in full would
make a token budget mostly measure context length)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass
class Budget:
    usd: float | None
    tokens: int | None
    spent_usd: float = 0.0
    spent_tokens: int = 0

    def charge(self, usd: float, tokens: int) -> None:
        self.spent_usd += max(0.0, float(usd))
        self.spent_tokens += max(0, int(tokens))

    @property
    def exhausted(self) -> bool:
        return ((self.usd is not None and self.spent_usd >= self.usd)
                or (self.tokens is not None and self.spent_tokens >= self.tokens))

    @property
    def remaining_usd(self) -> float | None:
        return None if self.usd is None else max(0.0, self.usd - self.spent_usd)

    def describe(self) -> str:
        parts = []
        if self.usd is not None:
            parts.append(f"${self.spent_usd:.2f} of ${self.usd:.2f}")
        if self.tokens is not None:
            parts.append(f"{self.spent_tokens} of {self.tokens} tokens")
        return ", ".join(parts) or "unlimited"

    def to_json(self) -> dict:
        return {"usd": self.usd, "tokens": self.tokens, "spent_usd": self.spent_usd, "spent_tokens": self.spent_tokens}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Budget:
        return cls(d.get("usd"), d.get("tokens"), float(d.get("spent_usd") or 0.0), int(d.get("spent_tokens") or 0))


def turn_tokens(usage: Mapping[str, Any] | None) -> int:
    """Tokens charged for one turn's usage block (see the module docstring)."""
    if not usage:
        return 0
    get = lambda k: int(usage.get(k) or 0)  # noqa: E731
    return get("input_tokens") + get("output_tokens") + get("cache_creation_input_tokens") + get("cache_read_input_tokens") // 10


@dataclass
class Ledger:
    """The member budget plus one budget per task (created from the per-task limits on first use),
    and which tasks were already escalated. Persisted to `path` after every change."""
    member: Budget
    task_usd: float | None = None
    task_tokens: int | None = None
    path: Path | None = None
    tasks: dict[str, Budget] = field(default_factory=dict)
    escalated: set[str] = field(default_factory=set)
    member_escalated: bool = False

    @classmethod
    def load(cls, member: Budget, *, task_usd: float | None = None, task_tokens: int | None = None,
             path: Path | None = None) -> Ledger:
        """Restore spending from `path` (the limits always come from the arguments)."""
        ledger = cls(member, task_usd, task_tokens, Path(path) if path else None)
        if ledger.path is not None and ledger.path.exists():
            data = json.loads(ledger.path.read_text("utf-8"))
            m = data.get("member") or {}
            member.spent_usd = float(m.get("spent_usd") or 0.0)
            member.spent_tokens = int(m.get("spent_tokens") or 0)
            for task, b in (data.get("tasks") or {}).items():
                ledger.tasks[task] = Budget(task_usd, task_tokens, float(b.get("spent_usd") or 0.0),
                                            int(b.get("spent_tokens") or 0))
            ledger.escalated = set(data.get("escalated") or ())
            ledger.member_escalated = bool(data.get("member_escalated"))
        return ledger

    def task(self, task: str) -> Budget:
        if task not in self.tasks:
            self.tasks[task] = Budget(self.task_usd, self.task_tokens)
        return self.tasks[task]

    def charge(self, usd: float, tokens: int, task: str | None = None) -> None:
        self.member.charge(usd, tokens)
        if task:
            self.task(task).charge(usd, tokens)
        self.save()

    def blocked(self, task: str | None = None) -> str | None:
        """Why no more turns may be spent (on `task`), or None."""
        if self.member.exhausted:
            return f"member budget exhausted ({self.member.describe()})"
        if task and (self.task_usd is not None or self.task_tokens is not None) and self.task(task).exhausted:
            return f"task budget exhausted ({self.task(task).describe()})"
        return None

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"member": self.member.to_json(), "tasks": {k: v.to_json() for k, v in sorted(self.tasks.items())},
                "escalated": sorted(self.escalated), "member_escalated": self.member_escalated}
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)
