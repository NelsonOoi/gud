import base64
import hashlib
import random

import pytest

from gud.core import envelope as E
from gud.core.types import EVENT_TYPES, ZERO, Envelope, Record, Seen

SEEN = Seen(3, "a" * 64)
NAME_SEGS = ["Parent", "Task", "Base", "Result", "X", "Y1", "Zz9", "Chunk", "Size"]
RNAMES = ["text", "put", "patch", "delete", "rename", "mode", "data", "x-1", "zz", "evidence"]


def _value(rng: random.Random) -> str:
    alphabet = "abcXYZ019 -_:/@.,%=é漢😀\u0085"
    while True:
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
        if value[0] != " " and value[-1] != " ":
            return value


def _path(rng: random.Random) -> str:
    alphabet = ["a", "B", "%", " ", "é", "\n", "\r", "\t", "~", "/", "漢", "#"]
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))


def _data(rng: random.Random) -> bytes:
    pick = rng.random()
    if pick < 0.2:
        return b""
    if pick < 0.5:
        return bytes(rng.choice(b"\n\r\0 ab") for _ in range(rng.randint(1, 40)))
    return bytes(rng.randrange(256) for _ in range(rng.randint(1, 200)))


def _random_envelope(rng: random.Random) -> Envelope:
    headers = {"Gud-Version": "1"}
    for _ in range(rng.randint(0, 8)):
        name = "Gud-" + "-".join(rng.choice(NAME_SEGS) for _ in range(rng.randint(1, 3)))
        headers[name] = _value(rng)
    records = []
    for _ in range(rng.randint(0, 6)):
        name = rng.choice(RNAMES)
        if name in ("put", "patch", "delete", "rename", "mode", "evidence"):
            count = {"rename": 2}.get(name, rng.randint(1, 3))
            paths = (0, 1) if name == "rename" else (0,)
            args = tuple(_path(rng) if i in paths else rng.choice(["100644", "x", "~!"]) for i in range(count))
        else:
            args = tuple(rng.choice(["a", "b=c", "%zz", "~"]) for _ in range(rng.randint(0, 2)))
        records.append(Record(name, args, _data(rng)))
    sig = None
    if rng.random() < 0.5:
        sig = "ed25519:" + base64.b64encode(bytes(rng.randrange(256) for _ in range(64))).decode()
    return Envelope(tuple(sorted(headers.items())), tuple(records), sig)


def _push(**extra) -> Envelope:
    headers = {"Parent": ZERO, "Tree": "1" * 64}
    headers.update(extra)
    return E.build("push", author="@alice", headers=headers, sent=1_760_000_000, seen=SEEN,
                   records=[Record("text", (), b"intent"), Record("delete", ("a.txt",), b"")])


# ---------------------------------------------------------------- bijection

def test_encode_decode_is_a_bijection_on_random_envelopes():
    rng = random.Random(1)
    for _ in range(600):
        env = _random_envelope(rng)
        data = E.encode(env)
        back = E.decode(data)
        assert back == env
        assert E.encode(back) == data


def test_mutated_bytes_either_reject_or_reencode_identically():
    rng = random.Random(2)
    accepted = 0
    for _ in range(300):
        data = bytearray(E.encode(_random_envelope(rng)))
        for _ in range(rng.randint(1, 3)):
            op = rng.random()
            i = rng.randrange(len(data))
            if op < 0.4:
                data[i] = rng.choice(b"\n\r\0 %:aZ")
            elif op < 0.7:
                data.insert(i, rng.randrange(256))
            else:
                del data[i]
        try:
            env = E.decode(bytes(data))
        except E.EnvelopeError:
            continue
        accepted += 1
        assert E.encode(env) == bytes(data)
    assert accepted > 0


def test_binary_record_data_with_cr_lf_nul_round_trips():
    payload = b"\r\n\0\n\n\r\x00\xff\xfe line\nGud-Sig: x\n\n"
    env = Envelope((("Gud-Kind", "blob"), ("Gud-Version", "1")), (Record("data", (), payload),))
    data = E.encode(env)
    assert data.endswith(b"data %d\n" % len(payload) + payload + b"\n")
    assert E.decode(data).records[0].data == payload


# ---------------------------------------------------------------- MUST-reject rules (§3.1, §3.2)

GOOD = b"Gud-Kind: push\nGud-Version: 1\n\ntext 2\nhi\n"


def test_good_reference_decodes():
    env = E.decode(GOOD)
    assert env.kind == "push" and env.text == "hi"


@pytest.mark.parametrize("data, why", [
    (b"Gud-Kind: push\nGud-Version: 1\n", "missing empty line"),
    (b"Gud-Kind: push\nGud-Version: 1", "missing empty line, no LF"),
    (b"Gud-Version: 1\nGud-Kind: push\n\n", "unsorted"),
    (b"Gud-Kind: push\nGud-Kind: push\nGud-Version: 1\n\n", "duplicate"),
    (b"gud-kind: push\nGud-Version: 1\n\n", "lowercase name"),
    (b"Gud-KInd: push\nGud-Version: 1\n\n", "mixed case segment"),
    (b"GUD-Kind: push\nGud-Version: 1\n\n", "uppercase prefix"),
    (b"Gud-Kind : push\nGud-Version: 1\n\n", "space before colon"),
    (b"Gud-Kind:push\nGud-Version: 1\n\n", "no space after colon"),
    (b"Gud-Kind: push\r\nGud-Version: 1\n\n", "CR in value"),
    (b"Gud-Kind: pu\x00sh\nGud-Version: 1\n\n", "NUL in value"),
    (b"Gud-Kind: pu\tsh\nGud-Version: 1\n\n", "TAB in value"),
    (b"Gud-Kind: pu\x7fsh\nGud-Version: 1\n\n", "DEL in value"),
    (b"Gud-Kind: \xff\nGud-Version: 1\n\n", "invalid UTF-8 value"),
    (b"Gud-Kind:  push\nGud-Version: 1\n\n", "leading space"),
    (b"Gud-Kind: push \nGud-Version: 1\n\n", "trailing space"),
    (b"Gud-Kind: \nGud-Version: 1\n\n", "empty value"),
    (b"Gud-Kind: push\n\n", "no version"),
    (b"Gud-Kind: push\nGud-Version: 2\n\n", "wrong version"),
    (b"Gud-Sig: ed25519:" + base64.b64encode(b"\1" * 64) + b"\nGud-Version: 1\n\n", "sig not last"),
    (b"Gud-Version: 1\nGud-Sig: ed25519:" + base64.b64encode(b"\1" * 63) + b"\n\n", "short sig"),
    (b"Gud-Version: 1\nGud-Sig: ed25519:" + base64.b64encode(b"\1" * 64)[:-2] + b"\n\n", "unpadded sig"),
    (b"Gud-Version: 1\nGud-Sig: ed25519:" + base64.b64encode(b"\1" * 64)[:-3] + b"B==\n\n", "non-canonical sig"),
    (b"Gud-Version: 1\nGud-Sig: rsa:abc\n\n", "bad sig scheme"),
    (GOOD + b"x", "bytes after the last record"),
    (GOOD + b"\n", "extra LF after record"),
    (b"Gud-Version: 1\n\ntext 2\nhiX", "data not followed by LF"),
    (b"Gud-Version: 1\n\ntext 2\nh", "short data"),
    (b"Gud-Version: 1\n\ntext 02\nhi\n", "leading zero length"),
    (b"Gud-Version: 1\n\ntext -1\nhi\n", "negative length"),
    (b"Gud-Version: 1\n\nText 2\nhi\n", "uppercase record name"),
    (b"Gud-Version: 1\n\n1text 2\nhi\n", "record name starts with digit"),
    (b"Gud-Version: 1\n\ntext  2\nhi\n", "double space"),
    (b"Gud-Version: 1\n\ntext 2 \nhi\n", "trailing space in record line"),
    (b"Gud-Version: 1\n\ntext\nhi\n", "missing length"),
    (b"Gud-Version: 1\n\ndelete 0 a\xc3\xa9\n\n", "raw non-ASCII arg"),
    (b"Gud-Version: 1\n\ndelete 0 a%2f\n\n", "lowercase percent hex"),
    (b"Gud-Version: 1\n\ndelete 0 a%41\n\n", "needless percent-encoding"),
    (b"Gud-Version: 1\n\ndelete 0 a%FF\n\n", "invalid UTF-8 after decoding"),
    (b"Gud-Version: 1\n\ndelete 0 a%2\n\n", "truncated percent"),
    (b"Gud-Version: 1\n\ndelete 0 %ZZ\n\n", "non-hex percent"),
])
def test_decoder_rejects(data, why):
    with pytest.raises(E.EnvelopeError):
        E.decode(data)


def test_encoder_rejects_what_the_decoder_rejects():
    base = Envelope((("Gud-Version", "1"),), ())
    bad = [
        Envelope((("Gud-Version", "1"), ("Gud-Kind", "x")), ()),
        Envelope((("Gud-Kind", "x"), ("Gud-Kind", "x"), ("Gud-Version", "1")), ()),
        Envelope((("Gud-kind", "x"), ("Gud-Version", "1")), ()),
        Envelope((("Gud-Kind", " x"), ("Gud-Version", "1")), ()),
        Envelope((("Gud-Kind", "a\nb"), ("Gud-Version", "1")), ()),
        Envelope((("Gud-Sig", "ed25519:x"), ("Gud-Version", "1")), ()),
        Envelope((("Gud-Kind", "x"),), ()),
        Envelope(base.headers, (Record("Bad", (), b""),)),
        Envelope(base.headers, (Record("text", ("a b",), b""),)),
        Envelope(base.headers, (Record("text", ("",), b""),)),
        Envelope(base.headers, (), sig="ed25519:short"),
    ]
    for env in bad:
        with pytest.raises(E.EnvelopeError):
            E.encode(env)


def test_unknown_headers_are_kept_and_signed():
    env = E.build("comment", author="@a", headers={"Target": "b" * 64, "Gud-Zz-Future": "x"},
                  records=[Record("text", (), b"hi")], sent=1, seen=SEEN)
    data = E.encode(env)
    assert b"Gud-Zz-Future: x\n" in data
    assert E.decode(data).get("Gud-Zz-Future") == "x"
    assert E.validate_kind(env) is None


# ---------------------------------------------------------------- paths and lists

def test_path_percent_encoding():
    assert E.encode_path("a b/c%d") == "a%20b/c%25d"
    assert E.encode_path("é") == "%C3%A9"
    assert E.encode_path("a\nb\rc\td") == "a%0Ab%0Dc%09d"
    rng = random.Random(3)
    for _ in range(300):
        path = _path(rng)
        assert E.decode_path(E.encode_path(path)) == path


def test_lists():
    assert E.parse_list("a,b, c,,d ") == ("a", "b", "c", "d")
    assert E.parse_list("x%2Cy,z%25") == ("x,y", "z%")
    assert E.render_list(["x,y", "z%"]) == "x%2Cy,z%25"
    assert E.parse_list(E.render_list(["a,b", "%2C", "c"])) == ("a,b", "%2C", "c")
    assert E.parse_list(None) == () and E.parse_list("") == ()
    env = Envelope((("Gud-After", "a, b"), ("Gud-Version", "1")), ())
    assert env.get_list("Gud-After") == ("a", "b")


def test_canonical_name():
    assert E.canonical_name("gud-parent-task") == "Gud-Parent-Task"
    assert E.canonical_name("parent-task") == "Gud-Parent-Task"
    assert E.canonical_name("GUD-BASE-RESULT") == "Gud-Base-Result"
    with pytest.raises(E.EnvelopeError):
        E.canonical_name("gud-1x")
    with pytest.raises(E.EnvelopeError):
        E.canonical_name("gud--x")


# ---------------------------------------------------------------- ids and signing

def test_signing_bytes_and_ids_match_the_spec():
    priv, pub = E.generate_key()
    env = E.sign(_push(), priv)
    data = E.encode(env)
    sig_line = b"Gud-Sig: " + env.sig.encode() + b"\n"
    assert sig_line in data
    assert data.index(sig_line) + len(sig_line) == data.index(b"\n\n") + 1
    assert E.signing_bytes(env) == data.replace(sig_line, b"")
    assert E.gud_id(env) == hashlib.sha256(data.replace(sig_line, b"")).hexdigest()
    unsigned = Envelope(env.headers, env.records)
    assert E.gud_id(unsigned) == E.gud_id(env)
    assert E.unsigned_id(b"raw mime") == hashlib.sha256(b"raw mime").hexdigest()
    assert E.verify(env, pub)
    assert E.verify(E.decode(data), pub)


def test_resent_bytes_keep_the_id_and_resigning_changes_it():
    priv, _ = E.generate_key()
    env = E.sign(_push(), priv)
    assert E.gud_id(E.decode(E.encode(env))) == E.gud_id(env)
    later = E.build("push", author="@alice", headers={"Parent": ZERO, "Tree": "1" * 64},
                    sent=1_760_000_001, seen=SEEN, records=env.records)
    assert E.gud_id(later) != E.gud_id(env)


def test_verify_rejects_tampering_and_never_raises():
    priv, pub = E.generate_key()
    _, other = E.generate_key()
    env = E.sign(_push(), priv)
    assert E.verify(env, pub)
    assert not E.verify(env, other)
    assert not E.verify(Envelope(env.headers, env.records), pub)
    tampered = Envelope(env.headers, env.records + (Record("delete", ("b",), b""),), env.sig)
    assert not E.verify(tampered, pub)
    assert not E.verify(env, None)
    assert not E.verify(env, "ed25519:!!!")
    assert not E.verify(env, "rsa:" + pub[8:])
    assert not E.verify(Envelope((("Bad", "x"),), (), env.sig), pub)
    assert not E.verify_bytes(pub, b"x", "ed25519:" + base64.b64encode(b"\0" * 64).decode())
    assert E.verify_bytes(pub, b"x", E.sign_bytes(priv, b"x"))
    assert E.public_of(priv) == pub


def test_ed25519_is_deterministic_rfc8032():
    # RFC 8032 section 7.1, test 1.
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    expected_pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    expected_sig = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert E.public_of(seed) == "ed25519:" + base64.b64encode(expected_pub).decode()
    assert E.sign_bytes(seed, b"") == "ed25519:" + base64.b64encode(expected_sig).decode()


def test_build_rejects_conflicts_and_sig():
    with pytest.raises(E.EnvelopeError):
        E.build("push", author="@a", headers={"Gud-Kind": "comment"})
    with pytest.raises(E.EnvelopeError):
        E.build("push", author="@a", headers={"Sig": "ed25519:x"})
    with pytest.raises(E.EnvelopeError):
        E.build("push", author="@a", headers={"Parent": "a\nb"})


def test_parse_seen_and_helpers():
    assert E.parse_seen("12:" + "f" * 64) == Seen(12, "f" * 64)
    for bad in ["012:" + "f" * 64, "1:" + "F" * 64, "1:abc", "x:" + "f" * 64, "", "1" + "f" * 64]:
        with pytest.raises(E.EnvelopeError):
            E.parse_seen(bad)
    assert E.now_sent(lambda: 1760000000.9) == 1760000000
    assert E.subject_tag("abcdef0123456789" + "0" * 48) == "[gud:abcdef012345]"
    assert E.sha256_hex(b"") == hashlib.sha256(b"").hexdigest()


def test_envelope_properties():
    env = _push()
    assert env.kind == "push" and env.author == "@alice"
    assert env.sent_ms == 1_760_000_000_000
    assert env.text == "intent"
    assert env.records_named("delete") == (Record("delete", ("a.txt",), b""),)
    assert Envelope((("Gud-Sent", "x"),), ()).sent_ms is None


# ---------------------------------------------------------------- jcs

def test_jcs():
    assert E.jcs({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'
    assert E.jcs({"€": 1, "\r": 2, "1": 3, "é": 4}) == '{"\\r":2,"1":3,"é":4,"€":1}'.encode()
    assert E.jcs("a \"\\\x01\x7f") == '"a \\"\\\\\\u0001\x7f"'.encode()
    # RFC 8785 sorts by UTF-16 code units: U+FB33 sorts after U+1F600 (surrogates D83D..).
    assert E.jcs({"דּ": 1, "\U0001f600": 2}) == '{"\U0001f600":2,"דּ":1}'.encode()
    for bad in [1.5, {1: 2}, 2 ** 53, object()]:
        with pytest.raises(ValueError):
            E.jcs(bad)


# ---------------------------------------------------------------- §7 kinds

def _env(kind, headers, records=(), author="@a", seen=SEEN):
    return E.build(kind, author=author, headers=headers, records=list(records), sent=1, seen=seen)


T = Record("text", (), b"why")
H = "c" * 64


@pytest.mark.parametrize("env", [
    _push(),
    _env("revert", {"Parent": H, "Revert": H, "Tree": H}),
    _env("object", {"Target": H}, [T, Record("evidence", ("t/x.py", H, "3"), b"abc")]),
    _env("object", {"Target": H}, [T, Record("evidence", ("t/x.py", H, "30"), b"")]),
    _env("approve", {"Target": H}),
    _env("hold", {"Target": H}, [T]),
    _env("comment", {"Target": H, "Escalate": "stuck", "Task": H}, [T]),
    _env("test", {"Scope": "stack", "Target": H, "Tree": H, "Result": "pass", "Stack": H + "," + H}),
    _env("test", {"Scope": "evidence", "Target": H, "Tree": H, "Result": "fail", "On": H, "Base-Result": "pass"}),
    _env("claim", {"Task": H, "Files": "src/**,docs/"}),
    _env("task", {"Title": "Do it", "Accept": "tests/test_a.py"}, [T, Record("accept", (), b"make\n")]),
    _env("subtask", {"Parent-Task": H, "Title": "Part", "After": H}),
    _env("no-change", {"Task": H, "Tip": H}, [T]),
    _env("checkpoint", {"Log": "5:" + H, "Link": H, "Tree": H, "Snapshot": H}),
    _env("join", {"Role": "agent", "Address": "x@y.org", "Key": "ed25519:" + base64.b64encode(b"k" * 32).decode()},
         seen=None),
    _env("notify", {"Event": H, "Event-Type": "task.done"}),
    E.build("blob", author=None, headers={"Blob": H, "Size": "5", "Chunk": "1/2", "Chunk-Size": "3"},
            records=[Record("data", (), b"de")]),
    _env("comment", {"Target": H}, [T, Record("future", (), b"ignored")]),
])
def test_validate_kind_accepts(env):
    assert E.validate_kind(env) is None


@pytest.mark.parametrize("env", [
    _env("push", {"Parent": H, "Tree": H}, [T]),                                    # zero ops
    _env("push", {"Parent": H, "Tree": H}, [Record("delete", ("a",), b""), T]),     # text not first
    _env("push", {"Parent": H, "Tree": H}, [Record("future", (), b"")]),            # unknown record
    _env("push", {"Parent": H[:12], "Tree": H}, [Record("delete", ("a",), b"")]),   # prefix id
    _env("push", {"Parent": H, "Tree": H, "Carry": "yes"}, [Record("delete", ("a",), b"")]),
    _env("push", {"Parent": H, "Tree": H}, [Record("put", ("a", "100644", H, "3"), b"ab")]),
    _env("push", {"Parent": H, "Tree": H}, [Record("put", ("a", "100600", H, "2"), b"ab")]),
    _env("push", {"Parent": H, "Tree": H}, [Record("put", ("a", "100644", H, "02"), b"ab")]),
    _env("push", {"Parent": H, "Tree": H}, [Record("mode", ("a", "120000"), b"")]),
    _env("push", {"Parent": H, "Tree": H}, [Record("delete", ("a",), b"x")]),
    _env("push", {"Parent": H, "Tree": H}, [Record("rename", ("a",), b"")]),
    _env("push", {"Parent": H, "Tree": H}, [Record("text", (), b"\xff"), Record("delete", ("a",), b"")]),
    _env("push", {"Parent": H}, [Record("delete", ("a",), b"")]),                   # no tree
    E.build("push", author="@a", headers={"Parent": H, "Tree": H}, records=[Record("delete", ("a",), b"")], sent=1),
    E.build("push", author="@a", headers={"Parent": H, "Tree": H}, records=[Record("delete", ("a",), b"")], seen=SEEN),
    E.build("push", author="A", headers={"Parent": H, "Tree": H}, records=[Record("delete", ("a",), b"")], sent=1, seen=SEEN),
    _env("revert", {"Parent": H, "Revert": H, "Tree": H}, [Record("delete", ("a",), b"")]),
    _env("object", {"Target": H}),
    _env("object", {"Target": H}, [Record("text", (), b"")]),
    _env("comment", {"Target": H}, [T, T]),
    _env("test", {"Scope": "stack", "Target": H, "Tree": H, "Result": "pass"}),
    _env("test", {"Scope": "tip", "Target": H, "Tree": H, "Result": "maybe"}),
    _env("test", {"Scope": "tip", "Target": H, "Tree": H, "Result": "pass"}, [Record("text", (), b"x" * 65537)]),
    _env("claim", {"Task": H, "Files": ",,"}),
    _env("claim", {"Task": H, "Files": "**", "Release": "no"}),
    _env("no-change", {"Task": H, "Tip": H}, [Record("text", (), b"two\nlines")]),
    _env("task", {"Title": "x"}, [Record("accept", (), b""), T]),
    _env("join", {"Role": "lead", "Address": "x@y.org", "Key": "ed25519:x"}, seen=None),
    _env("notify", {"Event": H, "Event-Type": "nope"}),
    E.build("blob", author=None, headers={"Blob": H, "Size": "5", "Chunk": "2/2", "Chunk-Size": "3"},
            records=[Record("data", (), b"de")]),
    E.build("blob", author=None, headers={"Blob": H, "Size": "5", "Chunk": "0/2", "Chunk-Size": "3"},
            records=[Record("data", (), b"de")]),
    E.build("blob", author=None, headers={"Blob": H, "Size": "5", "Chunk": "0/3", "Chunk-Size": "3"},
            records=[Record("data", (), b"abc")]),
    Envelope((("Gud-Kind", "nope"), ("Gud-Version", "1")), ()),
])
def test_validate_kind_rejects(env):
    assert E.validate_kind(env) is not None


# ---------------------------------------------------------------- Authentication-Results

def test_parse_authentication_results():
    value = ('mx.example.net (comment; with semicolon) 1;\r\n dkim=pass (good) header.d=a.org header.b="ab;c=";'
             ' spf=pass smtp.mailfrom=a.org; dmarc = pass (p=reject) header.from=A.org')
    parsed = E.parse_authentication_results(value)
    assert parsed.authserv_id == "mx.example.net"
    assert parsed.results[0] == ("dkim", "pass", (("header.d", "a.org"), ("header.b", "ab;c=")))
    assert parsed.results[1] == ("spf", "pass", (("smtp.mailfrom", "a.org"),))
    assert parsed.results[2] == ("dmarc", "pass", (("header.from", "A.org"),))
    assert E.parse_authentication_results("mx.example.net; none").results == ()
    assert E.parse_authentication_results("") is None
    assert E.parse_authentication_results("(only a comment)") is None


def test_dmarc_pass_uses_the_topmost_matching_header_only():
    ours = "mx.example.net; dmarc=pass header.from=a.org"
    forged_fail = "mx.example.net; dmarc=fail header.from=a.org"
    other = "evil.example; dmarc=pass header.from=a.org"
    assert E.dmarc_pass([ours], "mx.example.net", "a.org")
    assert E.dmarc_pass([ours], "MX.Example.NET", "A.ORG")
    assert E.dmarc_pass([other, ours, forged_fail], "mx.example.net", "a.org")
    assert not E.dmarc_pass([forged_fail, ours], "mx.example.net", "a.org")
    assert not E.dmarc_pass([other], "mx.example.net", "a.org")
    assert not E.dmarc_pass([ours], "mx.example.net", "b.org")
    assert not E.dmarc_pass([ours], "", "a.org")
    assert not E.dmarc_pass(["mx.example.net; dkim=pass header.d=a.org"], "mx.example.net", "a.org")


# ---------------------------------------------------------------- events

def test_event_ids():
    assert set(E.EVENT_KEYS) == set(EVENT_TYPES)
    expected = hashlib.sha256(b'{"key":["' + b"p" * 64 + b'"],"type":"linked"}').hexdigest()
    assert E.event_id("linked", ["p" * 64]) == expected
    ev = E.make_event("linked", 5, ["@b", "@a", "@b"], {"link": "p" * 64, "push": "q" * 64, "seq": 1})
    assert ev.id == expected and ev.concerns == ("@a", "@b") and ev.at == 5
    a = E.make_event("claim.overlap", 1, [], {"claims": ["y", "x"], "holders": [], "globs": []})
    b = E.make_event("claim.overlap", 2, [], {"claims": ["x", "y"], "holders": [], "globs": []})
    assert a.id == b.id == E.event_id("claim.overlap", ["x", "y"])
    w = E.make_event("window.closing", 9, [], {"candidate": "c", "closes_at": 100})
    assert w.id == E.event_id("window.closing", ["c", 100])
    t1 = E.make_event("escalated", 7, [], {"escalation": None, "task": "t", "reason": "stuck"})
    assert t1.id == E.event_id("escalated", ["t", "stuck", 7])
    f = E.make_event("host.fork", 1, [], {"message": None, "proof": {"a": "x", "b": "y"}})
    assert f.id == E.event_id("host.fork", [E.sha256_hex(E.jcs({"a": "x", "b": "y"}))])
    with pytest.raises(E.EnvelopeError):
        E.make_event("linked", 1, [], {})
    with pytest.raises(E.EnvelopeError):
        E.make_event("nope", 1, [], {})
