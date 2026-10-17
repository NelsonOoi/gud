"""Open the mailbox named by ``Config.mailbox``.

``open_mailbox`` is the host's own account (unchanged). ``open_member_mailbox``
is what a swarm, tester, or MCP server should call: with ``GUD_HOST_URL`` it
reads the host mirror and sends through the member's own account.
"""

from __future__ import annotations

from .config import Config, configured_address
from .mail_fake import FileMailbox
from .mail_primitive import PrimitiveMailbox
from .types import Mailbox


def open_own_mailbox(cfg: Config, repo_address: str | None = None) -> Mailbox:
    """This process's Primitive (or fake) account. ``repo_address`` is the repo it mails."""
    repo = repo_address or cfg.repo_address
    kind = (cfg.mailbox or "fake").strip().lower()
    if kind == "fake":
        return FileMailbox(cfg.home / "mailbox.jsonl", repo, cfg.domain)
    if kind == "primitive":
        if not cfg.primitive_api_key:
            raise RuntimeError(
                "PRIMITIVE_API_KEY is missing. Set it in the environment or .env "
                "before using GUD_MAILBOX=primitive, or use GUD_MAILBOX=fake."
            )
        return PrimitiveMailbox(cfg.primitive_api_key, repo)
    raise ValueError(f"unknown GUD_MAILBOX {cfg.mailbox!r} (expected 'fake' or 'primitive')")


def open_mailbox(cfg: Config) -> Mailbox:
    return open_own_mailbox(cfg)


def open_member_mailbox(cfg: Config) -> Mailbox:
    """Host mailbox, or a remote mirror when ``cfg.host_url`` (``GUD_HOST_URL``) is set.

    The mirror identity is ``GUD_HANDLE`` plus the key at ``GUD_HOME/keys/<handle>.key``,
    using ``GUD_ADDRESS`` when that is set. Sends go through ``open_mailbox`` — the
    member's own ``PRIMITIVE_API_KEY``, or the fake mailbox when ``GUD_MAILBOX=fake``.
    """
    if not cfg.host_url:
        return open_mailbox(cfg)
    from .keys import load_identity
    from .mail_remote import RemoteMailbox
    if not cfg.handle:
        raise RuntimeError("GUD_HANDLE is required when GUD_HOST_URL is set")
    identity = load_identity(cfg, cfg.handle, address=configured_address(cfg))
    return RemoteMailbox(str(cfg.host_url), identity, open_own_mailbox(cfg), cfg.repo_address)
