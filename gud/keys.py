"""Local Ed25519 identities. Private keys live under ``cfg.keys_dir``, never in the tree or the mailbox.

A host with no key files (Manufact) can set ``GUD_KEY_<HANDLE>`` to the base64 raw
Ed25519 private key. ``GUD_KEY`` is that key for ``@console``.
"""

from __future__ import annotations

import base64
import os

from . import sig
from .compose import Identity
from .config import Config


def create_identity(cfg: Config, handle: str, overwrite: bool = False) -> tuple[Identity, str]:
    """Generate an Ed25519 key, store it at ``cfg.key_path(handle)`` (mode 0600).

    Returns ``(Identity, public_key)``. Refuses to replace an existing key unless
    ``overwrite`` is true.
    """
    handle = _handle(handle)
    path = cfg.key_path(handle)
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"key for {handle} already exists at {path}; pass overwrite=True to replace it"
        )
    raw, public = sig.generate()
    sig.save_private(path, raw)
    return Identity(handle, cfg.address(handle), raw), public


def load_identity(cfg: Config, handle: str, address: str | None = None) -> Identity:
    handle = _handle(handle)
    path = cfg.key_path(handle)
    if path.exists():
        raw = sig.load_private(path)
    else:
        raw = env_private(handle)
        if raw is None:
            raise FileNotFoundError(path)
    return Identity(handle, address or cfg.address(handle), raw)


def env_private(handle: str) -> bytes | None:
    """Base64 raw Ed25519 key from ``GUD_KEY_<HANDLE>``, or ``GUD_KEY`` for ``@console``."""
    handle = _handle(handle)
    slug = handle.lstrip("@").upper().replace("-", "_")
    raw = os.environ.get(f"GUD_KEY_{slug}")
    if not raw and handle == "@console":
        raw = os.environ.get("GUD_KEY")
    if not raw or not raw.strip():
        return None
    return base64.b64decode(raw.strip())


def human_identity(handle: str, address: str) -> Identity:
    """An identity with no private key. Humans are trusted by verified sender address."""
    return Identity(_handle(handle), address, None)


def rotate(cfg: Config, handle: str) -> tuple[Identity, str]:
    """Replace ``handle``'s private key. The previous key stops being able to sign."""
    return create_identity(cfg, handle, overwrite=True)


def _handle(handle: str) -> str:
    handle = handle.strip()
    return handle if handle.startswith("@") else f"@{handle}"
