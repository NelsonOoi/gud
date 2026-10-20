"""Fixtures for the working-copy tests: kit members, a virtual clock, the §21 host test double,
and RepoClients that reach it over HTTP (FastAPI TestClient) or in process."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from gud.core.types import Hex
from gud.wc.copy import WorkingCopy, create_genesis
from gud.wc.remote import RepoClient

from tests.v1.kit import REPO, T0, Kit
from tests.v1.wc.host_double import Clock, HostDouble, InProcess


class Net:
    """One repo: kit members, the host double, and a client factory (one GUD_HOME per member)."""

    def __init__(self, tmp: Path, *, http: bool = True, max_message_bytes: int | None = None, **kit_kw: Any) -> None:
        self.tmp = Path(tmp)
        self.kit = Kit(**kit_kw)
        self.clock = Clock(T0)
        kw = {"max_message_bytes": max_message_bytes} if max_message_bytes else {}
        self.host = HostDouble(self.tmp / "host", self.kit, self.clock, **kw)
        self.http = http
        self._app = None
        self._clients: dict[str, RepoClient] = {}

    def home(self, handle: str) -> Path:
        return self.tmp / "homes" / handle.lstrip("@")

    def client(self, handle: str, *, fresh: bool = False, home: Path | None = None) -> RepoClient:
        if handle in self._clients and not fresh and home is None:
            return self._clients[handle]
        mailbox = self.host.mailbox_for(self.kit.address(handle))
        kw: dict[str, Any] = {}
        if self.http:
            from fastapi.testclient import TestClient
            if self._app is None:
                self._app = self.host.app()
            url = "http://testserver"
            kw["http"] = TestClient(self._app, base_url=url)
        else:
            url = "inprocess:"
            kw["mirror"] = InProcess(self.host)
        c = RepoClient(url, handle, self.kit.key(handle), send_via=mailbox, repo_address=REPO,
                       home=home or self.home(handle), clock=self.clock, **kw)
        if home is None:
            self._clients[handle] = c
        return c

    def genesis(self, files: Mapping[str, Any], author: str = "@nelson") -> Hex:
        c = self.client(author)
        c.sync_log()
        content = {p: ("100644", v) for p, v in self.kit.gud_files().items()}
        for path, value in files.items():
            content[path] = value if isinstance(value, tuple) else ("100644", value if isinstance(value, bytes)
                                                                     else value.encode())
        gid = create_genesis(c, content, "genesis")
        self.settle()
        c.sync_log()
        assert c.chain().tip is not None and c.chain().tip.state == gid
        return gid

    def settle(self, ms: int = 1000) -> None:
        """Let the host log what was sent, and move the clock on."""
        self.clock.advance(ms)
        self.host.pull()

    def clone(self, handle: str, name: str | None = None, **kw: Any) -> WorkingCopy:
        return WorkingCopy.clone(self.client(handle), self.tmp / "wc" / (name or handle.lstrip("@")), **kw)


@pytest.fixture
def net(tmp_path: Path) -> Net:
    return Net(tmp_path)


@pytest.fixture
def local_net(tmp_path: Path) -> Net:
    return Net(tmp_path, http=False)
