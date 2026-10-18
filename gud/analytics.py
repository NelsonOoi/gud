"""Record MCP tool calls with Armature, or do nothing when it isn't configured.

The installed SDK is ``armature-mcp-analytics``. It instruments a server by
patching ``tool()`` (``instrument_fastmcp`` / ``with_mcp_analytics``) so each
later registration is wrapped in ``AnalyticsRecorder.instrument_tool_call``.
That ships tool name, arguments, duration, and ok/error to
``https://app.armature.tech/api/mcp-analytics/ingest``.

``ARMATURE_API_KEY`` is passed as ``armature.api_key`` (the SDK's own env var
is ``ANALYTICS_INGEST_API_KEY``, which we do not consult). When the key is
unset or the package is missing, ``instrument`` and ``observe`` record nothing
remotely. A local trace of tool name, args size, latency, and error is kept
either way so evals can inspect calls without a network.

``capture_telemetry`` and ``send_feedback`` are off so the tools an agent sees
stay the ones gud and the git baseline register. Calls are still recorded.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import threading
import time
from functools import wraps
from typing import Any, Callable

_lock = threading.Lock()
_local: list[dict[str, Any]] = []
_recorder: Any = None
_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()


def api_key() -> str | None:
    key = os.environ.get("ARMATURE_API_KEY", "").strip()
    return key or None


def enabled() -> bool:
    return api_key() is not None and _import_sdk() is not None


def _import_sdk():
    try:
        import armature_mcp_analytics as sdk
    except ImportError:
        return None
    return sdk


def _config(key: str, actor: str | None = None) -> dict:
    armature: dict[str, Any] = {
        "api_key": key,
        # Keep advertised tool schemas exactly as registered. Recording stays on.
        "capture_telemetry": False,
        "send_feedback": False,
    }
    if actor:
        armature["actor_id"] = actor
    return {"armature": armature}


def instrument(server: Any, *, actor: str | None = None):
    """Instrument a FastMCP/MCPServer. No-op without a key or the SDK.

    Call this before registering tools. It patches ``server.tool`` so each
    tool registered afterwards is recorded by the SDK.
    """
    key = api_key()
    sdk = _import_sdk()
    if not key or sdk is None:
        return None
    try:
        return sdk.instrument_fastmcp(server, _config(key, actor))
    except Exception as exc:  # pragma: no cover - SDK mismatch should not take the server down
        print(f"gud analytics: not instrumenting ({exc})", file=sys.stderr)
        return None


def reset() -> None:
    """Clear the in-process trace. Does not affect Armature."""
    with _lock:
        _local.clear()


def recent() -> list[dict[str, Any]]:
    with _lock:
        return list(_local)


def _args_size(args: dict) -> int:
    try:
        return len(json.dumps(args, default=str).encode())
    except Exception:
        return 0


def _preview(value: Any, limit: int = 4000) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + "…"


def _ensure_recorder():
    global _recorder, _loop
    key = api_key()
    sdk = _import_sdk()
    if not key or sdk is None:
        return None, None
    with _lock:
        if _recorder is None:
            _recorder = sdk.create_analytics_recorder(_config(key))
        if _loop is None:
            loop = asyncio.new_event_loop()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(_loop_ready.set)
                loop.run_forever()

            threading.Thread(target=_run, name="gud-analytics", daemon=True).start()
            _loop = loop
    if not _loop_ready.wait(2):
        return None, None
    return _recorder, _loop


def flush(timeout: float = 5) -> None:
    """Wait for queued Armature events. No-op when analytics is off."""
    recorder, loop = _recorder, _loop
    if recorder is None or loop is None:
        return
    fut = asyncio.run_coroutine_threadsafe(recorder.flush(), loop)
    try:
        fut.result(timeout)
    except Exception:
        pass


def observe(server: str, tool: str, args: dict | None, fn: Callable[[], Any], *, actor: str | None = None) -> Any:
    """Run ``fn`` and record tool name, args size, latency, and error.

    ``private_key`` is a signing argument, not telemetry. It is dropped before
    the size is measured and before anything is shipped.
    """
    payload = {key: value for key, value in dict(args or {}).items() if key != "private_key"}
    handle = payload.get("handle")
    if isinstance(handle, str) and handle.strip():
        actor = handle.strip()
    started = time.perf_counter()
    error: str | None = None
    result: Any = None
    try:
        result = fn()
        if isinstance(result, str) and result.startswith("error:"):
            error = result.splitlines()[0][:500]
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        row = {
            "server": server,
            "tool": tool,
            "args_size": _args_size(payload),
            "latency_ms": latency_ms,
            "error": error is not None,
            "actor": actor,
        }
        with _lock:
            _local.append(row)
            if len(_local) > 5000:
                del _local[:1000]
        _ship(tool, payload, latency_ms, error, result, actor)


def _ship(tool: str, args: dict, latency_ms: int, error: str | None, result: Any, actor: str | None) -> None:
    recorder, loop = _ensure_recorder()
    if recorder is None or loop is None:
        return

    async def _send() -> None:
        try:
            await recorder.record_tool_call(
                name=tool,
                args=args,
                duration_ms=latency_ms,
                status="error" if error else "ok",
                result=None if error else _preview(result),
                error=error,
                auth_info={"principalId": actor} if actor else None,
            )
        except Exception:
            pass

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop)
    except Exception:
        pass


def _actor_of(obj: Any) -> str | None:
    ident = getattr(obj, "identity", None)
    handle = getattr(ident, "handle", None) if ident is not None else None
    return handle or getattr(obj, "handle", None)


def tracked(server: str):
    """Decorator for a tool method. Records every direct call via ``observe``."""

    def deco(fn: Callable) -> Callable:
        signature = inspect.signature(fn)

        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            payload = {k: v for k, v in bound.arguments.items() if k != "self"}
            return observe(server, fn.__name__, payload, lambda: fn(self, *args, **kwargs), actor=_actor_of(self))

        # Leave __signature__ unset so inspect follows __wrapped__ and still sees `self`.
        # A bound method whose explicit signature has no self is rejected by the MCP SDK.
        return wrapper

    return deco


def unwrap(bound):
    """Bound method under ``tracked``, as a plain bound method the SDK can wrap itself."""
    fn = bound.__func__
    self = bound.__self__
    seen = 0
    while hasattr(fn, "__wrapped__") and seen < 5:
        fn = fn.__wrapped__
        seen += 1
    return fn.__get__(self, type(self))
