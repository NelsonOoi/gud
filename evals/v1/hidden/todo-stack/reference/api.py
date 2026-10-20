"""HTTP JSON API for the todo library."""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import todolib


def make_server(store: todolib.TodoStore, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return None

        def _send(self, status: int, body=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"null")
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise todolib.InvalidTodo("the body must be a JSON object")
            return data

        def _route(self, method: str):
            url = urlsplit(self.path)
            m = re.fullmatch(r"/todos/(\d+)", url.path)
            try:
                if url.path == "/todos" and method == "GET":
                    q = parse_qs(url.query)
                    return self._send(200, store.list(tag=q.get("tag", [None])[0], status=q.get("status", [None])[0]))
                if url.path == "/todos" and method == "POST":
                    body = self._body()
                    return self._send(201, store.add(body.get("text"), tags=body.get("tags"), due=body.get("due")))
                if m and method == "GET":
                    return self._send(200, store.get(int(m.group(1))))
                if m and method == "PATCH":
                    return self._send(200, store.update(int(m.group(1)), **self._body()))
                if m and method == "DELETE":
                    store.remove(int(m.group(1)))
                    return self._send(204)
                return self._send(404, {"error": "not found"})
            except todolib.TodoNotFound as exc:
                return self._send(404, {"error": str(exc)})
            except todolib.InvalidTodo as exc:
                return self._send(400, {"error": str(exc)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="api.py")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--file", default=None)
    args = ap.parse_args(argv)
    server = make_server(todolib.TodoStore(args.file), port=args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
