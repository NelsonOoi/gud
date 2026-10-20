"""`gud1`: the v1 command line (INTERFACES.md `gud/cli_v1.py`, SPEC-v1 §21, §22).

Global flags (before or after the command): `--home PATH` (default `$GUD_HOME`, else `~/.gud`),
`--handle @x` (default `$GUD_HANDLE`, else the working copy's or the last init/clone/join's),
`--json` (machine output: one JSON document on stdout), `-C DIR` (run as if started in DIR),
`--offline` (read commands use the cache without syncing).

Where the repo is: a working copy's `.gud/local/config.json`, else `GUD_HOME/remote.json`
(written by `init`, `clone` and `join`). A host URL is `http(s)://...` (a §21 mirror) or
`local:<dir>` (local host mode: this machine hosts the repo address; see `gud.wc.remote`).

Exit codes: 0 ok; 1 the operation failed or found problems (sync conflict, refused push,
verify differences); 2 usage errors and commands whose module is not in this build.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from gud.core import envelope as E
from gud.core.config import MEMBERS, ConfigError, Members, parse_members, render_members
from gud.core.objects import Tree
from gud.core.ops import OpError, invert_ops
from gud.core.types import LIVE_STATUSES, Hex, LinkState, Member, Record
from gud.transport.base import TransportError
from gud.wc import copy as wc_copy
from gud.wc.copy import WcError, WorkingCopy, create_genesis, find_root, push_tree, read_local_config, verify_cache
from gud.wc.remote import (
    LocalHost, RemoteError, RepoClient, key_path, load_key, open_mirror, repo_slug, save_key,
    write_transport_toml,
)


class CliError(Exception):
    def __init__(self, message: str, code: int = 1, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = dict(data or {})


# ---------------------------------------------------------------- context

def _home(args: argparse.Namespace) -> Path:
    home = getattr(args, "home", None) or os.environ.get("GUD_HOME") or "~/.gud"
    return Path(home).expanduser().resolve()


def _handle(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value if value.startswith("@") else "@" + value


def _kv(items: Sequence[str] | None) -> dict[str, str]:
    out = {}
    for item in items or ():
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CliError(f"--set takes KEY=VALUE, not {item!r}", 2)
        out[key.strip()] = value
    return out


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.json = bool(getattr(args, "json", False))
        self.offline = bool(getattr(args, "offline", False))
        self.cwd = Path(getattr(args, "C", None) or os.getcwd()).resolve()
        self.home = _home(args)
        self.wc_root = find_root(self.cwd)
        self.local = read_local_config(self.wc_root) if self.wc_root else {}
        self._client: RepoClient | None = None

    # identity ---------------------------------------------------

    def remote_json(self) -> dict:
        path = self.home / "remote.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else {}

    def save_remote(self, host_url: str, repo: str, handle: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "remote.json").write_text(
            json.dumps({"host_url": host_url, "repo": repo, "handle": handle}, indent=1) + "\n", "utf-8")

    @property
    def handle(self) -> str:
        for value in (getattr(self.args, "handle", None), os.environ.get("GUD_HANDLE"),
                      self.local.get("handle"), self.remote_json().get("handle")):
            if value:
                return _handle(value)  # type: ignore[return-value]
        members = self.home / "members"
        found = sorted(p.name for p in members.iterdir() if (p / "key").exists()) if members.is_dir() else []
        if len(found) == 1:
            return found[0]
        raise CliError("which member? pass --handle @x (or set GUD_HANDLE)", 2)

    def key(self, handle: str | None = None) -> bytes:
        try:
            return load_key(self.home, handle or self.handle)
        except RemoteError as exc:
            raise CliError(str(exc), 2) from exc

    def mailbox(self, handle: str | None = None):
        handle = handle or self.handle
        from gud.transport.registry import member_config, open_mailbox
        override = self.wc_root / ".gud/local/transport.toml" if self.wc_root else None
        try:
            if override is not None and override.exists():
                from gud.wc.remote import _read_toml_transport
                name, cfg = _read_toml_transport(override)
            else:
                path = self.home / "members" / handle / "transport.toml"
                if not path.exists():
                    return None
                name, cfg = member_config(self.home, handle)
            return open_mailbox(name, cfg)
        except TransportError as exc:
            raise CliError(f"transport: {exc}") from exc

    # repo -------------------------------------------------------

    def remote(self) -> tuple[str, str]:
        for source in (self.local, self.remote_json()):
            if source.get("host_url") and source.get("repo"):
                return source["host_url"], source["repo"]
        raise CliError("no repo here: run `gud clone`, `gud init` or `gud join` first", 2)

    def client(self, host_url: str | None = None, repo: str | None = None) -> RepoClient:
        if self._client is None:
            if host_url is None:
                host_url, repo = self.remote()
            handle = self.handle
            key = self.key(handle)
            if repo is None:
                repo = open_mirror(host_url, handle, key).info()["repo"]
            self._client = RepoClient(host_url, handle, key, send_via=self.mailbox(handle),
                                      repo_address=repo, home=self.home)
        return self._client

    def synced(self) -> RepoClient:
        client = self.client()
        if not self.offline:
            client.sync_log()
        return client

    def wc(self) -> WorkingCopy:
        if self.wc_root is None:
            raise CliError(f"{self.cwd} is not inside a working copy (no .gud/BASE)", 2)
        return WorkingCopy(self.wc_root, self.client())

    def need_sender(self, client: RepoClient) -> None:
        if client.send_via is None:
            raise CliError(f"no transport for {client.handle}: run `gud keys new --transport NAME --address ADDR`", 2)

    def resolve(self, ref: str | None) -> Hex | None:
        if ref is None:
            return None
        try:
            return self.client().resolve(ref)
        except RemoteError as exc:
            raise CliError(str(exc), 2) from exc

    def wait_logged(self, gid: Hex, timeout: float | None = None) -> bool:
        """Sync until `gid` is in the host log (local hosts log on the first sync)."""
        client = self.client()
        timeout = float(os.environ.get("GUD_WAIT", "10")) if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            client.sync_log()
            if client.replayer().log().seq_of(gid) is not None:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    # output -----------------------------------------------------

    def out(self, data: Any, text: str | Callable[[], str] = "") -> int:
        if self.json:
            print(json.dumps(_jsonable(data), sort_keys=True))
        else:
            rendered = text() if callable(text) else text
            if rendered:
                print(rendered.rstrip("\n"))
        return 0


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if hasattr(value, "to_json"):
            return _jsonable(value.to_json())
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, Path):
        return str(value)
    return value


def _short(h: str | None) -> str:
    return h[:12] if h else "-"


def _when(ms: int | None) -> str:
    if ms is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) + "Z"


def _link_json(link: LinkState) -> dict:
    return {"seq": link.seq, "state": link.state, "push": link.push, "kind": link.kind, "author": link.author,
            "intent": link.intent, "linked_at": link.linked_at, "received_at": link.received_at,
            "task": link.task, "files": list(link.files), "tree_hash": link.tree_hash,
            "parent": link.parent, "carried_from": link.carried_from, "revert_of": link.revert_of}


# ---------------------------------------------------------------- keys

def cmd_keys_new(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    handle = ctx.handle
    path = key_path(ctx.home, handle)
    if path.exists() and not args.force:
        raw = load_key(ctx.home, handle)
        created = False
    else:
        raw, _ = E.generate_key()
        save_key(ctx.home, handle, raw, overwrite=True)
        created = True
    transport = None
    if args.transport:
        if not args.address:
            raise CliError("--transport needs --address (the member's own mail address)", 2)
        cfg = {**_kv(args.set), "address": args.address}
        write_transport_toml(ctx.home / "members" / handle / "transport.toml", args.transport, cfg)
        transport = args.transport
    data = {"handle": handle, "public_key": E.public_of(raw), "key_path": str(path), "created": created,
            "transport": transport, "address": args.address}
    return ctx.out(data, f"{handle} {E.public_of(raw)}\n{'created' if created else 'existing'} key at {path}")


def cmd_keys_show(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    handle = ctx.handle
    pub = E.public_of(ctx.key(handle))
    member = None
    try:
        member = ctx.client().member(handle)
    except CliError:
        pass
    data = {"handle": handle, "public_key": pub, "member": _jsonable(member) if member else None,
            "matches_tip": bool(member and member.public_key == pub)}
    return ctx.out(data, f"{handle} {pub}" + (f"\nmember: {member.role} {member.address}" if member else ""))


def _members_file(client: RepoClient) -> tuple[Tree, Members]:
    tree = client.reader().tree()
    return tree, parse_members(tree.read(MEMBERS))


def cmd_keys_rotate(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    if args.host:
        return _not_available(ctx, "keys rotate --host", "gud.ops.keys")
    client = ctx.synced()
    ctx.need_sender(client)
    tree, members = _members_file(client)
    me = members.get(client.handle)
    if me is None:
        raise CliError(f"{client.handle} is not a member at the tip")
    new_raw, new_pub = E.generate_key()
    by = dict(members.by_handle)
    by[me.handle] = dataclasses.replace(me, public_key=new_pub)
    new_tree = tree.set(MEMBERS, "100644", render_members(Members(by)))
    pid = push_tree(client, client.chain().tip.state, new_tree, f"rotate key of {me.handle}")  # type: ignore[union-attr]
    save_key(ctx.home, client.handle, new_raw, name="key.pending", overwrite=True)
    return ctx.out({"push": pid, "public_key": new_pub, "pending": str(key_path(ctx.home, me.handle).with_name("key.pending"))},
                   f"members push {_short(pid)} sent; the new key is in key.pending until it links "
                   f"(then run `gud keys promote`)")


def cmd_keys_promote(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    pending = key_path(ctx.home, client.handle).with_name("key.pending")
    if not pending.exists():
        raise CliError("no pending key")
    raw = load_key(ctx.home, client.handle, name="key.pending")
    member = client.member()
    if member is None or member.public_key != E.public_of(raw):
        raise CliError("the pending key is not yet the member key at the tip")
    save_key(ctx.home, client.handle, raw, overwrite=True)
    pending.unlink()
    return ctx.out({"handle": client.handle, "public_key": E.public_of(raw)}, "key promoted")


# ---------------------------------------------------------------- host

def cmd_host_init(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    root = Path(args.dir).expanduser().resolve() if args.dir else ctx.home / "host" / repo_slug(args.repo)
    try:
        host = LocalHost.create(root, repo=args.repo, transport=args.transport, cfg=_kv(args.set), bugs=args.bugs,
                                objects=ctx.home / "objects")
    except RemoteError as exc:
        raise CliError(str(exc)) from exc
    probe: dict[str, Any] = {}
    try:
        caps = host.mailbox().capabilities()
        probe = {"transport": caps.name, "max_message_bytes": caps.max_message_bytes, "push": caps.push,
                 "raw_mime": caps.raw_mime, "authserv_id": caps.authserv_id}
    except Exception as exc:          # transport probe is advisory here
        probe = {"error": str(exc)}
    data = {"host_url": f"local:{root}", "host_key": host.host_key, "repo": args.repo, "probe": probe}
    return ctx.out(data, f"local host for {args.repo} at {root}\nhost key {host.host_key}\nhost url local:{root}")


def _host_dir(ctx: Ctx, args: argparse.Namespace) -> Path:
    """The host directory a host command works on: `--dir`, else the repo's (`--repo`, or the
    local host this member uses), else the only one under GUD_HOME/host/."""
    if getattr(args, "dir", None):
        return Path(args.dir).expanduser().resolve()
    if getattr(args, "repo", None):
        return ctx.home / "host" / repo_slug(args.repo)
    for source in (ctx.local, ctx.remote_json()):
        url = source.get("host_url") or ""
        if url.startswith("local:"):
            return Path(url[len("local:"):]).expanduser().resolve()
    found = sorted(p.parent for p in (ctx.home / "host").glob("*/host.json")) if (ctx.home / "host").is_dir() else []
    if len(found) == 1:
        return found[0]
    raise CliError("which host? pass --dir DIR (a directory made by `gud host init`)", 2)


def cmd_host_serve(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    from gud import host_v1
    root = _host_dir(ctx, args)
    if not (root / "host.json").exists():
        raise CliError(f"{root} is not a host directory (run `gud host init`)", 2)

    def ready(host) -> None:
        head = host.intake.head()
        ctx.out({"serving": f"http://{args.bind}", "dir": str(root), "repo": host.repo, "host_key": host.intake.log.key,
                 "log_head": head.seq}, f"serving {host.repo} at http://{args.bind} (log head {head.seq})")
        sys.stdout.flush()

    host_v1.serve_dir(root, bind=args.bind, webhook_bind=args.webhook_bind, on_ready=ready)
    return 0


def cmd_host_key(args: argparse.Namespace) -> int:
    """New host, before a planned handover: the key it will sign with (kept in DIR/key)."""
    ctx = Ctx(args)
    from gud.wc.remote import decode_private_key, encode_private_key
    root = Path(args.dir).expanduser().resolve()
    path = root / "key"
    created = not path.exists()
    if created:
        root.mkdir(parents=True, exist_ok=True)
        raw, _ = E.generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(encode_private_key(raw) + "\n")
    pub = E.public_of(decode_private_key(path.read_text("ascii")))
    return ctx.out({"dir": str(root), "host_key": pub, "created": created}, pub)


def cmd_host_handover(args: argparse.Namespace) -> int:
    """Old host: sign the planned handover (§11.6) to a new host key, optionally to a new repo
    address; then `gud export-archive --host OUT` and give the archive to the new host."""
    ctx = Ctx(args)
    root = _host_dir(ctx, args)
    if not E.is_public_key(args.key):
        raise CliError(f"not a public key: {args.key!r}", 2)
    try:
        entry = LocalHost(root).handover(args.key, repo=args.new_repo)
    except (RemoteError, ValueError) as exc:
        raise CliError(str(exc)) from exc
    return ctx.out({"dir": str(root), "seq": entry.seq, "hash": entry.hash, "next": args.key,
                    "repo": entry.field("repo")},
                   f"handover {entry.seq} to {args.key}" + (f" at {args.new_repo}" if args.new_repo else ""))


def cmd_host_follow(args: argparse.Namespace) -> int:
    """Member: move to a new host URL after a handover. The new mirror's log must continue the
    log this member verified (handover, then the new host's open); the cache is kept."""
    ctx = Ctx(args)
    old = ctx.client()
    handle = old.handle
    new = RepoClient(args.host_url, handle, ctx.key(handle), send_via=ctx.mailbox(handle), repo_address=old.repo,
                     home=ctx.home, trusted_host_keys=old.trusted_host_keys)
    try:
        new.sync_log()
    except RemoteError as exc:
        raise CliError(f"the new host does not continue this repo's log: {exc}") from exc
    log = new.replayer().log()
    ctx.save_remote(args.host_url, old.repo, handle)
    if ctx.wc_root is not None:
        cfg = read_local_config(ctx.wc_root)
        cfg["host_url"] = args.host_url
        wc_copy.write_local_config(ctx.wc_root, cfg)
    return ctx.out({"host_url": args.host_url, "repo": log.repo, "repos": list(log.repos), "host_key": log.key,
                    "log_head": log.head.seq if log.head else None},
                   f"following {log.repo} at {args.host_url} (host key {log.key})")


def cmd_import_archive(args: argparse.Namespace) -> int:
    """New host: continue an archived repo (§11.6) in a new host directory; serve it with
    `gud host serve --dir DIR`. The key is DIR/key (`gud host key`), else a new one."""
    ctx = Ctx(args)
    from gud.ops.archive import ArchiveError, import_archive, load_archive
    from gud.wc.remote import decode_private_key
    try:
        archived = load_archive(Path(args.archive))
    except ArchiveError as exc:
        raise CliError(str(exc)) from exc
    from gud.core.hostlog import HostLog
    head = archived.entries[-1]
    target_repo = args.repo or (head.field("repo") if head.type == "handover" and head.field("repo") else
                                HostLog(archived.entries).repo)
    root = Path(args.dir).expanduser().resolve() if args.dir else ctx.home / "host" / repo_slug(target_repo)
    if (root / "host.json").exists():
        raise CliError(f"{root} already holds a host")
    key_file = root / "key"
    raw = decode_private_key(key_file.read_text("ascii")) if key_file.exists() else E.generate_key()[0]
    if not args.transport:
        raise CliError("import-archive needs the new repo mailbox: --transport NAME [--set k=v]", 2)
    try:
        log = import_archive(Path(args.archive), repo=args.repo, host_private=raw, mail_store=root / "mail",
                             trust_unplanned=args.trust_unplanned)
        host = LocalHost.install(root, log=log, transport=args.transport, cfg=_kv(args.set), host_private=raw,
                                 objects=ctx.home / "objects")
    except (ArchiveError, RemoteError) as exc:
        raise CliError(str(exc)) from exc
    data = {"dir": str(root), "host_url": f"local:{root}", "repo": log.repo, "host_key": host.host_key,
            "log_head": log.head.seq if log.head else None, "messages": len(archived.mail)}
    return ctx.out(data, f"{log.repo} continues at {root} (host key {host.host_key}); "
                         f"run `gud host serve --dir {root}`")


def cmd_host_trust(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.client()
    try:
        client.trust_host_key(args.key)
    except RemoteError as exc:
        raise CliError(str(exc), 2) from exc
    client.sync_log()
    return ctx.out({"trusted": list(client.trusted_host_keys)}, f"trusted {args.key}; cache rebuilt")


# ---------------------------------------------------------------- doctor

def cmd_doctor(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    checks: list[dict] = []

    def check(name: str, fn: Callable[[], str]) -> bool:
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": detail})
            return True
        except Exception as exc:  # each check reports its own failure
            checks.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
            return False

    handle = ctx.handle
    check("key", lambda: E.public_of(ctx.key(handle)))
    latency: dict[str, int] = {}

    def transport() -> str:
        if args.transport:
            from gud.transport.registry import open_mailbox
            mb = open_mailbox(args.transport, _kv(args.set))
        else:
            mb = ctx.mailbox(handle)
        if mb is None:
            raise CliError("no transport configured")
        caps = mb.capabilities()
        if not args.no_send:
            from gud.transport.base import Outgoing
            head = mb.head() if hasattr(mb, "head") else None
            token = E.sha256_hex(os.urandom(16))[:16]
            start = time.monotonic()
            mb.send(Outgoing(from_addr=mb.address, to=(mb.address,), subject=f"gud doctor {token}",
                             text=f"gud doctor round trip {token}\n"))
            got = False
            deadline = start + float(args.timeout)
            cursor = head
            while time.monotonic() < deadline and not got:
                items = mb.wait(since=cursor, timeout=max(0.0, deadline - time.monotonic()))
                for item in items:
                    cursor = item.cursor
                    got = got or token.encode() in item.raw_mime
            if not got:
                raise CliError(f"round trip did not arrive within {args.timeout}s")
            latency["round_trip_ms"] = int((time.monotonic() - start) * 1000)
        return f"{caps.name}: max {caps.max_message_bytes} bytes, push {caps.push}, raw MIME {caps.raw_mime}"

    check("transport", transport)
    try:
        host_url, repo = ctx.remote()
    except CliError:
        host_url = None
    if host_url:
        client_holder: list[RepoClient] = []

        def mirror() -> str:
            client_holder.append(ctx.client())
            info = client_holder[0].info()
            return f"{info.get('repo')} head {info.get('log_head')}"

        if check("mirror", mirror):
            check("log", lambda: f"{len(client_holder[0].sync_log().new_links)} new links; "
                                 f"{len(client_holder[0].replayer().log())} entries verified")

            def membership() -> str:
                member = client_holder[0].member()
                if member is None:
                    raise CliError(f"{handle} is not a member at the tip")
                if member.public_key != E.public_of(ctx.key(handle)):
                    raise CliError("the local key is not the member key at the tip")
                return f"{member.role} {member.address}"

            check("membership", membership)
    proposed = None
    if latency:
        grace = max(30_000, 3 * latency["round_trip_ms"])
        proposed = {"grace_ms": grace, "window_ms": max(60_000, 2 * grace), **latency}
    ok = all(c["ok"] for c in checks)
    data = {"ok": ok, "checks": checks, "proposed": proposed}

    def text() -> str:
        lines = [f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}" for c in checks]
        if proposed:
            lines.append(f"proposed .gud/config: window {proposed['window_ms'] // 1000}s grace {proposed['grace_ms'] // 1000}s")
        return "\n".join(lines)

    ctx.out(data, text)
    return 0 if ok else 1


# ---------------------------------------------------------------- init / import / join / admit / clone

def _roster_with_me(data: bytes, handle: str, pub: str) -> bytes:
    lines = []
    for raw in data.decode("utf-8").splitlines():
        parts = raw.split("#", 1)[0].split()
        if parts and parts[0] == handle:
            if len(parts) == 3:
                parts.append(pub)
            elif len(parts) == 4 and parts[3].startswith("@"):
                parts.insert(3, pub)
            elif len(parts) >= 4 and parts[3] == "-":
                parts[3] = pub
            raw = " ".join(parts)
        lines.append(raw)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _setup_host(ctx: Ctx, args: argparse.Namespace, repo: str) -> str:
    if getattr(args, "host_url", None):
        return args.host_url
    root = ctx.home / "host" / repo_slug(repo)
    if (root / "host.json").exists():
        return f"local:{root}"
    if args.transport:
        name, cfg = args.transport, _kv(args.set)
    else:
        path = ctx.home / "members" / ctx.handle / "transport.toml"
        if not path.exists():
            raise CliError("no transport for the repo mailbox: pass --transport NAME [--set k=v]", 2)
        from gud.wc.remote import _read_toml_transport
        name, cfg = _read_toml_transport(path)
    LocalHost.create(root, repo=repo, transport=name, cfg={k: v for k, v in cfg.items() if k != "address"},
                     objects=ctx.home / "objects")
    return f"local:{root}"


def _init(ctx: Ctx, args: argparse.Namespace, *, git_dir: str | None, ref: str, roster_path: str,
          directory: str | None) -> int:
    handle = ctx.handle
    raw = ctx.key(handle)
    repo = args.repo
    host_url = _setup_host(ctx, args, repo)
    client = ctx.client(host_url, repo)
    ctx.need_sender(client)
    client.sync_log()
    if client.chain().tip is not None:
        raise CliError(f"{repo} already has a genesis link")
    host_key = client.info()["host_key"]
    roster = _roster_with_me(Path(roster_path).read_bytes(), handle, E.public_of(raw))
    from gud import gitbridge
    warnings: list[str] = []
    try:
        if git_dir:
            with tempfile.NamedTemporaryFile("wb", suffix=".roster", delete=False) as fh:
                fh.write(roster)
            try:
                plan = gitbridge.import_git(Path(git_dir), ref=ref, roster=Path(fh.name), host_key=host_key)
            finally:
                os.unlink(fh.name)
            files = dict(plan.files)
            intent = plan.intent
            warnings = list(plan.warnings)
        else:
            members = gitbridge.parse_roster(roster, host_key)
            files = {}
            if directory:
                root = Path(directory)
                root.mkdir(parents=True, exist_ok=True)
                if (root / wc_copy.BASE_FILE).exists():
                    raise CliError(f"{root} is already a working copy")
                tmp = WorkingCopy(root, client)
                files = tmp._scan(Tree.empty(client.store)).to_files()
                warnings = tmp.warnings
            files[MEMBERS] = ("100644", members)
            for path, default in ((".gud/config", gitbridge.DEFAULT_CONFIG), (".gud/test", gitbridge.DEFAULT_TEST),
                                  (".gud/owners", gitbridge.DEFAULT_OWNERS)):
                files.setdefault(path, ("100644", default))
            intent = args.message or "gud init"
    except gitbridge.GitBridgeError as exc:
        raise CliError(str(exc)) from exc
    gid = create_genesis(client, files, intent)
    if not ctx.wait_logged(gid):
        raise CliError(f"genesis {_short(gid)} was sent but the host has not logged it yet")
    tip = client.chain().tip
    if tip is None or tip.state != gid:
        status = client.chain().push_status(gid)
        raise CliError(f"genesis did not link ({status.status if status else 'unknown'})")
    ctx.save_remote(host_url, repo, handle)
    wc_dir = None
    if directory:
        wc = WorkingCopy.adopt(client, Path(directory), gid)
        wc_dir = str(wc.root)
    data = {"genesis": gid, "host_url": host_url, "repo": repo, "dir": wc_dir, "files": len(files),
            "warnings": warnings}
    return ctx.out(data, f"genesis {_short(gid)} linked ({len(files)} files)\nhost {host_url}"
                         + (f"\nworking copy {wc_dir}" if wc_dir else ""))


def cmd_init(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    directory = args.dir if args.dir is not None else (None if args.from_git else str(ctx.cwd))
    return _init(ctx, args, git_dir=args.from_git, ref=args.ref, roster_path=args.member_file, directory=directory)


def cmd_import(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    return _init(ctx, args, git_dir=args.git_dir, ref=args.ref, roster_path=args.roster, directory=args.dir)


def cmd_join(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    handle = _handle(args.handle)
    args.handle = handle
    if not key_path(ctx.home, handle).exists():
        raw, _ = E.generate_key()
        save_key(ctx.home, handle, raw)
    raw = load_key(ctx.home, handle)
    if args.transport:
        write_transport_toml(ctx.home / "members" / handle / "transport.toml", args.transport,
                             {**_kv(args.set), "address": args.address})
    client = ctx.client(args.host_url)
    ctx.need_sender(client)
    headers = {"Gud-Role": args.role, "Gud-Address": args.address, "Gud-Key": E.public_of(raw)}
    if args.sponsor:
        headers["Gud-Sponsor"] = _handle(args.sponsor)  # type: ignore[assignment]
    records = (Record("text", (), args.reason.encode("utf-8")),) if args.reason else ()
    env = client.build_signed("join", headers, records, seen=False)
    gid = client.send(env, summary=f"{handle} as {args.role}")
    ctx.save_remote(args.host_url, client.repo, handle)
    return ctx.out({"join": gid, "repo": client.repo, "handle": handle, "public_key": E.public_of(raw)},
                   f"join request {_short(gid)} sent to {client.repo}; a member admits it with `gud admit {_short(gid)}`")


def cmd_admit(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    ctx.need_sender(client)
    jid = ctx.resolve(args.join)
    email = client.reader().email(jid)  # type: ignore[arg-type]
    env = email.envelope if email is not None else None
    if env is None or env.kind != "join":
        raise CliError(f"{_short(jid)} is not a logged join request")
    if not E.verify(env, env.get("Gud-Key")):
        raise CliError("the join request's self-signature does not verify")
    tree, members = _members_file(client)
    by = dict(members.by_handle)
    new = Member(env.author or "", env.get("Gud-Role") or "agent", env.get("Gud-Address") or "",  # type: ignore[arg-type]
                 env.get("Gud-Key"), env.get("Gud-Sponsor"))
    by[new.handle] = new
    try:
        data = render_members(Members(by))
        parse_members(data)
    except ConfigError as exc:
        raise CliError(f"cannot admit: {exc}") from exc
    pid = push_tree(client, client.chain().tip.state, tree.set(MEMBERS, "100644", data),  # type: ignore[union-attr]
                    args.message or f"admit {new.handle} ({new.role})", headers={"Gud-Join": jid})  # type: ignore[dict-item]
    ctx.wait_logged(pid)
    return ctx.out({"push": pid, "join": jid, "member": _jsonable(new)},
                   f"members push {_short(pid)} admits {new.handle}; it needs a human approve (members rule)")


def cmd_clone(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.client(args.host_url)
    target = Path(args.dir) if args.dir else ctx.cwd / repo_slug(client.repo).split("@")[0]
    if not target.is_absolute():
        target = ctx.cwd / target
    how = client.bootstrap(checkpoint=not args.no_checkpoint)
    at = ctx.resolve(args.at) if args.at else None
    try:
        wc = WorkingCopy.clone(client, target, at=at, checkpoint=not args.no_checkpoint)
    except WcError as exc:
        raise CliError(str(exc)) from exc
    ctx.save_remote(args.host_url, client.repo, client.handle)
    tip = client.chain().tip
    data = {"dir": str(wc.root), "base": wc.base, "tip": tip.state if tip else None, "from": how,
            "files": len(client.reader().tree(wc.base))}
    return ctx.out(data, f"cloned {client.repo} into {wc.root} at {_short(wc.base)} (from {how})")


# ---------------------------------------------------------------- working copy commands

def cmd_status(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    wc = ctx.wc()
    if not ctx.offline:
        wc.client.sync_log()
    st = wc.status()
    if args.paths:
        keep = [p.strip("/") for p in args.paths]

        def chosen(path: str) -> bool:
            return any(path == k or path.startswith(k + "/") for k in keep)

        st = dataclasses.replace(st, added=tuple(filter(chosen, st.added)), modified=tuple(filter(chosen, st.modified)),
                                 deleted=tuple(filter(chosen, st.deleted)), mode_changed=tuple(filter(chosen, st.mode_changed)))

    def text() -> str:
        lines = [f"base {_short(st.base)}  tip {_short(st.tip)}" + (f"  ({st.behind} behind)" if st.behind else "")]
        lines += [f"  A {p}" for p in st.added] + [f"  M {p}" for p in st.modified]
        lines += [f"  D {p}" for p in st.deleted] + [f"  X {p} (mode)" for p in st.mode_changed]
        if st.clean:
            lines.append("  clean")
        if st.conflicts:
            lines.append("sync stopped on conflicts (nothing was changed; see .gud/local/conflicts/):")
            lines += [f"  ! {c}" for c in st.conflicts]
        for pid, s in st.pushes[-5:]:
            lines.append(f"push {_short(pid)}: {s}")
        return "\n".join(lines)

    data = st.to_json()
    data["warnings"] = wc.warnings
    return ctx.out(data, text)


def cmd_diff(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    wc = ctx.wc()
    text = wc.diff(args.paths)
    files = [line.split(" b/", 1)[1] for line in text.splitlines() if line.startswith("diff --git ")]
    return ctx.out({"diff": text, "files": files}, text)


def cmd_push(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    wc = ctx.wc()
    ctx.need_sender(wc.client)
    wc.client.sync_log()
    task = ctx.resolve(args.task)
    revises = ctx.resolve(args.revises)
    try:
        result = wc.push(args.message, task=task, revises=revises, carry=not args.no_carry,
                         allow_secret=args.allow_secret or ())
    except wc_copy.SecretsFound as exc:
        raise CliError(str(exc), 1, {"secrets": [dataclasses.asdict(h) for h in exc.hits]}) from exc
    logged = ctx.wait_logged(result.push)
    chain = wc.client.chain()
    linked = any(link.push == result.push for link in chain.links)
    prop = chain.push_status(result.push)
    status = "linked" if linked else (prop.status if prop else ("sent" if not logged else "unknown"))
    if linked:
        wc.fast_forward()
    data = result.to_json() | {"status": status, "reason": prop.reason if prop and not linked else "",
                               "base": wc.base}

    def text() -> str:
        lines = [f"push {_short(result.push)} on {_short(result.parent)}: {result.ops} ops, {result.blobs} blobs; {status}"]
        if prop is not None and not linked and prop.reason:
            lines.append(f"  {prop.reason}")
        if result.forecast is not None:
            f = result.forecast
            if f.askers:
                lines.append(f"  asks: {' '.join(f.askers)}")
            if f.quorum_needed:
                lines.append(f"  quorum: {f.quorum_needed} approvals")
            if f.members_rule:
                lines.append("  members rule: a human approve")
        return "\n".join(lines)

    return ctx.out(data, text)


def cmd_sync(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    wc = ctx.wc()
    result = wc.sync()
    if result.ok:
        return ctx.out(result.to_json(), f"synced to {_short(result.new_base)}"
                       + (f" ({len(result.written)} files updated)" if result.written else ""))
    lines = ["sync stopped: conflicts with the tip (nothing was changed, .gud/BASE kept):"]
    lines += [f"  ! {c}" for c in result.conflicts]
    if result.markers:
        lines.append("conflict markers: " + ", ".join(result.markers))
    lines.append("resolve by editing your files to include the tip's change, then sync again")
    ctx.out(result.to_json(), "\n".join(lines))
    return 1


def cmd_verify(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    from_cp = not args.from_genesis
    if ctx.wc_root is not None:
        diffs = ctx.wc().verify(from_checkpoint=from_cp)
    else:
        diffs = verify_cache(ctx.client(), from_checkpoint=from_cp)
    ok = not diffs
    ctx.out({"ok": ok, "differences": diffs, "from": "checkpoint" if from_cp else "genesis"},
            "verify ok: the cache equals a fresh replay" if ok else "verify FAILED:\n" + "\n".join(f"  {d}" for d in diffs))
    return 0 if ok else 1


# ---------------------------------------------------------------- reading

def cmd_log(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    links = list(reversed(client.chain().links))[: args.n]
    data = {"links": [_link_json(l) for l in links]}
    return ctx.out(data, "\n".join(f"{l.seq:>4} {_short(l.state)} {l.author:<12} {_when(l.linked_at)} "
                                    f"{(l.intent.splitlines() or [''])[0]}" for l in links))


def cmd_blame(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    at = ctx.resolve(args.at) if args.at else None
    path = args.path
    if ctx.wc_root is not None and not args.path.startswith("/"):
        full = (ctx.cwd / args.path).resolve()
        try:
            path = full.relative_to(ctx.wc_root).as_posix()
        except ValueError:
            pass
    rows = client.reader().blame(path, at)
    if not rows:
        raise CliError(f"{path} is not in the tree")
    out = [{"line": i + 1, "owner": o, "seq": s, "text": t.decode("utf-8", "replace")} for i, (o, s, t) in enumerate(rows)]
    return ctx.out({"path": path, "at": at, "lines": out},
                   "\n".join(f"{r['owner']:<12} {r['seq']:>4} {r['line']:>5}| {r['text'].rstrip(chr(10))}" for r in out))


def _show(client: RepoClient, gid: Hex) -> dict:
    chain = client.chain()
    for link in chain.links:
        if gid in (link.state, link.push):
            prop = None
            return {"type": "link", "link": _link_json(link), "proposal": prop}
    if gid in chain.tasks:
        t = chain.tasks[gid]
        claims = [c for c in chain.claims.values() if c.task == gid]
        return {"type": "task", "task": _jsonable(t), "claims": _jsonable(claims)}
    for c in chain.claims.values():
        if gid in (c.id, c.latest):
            return {"type": "claim", "claim": _jsonable(c)}
    if gid in chain.escalations:
        return {"type": "escalation", "escalation": _jsonable(chain.escalations[gid])}
    prop = chain.proposals.get(gid) or chain.push_status(gid)
    if prop is not None:
        return {"type": "proposal", "proposal": _jsonable(prop)}
    email = client.reader().email(gid)
    if email is not None:
        env = email.envelope
        return {"type": "message", "id": gid, "kind": env.kind if env else "unsigned",
                "author": env.author if env else email.from_addr, "subject": email.subject,
                "headers": dict(env.headers) if env else {}, "text": env.text if env else email.text}
    raise CliError(f"nothing known as {gid[:12]}")


def cmd_show(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    data = _show(client, ctx.resolve(args.id))  # type: ignore[arg-type]
    return ctx.out(data, lambda: json.dumps(data, indent=1, sort_keys=True, default=str))


def _review(client: RepoClient) -> list[dict]:
    chain = client.chain()
    me = client.handle
    members = client.reader().members() if chain.tip else None
    member = members.get(me) if members else None
    items: list[dict] = []
    for cid in sorted(chain.proposals):
        prop = chain.proposals[cid]
        if prop.status not in LIVE_STATUSES:
            continue
        approved = {h for h, _ in prop.approvals}
        if prop.author == me:
            for obj in prop.objections:
                if obj.status in ("blocking", "pending_verdict", "confirmed"):
                    items.append({"type": "objection", "candidate": cid, "push": prop.push, "objection": obj.id,
                                  "by": obj.by, "reason": obj.reason, "status": obj.status})
            continue
        if me in approved:
            continue
        why = []
        if me in prop.askers:
            why.append("asker")
        why += [f"explicit {path}" for path, rule, owners in prop.declared if rule == "explicit" and me in owners]
        if prop.quorum_needed and member is not None and member.votes:
            why.append("quorum")
        if prop.members_rule and member is not None and member.role == "human":
            why.append("members")
        if why:
            items.append({"type": "review", "candidate": cid, "push": prop.push, "author": prop.author,
                          "status": prop.status, "why": why, "files": list(prop.files),
                          "closes_at": prop.window_closes_at})
    for esc in chain.escalations.values():
        if esc.status == "open" and me in esc.deciders:
            items.append({"type": "escalation", "escalation": esc.id, "reason": esc.reason,
                          "pushes": list(esc.pushes), "deadline": esc.deadline})
    for task in chain.tasks.values():
        if task.status == "work_done" and task.requester == me and not (task.accept_paths or task.accept_cmds):
            items.append({"type": "accept", "task": task.id, "alias": task.alias, "title": task.title})
    return items


def cmd_review(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    items = _review(client)

    def text() -> str:
        if not items:
            return "nothing needs you"
        lines = []
        for it in items:
            if it["type"] == "review":
                lines.append(f"review {_short(it['candidate'])} by {it['author']} ({', '.join(it['why'])}; {it['status']}): "
                             f"{' '.join(it['files'])}")
            elif it["type"] == "objection":
                lines.append(f"objection {_short(it['objection'])} by {it['by']} on your push {_short(it['push'])}: {it['reason']}")
            elif it["type"] == "escalation":
                lines.append(f"escalation {_short(it['escalation'])} ({it['reason']}) needs your decision")
            else:
                lines.append(f"accept {it['alias']} {it['title']}")
        return "\n".join(lines)

    return ctx.out({"items": items}, text)


# ---------------------------------------------------------------- votes and messages

def _send(ctx: Ctx, kind: str, headers: Mapping[str, str], records: Sequence[Record], summary: str, *,
          in_reply_to: Hex | None = None, blobs: Sequence[bytes] = ()) -> int:
    client = ctx.client()
    ctx.need_sender(client)
    gid = client.sign_and_send(kind, headers, records, summary=summary, in_reply_to=in_reply_to, blobs=blobs)
    logged = ctx.wait_logged(gid)
    return ctx.out({"id": gid, "kind": kind, "logged": logged, **{k: v for k, v in headers.items()}},
                   f"{kind} {_short(gid)} sent" + ("" if logged else " (not logged yet)"))


def _text(value: str | None) -> tuple[Record, ...]:
    return (Record("text", (), value.encode("utf-8")),) if value else ()


def _vote(args: argparse.Namespace, kind: str) -> int:
    ctx = Ctx(args)
    ctx.synced()
    target = ctx.resolve(args.target)
    return _send(ctx, kind, {"Gud-Target": target}, _text(args.message), f"{kind} {_short(target)}",  # type: ignore[dict-item]
                 in_reply_to=target)


def cmd_approve(args: argparse.Namespace) -> int:
    return _vote(args, "approve")


def cmd_hold(args: argparse.Namespace) -> int:
    return _vote(args, "hold")


def cmd_object(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    target = ctx.resolve(args.target)
    reason = args.message or ""
    if getattr(args, "veto", False):
        reason = "veto " + (reason or "vetoed")
    if not reason:
        raise CliError("an objection needs a reason: -m TEXT", 2)
    records = [Record("text", (), reason.encode("utf-8"))]
    blobs: list[bytes] = []
    evidence = getattr(args, "evidence", None)
    test_file = getattr(args, "test", None)
    if evidence or test_file:
        if evidence:
            repo_path, sep, local = evidence.partition(":")
            if not sep:
                raise CliError("--evidence takes REPO_PATH:LOCAL_FILE", 2)
            data = Path(local).read_bytes()
        else:
            local_path = (ctx.cwd / test_file).resolve()
            data = local_path.read_bytes()
            if args.test_path:
                repo_path = args.test_path
            elif ctx.wc_root is not None and local_path.is_relative_to(ctx.wc_root):
                repo_path = local_path.relative_to(ctx.wc_root).as_posix()
            else:
                raise CliError("--test FILE outside a working copy needs --test-path REPO_PATH", 2)
        sha = E.sha256_hex(data)
        if len(data) + 4096 <= client.budget():
            records.append(Record("evidence", (repo_path, sha, str(len(data))), data))
        else:
            records.append(Record("evidence", (repo_path, sha, str(len(data))), b""))
            blobs.append(data)
    if getattr(args, "veto", False):
        member = client.member()
        if member is None or member.role != "human":
            print("warning: only a human member's veto counts (SPEC §13.5)", file=sys.stderr)
    return _send(ctx, "object", {"Gud-Target": target}, records, f"{'veto' if getattr(args, 'veto', False) else 'object'} "  # type: ignore[dict-item]
                 f"{_short(target)}", in_reply_to=target, blobs=blobs)


def cmd_veto(args: argparse.Namespace) -> int:
    args.veto = True
    return cmd_object(args)


def cmd_comment(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    ctx.synced()
    target = ctx.resolve(args.target)
    headers = {"Gud-Target": target}
    if args.task:
        headers["Gud-Task"] = ctx.resolve(args.task)  # type: ignore[assignment]
    if args.escalate:
        headers["Gud-Escalate"] = args.escalate
    if args.verdict:
        headers["Gud-Verdict"] = args.verdict
    return _send(ctx, "comment", headers, _text(args.message), f"on {_short(target)}", in_reply_to=target)  # type: ignore[arg-type]


# ---------------------------------------------------------------- tasks and claims

def _task_headers(args: argparse.Namespace) -> tuple[dict[str, str], list[Record]]:
    headers: dict[str, str] = {"Gud-Title": args.title}
    if getattr(args, "accept", None):
        headers["Gud-Accept"] = E.render_list([a for a in args.accept.split(",") if a])
    records = list(_text(args.message))
    if getattr(args, "accept_cmd", None):
        records.append(Record("accept", (), "\n".join(args.accept_cmd).encode("utf-8")))
    return headers, records


def cmd_task_new(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    ctx.synced()
    headers, records = _task_headers(args)
    if args.requester:
        headers["Gud-Requester"] = _handle(args.requester)  # type: ignore[assignment]
    return _send(ctx, "task", headers, records, args.title)


def _tasks(ctx: Ctx, show_all: bool) -> int:
    client = ctx.synced()
    tasks = sorted(client.chain().tasks.values(), key=lambda t: (t.opened_at, t.alias))
    if not show_all:
        tasks = [t for t in tasks if t.status != "done"]
    data = {"tasks": [_jsonable(t) for t in tasks]}
    return ctx.out(data, lambda: "\n".join(f"{t.alias:<6} {t.status:<9} {_short(t.id)} {t.title}" for t in tasks)
                   or "no open tasks")


def cmd_tasks(args: argparse.Namespace) -> int:
    return _tasks(Ctx(args), args.all)


def cmd_task_list(args: argparse.Namespace) -> int:
    return _tasks(Ctx(args), args.all)


def cmd_task_show(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    tid = ctx.resolve(args.id)
    task = client.chain().tasks.get(tid)  # type: ignore[arg-type]
    if task is None:
        raise CliError(f"{args.id} is not a task")
    data = _show(client, task.id)
    t = task

    def text() -> str:
        lines = [f"{t.alias} {t.title}  [{t.status}]", f"id {t.id}", f"requester {t.requester or '-'}  author {t.author or '-'}"]
        if t.description:
            lines += ["", t.description]
        if t.accept_paths or t.accept_cmds:
            lines.append("criteria: " + ", ".join(list(t.accept_paths) + list(t.accept_cmds)))
        for c in data["claims"]:
            lines.append(f"claim {_short(c['id'])} by {c['holder']} ({c['status']}): {','.join(c['files'])}")
        return "\n".join(lines)

    return ctx.out(data, text)


def cmd_claim(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    ctx.synced()
    items = list(args.items)
    if items and items[0] == "update":
        if len(items) != 2:
            raise CliError("usage: gud claim update CLAIM --files GLOBS", 2)
        claim_id = ctx.resolve(items[1])
        claim = next((c for c in ctx.client().chain().claims.values() if claim_id in (c.id, c.latest)), None)
        if claim is None:
            raise CliError(f"{items[1]} is not a claim")
        headers = {"Gud-Task": claim.task, "Gud-Files": E.render_list(args.files.split(",")), "Gud-Revises": claim.latest}
        return _send(ctx, "claim", headers, _text(args.message), f"update {_short(claim.id)}")
    if len(items) != 1:
        raise CliError("usage: gud claim TASK --files GLOBS", 2)
    if not args.files:
        raise CliError("gud claim needs --files GLOBS (`**` for everything)", 2)
    task = ctx.resolve(items[0])
    headers = {"Gud-Task": task, "Gud-Files": E.render_list([f for f in args.files.split(",") if f])}
    if args.after:
        headers["Gud-After"] = E.render_list([ctx.resolve(a) for a in args.after.split(",") if a])  # type: ignore[misc]
    return _send(ctx, "claim", headers, _text(args.message), f"claim {_short(task)}")  # type: ignore[arg-type]


def cmd_release(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    ctx.synced()
    claim_id = ctx.resolve(args.claim)
    claim = next((c for c in ctx.client().chain().claims.values() if claim_id in (c.id, c.latest)), None)
    if claim is None:
        raise CliError(f"{args.claim} is not a claim")
    headers = {"Gud-Task": claim.task, "Gud-Files": E.render_list(list(claim.files) or ["**"]),
               "Gud-Revises": claim.latest, "Gud-Release": "yes"}
    return _send(ctx, "claim", headers, (), f"release {_short(claim.id)}")


def cmd_subtask(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    ctx.synced()
    headers, records = _task_headers(args)
    headers["Gud-Parent-Task"] = ctx.resolve(args.parent)  # type: ignore[assignment]
    if args.after:
        headers["Gud-After"] = E.render_list([ctx.resolve(a) for a in args.after.split(",") if a])  # type: ignore[misc]
    if args.files:
        headers["Gud-Files"] = E.render_list([f for f in args.files.split(",") if f])
    return _send(ctx, "subtask", headers, records, args.title)


def cmd_no_change(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    if client.chain().tip is None:
        raise CliError("no tip yet")
    task = ctx.resolve(args.task)
    headers = {"Gud-Task": task, "Gud-Tip": client.chain().tip.state}  # type: ignore[union-attr]
    return _send(ctx, "no-change", headers, _text(args.message.splitlines()[0] if args.message else ""),  # type: ignore[arg-type]
                 f"no change for {_short(task)}")


def cmd_revert(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    ctx.need_sender(client)
    link_id = ctx.resolve(args.link)
    chain = client.chain()
    link = chain.link(link_id) or next((l for l in chain.links if l.push == link_id), None)  # type: ignore[arg-type]
    if link is None or link.parent is None:
        raise CliError(f"{args.link} is not a link that can be reverted (genesis cannot be)")
    reader = client.reader()
    tip = chain.tip
    new, conflicts = invert_ops(reader.tree(link.parent), reader.tree(link.state), reader.tree(tip.state))  # type: ignore[union-attr]
    if conflicts:
        raise CliError(f"revert conflicts with later links on: {', '.join(conflicts)}")
    headers = {"Gud-Parent": tip.state, "Gud-Revert": link.state, "Gud-Tree": new.hash()}  # type: ignore[union-attr]
    return _send(ctx, "revert", headers, _text(args.message or f"revert {_short(link.state)}"), f"revert {_short(link.state)}")


def cmd_checkpoint(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.synced()
    seq = args.at_log if args.at_log is not None else client.recommended_checkpoint()
    if seq is None:
        raise CliError("nothing to checkpoint yet")
    entry = client.replayer().log().at(seq)
    data = client.snapshot_at(seq)
    from gud.core.replay import Snapshot
    state = Snapshot.from_bytes(data).state
    links = state.get("links") or []
    if not links:
        raise CliError(f"no link at log entry {seq}")
    link = links[-1]
    headers = {"Gud-Log": f"{entry.seq}:{entry.hash}", "Gud-Link": link["state"], "Gud-Tree": link["tree_hash"],
               "Gud-Snapshot": E.sha256_hex(data)}
    return _send(ctx, "checkpoint", headers, (), f"at {seq}")


# ---------------------------------------------------------------- integrations

def cmd_mirror_git(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    client = ctx.client()
    from gud.gitbridge import GitBridgeError, GitMirror
    workdir = Path(args.workdir) if args.workdir else ctx.home / "gitmirror" / repo_slug(client.repo)
    try:
        n = GitMirror(args.remote, workdir, client.store, branch=args.branch, client=client).run_once()
    except GitBridgeError as exc:
        raise CliError(str(exc)) from exc
    return ctx.out({"pushed": n, "remote": args.remote, "branch": args.branch}, f"mirrored {n} links to {args.remote} {args.branch}")


def _not_available(ctx: Ctx, command: str, module: str) -> int:
    message = f"`gud {command}` needs {module}, which is not in this build"
    if ctx.json:
        print(json.dumps({"error": message, "available": False}))
    else:
        print(message, file=sys.stderr)
    return 2


def _stub(command: str, module: str) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        ctx = Ctx(args)
        try:
            mod = __import__(module, fromlist=["_"])
        except ImportError:
            return _not_available(ctx, command, module)
        main = getattr(mod, "main", None)
        if main is None:
            return _not_available(ctx, command, module + ".main")
        return int(main(list(getattr(args, "rest", []) or [])) or 0)
    run.__name__ = "cmd_" + command.replace(" ", "_").replace("-", "_")
    return run


def cmd_tester(args: argparse.Namespace) -> int:
    """`gud tester`: this member (role tester) runs the repo's test policy (gud.tester_v1)."""
    ctx = Ctx(args)
    import logging

    from gud import tester_v1
    client = ctx.client(args.host_url, args.repo) if args.host_url else ctx.client()
    ctx.need_sender(client)
    tester = tester_v1.Tester(client, tester_v1.make_runner(args.runner),
                              identity=tester_v1.TesterIdentity(client.handle, client.private_raw),
                              parallel=args.parallel, state_dir=ctx.home / "tester" / client.handle)
    if args.once:
        sent = tester.step()
        return ctx.out({"handle": client.handle, "sent": sent}, f"tester {client.handle}: {len(sent)} results sent")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    print(f"tester {client.handle} running ({args.runner}, every {args.interval}s)", file=sys.stderr)
    tester.run_forever(interval=args.interval)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    from gud.ops.audit import audit
    client = ctx.client()
    sync_error = None
    if not ctx.offline:
        try:
            client.sync_log()
        except RemoteError as exc:            # a forked or rolled-back log is what audit reports
            sync_error = str(exc)
    report = audit(client, trusted_keys=client.trusted_host_keys)
    data = report.to_json() | {"sync_error": sync_error}
    if args.evidence:
        Path(args.evidence).write_text(json.dumps(report.to_json(), indent=1, sort_keys=True) + "\n", "utf-8")
    ctx.out(data, "audit ok" if report.ok else "audit found problems:\n" + json.dumps(report.to_json(), indent=1))
    return report.exit_status


class _HostFiles:
    """A host directory (`gud host init` layout) as an archive source: the log and raw mail on disk."""

    def __init__(self, root: Path) -> None:
        self.host = LocalHost(root)

    def log_entries(self, since: int, limit: int):
        from gud.core.hostlog import decode_entry
        return [decode_entry(t.encode("utf-8")) for t in self.host.log(since, limit)["entries"]]

    def raw(self, gud_id: Hex) -> bytes | None:
        return self.host.mail(gud_id)


def cmd_export_archive(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    from gud.ops.archive import ArchiveError, export_archive
    try:
        if args.host:
            root = _host_dir(ctx, args)
            from gud.wc.remote import decode_private_key
            key = decode_private_key((root / "key").read_text("ascii"))
            out = export_archive(_HostFiles(root), Path(args.out), signer=key)
        else:
            client = ctx.synced()
            out = export_archive(client, Path(args.out), trusted_keys=client.trusted_host_keys)
    except ArchiveError as exc:
        raise CliError(str(exc)) from exc
    manifest = json.loads((out / "manifest.json").read_text("utf-8")) if out.is_dir() else {}
    return ctx.out({"archive": str(out), "entries": manifest.get("log", {}).get("entries"),
                    "messages": manifest.get("mbox", {}).get("messages"), "missing": manifest.get("missing")},
                   f"archive at {out}")


# ---------------------------------------------------------------- parser

def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--home", default=argparse.SUPPRESS, help="GUD_HOME (default $GUD_HOME or ~/.gud)")
    p.add_argument("--handle", default=argparse.SUPPRESS, help="member handle (default $GUD_HANDLE)")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
    p.add_argument("-C", default=argparse.SUPPRESS, metavar="DIR", help="run as if started in DIR")
    p.add_argument("--offline", action="store_true", default=argparse.SUPPRESS, help="do not sync before reading")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(prog="gud1", description="gud v1: email-native source control", parents=[common])
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def add(name: str, func: Callable, help: str, parent: Any = sub) -> argparse.ArgumentParser:
        p = parent.add_parser(name, help=help, parents=[common], description=help)
        p.set_defaults(func=func)
        return p

    def transport_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--transport", help="transport name (gud.transport.registry)")
        p.add_argument("--set", action="append", metavar="KEY=VALUE", help="transport setting (repeatable)")

    # keys
    keys = sub.add_parser("keys", help="member keys", parents=[common]).add_subparsers(dest="keys_cmd", metavar="ACTION")
    keys.required = True
    p = add("new", cmd_keys_new, "create this member's key (and transport config)", keys)
    p.add_argument("--address", help="the member's own mail address")
    p.add_argument("--force", action="store_true", help="replace an existing key")
    transport_args(p)
    add("show", cmd_keys_show, "show this member's public key", keys)
    p = add("rotate", cmd_keys_rotate, "members push replacing this member's key", keys)
    p.add_argument("--host", action="store_true", help="rotate the host key (gud.ops.keys)")
    add("promote", cmd_keys_promote, "use the pending key once the rotation linked", keys)

    # host
    host = sub.add_parser("host", help="hosting", parents=[common]).add_subparsers(dest="host_cmd", metavar="ACTION")
    host.required = True
    p = add("init", cmd_host_init, "create a local host for a repo address", host)
    p.add_argument("--repo", required=True)
    p.add_argument("--transport", required=True)
    p.add_argument("--set", action="append", metavar="KEY=VALUE")
    p.add_argument("--bugs")
    p.add_argument("--dir", help="host directory (default GUD_HOME/host/<repo>)")
    p = add("serve", cmd_host_serve, "serve a host directory: intake, dispatch and the §21 mirror (gud.host_v1)", host)
    p.add_argument("--bind", default="127.0.0.1:8787")
    p.add_argument("--webhook-bind", help="a second port with only the provider webhook route")
    p.add_argument("--dir", help="host directory (default: this member's local host, or the only one)")
    p.add_argument("--repo", help="pick the host directory by repo address")
    p = add("key", cmd_host_key, "create (or show) a host key in a new host directory, for a handover", host)
    p.add_argument("--dir", required=True)
    p = add("handover", cmd_host_handover, "old host: sign the handover to a new host key (§11.6)", host)
    p.add_argument("key", help="the new host's public key (ed25519:...)")
    p.add_argument("--new-repo", help="the repo's new address, when it moves (integration note 4)")
    p.add_argument("--dir", help="host directory (default: this member's local host, or the only one)")
    p.add_argument("--repo", help="pick the host directory by repo address")
    p = add("follow", cmd_host_follow, "move to the new host after a handover (keeps the cache)", host)
    p.add_argument("host_url")
    p = add("trust", cmd_host_trust, "trust a host key after an unplanned handover (§11.6)", host)
    p.add_argument("key")

    p = add("doctor", cmd_doctor, "check key, transport (round trip), mirror and membership")
    transport_args(p)
    p.add_argument("--no-send", action="store_true", help="skip the transport round trip")
    p.add_argument("--timeout", type=float, default=30.0)

    p = add("init", cmd_init, "create a repo: genesis from a roster (and DIR or a git commit)")
    p.add_argument("dir", nargs="?", help="directory to start from and turn into a working copy (default .)")
    p.add_argument("--repo", required=True, help="repo address")
    p.add_argument("--member-file", required=True, help="roster: handle role address [pubkey] [sponsor]")
    p.add_argument("--from-git", metavar="GIT_DIR", help="genesis from a git commit (gud.gitbridge)")
    p.add_argument("--ref", default="main")
    p.add_argument("--host-url", help="an existing host (default: a new local host)")
    p.add_argument("-m", "--message", help="genesis intent")
    transport_args(p)

    p = add("import", cmd_import, "genesis from a git commit (same as init --from-git)")
    p.add_argument("git_dir")
    p.add_argument("--repo", required=True)
    p.add_argument("--roster", required=True)
    p.add_argument("--ref", default="main")
    p.add_argument("--dir", help="also create a working copy here")
    p.add_argument("--host-url")
    p.add_argument("-m", "--message")
    transport_args(p)

    p = add("join", cmd_join, "ask to join a repo (signed join request)")
    p.add_argument("host_url")
    p.add_argument("--address", required=True)
    p.add_argument("--role", default="agent", choices=["agent", "human", "tester", "console"])
    p.add_argument("--sponsor")
    p.add_argument("--reason")
    transport_args(p)

    p = add("admit", cmd_admit, "members push admitting a join request")
    p.add_argument("join")
    p.add_argument("-m", "--message")

    p = add("clone", cmd_clone, "clone a repo into a working copy")
    p.add_argument("host_url")
    p.add_argument("dir", nargs="?")
    p.add_argument("--at", help="link to check out (default the tip)")
    p.add_argument("--no-checkpoint", action="store_true", help="replay from genesis")

    p = add("status", cmd_status, "working copy against its base")
    p.add_argument("paths", nargs="*")
    p = add("diff", cmd_diff, "unified diff against the base")
    p.add_argument("paths", nargs="*")
    p = add("push", cmd_push, "push the working copy's changes against its base")
    p.add_argument("-m", "--message", required=True, help="intent")
    p.add_argument("--task")
    p.add_argument("--revises")
    p.add_argument("--no-carry", action="store_true")
    p.add_argument("--allow-secret", action="append", metavar="PATH")
    add("sync", cmd_sync, "merge the tip into the working copy (stops on conflict)")
    p = add("log", cmd_log, "links, newest first")
    p.add_argument("-n", type=int, default=20)
    p = add("blame", cmd_blame, "line owners of a file")
    p.add_argument("path")
    p.add_argument("--at")
    p.add_argument("--full", action="store_true", help="include archived epochs (all mail is read here)")
    p = add("show", cmd_show, "show a link, push, task, claim or message")
    p.add_argument("id")
    p = add("verify", cmd_verify, "rebuild replay from the mirror and compare with the cache")
    p.add_argument("--from-genesis", action="store_true")
    add("review", cmd_review, "pending items that need me")

    for name, func in (("approve", cmd_approve), ("hold", cmd_hold), ("object", cmd_object), ("veto", cmd_veto)):
        p = add(name, func, f"{name} a push" + (" (or accept a task)" if name == "approve" else ""))
        p.add_argument("target")
        p.add_argument("-m", "--message")
        if name in ("object", "veto"):
            p.add_argument("--evidence", metavar="REPO_PATH:FILE", help="evidence test file (object only)")
            p.add_argument("--test", metavar="FILE", help="evidence test file from the working copy")
            p.add_argument("--test-path", metavar="REPO_PATH")

    p = add("comment", cmd_comment, "comment on anything")
    p.add_argument("target")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--task")
    p.add_argument("--escalate", choices=["budget", "stuck"])
    p.add_argument("--verdict", choices=["reject"])

    def task_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("title")
        p.add_argument("-m", "--message", help="description")
        p.add_argument("--accept", metavar="PATHS", help="test paths or globs, comma separated")
        p.add_argument("--accept-cmd", action="append", metavar="CMD")

    task = sub.add_parser("task", help="tasks", parents=[common]).add_subparsers(dest="task_cmd", metavar="ACTION")
    task.required = True
    p = add("new", cmd_task_new, "open a task", task)
    task_args(p)
    p.add_argument("--requester")
    p = add("list", cmd_task_list, "list tasks", task)
    p.add_argument("--all", action="store_true")
    p = add("show", cmd_task_show, "show one task", task)
    p.add_argument("id")
    p = add("tasks", cmd_tasks, "list tasks")
    p.add_argument("--all", action="store_true")

    p = add("claim", cmd_claim, "claim a task (`claim TASK --files G`, `claim update CLAIM --files G`)")
    p.add_argument("items", nargs="+", metavar="TASK")
    p.add_argument("--files", help="globs, comma separated")
    p.add_argument("--after")
    p.add_argument("-m", "--message")
    p = add("release", cmd_release, "release a claim")
    p.add_argument("claim")
    p = add("subtask", cmd_subtask, "post a subtask under a task I hold")
    p.add_argument("parent")
    task_args(p)
    p.add_argument("--after")
    p.add_argument("--files")
    p = add("no-change", cmd_no_change, "the task needs no change")
    p.add_argument("task")
    p.add_argument("-m", "--message", required=True)
    p = add("revert", cmd_revert, "revert a link")
    p.add_argument("link")
    p.add_argument("-m", "--message")
    p = add("checkpoint", cmd_checkpoint, "sign a checkpoint at the recommended log position")
    p.add_argument("--at-log", type=int)

    mirror = sub.add_parser("mirror", help="mirrors", parents=[common]).add_subparsers(dest="mirror_cmd", metavar="KIND")
    mirror.required = True
    p = add("git", cmd_mirror_git, "one-way git mirror (gud.gitbridge)", mirror)
    p.add_argument("remote")
    p.add_argument("--branch", default="gud")
    p.add_argument("--workdir")

    p = add("agent", _stub("agent", "gud.agent.runtime"), "run gud.agent.runtime (the reference agent)")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p = add("tester", cmd_tester, "run the tester (gud.tester_v1) as this member")
    p.add_argument("--runner", default="local", choices=["local", "docker", "instabox"])
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--interval", type=float, default=5.0, help="seconds between passes")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--host-url", help="host URL (default: this member's repo)")
    p.add_argument("--repo", help="repo address (with --host-url)")
    p = add("export-archive", cmd_export_archive, "export the mailbox, log and snapshot (gud.ops.archive)")
    p.add_argument("out")
    p.add_argument("--host", action="store_true", help="export a host directory, signed by the host key")
    p.add_argument("--dir", help="host directory (with --host)")
    p = add("import-archive", cmd_import_archive, "continue an archived repo on a new host (gud.ops.archive)")
    p.add_argument("archive")
    p.add_argument("--repo", help="repo address (default: the archived log's, or the handover's new address)")
    p.add_argument("--dir", help="new host directory (default GUD_HOME/host/<repo>)")
    p.add_argument("--trust-unplanned", action="store_true", help="§11.6 unplanned handover")
    transport_args(p)
    p = add("audit", cmd_audit, "host log audit (gud.ops.audit)")
    p.add_argument("--evidence", help="write the report (with any fork proof) to this file")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        _error(args, str(exc), exc.data)
        return exc.code
    except (WcError, RemoteError, TransportError, OpError, ConfigError, ValueError, OSError) as exc:
        _error(args, f"{exc}", {"type": type(exc).__name__})
        return 1


def _error(args: argparse.Namespace, message: str, data: Mapping[str, Any]) -> None:
    if getattr(args, "json", False):
        print(json.dumps({"error": message, **_jsonable(data)}, sort_keys=True))
    print(f"gud: {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
