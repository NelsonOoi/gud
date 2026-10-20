"""A repo in local host mode over the fake transport, with one GUD_HOME per member, so the MCP
server can run as a real stdio subprocess (`python -m gud.mcp_v1`) next to in-process tools."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from gud.mcp_v1 import GudTools
from gud.transport.fake import FAKE_AUTHSERV_ID, FakeMailbox
from gud.wc.copy import WorkingCopy, create_genesis
from gud.wc.remote import LocalHost, RepoClient, save_key, write_transport_toml

from tests.v1.kit import REPO, Kit

ROOT = Path(__file__).resolve().parents[3]


class LocalRepo:
    def __init__(self, tmp: Path, *, config: str = "", **kit_kw: Any) -> None:
        self.tmp = Path(tmp)
        self.kit = Kit(config=config + f"authserv-id {FAKE_AUTHSERV_ID}\n", authserv=False, **kit_kw)
        self.mail = self.tmp / "mail"
        self.host_dir = self.tmp / "host"
        LocalHost.create(self.host_dir, repo=REPO, transport="fake", cfg={"root": str(self.mail)},
                         host_private=self.kit.host_key, objects=self.tmp / "host-objects")
        self.host_url = f"local:{self.host_dir}"
        self._clients: dict[str, RepoClient] = {}

    def home(self, handle: str) -> Path:
        return self.tmp / "homes" / handle.lstrip("@")

    def wc_dir(self, handle: str) -> Path:
        return self.tmp / "wc" / handle.lstrip("@")

    def member(self, handle: str) -> Path:
        """GUD_HOME for `handle`: key, transport.toml and remote.json, as `gud1` writes them."""
        home = self.home(handle)
        if not (home / "members" / handle / "key").exists():
            save_key(home, handle, self.kit.key(handle))
            write_transport_toml(home / "members" / handle / "transport.toml", "fake",
                                 {"root": str(self.mail), "address": self.kit.address(handle)})
            (home / "remote.json").write_text(json.dumps({"host_url": self.host_url, "repo": REPO, "handle": handle}))
        return home

    def client(self, handle: str) -> RepoClient:
        if handle not in self._clients:
            self.member(handle)
            self._clients[handle] = RepoClient(
                self.host_url, handle, self.kit.key(handle), send_via=FakeMailbox(self.mail, self.kit.address(handle)),
                repo_address=REPO, home=self.home(handle))
        return self._clients[handle]

    def genesis(self, files: Mapping[str, str | bytes], author: str = "@nelson") -> str:
        c = self.client(author)
        c.sync_log()
        content = {p: ("100644", v) for p, v in self.kit.gud_files().items()}
        for path, value in files.items():
            content[path] = ("100644", value if isinstance(value, bytes) else value.encode())
        gid = create_genesis(c, content, "genesis")
        c.sync_log()
        assert c.chain().tip is not None and c.chain().tip.state == gid
        return gid

    def clone(self, handle: str) -> WorkingCopy:
        return WorkingCopy.clone(self.client(handle), self.wc_dir(handle))

    def tools(self, handle: str, *, json_out: bool = True) -> GudTools:
        wc = self.wc_dir(handle)
        return GudTools(self.client(handle), wc if wc.exists() else None, json=json_out, log_wait_s=5, poll_s=0.2)

    def task(self, title: str, text: str = "", author: str = "@nelson") -> str:
        c = self.client(author)
        from gud.core.types import Record
        gid = c.sign_and_send("task", {"Gud-Title": title}, (Record("text", (), text.encode()),) if text else (),
                              summary=title)
        c.sync_log()
        return gid

    def server_params(self, handle: str) -> Any:
        from mcp.client.stdio import StdioServerParameters
        env = {k: v for k, v in os.environ.items() if not k.startswith("GUD_")}
        env.update({"PYTHONPATH": str(ROOT), "GUD_WAIT": "5"})
        return StdioServerParameters(command=sys.executable, args=[
            "-m", "gud.mcp_v1", "--home", str(self.member(handle)), "--handle", handle,
            "--wc", str(self.wc_dir(handle)), "--json"], env=env, cwd=str(self.tmp))


@pytest.fixture
def repo(tmp_path: Path) -> LocalRepo:
    return LocalRepo(tmp_path)


