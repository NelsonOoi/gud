import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj=None):
        if code == 204:
            self.send_response(204)
            self.end_headers()
            return
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        """Return ('list', None), ('key', k) or (None, None)."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/kv":
            return "list", None
        if path.startswith("/kv/"):
            k = path[4:]
            if k and "/" not in k:
                return "key", unquote(k)
        return None, None

    def _live(self, k):
        store = self.server.store
        item = store.get(k)
        if item is None:
            return None
        if item[1] is not None and time.monotonic() >= item[1]:
            del store[k]
            return None
        return item

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def do_GET(self):
        kind, k = self._route()
        if kind is None:
            return self._send(404, {"error": "not found"})
        with self.server.lock:
            if kind == "list":
                keys = sorted(x for x in list(self.server.store) if self._live(x))
                return self._send(200, {"keys": keys})
            item = self._live(k)
            if item is None:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"key": k, "value": item[0]})

    def do_DELETE(self):
        kind, k = self._route()
        if kind is None:
            return self._send(404, {"error": "not found"})
        if kind == "list":
            return self._send(405, {"error": "method not allowed"})
        with self.server.lock:
            if self._live(k) is None:
                return self._send(404, {"error": "not found"})
            del self.server.store[k]
        self._send(204)

    def do_PUT(self):
        kind, k = self._route()
        raw = self._body()
        if kind is None:
            return self._send(404, {"error": "not found"})
        if kind == "list":
            return self._send(405, {"error": "method not allowed"})
        try:
            body = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "body must be a JSON object"})
        if "value" not in body:
            return self._send(400, {"error": "missing value"})
        expires = None
        if "ttl" in body:
            ttl = body["ttl"]
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not ttl > 0 or ttl == float("inf"):
                return self._send(400, {"error": "ttl must be a number greater than 0"})
            expires = time.monotonic() + ttl
        with self.server.lock:
            existed = self._live(k) is not None
            self.server.store[k] = (body["value"], expires)
        self._send(200 if existed else 201, {"key": k, "value": body["value"]})

    def _other(self):
        kind, _ = self._route()
        if kind is None:
            return self._send(404, {"error": "not found"})
        self._send(405, {"error": "method not allowed"})

    do_POST = do_PATCH = do_HEAD = do_OPTIONS = _other


def make_server(host="127.0.0.1", port=0):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.store = {}
    server.lock = threading.Lock()
    return server


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    a = p.parse_args()
    make_server(a.host, a.port).serve_forever()


if __name__ == "__main__":
    main()
