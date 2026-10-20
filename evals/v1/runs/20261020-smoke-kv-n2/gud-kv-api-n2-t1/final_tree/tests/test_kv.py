import http.client
import json
import threading
import time

import pytest

from kv_server import make_server


@pytest.fixture
def server():
    s = make_server("127.0.0.1", 0)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield s
    s.shutdown()
    s.server_close()


def req(server, method, path, body=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    data = raw if raw is not None else (None if body is None else json.dumps(body))
    c.request(method, path, body=data)
    r = c.getresponse()
    payload = r.read()
    ctype = r.getheader("Content-Type")
    c.close()
    return r.status, (json.loads(payload) if payload else None), ctype


def test_put_get_roundtrip(server):
    st, body, ct = req(server, "PUT", "/kv/a", {"value": {"x": [1, 2]}})
    assert st == 201 and body == {"key": "a", "value": {"x": [1, 2]}}
    assert ct == "application/json"
    st, body, ct = req(server, "GET", "/kv/a")
    assert st == 200 and body == {"key": "a", "value": {"x": [1, 2]}}
    assert ct == "application/json"


def test_put_replace_returns_200(server):
    req(server, "PUT", "/kv/a", {"value": 1})
    st, body, _ = req(server, "PUT", "/kv/a", {"value": None})
    assert st == 200 and body == {"key": "a", "value": None}
    assert req(server, "GET", "/kv/a")[1]["value"] is None


def test_get_missing(server):
    st, body, ct = req(server, "GET", "/kv/nope")
    assert st == 404 and body == {"error": "not found"}
    assert ct == "application/json"


def test_delete(server):
    req(server, "PUT", "/kv/a", {"value": 1})
    st, body, _ = req(server, "DELETE", "/kv/a")
    assert st == 204 and body is None
    assert req(server, "GET", "/kv/a")[0] == 404
    st, body, _ = req(server, "DELETE", "/kv/a")
    assert st == 404 and body == {"error": "not found"}


def test_list_sorted(server):
    assert req(server, "GET", "/kv")[:2] == (200, {"keys": []})
    for k in ("b", "c", "a"):
        req(server, "PUT", "/kv/" + k, {"value": k})
    assert req(server, "GET", "/kv")[1] == {"keys": ["a", "b", "c"]}


def test_ttl_expiry(server):
    st, _, _ = req(server, "PUT", "/kv/t", {"value": 1, "ttl": 0.2})
    assert st == 201
    assert req(server, "GET", "/kv/t")[0] == 200
    time.sleep(0.4)
    assert req(server, "GET", "/kv/t")[0] == 404
    assert req(server, "GET", "/kv")[1] == {"keys": []}
    assert req(server, "DELETE", "/kv/t")[0] == 404
    # expired key counts as new
    assert req(server, "PUT", "/kv/t", {"value": 2})[0] == 201


def test_put_replaces_ttl(server):
    req(server, "PUT", "/kv/t", {"value": 1, "ttl": 0.2})
    assert req(server, "PUT", "/kv/t", {"value": 2})[0] == 200
    time.sleep(0.4)
    assert req(server, "GET", "/kv/t")[1] == {"key": "t", "value": 2}


@pytest.mark.parametrize("body", [
    {"value": 1, "ttl": 0},
    {"value": 1, "ttl": -1},
    {"value": 1, "ttl": "5"},
    {"value": 1, "ttl": None},
    {"value": 1, "ttl": True},
    {"ttl": 5},
    [1, 2],
    "str",
])
def test_bad_put_bodies(server, body):
    st, resp, ct = req(server, "PUT", "/kv/a", body)
    assert st == 400 and isinstance(resp["error"], str)
    assert ct == "application/json"
    assert req(server, "GET", "/kv/a")[0] == 404


def test_invalid_json(server):
    st, resp, _ = req(server, "PUT", "/kv/a", raw="{nope")
    assert st == 400 and "error" in resp
    st, resp, _ = req(server, "PUT", "/kv/a", raw="")
    assert st == 400 and "error" in resp


def test_bad_paths_and_methods(server):
    for path in ("/", "/other", "/kv/", "/kv/a/b"):
        st, resp, _ = req(server, "GET", path)
        assert st == 404 and "error" in resp
    st, resp, _ = req(server, "POST", "/kv/a", {"value": 1})
    assert st in (404, 405) and "error" in resp
    st, resp, _ = req(server, "PUT", "/kv", {"value": 1})
    assert st in (404, 405) and "error" in resp


def test_servers_isolated():
    a, b = make_server("127.0.0.1", 0), make_server("127.0.0.1", 0)
    for s in (a, b):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        req(a, "PUT", "/kv/x", {"value": 1})
        assert req(b, "GET", "/kv/x")[0] == 404
    finally:
        for s in (a, b):
            s.shutdown()
            s.server_close()
