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
import os
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

gud_decide records one human reply (approve, object, or veto on a push; approve or reject on a bug report). gud_task opens a task as @console. When the server has a write token, pass it as token.
"""

GUD_DESC = (
    "Live feed of the gud repo: tip, tasks, bugs, and new mail. "
    "Omit since for a snapshot; pass the previous cursor to receive only newer events. "
    "focus is a task id (T3), a bug or push id, or a file path — a path is read from the tip with blame, without cloning."
)
DECIDE_DESC = (
    "Send one human reply on a push or bug report. "
    "verdict is approve, object, or veto for a push, and approve or reject for a bug. "
    "Pass token when the server requires GUD_MCP_TOKEN."
)
TASK_DESC = "Open a task. @console sends the signed task email. Pass token when the server requires GUD_MCP_TOKEN."


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


def _headers_from(ctx) -> dict:
    if ctx is None:
        return {}
    raw = None
    try:
        raw = getattr(ctx, "headers", None)
    except Exception:
        raw = None
    if not raw:
        getter = getattr(ctx, "get_http_request", None)
        if callable(getter):
            try:
                request = getter()
            except Exception:
                request = None
            if request is not None:
                raw = getattr(request, "headers", None)
    if not raw:
        return {}
    return {str(key).lower(): str(value) for key, value in raw.items()}


def _gud(console: Console, since: str, focus: str) -> CallToolResult:
    data = console.state(since=since or None, focus=focus or None)
    return _result(text_fallback(data), data)


def _decide(console: Console, target: str, verdict: str, reason: str, token: str, ctx) -> CallToolResult:
    try:
        console.authorize(token or None, _headers_from(ctx))
        mid = console.decide(target, verdict, reason or "", None)
    except ConsoleError as exc:
        return _result(str(exc), error=True)
    return _result(f"{verdict} {mid} target={target}")


def _task(console: Console, title: str, description: str, token: str, ctx) -> CallToolResult:
    try:
        console.authorize(token or None, _headers_from(ctx))
        tid = console.create_task(title, description or "", None)
    except ConsoleError as exc:
        return _result(str(exc), error=True)
    return _result(tid)


def _use_server():
    try:
        from mcp_use.server import MCPServer
    except Exception:
        return None
    return MCPServer


def build_server(console: Console | None = None):
    """Three tools: ``gud``, ``gud_decide``, ``gud_task``. ``gud`` carries the widget."""
    console = console or Console(load())
    server_cls = _use_server()
    if server_cls is not None:
        return _build_use(server_cls, console)
    return _build_sdk(console)


def _build_use(server_cls, console: Console):
    from mcp_use.server import Context as UseContext

    global Context
    Context = UseContext
    server = server_cls(name="gud", instructions=INSTRUCTIONS, host="0.0.0.0", port=_port())

    @server.tool(
        name="gud", description=GUD_DESC,
        meta={"ui": {"resourceUri": WIDGET_URI}}, structured_output=False,
    )
    def gud(since: str = "", focus: str = ""):
        return _gud(console, since, focus)

    @server.tool(name="gud_decide", description=DECIDE_DESC, structured_output=False)
    def gud_decide(target: str, verdict: str, reason: str = "", token: str = "",
                   ctx: Context | None = None):
        return _decide(console, target, verdict, reason, token, ctx)

    @server.tool(name="gud_task", description=TASK_DESC, structured_output=False)
    def gud_task(title: str, description: str = "", token: str = "",
                 ctx: Context | None = None):
        return _task(console, title, description, token, ctx)

    @server.resource(WIDGET_URI, name="gud feed", description="Live gud feed", mime_type=APP_MIME)
    def gud_feed() -> str:
        return widget_html()

    return server


def _build_sdk(console: Console):
    from mcp.server.apps import APP_MIME_TYPE, Apps
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.context import Context as SdkContext
    from mcp.server.mcpserver.resources import FunctionResource

    global Context
    Context = SdkContext
    apps = Apps()

    @apps.tool(resource_uri=WIDGET_URI, description=GUD_DESC, structured_output=False)
    def gud(since: str = "", focus: str = ""):
        return _gud(console, since, focus)

    apps.add_resource(FunctionResource.from_function(
        widget_html, WIDGET_URI, name="gud feed", description="Live gud feed", mime_type=APP_MIME_TYPE,
    ))
    server = MCPServer("gud", instructions=INSTRUCTIONS, extensions=[apps])

    @server.tool(name="gud_decide", description=DECIDE_DESC, structured_output=False)
    def gud_decide(target: str, verdict: str, reason: str = "", token: str = "",
                   ctx: Context | None = None):
        return _decide(console, target, verdict, reason, token, ctx)

    @server.tool(name="gud_task", description=TASK_DESC, structured_output=False)
    def gud_task(title: str, description: str = "", token: str = "",
                 ctx: Context | None = None):
        return _task(console, title, description, token, ctx)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gud.mcp_app", description="gud live-feed MCP app")
    parser.add_argument("--stdio", action="store_true", help="speak MCP on stdin instead of HTTP")
    args = parser.parse_args(argv)
    server = build_server()
    if args.stdio:
        server.run(transport="stdio")
        return
    server.run(transport="streamable-http", host="0.0.0.0", port=_port())


if __name__ == "__main__":
    main()
