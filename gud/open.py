"""Open a repo from the GUI, provision one agent, and run the optional in-process swarm.

``python -m gud.swarm`` and the demo start a swarm themselves. They do not read
``GUD_SWARM`` or ``swarm.txt``.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from . import compose, sig
from .config import Config, domain_address, resolved_swarm
from .keys import create_identity
from .mail_primitive import _addr_list, _parse_time
from .memberfile import MEMBERS, format_members
from .repos import GENESIS_SUBJECT, genesis_addresses, is_repo_address, message_tos
from .types import Member

_LOCAL = re.compile(r"[a-z0-9._-]+")
GENESIS_AGENTS: tuple[tuple[str, str], ...] = (
    ("@lead", "lead"),
    ("@tester", "tester"),
    ("@console", "console"),
    ("@sub-1", "agent"),
)
AGENT_ROLES = frozenset({"agent", "lead", "tester", "console"})


def explicit_tos(row: dict) -> list[str]:
    """To addresses on a genesis row. Delivery ``recipient`` is not a repo."""
    parsed = row.get("parsed") if isinstance(row.get("parsed"), dict) else {}
    found: list[str] = []
    for source in (
        row.get("to"),
        parsed.get("to_addresses"),
        row.get("to_addresses"),
        row.get("to_email"),
        row.get("to_address"),
    ):
        found.extend(_addr_list(source))
    out: list[str] = []
    seen: set[str] = set()
    for addr in found:
        key = addr.lower()
        if key and "@" in key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def catalog_addresses(rows: list[dict]) -> list[str]:
    """Genesis repo addresses, newest first.

    Every explicit To of a ``[gud] genesis`` is a repo, so a GUI name such as
    ``acme@domain`` shows up beside ``repo`` / ``repo-<id>``. A row that only
    names a contributor as its recipient does not.
    """
    latest: dict[str, float] = {}

    def bump(addr: str, when: float) -> None:
        key = (addr or "").strip().lower()
        if "@" not in key:
            return
        if when >= latest.get(key, -1.0):
            latest[key] = when

    for row in rows:
        if str(row.get("subject") or "").strip() != GENESIS_SUBJECT:
            continue
        when = _parse_time(row.get("received_at")) or 0.0
        explicit = explicit_tos(row)
        for addr in explicit:
            bump(addr, when)
        if explicit:
            continue
        for addr in message_tos(row):
            if is_repo_address(addr):
                bump(addr, when)
    for addr in genesis_addresses(rows):
        latest.setdefault(addr, -1.0)
    return [addr for addr, _when in sorted(latest.items(), key=lambda item: (-item[1], item[0]))]


def repo_address(cfg: Config, name: str) -> str:
    """``repo-<name>@domain``. ``repo`` and a name that already starts with ``repo-`` stay as typed."""
    local = (name or "").strip().lower()
    if not local or not _LOCAL.fullmatch(local):
        raise ValueError("name must be a single local-part")
    if local != "repo" and not local.startswith("repo-"):
        local = f"repo-{local}"
    domain = (cfg.domain or "").strip().lower()
    if not domain:
        raise ValueError("name must be a single local-part")
    return f"{local}@{domain}"


def human_members(emails: list[str]) -> list[Member]:
    """One human per email. Handles that collide with an agent or another human get ``-2``, ``-3``, ..."""
    if not emails:
        raise ValueError("at least one human email is required")
    taken = {handle for handle, _role in GENESIS_AGENTS}
    members: list[Member] = []
    seen: set[str] = set()
    for raw in emails:
        address = _human_address(raw)
        if address in seen:
            continue
        seen.add(address)
        local = address.split("@", 1)[0]
        handle = _with_suffix(local, taken)
        taken.add(handle)
        members.append(Member(handle, "human", address, None, None))
    if not members:
        raise ValueError("at least one human email is required")
    return members


def send_genesis(cfg: Config, mailbox, humans: list[Member]) -> None:
    """One genesis push, parent none, signed by ``@lead``. The tree is only ``.gud/members``."""
    if not humans:
        raise ValueError("at least one human email is required")
    sponsor = humans[0].handle
    members: dict[str, Member] = {member.handle: member for member in humans}
    lead: compose.Identity | None = None
    for handle, role in GENESIS_AGENTS:
        raw = ensure_private(cfg, handle)
        address = domain_address(cfg.domain, handle).lower()
        ident = compose.Identity(handle, address, raw)
        members[handle] = Member(handle, role, address, sig.public_of(raw), sponsor)
        if handle == "@lead":
            lead = ident
    if lead is None:
        raise RuntimeError("genesis roster is missing @lead")
    tree = {MEMBERS: format_members(members)}
    body, _state, _tree_hash = compose.push_body(
        lead, parent=None, parent_state=None, parent_tree={},
        new_tree=tree, intent=f"genesis {mailbox.repo_address}",
    )
    compose.send(mailbox, lead, members, "[gud] genesis", body)
    for member in humans:
        mailbox.send(
            from_addr=lead.address, to=member.address, cc=[mailbox.repo_address],
            subject=f"[gud] {mailbox.repo_address}",
            body=compose.notify_body(
                lead, f"{mailbox.repo_address} is open. You are {member.handle}. "
                "Mail that address to open a task.\n",
            ),
        )


@dataclass(frozen=True)
class PreparedAgent:
    handle: str
    role: str
    address: str
    public_key: str
    private_key: str
    addition: Member | None


def prepare_agent(cfg: Config, handle: str, role: str | None, roster: dict[str, Member]) -> PreparedAgent:
    """Reuse or create the key. ``addition`` is set when the tip roster does not already contain the handle."""
    normalized = normalize_handle(handle)
    chosen = normalize_role(role)
    raw = ensure_private(cfg, normalized)
    public = sig.public_of(raw)
    address = domain_address(cfg.domain, normalized).lower()
    addition = None
    if normalized not in roster:
        sponsor = next((member.handle for member in roster.values() if member.role == "human"), None)
        addition = Member(normalized, chosen, address, public, sponsor)
    return PreparedAgent(
        handle=normalized,
        role=chosen,
        address=address,
        public_key=public,
        private_key=base64.b64encode(raw).decode("ascii"),
        addition=addition,
    )


def ensure_private(cfg: Config, handle: str) -> bytes:
    """Load ``cfg.keys_dir/<handle>.key``, or generate and save it when the file is missing."""
    path = cfg.key_path(handle)
    if path.exists():
        return sig.load_private(path)
    try:
        ident, _public = create_identity(cfg, handle)
    except FileExistsError:
        return sig.load_private(path)
    if not ident.private_raw:
        raise FileNotFoundError(path)
    return ident.private_raw


def normalize_handle(value: str) -> str:
    text = (value or "").strip().lower()
    if text.startswith("@"):
        text = text[1:]
    if not text or not _LOCAL.fullmatch(text):
        raise ValueError("handle must be a single local-part")
    return f"@{text}"


def normalize_role(role: str | None) -> str:
    text = (role or "agent").strip().lower()
    if text not in AGENT_ROLES:
        raise ValueError("role must be agent, lead, tester, or console")
    return text


def boot_swarm(ctx) -> None:
    """Start once when the resolved flag is on and a mailbox exists. No genesis leaves it off."""
    if getattr(ctx, "mailbox", None) is None:
        return
    if not resolved_swarm(ctx.cfg):
        return
    try:
        start_swarm(ctx)
    except SystemExit as exc:
        print(f"swarm: left off ({exc})", flush=True)
    except Exception as exc:
        print(f"swarm: left off ({exc})", flush=True)


def start_swarm(ctx) -> None:
    """Construct ``gud.swarm.Swarm`` on the current mailbox. Raises ``SystemExit`` when it cannot."""
    with ctx._swarm_lock:
        if ctx.swarm_enabled:
            return
        if ctx.mailbox is None:
            raise SystemExit("mailbox sign-in required")
        from .swarm import Swarm

        key = getattr(ctx.cfg, "openrouter_key", None) or ""
        mode = "llm" if str(key).strip() else "scripted"
        # Do not wait for genesis. The POST returns 409, and startup stays up.
        swarm = Swarm(
            ctx.cfg, ctx.mailbox, mode=mode, workers=1, clock=ctx.clock, genesis_timeout=0,
        )
        swarm.start()
        ctx.swarm = swarm
        ctx.swarm_enabled = True


def stop_swarm(ctx) -> None:
    with ctx._swarm_lock:
        swarm = ctx.swarm
        ctx.swarm = None
        ctx.swarm_enabled = False
    if swarm is not None:
        swarm.stop()


def swarm_is_on(ctx) -> bool:
    with ctx._swarm_lock:
        return bool(ctx.swarm_enabled)


def _human_address(raw: str) -> str:
    address = (raw or "").strip().lower()
    local, sep, domain = address.partition("@")
    if not sep or not domain or not _LOCAL.fullmatch(local) or any(ch.isspace() for ch in domain):
        shown = (raw or "").strip() or "(empty)"
        raise ValueError(f"invalid email {shown}")
    return address


def _with_suffix(base: str, taken: set[str]) -> str:
    handle = f"@{base}"
    if handle not in taken:
        return handle
    number = 2
    while f"@{base}-{number}" in taken:
        number += 1
    return f"@{base}-{number}"
