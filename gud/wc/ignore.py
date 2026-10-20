"""`.gudignore` rules (SPEC-v1 §22): gitignore syntax over repo paths.

Syntax, one pattern per line (the file is read as UTF-8, invalid bytes replaced):

- Blank lines are ignored; a line starting with `#` is a comment (`\\#` for a literal `#`).
- Trailing spaces are dropped unless escaped with `\\`.
- `!` negates (re-includes) what an earlier pattern excluded (`\\!` for a literal `!`).
- A trailing `/` matches directories only (and so everything under them).
- A pattern containing a `/` other than a trailing one is anchored at the repo root (a
  leading `/` only anchors); any other pattern matches a file or directory name at any depth.
- `*` matches any run of characters other than `/`, `?` one character other than `/`,
  `[...]` a character class (`[!...]` or `[^...]` negated), `\\x` the character `x`.
  `**/` at the start matches any leading directories, `/**` at the end everything inside,
  and `/**/` in the middle zero or more directories.

The last matching pattern decides. As in git, a path under an excluded directory cannot be
re-included by a negation: once a directory is ignored, nothing below it is looked at.

`.git` (any path segment equal to `.git` in any case), `.gud/BASE` and `.gud/local/` are
always ignored, whatever the file says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from gud.core.objects import Tree

GUDIGNORE = ".gudignore"


@dataclass(frozen=True)
class _Rule:
    pattern: str
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool


def _class(pat: str, i: int) -> tuple[str, int]:
    """Translate `[...]` starting at pat[i] == "["; returns (regex, next index)."""
    j = i + 1
    out = "["
    if j < len(pat) and pat[j] in "!^":
        out += "^"
        j += 1
    first = True
    while j < len(pat):
        c = pat[j]
        if c == "]" and not first:
            return out + "]", j + 1
        if c == "\\" and j + 1 < len(pat):
            out += re.escape(pat[j + 1])
            j += 2
        else:
            out += "\\" + c if c in "\\^[" else c
            j += 1
        first = False
    return re.escape("["), i + 1       # unterminated: a literal "["


def _translate(body: str) -> str:
    """Regex (without anchors) for a pattern body with no leading/trailing slash."""
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        if body.startswith("**/", i) and (i == 0 or body[i - 1] == "/"):
            out.append("(?:[^/]*/)*")
            i += 3
            continue
        if body.startswith("**", i) and i + 2 == n and (i == 0 or body[i - 1] == "/"):
            out.append(".*")
            i += 2
            continue
        c = body[i]
        if c == "*":
            while i < n and body[i] == "*":
                i += 1
            out.append("[^/]*")
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            rx, i = _class(body, i)
            out.append(rx)
            continue
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(body[i + 1]))
            i += 2
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _strip_trailing_spaces(line: str) -> str:
    end = len(line)
    while end > 0 and line[end - 1] == " ":
        backslashes = 0
        k = end - 2
        while k >= 0 and line[k] == "\\":
            backslashes += 1
            k -= 1
        if backslashes % 2 == 1:
            break
        end -= 1
    return line[:end]


def _parse_line(raw: str) -> _Rule | None:
    line = raw.rstrip("\r\n")
    if not line or line.startswith("#"):
        return None
    line = _strip_trailing_spaces(line)
    if not line:
        return None
    negate = False
    if line.startswith("!"):
        negate = True
        line = line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]
    dir_only = False
    if line.endswith("/") and not line.endswith("\\/"):
        dir_only = True
        line = line.rstrip("/")
    if not line:
        return None
    anchored = "/" in line
    body = line.lstrip("/")
    if not body:
        return None
    rx = _translate(body)
    prefix = "" if anchored else "(?:[^/]*/)*"
    return _Rule(raw.rstrip("\r\n"), re.compile(prefix + rx + r"\Z", re.DOTALL), negate, dir_only)


def builtin_ignored(path: str) -> bool:
    """`.git` segments, `.gud/BASE` and `.gud/local/` (§22): always ignored."""
    if any(seg.lower() == ".git" for seg in path.split("/")):
        return True
    return path == ".gud/BASE" or path == ".gud/local" or path.startswith(".gud/local/")


class IgnoreRules:
    """Parsed `.gudignore` plus the built-ins. `ignored(path, is_dir)` takes repo paths."""

    def __init__(self, rules: tuple[_Rule, ...] = ()) -> None:
        self._rules = rules

    @classmethod
    def parse(cls, text: str) -> IgnoreRules:
        rules = []
        for raw in text.split("\n"):
            rule = _parse_line(raw)
            if rule is not None:
                rules.append(rule)
        return cls(tuple(rules))

    @classmethod
    def load(cls, root: Path, tree: Tree | None = None) -> IgnoreRules:
        """Rules from `root/.gudignore` when it is a regular file, else from `tree`'s copy."""
        path = Path(root) / GUDIGNORE
        data: bytes | None = None
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
        elif tree is not None:
            entry = tree.get(GUDIGNORE)
            if entry is not None and entry.mode != "120000":
                data = tree.read(GUDIGNORE)
        return cls.parse(data.decode("utf-8", "replace")) if data else cls()

    @property
    def patterns(self) -> tuple[str, ...]:
        return tuple(r.pattern for r in self._rules)

    def _match(self, path: str, is_dir: bool) -> bool:
        result = False
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(path):
                result = not rule.negate
        return result

    def ignored(self, path: str, is_dir: bool = False) -> bool:
        path = path.strip("/")
        if not path:
            return False
        if builtin_ignored(path):
            return True
        segs = path.split("/")
        for i in range(1, len(segs)):
            if self._match("/".join(segs[:i]), True):
                return True
        return self._match(path, is_dir)
