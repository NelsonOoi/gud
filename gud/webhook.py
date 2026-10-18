"""Receive Primitive webhooks on a port of their own and append them to the local cache.

Run: ``python -m gud.webhook --port 8788``

This is not a route on the main API. That API trusts every loopback caller
with its write endpoints, and the tunnel would publish them. Only this port
is tunnelled.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .mail_primitive import PrimitiveError, _data, _unwrap_webhook
from .replay import replay

_TOLERANCE = 300


def signature_header(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{int(timestamp)}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={int(timestamp)},v1={digest}"


def verify_signature(secret: str, header: str | None, body: bytes, now: float,
                     tolerance: float = _TOLERANCE) -> str | None:
    """Return an error string, or None when ``header`` signs ``body``."""
    if not secret:
        return "webhook secret is not configured"
    if not header:
        return "missing Primitive-Signature"
    parts = {}
    for piece in header.split(","):
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        parts[key.strip()] = value.strip()
    raw_t = parts.get("t")
    given = parts.get("v1")
    if not raw_t or not given:
        return "Primitive-Signature needs t and v1"
    try:
        timestamp = int(raw_t)
    except ValueError:
        return "Primitive-Signature timestamp is not an integer"
    if abs(float(now) - timestamp) > float(tolerance):
        return "Primitive-Signature timestamp is outside the tolerance"
    expected = signature_header(secret, body, timestamp).split("v1=", 1)[1]
    if not hmac.compare_digest(expected, given.lower()):
        return "Primitive-Signature does not match"
    return None


def event_name(payload: dict) -> str:
    for key in ("type", "event"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("type", "event"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _record_text(record: dict) -> str:
    if record.get("body_text"):
        return str(record.get("body_text"))
    parsed = record.get("parsed") or {}
    if isinstance(parsed, dict) and parsed.get("body_text"):
        return str(parsed.get("body_text"))
    return ""


def delivery_recipients(payload: dict, email, domain: str) -> list[str]:
    """The delivered recipient, or every to/cc address on ``domain`` when the payload names none."""
    record = _unwrap_webhook(payload)
    named = ""
    if isinstance(record, dict):
        named = str(record.get("recipient") or "").strip().lower()
    if named:
        return [named]
    suffix = "@" + domain.lower()
    out = []
    seen = set()
    for addr in (*email.to, *email.cc):
        key = addr.lower()
        if key.endswith(suffix) and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def accept_delivery(payload: dict, raw: bytes, header: str | None, hosted, primitive, secret: str,
                    domain: str, now: float | None = None) -> tuple[int, dict]:
    """Verify, parse, and ingest one delivery. The status is 200 only after the row is written."""
    now = time.time() if now is None else now
    reason = verify_signature(secret, header, raw, now)
    if reason:
        return 401, {"detail": reason}
    if not isinstance(payload, dict):
        return 400, {"detail": "webhook payload must be a JSON object"}
    name = event_name(payload)
    if name and name != "email.received":
        print(f"webhook: ignored {name}", flush=True)
        return 200, {"ignored": name}
    record = _unwrap_webhook(payload)
    if isinstance(record, dict) and not _record_text(record) and record.get("id"):
        fetched = primitive.get(str(record["id"]))
        if fetched is not None and fetched.body:
            recipients = delivery_recipients(payload, fetched, domain)
            hosted.ingest(fetched, recipients)
            return 200, {"ingested": fetched.message_id, "recipients": recipients, "fetched": True}
    try:
        email = primitive.parse_webhook(payload)
    except PrimitiveError as exc:
        return 400, {"detail": exc.message}
    recipients = delivery_recipients(payload, email, domain)
    if not recipients:
        return 400, {"detail": "delivery names no recipient on this domain"}
    hosted.ingest(email, recipients)
    return 200, {"ingested": email.message_id, "recipients": recipients}


def create_app(hosted, secret: str, domain: str, primitive) -> FastAPI:
    """One route. Docs and the schema are off, so nothing else is mounted."""
    app = FastAPI(title="gud-webhook", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/primitive/webhook")
    async def primitive_webhook(request: Request):
        raw = await request.body()
        header = request.headers.get("primitive-signature")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({"detail": "webhook body is not JSON"}, status_code=400)
        status, body = accept_delivery(payload, raw, header, hosted, primitive, secret, domain)
        return JSONResponse(body, status_code=status)

    return app


def newest_received(hosted, recipient: str) -> float | None:
    rows = hosted.list(recipient=recipient)
    if not rows:
        return None
    return max(email.received_at for email in rows)


def served_addresses(hosted, cfg) -> list[str]:
    """Repo address, bugs@, and every member address on our domain."""
    from .bugs import bugs_address_for

    emails = hosted.list(recipient=hosted.repo_address)
    chain = replay(emails, cfg.window_seconds, time.time())
    found = {hosted.repo_address.lower(), bugs_address_for(hosted.repo_address)}
    tip = chain.tip
    if tip is not None:
        suffix = "@" + cfg.domain.lower()
        for member in tip.members.values():
            address = (member.address or "").lower()
            if address.endswith(suffix):
                found.add(address)
    return sorted(found)


def catch_up(hosted, primitive, addresses: list[str]) -> int:
    """List each address once, from the newest cached ``received_at``, and ingest the rest.

    This list is the only read of Primitive's mailbox. Later mail arrives by webhook.
    """
    written = 0
    for address in addresses:
        since = newest_received(hosted, address)
        for email in primitive.list(recipient=address, since=since):
            written += hosted.ingest(email, [address])
    return written


def load_secret(cfg, primitive=None) -> str:
    """``PRIMITIVE_WEBHOOK_SECRET``, else ``GUD_HOME/webhook_secret``, else one fetch."""
    env = (os.environ.get("PRIMITIVE_WEBHOOK_SECRET") or "").strip()
    if env:
        return env
    path = Path(cfg.home) / "webhook_secret"
    if path.exists():
        return path.read_text().strip()
    if primitive is None:
        raise RuntimeError("PRIMITIVE_WEBHOOK_SECRET is not set and webhook_secret is missing")
    data = _data(primitive._request("GET", "/account/webhook-secret")) or {}
    secret = str(data.get("secret") or "").strip()
    if not secret:
        raise RuntimeError("Primitive returned no webhook secret")
    _write_secret(path, secret)
    return secret


def _write_secret(path: Path, secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n")
    path.chmod(0o600)


def setup_endpoint(primitive, url: str, secret_path: Path) -> dict:
    """Create or reactivate the HTTP endpoint, store the secret, and fire a test delivery."""
    created = primitive._request("POST", "/endpoints", json_body={
        "kind": "http",
        "url": url,
        "rules": {"event_types": ["email.received"]},
    })
    endpoint = _data(created) or {}
    endpoint_id = endpoint.get("id")
    if not endpoint_id:
        raise RuntimeError(f"Primitive did not return an endpoint id: {created!r}"[:300])
    secret = load_secret_from(primitive)
    _write_secret(secret_path, secret)
    tested = primitive._request("POST", f"/endpoints/{endpoint_id}/test")
    result = _data(tested) or {}
    status = result.get("status")
    if status != 200:
        raise RuntimeError(
            f"test delivery to {url} returned {status}: {str(result.get('body') or '')[:300]}"
        )
    return {"id": endpoint_id, "url": url, "status": status, "secret_path": str(secret_path)}


def load_secret_from(primitive) -> str:
    data = _data(primitive._request("GET", "/account/webhook-secret")) or {}
    secret = str(data.get("secret") or "").strip()
    if not secret:
        raise RuntimeError("Primitive returned no webhook secret")
    return secret


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gud.webhook", description="Primitive webhook receiver")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args(argv)
    import uvicorn

    from .config import load
    from .mailbox import open_mailbox

    cfg = load()
    if (cfg.mailbox or "").strip().lower() != "primitive-webhook":
        raise SystemExit("gud.webhook needs GUD_MAILBOX=primitive-webhook")
    hosted = open_mailbox(cfg)
    primitive = hosted.send_via
    secret = load_secret(cfg, primitive)
    addresses = served_addresses(hosted, cfg)
    written = catch_up(hosted, primitive, addresses)
    print(f"webhook catch-up ingested {written} rows for {', '.join(addresses)}", flush=True)
    uvicorn.run(
        create_app(hosted, secret, cfg.domain, primitive),
        host="127.0.0.1", port=args.port, log_level="warning",
    )


if __name__ == "__main__":
    main()
