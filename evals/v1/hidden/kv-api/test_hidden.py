"""Hidden acceptance tests for the kv-api spec. Agents never see this file.

The harness runs them against a copy of the final tree, named by EVAL_TREE.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

TREE = Path(os.environ.get("EVAL_TREE", ".")).resolve()


def _module():
    path = TREE / "kv_server.py"
    if not path.is_file():
        pytest.fail("kv_server.py is missing")
    spec = importlib.util.spec_from_file_location("kv_server_hidden", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server():
    mod = _module()
    srv = mod.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": "application/json"} if data is not None else {}
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    text = resp.read()
    ctype = resp.getheader("Content-Type") or ""
    conn.close()
    parsed = json.loads(text) if text else None
    return resp.status, parsed, ctype


def test_make_server_is_threading_http_server():
    import http.server
    mod = _module()
    srv = mod.make_server("127.0.0.1", 0)
    try:
        assert isinstance(srv, http.server.ThreadingHTTPServer)
    finally:
        srv.server_close()


def test_put_new_then_replace(server):
    status, body, ctype = call(server, "PUT", "/kv/a", {"value": 1})
    assert status == 201 and body == {"key": "a", "value": 1}
    assert "application/json" in ctype
    status, body, _ = call(server, "PUT", "/kv/a", {"value": {"x": [1, 2]}})
    assert status == 200 and body == {"key": "a", "value": {"x": [1, 2]}}


def test_get(server):
    call(server, "PUT", "/kv/name", {"value": "acme"})
    status, body, ctype = call(server, "GET", "/kv/name")
    assert status == 200 and body == {"key": "name", "value": "acme"}
    assert "application/json" in ctype


def test_get_missing(server):
    status, body, _ = call(server, "GET", "/kv/nope")
    assert status == 404 and body == {"error": "not found"}


def test_delete(server):
    call(server, "PUT", "/kv/d", {"value": None})
    status, body, _ = call(server, "DELETE", "/kv/d")
    assert status == 204 and body is None
    status, _, _ = call(server, "GET", "/kv/d")
    assert status == 404
    status, body, _ = call(server, "DELETE", "/kv/d")
    assert status == 404 and body == {"error": "not found"}


def test_list_keys_sorted(server):
    for key in ("b", "a", "c"):
        call(server, "PUT", f"/kv/{key}", {"value": key})
    status, body, _ = call(server, "GET", "/kv")
    assert status == 200 and body == {"keys": ["a", "b", "c"]}


def test_ttl_expires(server):
    status, _, _ = call(server, "PUT", "/kv/t", {"value": 1, "ttl": 0.5})
    assert status == 201
    assert call(server, "GET", "/kv/t")[0] == 200
    time.sleep(0.9)
    assert call(server, "GET", "/kv/t")[0] == 404
    assert call(server, "GET", "/kv")[1] == {"keys": []}
    assert call(server, "DELETE", "/kv/t")[0] == 404
    status, _, _ = call(server, "PUT", "/kv/t", {"value": 2})
    assert status == 201, "a PUT over an expired key creates it"


def test_put_without_ttl_clears_old_ttl(server):
    call(server, "PUT", "/kv/k", {"value": 1, "ttl": 0.5})
    call(server, "PUT", "/kv/k", {"value": 2})
    time.sleep(0.9)
    status, body, _ = call(server, "GET", "/kv/k")
    assert status == 200 and body["value"] == 2


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'{"nope": 1}', b'{"value": 1, "ttl": 0}',
                                 b'{"value": 1, "ttl": -3}', b'{"value": 1, "ttl": "soon"}'])
def test_bad_put_is_400(server, raw):
    status, body, _ = call(server, "PUT", "/kv/bad", raw=raw)
    assert status == 400
    assert isinstance(body, dict) and "error" in body
    assert call(server, "GET", "/kv/bad")[0] == 404


def test_unknown_path_is_error(server):
    status, body, _ = call(server, "GET", "/nothing")
    assert status in (404, 405)
    assert isinstance(body, dict) and "error" in body


def test_servers_have_separate_stores():
    mod = _module()
    one, two = mod.make_server("127.0.0.1", 0), mod.make_server("127.0.0.1", 0)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (one, two)]
    for t in threads:
        t.start()
    try:
        call(one.server_address[1], "PUT", "/kv/x", {"value": 1})
        assert call(two.server_address[1], "GET", "/kv/x")[0] == 404
    finally:
        for s in (one, two):
            s.shutdown()
            s.server_close()


def test_cli_serves(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(TREE / "kv_server.py"), "--port", str(port)],
                            cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        while True:
            try:
                status, _, _ = call(port, "GET", "/kv")
                break
            except OSError:
                if time.time() > deadline or proc.poll() is not None:
                    pytest.fail("python kv_server.py --port PORT did not start serving")
                time.sleep(0.2)
        assert status == 200
    finally:
        proc.kill()
        proc.wait(timeout=10)
