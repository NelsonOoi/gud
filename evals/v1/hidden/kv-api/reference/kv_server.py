"""Reference kv server (hidden from agents; used to validate the hidden tests)."""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}   # key -> (value, expires_at or None)

    def _live(self, key, now):
        item = self.data.get(key)
        if item is None:
            return None
        if item[1] is not None and item[1] <= now:
            del self.data[key]
            return None
        return item

    def get(self, key):
        with self.lock:
            return self._live(key, time.monotonic())

    def put(self, key, value, ttl):
        with self.lock:
            now = time.monotonic()
            existed = self._live(key, now) is not None
            self.data[key] = (value, now + ttl if ttl else None)
            return existed

    def delete(self, key):
        with self.lock:
            if self._live(key, time.monotonic()) is None:
                return False
            del self.data[key]
            return True

    def keys(self):
        with self.lock:
            now = time.monotonic()
            return sorted(k for k in list(self.data) if self._live(k, now) is not None)


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body=None):
            self.send_response(status)
            if body is None:
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _key(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/kv/"):
                key = path[4:]
                if key and "/" not in key:
                    return key
            return None

        def do_GET(self):
            if self.path.split("?", 1)[0] in ("/kv", "/kv/"):
                return self._send(200, {"keys": store.keys()})
            key = self._key()
            if key is None:
                return self._send(404, {"error": "not found"})
            item = store.get(key)
            if item is None:
                return self._send(404, {"error": "not found"})
            self._send(200, {"key": key, "value": item[0]})

        def do_PUT(self):
            key = self._key()
            if key is None:
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"")
            except ValueError:
                return self._send(400, {"error": "body must be JSON"})
            if not isinstance(body, dict) or "value" not in body:
                return self._send(400, {"error": "body must be an object with a value"})
            ttl = body.get("ttl")
            if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0):
                return self._send(400, {"error": "ttl must be a number greater than 0"})
            existed = store.put(key, body["value"], ttl)
            self._send(200 if existed else 201, {"key": key, "value": body["value"]})

        def do_DELETE(self):
            key = self._key()
            if key is None or not store.delete(key):
                return self._send(404, {"error": "not found"})
            self._send(204)

    return Handler


def make_server(host, port):
    return ThreadingHTTPServer((host, port), make_handler(Store()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    make_server(args.host, args.port).serve_forever()


if __name__ == "__main__":
    main()
