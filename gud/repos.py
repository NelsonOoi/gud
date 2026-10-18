"""Repo addresses in one mailbox, and the GUI's saved mailbox token.

A repo is the To address of an inbound ``[gud] genesis`` message. The list is
built from headers already on the row. Message bodies stay unloaded.
"""

from __future__ import annotations

import json
from pathlib import Path

from .mail_primitive import _addr_list, _parse_time

GENESIS_SUBJECT = "[gud] genesis"
TOKEN_FILE = "mailbox.token"
ACTIVE_FILE = "repo.txt"


def read_token(home: Path) -> str | None:
    path = home / TOKEN_FILE
    if not path.is_file():
        return None
    text = path.read_text().strip()
    return text or None


def write_token(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = home / TOKEN_FILE
    path.write_text(token.strip() + "\n")
    path.chmod(0o600)


def read_active(home: Path) -> str | None:
    path = home / ACTIVE_FILE
    if not path.is_file():
        return None
    text = path.read_text().strip().lower()
    return text or None


def write_active(home: Path, address: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ACTIVE_FILE).write_text(address.strip().lower() + "\n")


def check_token(token: str, client=None) -> None:
    """Raise ``PrimitiveError`` when Primitive rejects ``token``."""
    from .mail_primitive import PrimitiveMailbox

    PrimitiveMailbox(token, "repos@gud.local", client=client).ping()


def header_rows(mailbox) -> list[dict]:
    """Genesis rows for the mailbox, without downloading bodies."""
    path = getattr(mailbox, "path", None)
    if path is not None:
        return _jsonl_rows(Path(path))
    search = getattr(mailbox, "iter_subject_headers", None)
    if callable(search):
        return list(search(GENESIS_SUBJECT))
    headers = getattr(mailbox, "iter_headers", None)
    if callable(headers):
        return list(headers())
    return []


def is_repo_address(address: str) -> bool:
    """A repo mailbox is named ``repo`` or ``repo-<id>``. A contributor uses their handle."""
    local = address.split("@", 1)[0].strip().lower()
    return local == "repo" or local.startswith("repo-")


def genesis_addresses(rows: list[dict]) -> list[str]:
    """Repo addresses of genesis messages, newest first.

    A genesis is copied to every member, so the rows also name contributors.
    Only a local-part of ``repo`` or ``repo-<id>`` is a repo.
    """
    latest: dict[str, float] = {}
    for row in rows:
        if str(row.get("subject") or "").strip() != GENESIS_SUBJECT:
            continue
        when = _parse_time(row.get("received_at")) or 0.0
        for addr in message_tos(row):
            if not is_repo_address(addr):
                continue
            if when >= latest.get(addr, -1.0):
                latest[addr] = when
    return [addr for addr, _when in sorted(latest.items(), key=lambda item: (-item[1], item[0]))]


def message_tos(row: dict) -> list[str]:
    """The message's To addresses. A delivery copy's own recipient is only a fallback."""
    parsed = row.get("parsed") if isinstance(row.get("parsed"), dict) else {}
    found: list[str] = []
    found.extend(_addr_list(row.get("to")))
    found.extend(_addr_list(parsed.get("to_addresses")))
    found.extend(_addr_list(row.get("to_addresses")))
    found.extend(_addr_list(row.get("to_email")))
    found.extend(_addr_list(row.get("to_address")))
    if not found:
        found.extend(_addr_list(row.get("recipient")))
    out: list[str] = []
    seen: set[str] = set()
    for addr in found:
        key = addr.lower()
        if key and "@" in key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _jsonl_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


