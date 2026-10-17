"""Open the mailbox named by ``Config.mailbox``."""

from __future__ import annotations

from .config import Config
from .mail_fake import FileMailbox
from .mail_primitive import PrimitiveMailbox
from .types import Mailbox


def open_mailbox(cfg: Config) -> Mailbox:
    kind = (cfg.mailbox or "fake").strip().lower()
    if kind == "fake":
        return FileMailbox(cfg.home / "mailbox.jsonl", cfg.repo_address, cfg.domain)
    if kind == "primitive":
        if not cfg.primitive_api_key:
            raise RuntimeError(
                "PRIMITIVE_API_KEY is missing. Set it in the environment or .env "
                "before using GUD_MAILBOX=primitive, or use GUD_MAILBOX=fake."
            )
        return PrimitiveMailbox(cfg.primitive_api_key, cfg.repo_address)
    raise ValueError(f"unknown GUD_MAILBOX {cfg.mailbox!r} (expected 'fake' or 'primitive')")
