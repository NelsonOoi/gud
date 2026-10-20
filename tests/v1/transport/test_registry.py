from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gud.transport import registry
from gud.transport.base import TransportError
from gud.transport.fake import FakeMailbox
from gud.transport.primitive import PrimitiveMailbox


def test_names_and_lazy_errors(monkeypatch) -> None:
    assert registry.names()[:7] == ("fake", "imap_smtp", "jmap", "primitive", "ses", "postmark", "gmail")
    for name in ("jmap", "ses", "postmark", "gmail"):   # every built-in is implemented and checks its config
        with pytest.raises(TransportError, match="needs"):
            registry.open_mailbox(name, {})
    monkeypatch.setattr(registry, "BUILTINS", registry.BUILTINS + ("future_adapter",))
    with pytest.raises(TransportError, match="not implemented"):
        registry.open_mailbox("future_adapter", {})
    with pytest.raises(TransportError, match="unknown"):
        registry.open_mailbox("carrier_pigeon", {})


def test_register_custom(tmp_path: Path) -> None:
    registry.register("custom_test", lambda cfg: FakeMailbox(tmp_path, cfg["address"]))
    assert "custom_test" in registry.names()
    assert registry.open_mailbox("custom_test", {"address": "a@b.c"}).address == "a@b.c"
    with pytest.raises(ValueError):
        registry.register("Bad-Name", lambda cfg: None)


def test_from_env(tmp_path: Path) -> None:
    box = registry.from_env({"GUD_TRANSPORT": "fake", "GUD_FAKE_ROOT": str(tmp_path),
                             "GUD_FAKE_ADDRESS": "repo@gud.test", "GUD_PRIMITIVE_API_KEY": "ignored"})
    assert isinstance(box, FakeMailbox) and box.address == "repo@gud.test"
    prim = registry.from_env({"GUD_TRANSPORT": "primitive", "GUD_PRIMITIVE_API_KEY": "k",
                              "GUD_PRIMITIVE_ADDRESS": "r@x.org"})
    assert isinstance(prim, PrimitiveMailbox) and prim.api_key == "k"
    with pytest.raises(TransportError):
        registry.from_env({})


def test_member_config(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "members" / "@alice" / "transport.toml"
    path.parent.mkdir(parents=True)
    path.write_text('transport = "imap_smtp"\nimap_host = "imap.example.org"\nimap_port = 993\n'
                    'password_env = "ALICE_PW"\n[imap_smtp]\nusername = "alice@example.org"\n'
                    'folders = ["INBOX", "Spam"]\n')
    monkeypatch.setenv("ALICE_PW", "s3cret")
    name, cfg = registry.member_config(tmp_path, "@alice")
    assert name == "imap_smtp"
    assert cfg == {"imap_host": "imap.example.org", "imap_port": "993", "password": "s3cret",
                   "username": "alice@example.org", "folders": "INBOX,Spam"}
    monkeypatch.delenv("ALICE_PW")
    with pytest.raises(TransportError):
        registry.member_config(tmp_path, "@alice")
    with pytest.raises(TransportError):
        registry.member_config(tmp_path, "@nobody")


def test_v1_transport_does_not_import_v0() -> None:
    code = ("import sys, gud.transport.registry, gud.transport.fake, gud.transport.imap_smtp, "
            "gud.transport.primitive, gud.transport.auth, gud.transport.mime, gud.transport.jmap, "
            "gud.transport.ses, gud.transport.postmark, gud.transport.gmail, gud.transport.spool\n"
            "v0 = {'gud.types','gud.wire','gud.replay','gud.mail_primitive','gud.mail_fake','gud.webhook',"
            "'gud.mail_hosted','gud.mail_remote','gud.mailbox','gud.config'}\n"
            "bad = sorted(v0 & set(sys.modules)); assert not bad, bad\n"
            "assert not any(m.startswith(('gud.events', 'gud.wc')) for m in sys.modules)")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parents[3])
