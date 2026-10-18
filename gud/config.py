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
    bugs_address: str = ""             # GUD_BUGS_ADDRESS; empty means bugs@<domain>
    human: str = "@nelson"             # GUD_HUMAN, the member decisions go out as
    mcp_token: str | None = None       # GUD_MCP_TOKEN; write tools require it when set
    review_model: str = "google/gemini-2.5-flash"  # GUD_REVIEW_MODEL, for owner reviews; empty uses agent_model
    swarm: bool = False               # GUD_SWARM; GUD_HOME/swarm.txt wins when the API starts

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


_SWARM_TRUE = frozenset({"1", "true", "yes", "on"})
SWARM_FILE = "swarm.txt"


def parse_swarm(value: str | None) -> bool:
    """``GUD_SWARM``: unset, empty, 0, false, no, and off are false. 1, true, yes, and on are true."""
    if value is None:
        return False
    return value.strip().lower() in _SWARM_TRUE


def swarm_path(home: Path) -> Path:
    return home / SWARM_FILE


def read_swarm_file(home: Path) -> bool | None:
    """``on``/``off`` from ``GUD_HOME/swarm.txt``, or None when the file is absent.

    A file that exists but does not say ``on`` wins as off.
    """
    path = swarm_path(home)
    if not path.is_file():
        return None
    return path.read_text().strip().lower() == "on"


def write_swarm_file(home: Path, enabled: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    swarm_path(home).write_text("on\n" if enabled else "off\n")


def resolved_swarm(cfg: Config) -> bool:
    """File wins over ``cfg.swarm`` (the env var) when ``swarm.txt`` exists."""
    saved = read_swarm_file(cfg.home)
    if saved is not None:
        return saved
    return cfg.swarm


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


_PRIMITIVE_MAILBOXES = frozenset({"primitive", "primitive-webhook"})


def _domain(mailbox: str) -> str:
    """``GUD_DOMAIN``, else the domain of ``PRIMITIVE_EMAIL`` for a Primitive mailbox.

    The fake mailbox stays on ``gud.local``. Primitive only accepts mail from a
    verified sending domain, which is the domain of ``PRIMITIVE_EMAIL``.
    """
    explicit = (os.environ.get("GUD_DOMAIN") or "").strip()
    if explicit:
        return explicit
    if mailbox.strip().lower() in _PRIMITIVE_MAILBOXES:
        email = (os.environ.get("PRIMITIVE_EMAIL") or "").strip()
        _local, sep, domain = email.partition("@")
        domain = domain.strip()
        if sep and domain and "@" not in domain and not any(ch.isspace() for ch in domain):
            return domain
    return "gud.local"


def load() -> Config:
    _load_dotenv()
    home = Path(os.environ.get("GUD_HOME", ROOT / ".gud-home")).resolve()
    mailbox = os.environ.get("GUD_MAILBOX", "fake")
    domain = _domain(mailbox)
    # grok-4-fast was removed from OpenRouter and chat/completions 404s on it.
    # grok-4.3 is the fast successor still listed there.
    agent_model = os.environ.get("GUD_AGENT_MODEL", "x-ai/grok-4.3")
    return Config(
        home=home,
        mailbox=mailbox,
        runner=os.environ.get("GUD_RUNNER", "local"),
        domain=domain,
        repo_address=os.environ.get("GUD_REPO_ADDRESS", f"repo@{domain}"),
        window_seconds=float(os.environ.get("GUD_WINDOW_SECONDS", "60")),
        primitive_api_key=os.environ.get("PRIMITIVE_API_KEY"),
        instabox_api_key=os.environ.get("INSTABOX_API_KEY"),
        armature_api_key=os.environ.get("ARMATURE_API_KEY"),
        openrouter_key=os.environ.get("OPENROUTER_KEY") or os.environ.get("OPENROUTER_API_KEY"),
        agent_model=agent_model,
        host_url=_blank(os.environ.get("GUD_HOST_URL")),
        handle=_blank(os.environ.get("GUD_HANDLE")),
        member_address=_blank(os.environ.get("GUD_ADDRESS")),
        audit_skew=float(os.environ.get("GUD_AUDIT_SKEW", "120")),
        bugs_address=(os.environ.get("GUD_BUGS_ADDRESS") or f"bugs@{domain}").strip(),
        human=_at(os.environ.get("GUD_HUMAN") or "@nelson"),
        mcp_token=_blank(os.environ.get("GUD_MCP_TOKEN")),
        review_model=_blank(os.environ.get("GUD_REVIEW_MODEL", "google/gemini-2.5-flash")) or agent_model,
        swarm=parse_swarm(os.environ.get("GUD_SWARM")),
    )
