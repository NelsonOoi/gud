import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}  # key -> (value, expires_at or None)

    def _live(self, key, now):
        item = self.data.get(key)
        if item is None:
            return None
        if item[1] is not None and item[1] <= now:
            del self.data[key]
            return None
        return item

    def put(self, key, value, ttl):
        now = time.monotonic()
        with self.lock:
            existed = self._live(key, now) is not None
            self.data[key] = (value, None if ttl is None else now + ttl)
            return not existed

    def get(self, key):
        with self.lock:
            item = self._live(key, time.monotonic())
            return item

    def delete(self, key):
        with self.lock:
            if self._live(key, time.monotonic()) is None:
                return False
            del self.data[key]
            return True

    def keys(self):
        now = time.monotonic()
        with self.lock:
            return sorted(k for k in list(self.data) if self._live(k, now) is not None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _route(self):
        """Return ('list', None), ('key', k) or (None, None)."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/kv":
            return "list", None
        if path.startswith("/kv/"):
            from urllib.parse import unquote
            rest = path[4:]
            if rest and "/" not in rest:
                key = unquote(rest)
                if key:
                    return "key", key
        return None, None

    def _dispatch(self, method):
        kind, key = self._route()
        if kind is None:
            self._drain()
            return self._send(404, {"error": "not found"})
        allowed = {"list": ("GET",), "key": ("GET", "PUT", "DELETE")}[kind]
        if method not in allowed:
            self._drain()
            return self._send(405, {"error": "method not allowed"})
        store = self.server.store
        if kind == "list":
            return self._send(200, {"keys": store.keys()})
        if method == "PUT":
            return self._put(key)
        self._drain()
        if method == "GET":
            item = store.get(key)
            if item is None:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"key": key, "value": item[0]})
        if store.delete(key):
            return self._send(204)
        return self._send(404, {"error": "not found"})

    def _drain(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _put(self, key):
        raw = self._drain()
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "body is not valid JSON"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "body must be a JSON object"})
        if "value" not in body:
            return self._send(400, {"error": "missing 'value'"})
        ttl = body.get("ttl")
        if "ttl" in body:
            if (isinstance(ttl, bool) or not isinstance(ttl, (int, float))
                    or not ttl > 0 or ttl == float("inf")):
                return self._send(400, {"error": "ttl must be a number greater than 0"})
        else:
            ttl = None
        created = self.server.store.put(key, body["value"], ttl)
        self._send(201 if created else 200, {"key": key, "value": body["value"]})

    def do_GET(self):
        self._dispatch("GET")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")


def make_server(host="127.0.0.1", port=0):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.store = Store()
    return server


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()
    make_server(a.host, a.port).serve_forever()


if __name__ == "__main__":
    main()
