"""Repo config files (SPEC §10): globs, durations, .gud/members, .gud/owners, .gud/test, .gud/config."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import asdict, dataclass, fields
from email.utils import parseaddr
from functools import lru_cache
from typing import Iterable, Literal, Mapping, Sequence

from .objects import Tree
from .types import REGULAR_MODES, ROLES, VOTER_ROLES, Handle, Member, Ms, _split_list


class ConfigError(ValueError):
    pass


MEMBERS, OWNERS, TEST, CONFIG = ".gud/members", ".gud/owners", ".gud/test", ".gud/config"
BUILTIN_QUORUM: tuple[str, ...] = (OWNERS, CONFIG)

_HANDLE_RE = re.compile(r"@[a-z0-9][a-z0-9._-]{0,63}")
_UINT_RE = re.compile(r"[0-9]+")


# ---------------------------------------------------------------- globs (§10.1)

def _class_regex(pattern: str, i: int) -> tuple[str, int]:
    """Translate `[...]` starting at pattern[i] == "["; returns (regex, index after "]")."""
    j = i + 1
    negate = j < len(pattern) and pattern[j] == "!"
    if negate:
        j += 1
    items: list[str] = []
    first = True
    while True:
        if j >= len(pattern):
            raise ConfigError(f"unterminated [ in glob {pattern!r}")
        ch = pattern[j]
        if ch == "]" and not first:
            j += 1
            break
        if ch == "\\":
            if j + 1 >= len(pattern):
                raise ConfigError(f"trailing \\ in glob {pattern!r}")
            ch = pattern[j + 1]
            j += 1
        if j + 2 < len(pattern) and pattern[j + 1] == "-" and pattern[j + 2] != "]":
            end = pattern[j + 2]
            if end == "\\":
                if j + 3 >= len(pattern):
                    raise ConfigError(f"trailing \\ in glob {pattern!r}")
                end = pattern[j + 3]
                j += 1
            if ord(end) < ord(ch):
                raise ConfigError(f"bad range {ch}-{end} in glob {pattern!r}")
            items.append(re.escape(ch) + "-" + re.escape(end))
            j += 3
        else:
            items.append(re.escape(ch))
            j += 1
        first = False
    body = "".join(items)
    return ("(?!/)" + ("[^" if negate else "[") + body + "]"), j


def _segment_regex(seg: str, pattern: str) -> str:
    out = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            regex, i = _class_regex(seg, i)
            out.append(regex)
        elif ch == "\\":
            if i + 1 >= len(seg):
                raise ConfigError(f"trailing \\ in glob {pattern!r}")
            out.append(re.escape(seg[i + 1]))
            i += 2
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _split_glob(pattern: str) -> list[str]:
    """Split on unescaped `/` (a `/` inside [...] is not a separator)."""
    segs, cur, i, depth = [], [], 0, False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            cur.append(pattern[i:i + 2])
            i += 2
            continue
        if ch == "[" and not depth:
            depth = True
        elif ch == "]" and depth:
            depth = False
        if ch == "/" and not depth:
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return segs


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise ConfigError("empty glob")
    segs = _split_glob(pattern)
    if len(segs) > 1 and segs[-1] == "":
        segs[-1] = "**"
    parts = []
    for seg in segs:
        if seg == "":
            raise ConfigError(f"empty segment in glob {pattern!r}")
        if seg == "**":
            parts.append("(?:/[^/]+)*")
        else:
            parts.append("/" + _segment_regex(seg, pattern))
    return re.compile("".join(parts), re.DOTALL)


class Glob:
    """§10.1 glob matched against the whole repo path."""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self._regex = _compile(pattern)

    def match(self, path: str) -> bool:
        return self._regex.fullmatch("/" + path) is not None

    def __repr__(self) -> str:
        return f"Glob({self.pattern!r})"


def glob_match(pattern: str, path: str) -> bool:
    return _compile(pattern).fullmatch("/" + path) is not None


def _safe(patterns: Sequence[str]) -> list[Glob]:
    out = []
    for pattern in patterns:
        try:
            out.append(Glob(pattern))
        except ConfigError:
            continue
    return out


def globs_overlap(a: Sequence[str], b: Sequence[str], paths: Iterable[str]) -> bool:
    """§10.1 overlap. Invalid globs match nothing (but still count when listed in both)."""
    if set(a) & set(b):
        return True
    ga, gb = _safe(a), _safe(b)
    if not ga or not gb:
        return False
    for x in ga:
        if any(y.match(x.pattern) for y in gb):
            return True
    for y in gb:
        if any(x.match(y.pattern) for x in ga):
            return True
    for path in paths:
        if any(x.match(path) for x in ga) and any(y.match(path) for y in gb):
            return True
    return False


# ---------------------------------------------------------------- durations (§10.6)

_DURATION_RE = re.compile(r"([0-9]+)(ms|s|m|h|d)?")
_UNITS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def parse_duration(text: str) -> Ms:
    """`<integer>[ms|s|m|h|d]` (default unit s) -> integer ms."""
    m = _DURATION_RE.fullmatch(text or "")
    if not m:
        raise ConfigError(f"bad duration {text!r}")
    return int(m.group(1)) * _UNITS[m.group(2) or "s"]


# ---------------------------------------------------------------- common line format

def _lines(data: bytes, name: str) -> list[tuple[int, list[str], str]]:
    """(line number, fields, rest-after-first-field) per non-blank, comment-stripped line."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{name}: not valid UTF-8") from exc
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        line = line.split("#", 1)[0]
        fields_ = re.split(r"[ \t]+", line.strip(" \t"))
        if fields_ == [""]:
            continue
        rest = re.split(r"[ \t]+", line.strip(" \t"), maxsplit=1)
        out.append((n, fields_, rest[1] if len(rest) > 1 else ""))
    return out


def _pubkey(value: str) -> bool:
    if not value.startswith("ed25519:"):
        return False
    try:
        raw = base64.b64decode(value[8:].encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False
    return len(raw) == 32 and base64.b64encode(raw).decode("ascii") == value[8:]


def bare_address(address: str) -> str:
    """Bare lowercase `local@domain` out of any display form."""
    return parseaddr(address)[1].strip().lower()


# ---------------------------------------------------------------- .gud/members (§10.2)

@dataclass(frozen=True)
class Members:
    by_handle: Mapping[Handle, Member]

    def get(self, handle: Handle) -> Member | None:
        return self.by_handle.get(handle)

    def by_address(self, address: str) -> Member | None:
        want = bare_address(address)
        for handle in sorted(self.by_handle):
            member = self.by_handle[handle]
            if bare_address(member.address) == want:
                return member
        return None

    def voters(self) -> tuple[Handle, ...]:
        return tuple(sorted(h for h, m in self.by_handle.items() if m.role in VOTER_ROLES))

    def humans(self) -> tuple[Handle, ...]:
        return tuple(sorted(h for h, m in self.by_handle.items() if m.role == "human"))

    def host(self) -> Member:
        hosts = [m for _, m in sorted(self.by_handle.items()) if m.role == "host"]
        if len(hosts) != 1:
            raise ConfigError("members must have exactly one host")
        return hosts[0]


def parse_members(data: bytes) -> Members:
    """§10.2. Raises ConfigError."""
    members: dict[Handle, Member] = {}
    addresses: set[str] = set()
    for n, parts, _ in _lines(data, MEMBERS):
        if len(parts) != 5:
            raise ConfigError(f"{MEMBERS}:{n}: expected 5 fields")
        handle, role, address, key, sponsor = parts
        if not _HANDLE_RE.fullmatch(handle):
            raise ConfigError(f"{MEMBERS}:{n}: bad handle {handle!r}")
        if role not in ROLES:
            raise ConfigError(f"{MEMBERS}:{n}: bad role {role!r}")
        bare = bare_address(address)
        local, at, domain = bare.rpartition("@")
        if not (at and local and domain) or bare != address.lower():
            raise ConfigError(f"{MEMBERS}:{n}: bad address {address!r}")
        if key != "-" and not _pubkey(key):
            raise ConfigError(f"{MEMBERS}:{n}: bad public key")
        if key == "-" and role != "human":
            raise ConfigError(f"{MEMBERS}:{n}: role {role} needs a key")
        if sponsor != "-" and not _HANDLE_RE.fullmatch(sponsor):
            raise ConfigError(f"{MEMBERS}:{n}: bad sponsor {sponsor!r}")
        if handle in members:
            raise ConfigError(f"{MEMBERS}:{n}: duplicate handle {handle}")
        if bare in addresses:
            raise ConfigError(f"{MEMBERS}:{n}: duplicate address {address}")
        addresses.add(bare)
        members[handle] = Member(handle, role, address, None if key == "-" else key,  # type: ignore[arg-type]
                                 None if sponsor == "-" else sponsor)
    result = Members(dict(sorted(members.items())))
    result.host()
    return result


def render_members(members: Members) -> bytes:
    lines = ["# handle role address public_key sponsor"]
    for handle in sorted(members.by_handle):
        m = members.by_handle[handle]
        lines.append(f"{m.handle} {m.role} {m.address} {m.public_key or '-'} {m.sponsor or '-'}")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------- .gud/owners (§10.3)

@dataclass(frozen=True)
class OwnerRule:
    glob: str
    rule: Literal["lazy", "explicit", "quorum"]
    owners: tuple[Handle, ...]


@dataclass(frozen=True)
class Owners:
    rules: tuple[OwnerRule, ...]

    def rule_for(self, path: str) -> OwnerRule | None:
        for rule in reversed(self.rules):
            if glob_match(rule.glob, path):
                return rule
        return None

    def builtin_for(self, path: str) -> Literal["quorum", "members"] | None:
        if path in BUILTIN_QUORUM:
            return "quorum"
        if path == MEMBERS:
            return "members"
        return None


def parse_owners(data: bytes) -> Owners:
    """§10.3. Raises ConfigError."""
    rules = []
    for n, parts, _ in _lines(data, OWNERS):
        if len(parts) < 2:
            raise ConfigError(f"{OWNERS}:{n}: expected a glob and a rule")
        glob, rule, owners = parts[0], parts[1], parts[2:]
        Glob(glob)
        if rule not in ("lazy", "explicit", "quorum"):
            raise ConfigError(f"{OWNERS}:{n}: bad rule {rule!r}")
        for owner in owners:
            if not _HANDLE_RE.fullmatch(owner):
                raise ConfigError(f"{OWNERS}:{n}: bad owner {owner!r}")
        if rule != "quorum" and not owners:
            raise ConfigError(f"{OWNERS}:{n}: {rule} needs at least one owner")
        rules.append(OwnerRule(glob, rule, () if rule == "quorum" else tuple(owners)))  # type: ignore[arg-type]
    return Owners(tuple(rules))


# ---------------------------------------------------------------- .gud/test (§10.4)

@dataclass(frozen=True)
class TestPolicy:
    __test__ = False   # not a pytest test class

    command: str | None = None
    image: str | None = None
    timeout: Ms = 600_000
    on_paths: tuple[str, ...] = ()
    on_tip_links: int | None = 5
    on_tip_time: Ms | None = 600_000
    every_link: bool = False
    stack_max: int = 8
    auto_revert: Ms | None = None


def _one(parts: list[str], name: str, n: int) -> str:
    if len(parts) != 2:
        raise ConfigError(f"{name}:{n}: {parts[0]} takes one value")
    return parts[1]


def _uint(value: str, name: str, n: int, minimum: int = 0) -> int:
    if not _UINT_RE.fullmatch(value) or int(value) < minimum:
        raise ConfigError(f"{name}:{n}: bad number {value!r}")
    return int(value)


def _dur(value: str, name: str, n: int) -> Ms:
    try:
        return parse_duration(value)
    except ConfigError as exc:
        raise ConfigError(f"{name}:{n}: {exc}") from None


def parse_test(data: bytes | None) -> TestPolicy:
    """§10.4; None (no file) gives the defaults. Unknown keys are ignored; repeated keys are errors."""
    values: dict[str, object] = {}
    seen: set[str] = set()
    for n, parts, rest in _lines(data or b"", TEST):
        key = parts[0]
        if key in seen:
            raise ConfigError(f"{TEST}:{n}: {key} given twice")
        seen.add(key)
        if len(parts) < 2:
            raise ConfigError(f"{TEST}:{n}: {key} needs a value")
        if key == "command":
            values["command"] = rest.rstrip(" \t")
        elif key == "image":
            values["image"] = _one(parts, TEST, n)
        elif key == "timeout":
            values["timeout"] = _dur(_one(parts, TEST, n), TEST, n)
        elif key == "on-paths":
            globs = _split_list(rest.rstrip(" \t"))
            for glob in globs:
                Glob(glob)
            values["on_paths"] = globs
        elif key == "on-tip":
            if parts[1:] == ["off"]:
                values["on_tip_links"] = values["on_tip_time"] = None
                continue
            links: int | None = None
            time: Ms | None = None
            for item in parts[1:]:
                k, eq, v = item.partition("=")
                if k == "links" and eq and links is None:
                    links = _uint(v, TEST, n, 1)
                elif k == "time" and eq and time is None:
                    time = _dur(v, TEST, n)
                else:
                    raise ConfigError(f"{TEST}:{n}: bad on-tip item {item!r}")
            values["on_tip_links"], values["on_tip_time"] = links, time
        elif key == "every-link":
            value = _one(parts, TEST, n)
            if value not in ("on", "off"):
                raise ConfigError(f"{TEST}:{n}: every-link is on or off")
            values["every_link"] = value == "on"
        elif key == "stack-max":
            values["stack_max"] = _uint(_one(parts, TEST, n), TEST, n, 1)
        elif key == "auto-revert":
            value = _one(parts, TEST, n)
            values["auto_revert"] = None if value == "off" else _dur(value, TEST, n)
    return TestPolicy(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------- .gud/config (§10.5)

@dataclass(frozen=True)
class RepoConfig:
    window: Ms = 60_000
    grace: Ms = 30_000
    skew: Ms = 120_000
    own_links: int = 50
    own_time: Ms = 86_400_000
    lease: Ms = 1_800_000
    escalate: Ms = 3_600_000
    expire: Ms = 604_800_000
    carry: Literal["file", "line"] = "file"
    fix_offer: Ms = 300_000
    lease_thrash: int = 3
    checkpoint: int = 1000
    authserv_id: str | None = None
    task_quiet: Ms | None = None          # §16.6 done by quiescence; None: the lease L

    @property
    def quiet(self) -> Ms:
        """`task-quiet`, defaulting to the lease (§10.5)."""
        return self.lease if self.task_quiet is None else self.task_quiet


_CONFIG_DURATIONS = {"window", "grace", "skew", "own-time", "lease", "escalate", "expire", "fix-offer", "task-quiet"}
_CONFIG_INTS = {"own-links": 0, "lease-thrash": 1, "checkpoint": 1}


def parse_config(data: bytes | None) -> RepoConfig:
    """§10.5; None gives the defaults. Unknown keys are ignored; repeated known keys are errors."""
    values: dict[str, object] = {}
    for n, parts, _ in _lines(data or b"", CONFIG):
        key = parts[0]
        attr = key.replace("-", "_")
        if key not in _CONFIG_DURATIONS and key not in _CONFIG_INTS and key not in ("carry", "authserv-id"):
            continue
        if attr in values:
            raise ConfigError(f"{CONFIG}:{n}: {key} given twice")
        if len(parts) != 2:
            raise ConfigError(f"{CONFIG}:{n}: {key} takes one value")
        value = parts[1]
        if key in _CONFIG_DURATIONS:
            values[attr] = _dur(value, CONFIG, n)
        elif key in _CONFIG_INTS:
            values[attr] = _uint(value, CONFIG, n, _CONFIG_INTS[key])
        elif key == "carry":
            if value not in ("file", "line"):
                raise ConfigError(f"{CONFIG}:{n}: carry is file or line")
            values[attr] = value
        else:
            values[attr] = value
    return RepoConfig(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------- RepoRules

@dataclass(frozen=True)
class RepoRules:
    members: Members
    owners: Owners
    test: TestPolicy
    config: RepoConfig

    def encode(self) -> bytes:
        """Canonical bytes (sorted-key compact JSON), stored at LinkState.rules_root."""
        config = asdict(self.config)
        if config["task_quiet"] is None:
            del config["task_quiet"]             # bundles without task-quiet keep their earlier bytes
        doc = {
            "members": [[m.handle, m.role, m.address, m.public_key, m.sponsor]
                        for _, m in sorted(self.members.by_handle.items())],
            "owners": [[r.glob, r.rule, list(r.owners)] for r in self.owners.rules],
            "test": {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self.test).items()},
            "config": config,
            "version": 1,
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> RepoRules:
        try:
            doc = json.loads(data.decode("utf-8"))
            members = Members({row[0]: Member(*row) for row in doc["members"]})
            owners = Owners(tuple(OwnerRule(g, r, tuple(o)) for g, r, o in doc["owners"]))
            test = dict(doc["test"])
            test["on_paths"] = tuple(test["on_paths"])
            known = {f.name for f in fields(TestPolicy)}
            policy = TestPolicy(**{k: v for k, v in test.items() if k in known})
            config = RepoConfig(**doc["config"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"bad rules bundle: {exc}") from exc
        return cls(members, owners, policy, config)


def _file(tree: Tree, path: str) -> bytes | None:
    entry = tree.get(path)
    if entry is None:
        return None
    if entry.mode not in REGULAR_MODES:
        raise ConfigError(f"{path} must be a regular file")
    return tree.read(path)


def rules_of(tree: Tree) -> RepoRules:
    """Parse the four config files of a tree. Raises ConfigError (the push becomes bad_config)."""
    members = _file(tree, MEMBERS)
    if members is None:
        raise ConfigError(f"{MEMBERS} is missing")
    return RepoRules(parse_members(members), parse_owners(_file(tree, OWNERS) or b""),
                     parse_test(_file(tree, TEST)), parse_config(_file(tree, CONFIG)))
