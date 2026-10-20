"""End to end over IMAP + SMTP (a local GreenMail): the same scenario as test_fake.py, with every
member sending by SMTP submission and the host's intake woken by IMAP IDLE (skipped when java or
the GreenMail jar is missing)."""

from __future__ import annotations

import imaplib
import json
from pathlib import Path

import pytest

from tests.v1.e2e.scenario import Scenario, Transport
from tests.v1.transport import greenmail


@pytest.fixture(scope="module")
def gm(tmp_path_factory):
    """A GreenMail of its own, so the repo mailbox holds only this scenario's mail."""
    server = greenmail.start(tmp_path_factory.mktemp("greenmail-e2e"))
    yield server
    server.stop()


def imap_transport(gm) -> Transport:
    def settings(user: str) -> dict[str, str]:
        return {"imap_host": gm.host, "imap_port": str(gm.imap_port), "smtp_host": gm.host,
                "smtp_port": str(gm.smtp_port), "username": user, "password": greenmail.PASSWORD,
                "imap_tls": "none", "smtp_tls": "none", "folders": "INBOX", "idle_poll": "5"}

    def sent() -> int:
        conn = imaplib.IMAP4(gm.host, gm.imap_port)
        conn.login("repo", greenmail.PASSWORD)
        typ, data = conn.select("INBOX", readonly=True)
        conn.logout()
        return int(data[0]) if typ == "OK" else 0

    return Transport(name="imap_smtp", repo=gm.address("repo"), address=gm.address, settings=settings,
                     window="12s", grace="4s", slow=2.0, count_sent=sent, env={"GUD_WAIT": "30"})


def test_e2e_greenmail(gm, tmp_path: Path, capsys):
    t = imap_transport(gm)
    sc = Scenario(tmp_path, t)
    try:
        result = sc.run_all()
    finally:
        sc.close()
    result["repo_mail"] = t.count_sent() if t.count_sent else None
    with capsys.disabled():
        print("\ne2e imap_smtp (GreenMail, IDLE intake):", json.dumps(result, sort_keys=True))
