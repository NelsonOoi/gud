"""Local Ed25519 identities. Private keys live under ``cfg.keys_dir``, never in the tree or the mailbox."""

from __future__ import annotations

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
    raw = sig.load_private(cfg.key_path(handle))
    return Identity(handle, address or cfg.address(handle), raw)


def human_identity(handle: str, address: str) -> Identity:
    """An identity with no private key. Humans are trusted by verified sender address."""
    return Identity(_handle(handle), address, None)


def rotate(cfg: Config, handle: str) -> tuple[Identity, str]:
    """Replace ``handle``'s private key. The previous key stops being able to sign."""
    return create_identity(cfg, handle, overwrite=True)


def _handle(handle: str) -> str:
    handle = handle.strip()
    return handle if handle.startswith("@") else f"@{handle}"
