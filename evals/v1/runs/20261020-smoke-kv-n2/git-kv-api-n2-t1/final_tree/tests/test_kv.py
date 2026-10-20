import http.client
import json
import threading
import time

import pytest

from kv_server import make_server


@pytest.fixture
def conn():
    srv = make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        c.request(method, path, body=data)
        r = c.getresponse()
        text = r.read()
        c.close()
        return r.status, (json.loads(text) if text else None), r.getheader("Content-Type")

    yield call
    srv.shutdown()
    srv.server_close()


def test_put_get_delete(conn):
    assert conn("PUT", "/kv/a", {"value": [1, {"x": None}]})[:2] == (201, {"key": "a", "value": [1, {"x": None}]})
    assert conn("PUT", "/kv/a", {"value": 2})[:2] == (200, {"key": "a", "value": 2})
    s, b, ct = conn("GET", "/kv/a")
    assert (s, b, ct) == (200, {"key": "a", "value": 2}, "application/json")
    assert conn("DELETE", "/kv/a")[0] == 204
    assert conn("DELETE", "/kv/a")[:2] == (404, {"error": "not found"})
    assert conn("GET", "/kv/a")[:2] == (404, {"error": "not found"})


def test_list(conn):
    for k in "cab":
        conn("PUT", "/kv/" + k, {"value": 1})
    assert conn("GET", "/kv")[:2] == (200, {"keys": ["a", "b", "c"]})


def test_ttl(conn):
    conn("PUT", "/kv/t", {"value": 1, "ttl": 0.2})
    assert conn("GET", "/kv/t")[0] == 200
    time.sleep(0.3)
    assert conn("GET", "/kv/t")[0] == 404
    assert conn("GET", "/kv")[1] == {"keys": []}
    assert conn("PUT", "/kv/t", {"value": 1})[0] == 201
    conn("PUT", "/kv/u", {"value": 1, "ttl": 0.2})
    conn("PUT", "/kv/u", {"value": 2})
    time.sleep(0.3)
    assert conn("GET", "/kv/u")[0] == 200


@pytest.mark.parametrize("body", [{"value": 1, "ttl": 0}, {"value": 1, "ttl": -1},
                                  {"value": 1, "ttl": "x"}, {"value": 1, "ttl": True},
                                  {"value": 1, "ttl": None}, {"ttl": 1}, [1]])
def test_bad_put(conn, body):
    s, b, ct = conn("PUT", "/kv/k", body)
    assert s == 400 and "error" in b and ct == "application/json"


def test_invalid_json(conn):
    assert conn("PUT", "/kv/k", raw="{nope")[0] == 400


def test_other_paths(conn):
    assert conn("GET", "/other")[0] == 404
    assert conn("GET", "/kv/")[0] == 404
    assert conn("GET", "/kv/a/b")[0] == 404
    s, b, _ = conn("POST", "/kv/a", {"value": 1})
    assert s in (404, 405) and "error" in b
    assert conn("DELETE", "/kv")[0] in (404, 405)
