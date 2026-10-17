"""Rendering and parsing gud emails, plus the canonical hashes. See SPEC.md."""

from __future__ import annotations

import hashlib
import re

from .types import Parsed, Tree

ZERO = "0" * 64
FENCE_OPEN = "```diff"
FENCE_CLOSE = "```"
TRAILER_RE = re.compile(r"^(Gud-[A-Za-z-]+):\s?(.*)$", re.IGNORECASE)

CANONICAL = {n.lower(): n for n in [
    "Gud-Kind", "Gud-Author", "Gud-Parent", "Gud-Base", "Gud-State", "Gud-Tree", "Gud-Task",
    "Gud-Consent", "Gud-Target", "Gud-Reason", "Gud-Result", "Gud-Requester", "Gud-Parent-Task",
    "Gud-Assignee", "Gud-Tip", "Gud-Sig",
]}


def canonical(name: str) -> str:
    return CANONICAL.get(name.lower(), "-".join(p.capitalize() for p in name.split("-")))


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def short(h: str) -> str:
    return h[:12]


def normalize(text: str) -> str:
    """Tree files end with exactly one newline."""
    return text.rstrip("\n") + "\n"


def tree_hash(tree: Tree) -> str:
    h = hashlib.sha256()
    for path in sorted(tree):
        h.update(f"{path}\0{sha(tree[path])}\n".encode())
    return h.hexdigest()


def state_hash(parent_state: str, author: str, consent: list[str], diff: str) -> str:
    h = hashlib.sha256(f"{parent_state}\n{author}\n{','.join(sorted(consent))}\n".encode())
    h.update(diff.encode())
    return h.hexdigest()


def parse_list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def parse_body(body: str) -> Parsed:
    lines = body.replace("\r\n", "\n").split("\n")
    fence_start = fence_end = None
    i = 0
    while i < len(lines):
        if lines[i].strip() == FENCE_OPEN:
            j = i + 1
            while j < len(lines) and lines[j].strip() != FENCE_CLOSE:
                j += 1
            if j < len(lines):
                fence_start, fence_end = i, j
            i = j + 1
        else:
            i += 1
    diff = ""
    outside = lines
    if fence_start is not None:
        diff_lines = lines[fence_start + 1:fence_end]
        diff = "\n".join(diff_lines) + ("\n" if diff_lines else "")
        outside = lines[:fence_start] + lines[fence_end + 1:]
    trailers: dict[str, str] = {}
    first_trailer = None
    for idx, line in enumerate(outside):
        m = TRAILER_RE.match(line.strip())
        if m:
            trailers[canonical(m.group(1))] = m.group(2).strip()
            if first_trailer is None:
                first_trailer = idx
    text_lines = outside if first_trailer is None else outside[:first_trailer]
    return Parsed(text="\n".join(text_lines).strip(), trailers=trailers, diff=diff)


def render_body(text: str, trailers: dict[str, str], diff: str = "") -> str:
    out = [text.strip(), ""] if text.strip() else []
    order = [k for k in CANONICAL.values() if k in trailers and k != "Gud-Sig"]
    order += sorted(k for k in trailers if k not in CANONICAL.values())
    out += [f"{k}: {trailers[k]}" for k in order]
    if "Gud-Sig" in trailers:
        out.append(f"Gud-Sig: {trailers['Gud-Sig']}")
    if diff:
        out += ["", FENCE_OPEN, diff.rstrip("\n"), FENCE_CLOSE]
    return "\n".join(out) + "\n"


def signing_bytes(trailers: dict[str, str], diff: str) -> bytes:
    lines = [f"{canonical(k)}: {v}\n" for k, v in sorted(trailers.items(), key=lambda kv: canonical(kv[0]))
             if canonical(k) != "Gud-Sig"]
    return ("".join(lines) + "--\n" + diff).encode()
