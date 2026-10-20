# Build a key-value HTTP API

Build `kv_server.py` at the repo root: a tiny JSON key-value store over HTTP. Standard library only
(`http.server`, `json`, `threading`, `time`, `argparse`).

- `make_server(host, port)` returns a `http.server.ThreadingHTTPServer` that is bound but not yet serving.
  Port 0 picks a free port. Each server has its own empty store.
- `python kv_server.py --port PORT [--host HOST]` serves forever (host defaults to 127.0.0.1).
- Every response body is JSON with `Content-Type: application/json`, except 204, which has no body.

Endpoints (keys are the rest of the path after `/kv/`, one non-empty path segment):

- `PUT /kv/<key>` with body `{"value": <any JSON>, "ttl": <optional seconds>}` stores the value.
  Responds `201` with `{"key": k, "value": v}` when the key was new (or had expired), `200` with the same body
  when it replaced a live key. `ttl` must be a number greater than 0; after that many seconds the key is gone.
  Without `ttl` the key never expires. A later PUT replaces both the value and the ttl.
- `GET /kv/<key>` responds `200` with `{"key": k, "value": v}`, or `404` with `{"error": "not found"}` when the
  key is missing or expired.
- `DELETE /kv/<key>` responds `204` when a live key was deleted, `404` with `{"error": "not found"}` otherwise.
- `GET /kv` responds `200` with `{"keys": [...]}`, the live keys sorted.
- A PUT body that is not valid JSON, is not an object, has no `value`, or has a bad `ttl` gets `400` with
  `{"error": "<message>"}`. Any other path or method gets `404` or `405` with an `error` body.

Add pytest tests under tests/ that start a server on port 0 in a thread and cover every endpoint, including TTL
expiry, and keep `python -m pytest -q` passing.
