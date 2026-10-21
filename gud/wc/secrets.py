"""Pre-push secret scan (SPEC §22).

Every member receives every diff, so a secret pushed once is leaked for good. `scan` runs the
§22 patterns over the bytes of every added or changed file and reports each hit with its
1-based line. A push is refused when anything matches, unless the path is allowed
(`gud push --allow-secret PATH`).

The gud private key file pattern matches a file whose whole content is one gud key in the
`ed25519:<base64 of 32 bytes>` form (the format `gud keys new` writes), optionally surrounded
by whitespace. A public key alone in a file has the same form and is reported too; public
keys inside `.gud/members` (one per member line among other fields) are not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

PATTERNS: tuple[tuple[str, "re.Pattern[bytes]"], ...] = (
    ("pem-private-key", re.compile(rb"-----BEGIN[^\r\n]{0,64}PRIVATE KEY-----")),
    ("openssh-private-key", re.compile(rb"-----BEGIN OPENSSH PRIVATE KEY-----")),
    ("gud-private-key", re.compile(rb"\A[ \t\r\n]*ed25519:[A-Za-z0-9+/]{43}=[ \t\r\n]*\Z")),
    ("aws-access-key-id", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(rb"ghp_[A-Za-z0-9]{36}")),
    ("github-pat", re.compile(rb"github_pat_")),
    ("slack-token", re.compile(rb"xox[baprs]-")),
    ("anthropic-key", re.compile(rb"sk-ant-")),
    ("api-key", re.compile(rb"sk-[A-Za-z0-9_-]{20,}")),
)


@dataclass(frozen=True)
class SecretHit:
    path: str
    line: int
    pattern: str


def scan(files: Mapping[str, bytes], allow: Iterable[str] = ()) -> list[SecretHit]:
    """Hits in `files` (path -> bytes), sorted by (path, line, pattern); `allow` paths are skipped."""
    allowed = set(allow)
    hits: set[SecretHit] = set()
    for path in files:
        if path in allowed:
            continue
        data = files[path]
        for name, rx in PATTERNS:
            for m in rx.finditer(data):
                start = m.start()
                if name == "gud-private-key":
                    start = data.find(b"ed25519:")
                hits.add(SecretHit(path, data.count(b"\n", 0, start) + 1, name))
    return sorted(hits, key=lambda h: (h.path.encode("utf-8"), h.line, h.pattern))
