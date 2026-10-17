"""gud command line.

  gud init [--from demo_repo]   create member keys and send the genesis push
  gud join HOST_URL --handle @x --address ADDR [--role agent] [--sponsor @h] [--reason TEXT]
  gud audit                     compare the host mirror with this member's inbox
  gud serve                     run the host API (same as python -m gud.api)
  gud rebuild                   replay the mailbox from genesis and print the tip
  gud status | log | blame PATH
  gud reset                     delete the local fake mailbox and keys
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from . import compose, config, sig
from .config import configured_address, domain_address
from .memberfile import MEMBERS, format_members
from .types import Member
from .wire import normalize, short

ROSTER = [
    ("@lead", "lead"), ("@auth-agent", "agent"), ("@billing-agent", "agent"), ("@api-agent", "agent"),
    ("@sub-1", "agent"), ("@sub-2", "agent"), ("@sub-3", "agent"), ("@sub-4", "agent"),
    ("@tester", "tester"), ("@console", "console"),
]
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules"}


def read_dir(root: Path) -> dict[str, str]:
    tree = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(root).parts) and p.suffix != ".pyc":
            tree[p.relative_to(root).as_posix()] = normalize(p.read_text())
    return tree


def cmd_init(cfg: config.Config, args) -> None:
    from .mailbox import open_mailbox
    mailbox = open_mailbox(cfg)
    if mailbox.list(recipient=cfg.repo_address):
        sys.exit("repo mailbox is not empty; run `gud reset` first (fake mailbox only)")
    human_handle = os.environ.get("GUD_HUMAN", "@nelson")
    human_address = os.environ.get("GUD_HUMAN_ADDRESS", domain_address(cfg.domain, human_handle))
    members: dict[str, Member] = {}
    ids: dict[str, compose.Identity] = {}
    for handle, role in ROSTER:
        path = cfg.key_path(handle)
        if path.exists():
            raw = sig.load_private(path)
        else:
            raw, _ = sig.generate()
            sig.save_private(path, raw)
        address = domain_address(cfg.domain, handle)
        ids[handle] = compose.Identity(handle, address, raw)
        members[handle] = Member(handle, role, address, sig.public_of(raw), human_handle)
    members[human_handle] = Member(human_handle, "human", human_address.lower(), None, None)
    tree = read_dir(Path(args.source))
    tree[MEMBERS] = format_members(members)
    body, state, _ = compose.push_body(ids["@lead"], parent=None, parent_state=None, parent_tree={},
                                       new_tree=tree, intent=f"genesis from {Path(args.source).name}")
    email = compose.send(mailbox, ids["@lead"], members, "[gud] genesis", body)
    print(f"genesis {email.message_id} state {short(state)}; {len(tree)} files, {len(members)} members")
    print(f"keys in {cfg.keys_dir}")


def cmd_join(cfg: config.Config, args) -> None:
    """Create a local key and mail a signed join to the repo address from /api/info."""
    import httpx

    from .joins import join_body
    from .keys import create_identity
    from .mailbox import open_own_mailbox

    host = args.host_url.rstrip("/")
    url = host + "/api/info"
    try:
        response = httpx.get(url, timeout=30)
    except httpx.HTTPError as exc:
        sys.exit(f"could not read {url}: {exc}")
    if response.status_code != 200:
        sys.exit(f"could not read {url}: HTTP {response.status_code}")
    info = response.json()
    repo = info.get("repo")
    if not repo:
        sys.exit(f"{url} did not return a repo address")
    try:
        ident, public = create_identity(cfg, args.handle)
    except FileExistsError as exc:
        sys.exit(str(exc))
    ident = compose.Identity(ident.handle, args.address, ident.private_raw)
    sponsor = args.sponsor
    if sponsor and not sponsor.startswith("@") and sponsor != "-":
        sponsor = f"@{sponsor}"
    try:
        body = join_body(ident, args.role, args.address, public, sponsor=sponsor, reason=args.reason or "")
    except ValueError as exc:
        sys.exit(str(exc))
    mailbox = open_own_mailbox(cfg, repo)
    email = mailbox.send(
        from_addr=ident.address, to=repo, subject=f"[gud] join {ident.handle}", body=body,
    )
    print(f"join {email.message_id}")
    print(f"public key {public}")
    print(f"key file {cfg.key_path(ident.handle)}")
    print(f"sent to {repo} from {ident.address}")
    print("Next:")
    print("  1. A member on the host proposes this join (console: POST /api/joins/<id>/propose).")
    print("  2. After it links, point this checkout at the mirror:")
    print(f"       GUD_HOST_URL={args.host_url}")
    print(f"       GUD_HANDLE={ident.handle}")
    print(f"       GUD_ADDRESS={args.address}")
    print(f"       GUD_REPO_ADDRESS={repo}")
    print("  3. gud rebuild    # replay the host mirror and check the welcome checkpoint")


def cmd_audit(cfg: config.Config, args) -> None:
    from .joins import audit
    from .mailbox import open_mailbox, open_member_mailbox

    if not cfg.host_url:
        sys.exit("GUD_HOST_URL is not set; audit compares your inbox with the host mirror")
    try:
        mirror_box = open_member_mailbox(cfg)
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        sys.exit(str(exc))
    own_box = open_mailbox(cfg)
    address = configured_address(cfg)
    if address is None and cfg.handle:
        address = domain_address(cfg.domain, cfg.handle)
    if not address:
        sys.exit("set GUD_ADDRESS or GUD_HANDLE so audit knows which inbox to read")
    report = audit(mirror_box.list(), own_box.list(recipient=address), cfg.repo_address, cfg.audit_skew)
    print(_format_audit(report))
    if report["missing"] or report["altered"] or report["retimed"]:
        sys.exit(1)


def _format_audit(report: dict) -> str:
    lines = []
    for row in report["missing"]:
        lines.append(f"missing {row['message_id']} {row.get('subject', '')}".rstrip())
    for row in report["altered"]:
        lines.append(f"altered {row['message_id']} {row.get('subject', '')}".rstrip())
    for row in report["retimed"]:
        lines.append(
            f"retimed {row['message_id']} own {row['received_at']} mirror {row.get('mirror_received_at')}"
        )
    if not lines:
        return "audit ok: mirror matches this inbox"
    return "\n".join(lines)


def cmd_serve(cfg: config.Config, args) -> None:
    from .api import main as serve_main
    serve_main()


def _repo(cfg):
    from .chain import Repo
    from .mailbox import open_member_mailbox
    return Repo(open_member_mailbox(cfg), cfg.window_seconds)


def cmd_rebuild(cfg, args) -> None:
    t0 = time.time()
    chain = _repo(cfg).refresh()
    tip = chain.tip
    if tip is None:
        sys.exit("no genesis found in the mailbox")
    print(f"replayed {len(chain.emails)} emails in {time.time() - t0:.2f}s")
    print(f"tip #{tip.seq} {short(tip.state)} by {tip.author}: {tip.subject}")
    print(f"{len(tip.tree)} files, {len(chain.links)} links, {len(chain.proposals)} proposals")


def cmd_status(cfg, args) -> None:
    chain = _repo(cfg).refresh()
    if chain.tip:
        print(f"tip #{chain.tip.seq} {short(chain.tip.state)} {chain.tip.author}: {chain.tip.subject}")
    for p in sorted(chain.proposals.values(), key=lambda p: p.received_at):
        print(f"  {p.status:15} {p.author:15} {p.subject}  {p.reason}")


def cmd_log(cfg, args) -> None:
    chain = _repo(cfg).refresh()
    for l in reversed(chain.links[-args.n:]):
        print(f"#{l.seq:<3} {short(l.state)} {l.author:15} {l.subject}")


def cmd_blame(cfg, args) -> None:
    for owner, link_id, text in _repo(cfg).blame(args.path):
        print(f"{owner:15} {link_id[1:9]} | {text}")


def cmd_reset(cfg, args) -> None:
    if cfg.mailbox != "fake":
        sys.exit("reset only works with the fake mailbox")
    shutil.rmtree(cfg.home, ignore_errors=True)
    print(f"removed {cfg.home}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="gud")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--from", dest="source", default=str(config.ROOT / "demo_repo"))
    p = sub.add_parser("join")
    p.add_argument("host_url")
    p.add_argument("--handle", required=True)
    p.add_argument("--address", required=True)
    p.add_argument("--role", default="agent")
    p.add_argument("--sponsor", default=None)
    p.add_argument("--reason", default="")
    sub.add_parser("audit")
    sub.add_parser("serve")
    sub.add_parser("rebuild")
    sub.add_parser("status")
    p = sub.add_parser("log")
    p.add_argument("-n", type=int, default=20)
    p = sub.add_parser("blame")
    p.add_argument("path")
    sub.add_parser("reset")
    args = ap.parse_args(argv)
    cfg = config.load()
    {"init": cmd_init, "join": cmd_join, "audit": cmd_audit, "serve": cmd_serve,
     "rebuild": cmd_rebuild, "status": cmd_status, "log": cmd_log,
     "blame": cmd_blame, "reset": cmd_reset}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
