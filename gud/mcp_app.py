"""Hosted MCP app: a live feed of the gud repo, rendered in the chat.

Run ``python -m gud.mcp_app`` (streamable HTTP on ``$PORT``, default 8000).
``--stdio`` speaks MCP on stdin instead. Add the URL to Claude, ChatGPT, Cursor,
or VS Code — nothing is cloned.

The widget is one HTML file (``resources/dist/feed.html``) served as a ``ui://``
resource. mcp-use is used when it imports; this tree's mcp 2 install does not
load mcp-use 1.7, so the same tools are registered on ``mcp.server.mcpserver``
with the Apps extension, which is the same ``_meta.ui.resourceUri`` wire.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from .config import load
from .console import Console, ConsoleError, text_fallback

# Tool annotations are evaluated against this module. Each builder assigns the
# Context class its SDK injects before the tools are registered.
Context = None

WIDGET_URI = "ui://gud/feed.html"
APP_MIME = "text/html;profile=mcp-app"
WIDGET_PATH = Path(__file__).resolve().parent.parent / "resources" / "dist" / "feed.html"

INSTRUCTIONS = """This is a gud repo. The repo is an email chain: every push is a signed diff email, consent and test results are replies, and nothing is cloned.

Call gud to show the live feed. The widget refreshes itself. gud(focus=<path>) reads a file at the tip with blame, without cloning. gud(focus=<id>) opens a task, bug, or push thread.

The chat prompt supplies handle, repo, and private_key. Pass those three on every call. private_key is the raw Ed25519 key. repo is the repo address and selects that inbox for the call. Writes are signed with that key as that handle. token is another name for private_key.

gud_decide records one reply (approve, object, or veto on a push; approve or reject on a bug report). gud_task opens a task. gud_push changes files: pass edits and it signs a push with that key. There is no git commit.
"""

GUD_DESC = (
    "Live feed of the gud repo: tip, tasks, bugs, and new mail. "
    "Pass handle, repo, and private_key from the chat prompt on every call. "
    "repo selects that inbox. Omit since for a snapshot; pass the previous cursor to receive only newer events. "
    "focus is a task id (T3), a bug or push id, or a file path — a path is read from the tip with blame, without cloning."
)
DECIDE_DESC = (
    "Send one reply on a push or bug report, signed with private_key as handle. "
    "verdict is approve, object, or veto for a push, and approve or reject for a bug. "
    "Pass handle, repo, and private_key. token is another name for private_key."
)
TASK_DESC = (
    "Open a task signed with private_key as handle. "
    "Pass handle, repo, and private_key. token is another name for private_key."
)
PUSH_DESC = (
    "Change files by signing a push. This is the commit. Pass handle, repo, and private_key. "
    "token is another name for private_key. "
    "edits is a list applied in order to the tip: "
    '{"path": "a.py", "old": "exact text, once in the file", "new": "replacement"} replaces; '
    '{"path": "a.py", "new": "full text"} creates a file; '
    '{"path": "a.py", "delete": true} removes it. '
    "intent is the note other agents read. task sets Gud-Task when this finishes a subtask."
)


def widget_html() -> str:
    if WIDGET_PATH.exists():
        return WIDGET_PATH.read_text()
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>gud</title></head>"
        "<body><p>gud feed is not built. Run npm run build:widget.</p></body></html>"
    )


def _port() -> int:
    return int(os.environ.get("PORT", "8000"))


def _result(text: str, data: dict | None = None, error: bool = False) -> CallToolResult:
    content = [TextContent(type="text", text=text)]
    names = set(CallToolResult.model_fields)
    kw: dict = {"content": content}
    kw["is_error" if "is_error" in names else "isError"] = error
    if data is not None:
        kw["structured_content" if "structured_content" in names else "structuredContent"] = data
    return CallToolResult(**kw)


def _credential(private_key: str, token: str) -> str:
    """``private_key`` wins. ``token`` is the same key under the old argument name."""
    return (private_key or "").strip() or (token or "").strip()


def _gud(console: Console, since: str, focus: str, repo: str) -> CallToolResult:
    console.select_repo(repo)
    data = console.state(since=since or None, focus=focus or None)
    return _result(text_fallback(data), data)


def _decide(console: Console, target: str, verdict: str, reason: str, handle: str, repo: str,
            private_key: str, token: str) -> CallToolResult:
    try:
        console.select_repo(repo)
        ident = console.identity_for_key(_credential(private_key, token), handle)
        mid = console.signed_reply(target, verdict, reason or "", ident)
    except ConsoleError as exc:
        return _result(str(exc), error=True)
    return _result(f"{verdict} {mid} target={target}")


def _task(console: Console, title: str, description: str, handle: str, repo: str,
          private_key: str, token: str) -> CallToolResult:
    try:
        console.select_repo(repo)
        ident = console.identity_for_key(_credential(private_key, token), handle)
        tid = console.create_task(title, description or "", identity=ident)
    except ConsoleError as exc:
        return _result(str(exc), error=True)
    return _result(tid)


def _push(console: Console, intent: str, edits: list, task: str, base: str, handle: str, repo: str,
          private_key: str, token: str) -> CallToolResult:
    """Same push the agent server signs. The key check matches the other feed writes."""
    from .mcp_server import GudTools

    try:
        console.select_repo(repo)
        ident = console.identity_for_key(_credential(private_key, token), handle)
    except ConsoleError as exc:
        return _result(str(exc), error=True)
    tools = GudTools(console.mailbox, window_seconds=console.cfg.window_seconds, clock=console.clock)
    text = tools.push(
        intent, edits, task.strip() or None, base.strip() or None, ident.handle, _credential(private_key, token),
    )
    console.invalidate()
    failed = text.startswith("error:") or "\nerror:" in text
    return _result(text, error=failed)


def _use_server():
    try:
        from mcp_use.server import MCPServer
    except Exception:
        return None
    return MCPServer


def default_console() -> Console:
    """The feed for this machine. ``repo.txt`` selects the inbox the GUI is showing."""
    from .mailbox import open_own_mailbox
    from .repos import read_active

    cfg = load()
    saved = read_active(cfg.home)
    if not saved:
        return Console(cfg)
    return Console(cfg, mailbox=open_own_mailbox(cfg, saved))


def build_server(console: Console | None = None):
    """Three tools: ``gud``, ``gud_decide``, ``gud_task``. ``gud`` carries the widget."""
    console = console or default_console()
    server_cls = _use_server()
    if server_cls is not None:
        return _build_use(server_cls, console)
    return _build_sdk(console)


def _build_use(server_cls, console: Console):
    from mcp_use.server import Context as UseContext

    global Context
    Context = UseContext
    server = server_cls(name="gud", instructions=INSTRUCTIONS, host="0.0.0.0", port=_port(),
                        debug=os.environ.get("GUD_MCP_DEBUG") == "1")

    @server.tool(
        name="gud", description=GUD_DESC,
        meta={"ui": {"resourceUri": WIDGET_URI}}, structured_output=False,
    )
    def gud(since: str = "", focus: str = "", handle: str = "", repo: str = "",
            private_key: str = "", token: str = ""):
        return _gud(console, since, focus, repo)

    @server.tool(name="gud_decide", description=DECIDE_DESC, structured_output=False)
    def gud_decide(target: str, verdict: str, reason: str = "", handle: str = "", repo: str = "",
                   private_key: str = "", token: str = "", ctx: Context | None = None):
        return _decide(console, target, verdict, reason, handle, repo, private_key, token)

    @server.tool(name="gud_task", description=TASK_DESC, structured_output=False)
    def gud_task(title: str, description: str = "", handle: str = "", repo: str = "",
                 private_key: str = "", token: str = "", ctx: Context | None = None):
        return _task(console, title, description, handle, repo, private_key, token)

    @server.tool(name="gud_push", description=PUSH_DESC, structured_output=False)
    def gud_push(intent: str, edits: list, task: str = "", base: str = "", handle: str = "", repo: str = "",
                 private_key: str = "", token: str = "", ctx: Context | None = None):
        return _push(console, intent, edits, task, base, handle, repo, private_key, token)

    @server.resource(WIDGET_URI, name="gud feed", description="Live gud feed", mime_type=APP_MIME)
    def gud_feed() -> str:
        return widget_html()

    _prime_tool_cache(server)
    return server


def _prime_tool_cache(server) -> None:
    # On a cache miss mcp refreshes via the tools/list handler with request=None, and mcp-use's
    # middleware wrapper reads None.params, so a tools/call before any tools/list fails.
    cache = getattr(getattr(server, "_mcp_server", None), "_tool_cache", None)
    if cache is None:
        return
    try:
        tools = asyncio.run(server.list_tools())
    except RuntimeError:
        return
    for tool in tools:
        cache[tool.name] = tool


def _build_sdk(console: Console):
    from mcp.server.apps import APP_MIME_TYPE, Apps
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.context import Context as SdkContext
    from mcp.server.mcpserver.resources import FunctionResource

    global Context
    Context = SdkContext
    apps = Apps()

    @apps.tool(resource_uri=WIDGET_URI, description=GUD_DESC, structured_output=False)
    def gud(since: str = "", focus: str = "", handle: str = "", repo: str = "",
            private_key: str = "", token: str = ""):
        return _gud(console, since, focus, repo)

    apps.add_resource(FunctionResource.from_function(
        widget_html, WIDGET_URI, name="gud feed", description="Live gud feed", mime_type=APP_MIME_TYPE,
    ))
    server = MCPServer("gud", instructions=INSTRUCTIONS, extensions=[apps])

    @server.tool(name="gud_decide", description=DECIDE_DESC, structured_output=False)
    def gud_decide(target: str, verdict: str, reason: str = "", handle: str = "", repo: str = "",
                   private_key: str = "", token: str = "", ctx: Context | None = None):
        return _decide(console, target, verdict, reason, handle, repo, private_key, token)

    @server.tool(name="gud_task", description=TASK_DESC, structured_output=False)
    def gud_task(title: str, description: str = "", handle: str = "", repo: str = "",
                 private_key: str = "", token: str = "", ctx: Context | None = None):
        return _task(console, title, description, handle, repo, private_key, token)

    @server.tool(name="gud_push", description=PUSH_DESC, structured_output=False)
    def gud_push(intent: str, edits: list, task: str = "", base: str = "", handle: str = "", repo: str = "",
                 private_key: str = "", token: str = "", ctx: Context | None = None):
        return _push(console, intent, edits, task, base, handle, repo, private_key, token)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gud.mcp_app", description="gud live-feed MCP app")
    parser.add_argument("--stdio", action="store_true", help="speak MCP on stdin instead of HTTP")
    args = parser.parse_args(argv)
    server = build_server()
    if args.stdio:
        server.run(transport="stdio")
        return
    port = _port()
    # mcp-use silently moves to the next free port; a host routing to $PORT would miss us.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError as exc:
            raise SystemExit(f"gud.mcp_app: port {port} is busy ({exc})") from exc
    server.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
