"""Settings from the environment and the nearest .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    for d in [ROOT, ROOT.parent]:
        f = d / ".env"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def domain_address(domain: str, handle: str) -> str:
    """``handle@domain``, the address ``gud init`` writes into the roster."""
    return f"{str(handle).lstrip('@')}@{domain}"


def _at(handle: str) -> str:
    handle = str(handle).strip()
    return handle if not handle or handle.startswith("@") else f"@{handle}"


def configured_address(cfg: "Config") -> str | None:
    """The member's own address from ``GUD_ADDRESS``, or None when it is unset."""
    return cfg.member_address


@dataclass(frozen=True)
class Config:
    home: Path
    mailbox: str            # "fake" or "primitive"
    runner: str             # "local" or "instabox"
    domain: str
    repo_address: str
    window_seconds: float
    primitive_api_key: str | None
    instabox_api_key: str | None
    armature_api_key: str | None
    openrouter_key: str | None
    agent_model: str
    host_url: str | None = None       # GUD_HOST_URL; absent means this process is the host
    handle: str | None = None         # GUD_HANDLE
    member_address: str | None = None # GUD_ADDRESS, the address GUD_HANDLE sends from
    audit_skew: float = 120.0         # GUD_AUDIT_SKEW, seconds

    def address(self, handle: str) -> str:
        """``GUD_ADDRESS`` for ``GUD_HANDLE``; ``handle@domain`` for everyone else."""
        if self.member_address and self.handle and _at(handle) == _at(self.handle):
            return self.member_address
        return domain_address(self.domain, handle)

    @property
    def keys_dir(self) -> Path:
        return self.home / "keys"

    def key_path(self, handle: str) -> Path:
        return self.keys_dir / f"{handle.lstrip('@')}.key"

    def address_for(self, handle: str) -> str:
        """``handle@domain``. Ignores ``GUD_ADDRESS``."""
        return domain_address(self.domain, handle)


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def load() -> Config:
    _load_dotenv()
    home = Path(os.environ.get("GUD_HOME", ROOT / ".gud-home")).resolve()
    domain = os.environ.get("GUD_DOMAIN", "gud.local")
    return Config(
        home=home,
        mailbox=os.environ.get("GUD_MAILBOX", "fake"),
        runner=os.environ.get("GUD_RUNNER", "local"),
        domain=domain,
        repo_address=os.environ.get("GUD_REPO_ADDRESS", f"repo@{domain}"),
        window_seconds=float(os.environ.get("GUD_WINDOW_SECONDS", "60")),
        primitive_api_key=os.environ.get("PRIMITIVE_API_KEY"),
        instabox_api_key=os.environ.get("INSTABOX_API_KEY"),
        armature_api_key=os.environ.get("ARMATURE_API_KEY"),
        openrouter_key=os.environ.get("OPENROUTER_KEY") or os.environ.get("OPENROUTER_API_KEY"),
        agent_model=os.environ.get("GUD_AGENT_MODEL", "x-ai/grok-4-fast"),
        host_url=_blank(os.environ.get("GUD_HOST_URL")),
        handle=_blank(os.environ.get("GUD_HANDLE")),
        member_address=_blank(os.environ.get("GUD_ADDRESS")),
        audit_skew=float(os.environ.get("GUD_AUDIT_SKEW", "120")),
    )
