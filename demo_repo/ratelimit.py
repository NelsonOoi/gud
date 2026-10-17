"""A token bucket keyed by user or route.

`allow` spends tokens; they refill at `rate` per second up to `capacity`.
Nothing in auth or billing calls this yet. The demo task wires it into `login`
and adds a way to drain a key after abuse (see TASKS.md).
"""

from __future__ import annotations

import time


class TokenBucket:
    """One bucket per key. Pass `now` in tests so refill does not depend on the clock."""

    def __init__(self, rate: float, capacity: float):
        if rate < 0 or capacity <= 0:
            raise ValueError("rate must be >= 0 and capacity must be > 0")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens: dict[str, float] = {}
        self._updated: dict[str, float] = {}

    def allow(self, key: str, now: float | None = None, cost: float = 1.0) -> bool:
        """Spend `cost` tokens for `key`. False when the bucket cannot afford it."""
        if cost <= 0:
            raise ValueError("cost must be > 0")
        now = time.monotonic() if now is None else now
        tokens = self._available(key, now)
        if tokens < cost:
            self._tokens[key] = tokens
            self._updated[key] = now
            return False
        self._tokens[key] = tokens - cost
        self._updated[key] = now
        return True

    def remaining(self, key: str, now: float | None = None) -> int:
        """Whole tokens available right now. Does not spend any."""
        now = time.monotonic() if now is None else now
        return int(self._available(key, now))

    def reset(self) -> None:
        """Forget every key. The rate and capacity stay."""
        self._tokens.clear()
        self._updated.clear()

    def _available(self, key: str, now: float) -> float:
        if key not in self._tokens:
            return self.capacity
        elapsed = max(0.0, now - self._updated.get(key, now))
        return min(self.capacity, self._tokens[key] + elapsed * self.rate)


_default = TokenBucket(rate=10, capacity=10)


def configure(rate: float, capacity: float) -> TokenBucket:
    """Replace the process-wide bucket. Returns it."""
    global _default
    _default = TokenBucket(rate, capacity)
    return _default


def reset() -> None:
    """Empty the process-wide bucket."""
    _default.reset()


def allow(key: str, now: float | None = None, cost: float = 1.0) -> bool:
    return _default.allow(key, now=now, cost=cost)


def remaining(key: str, now: float | None = None) -> int:
    return _default.remaining(key, now=now)
