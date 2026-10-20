"""Adapter registry keyed by name (`GUD_TRANSPORT`), with per-member configuration.

Built-in adapters are imported lazily by name: `open_mailbox("imap_smtp", cfg)` imports
`gud.transport.imap_smtp` and calls its `from_config(cfg)`. Each member may use a different
provider: `member_config(home, handle)` reads `GUD_HOME/members/<handle>/transport.toml`.

transport.toml:

    transport = "imap_smtp"
    imap_host = "imap.fastmail.com"
    smtp_host = "smtp.fastmail.com"
    username = "repo@example.org"
    password_env = "REPO_IMAP_PASSWORD"     # any `<key>_env` is read from the environment

Environment (`from_env`): `GUD_TRANSPORT=<name>` and `GUD_<NAME>_<KEY>=<value>`, for example
`GUD_IMAP_SMTP_IMAP_HOST=imap.example.org` becomes `cfg["imap_host"]`.
"""

from __future__ import annotations

import importlib
import os
import threading
import tomllib
from pathlib import Path
from typing import Callable, Mapping

from gud.core.types import Handle

from .base import Mailbox, TransportError

Factory = Callable[[Mapping[str, str]], Mailbox]

BUILTINS: tuple[str, ...] = ("fake", "imap_smtp", "jmap", "primitive", "ses", "postmark", "gmail")

_lock = threading.Lock()
_factories: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register (or replace) an adapter factory. Names are lowercase identifiers."""
    if not name or not name.replace("_", "").isalnum() or name.lower() != name:
        raise ValueError(f"invalid transport name {name!r}")
    with _lock:
        _factories[name] = factory


def names() -> tuple[str, ...]:
    with _lock:
        extra = tuple(sorted(n for n in _factories if n not in BUILTINS))
    return BUILTINS + extra


def _factory(name: str) -> Factory:
    with _lock:
        found = _factories.get(name)
    if found is not None:
        return found
    if name not in BUILTINS:
        raise TransportError(f"unknown transport {name!r}; known: {', '.join(names())}")
    module_name = f"gud.transport.{name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise TransportError(f"transport {name!r} is not implemented yet") from exc
        raise
    factory = getattr(module, "from_config", None)
    if factory is None:
        raise TransportError(f"{module_name} has no from_config")
    return factory


def open_mailbox(name: str, cfg: Mapping[str, str]) -> Mailbox:
    """Lazy-import the adapter and build it from string config."""
    return _factory((name or "").strip().lower())(dict(_resolve_env_refs(cfg)))


def from_env(env: Mapping[str, str] | None = None) -> Mailbox:
    """`GUD_TRANSPORT` + `GUD_<NAME>_*` variables."""
    env = os.environ if env is None else env
    name = (env.get("GUD_TRANSPORT") or "").strip().lower()
    if not name:
        raise TransportError("GUD_TRANSPORT is not set")
    prefix = f"GUD_{name.upper()}_"
    cfg = {key[len(prefix):].lower(): value for key, value in env.items() if key.startswith(prefix)}
    return open_mailbox(name, _resolve_env_refs(cfg, env))


def member_config(home: Path, handle: Handle) -> tuple[str, dict[str, str]]:
    """`GUD_HOME/members/<handle>/transport.toml` -> (transport name, string config).

    Top-level keys and the keys of a table named after the transport are merged (the table
    wins); `<key>_env` values are read from the environment into `<key>`."""
    path = Path(home) / "members" / handle / "transport.toml"
    if not path.exists():
        raise TransportError(f"no transport config for {handle} at {path}")
    return read_transport_toml(path)


def read_transport_toml(path: Path) -> tuple[str, dict[str, str]]:
    """A transport.toml (a member's, or a host directory's) -> (transport name, string config):
    top-level keys merged with the table named after the transport; `<key>_env` values are read
    from the environment into `<key>`, so secrets need not be written to the file."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TransportError(f"no transport config at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise TransportError(f"{path}: {exc}") from exc
    name = str(data.get("transport") or "").strip().lower()
    if not name:
        raise TransportError(f"{path}: missing `transport`")
    cfg: dict[str, str] = {}
    for key, value in data.items():
        if key != "transport" and not isinstance(value, dict):
            cfg[key] = _stringify(value)
    table = data.get(name)
    if isinstance(table, dict):
        for key, value in table.items():
            if not isinstance(value, dict):
                cfg[key] = _stringify(value)
    return name, _resolve_env_refs(cfg)


def _stringify(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def _resolve_env_refs(cfg: Mapping[str, str], env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    out = dict(cfg)
    for key in list(cfg):
        if key.endswith("_env") and len(key) > 4:
            target = key[:-4]
            if target not in out or not out[target]:
                value = env.get(str(cfg[key]))
                if value is None:
                    raise TransportError(f"environment variable {cfg[key]!r} (for {target}) is not set")
                out[target] = value
            out.pop(key)
    return out

