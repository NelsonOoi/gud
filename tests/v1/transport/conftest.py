from __future__ import annotations

import pytest

from . import greenmail


@pytest.fixture(scope="session")
def greenmail_server(tmp_path_factory):
    """One GreenMail for the session (skips when java or the jar is missing)."""
    gm = greenmail.start(tmp_path_factory.mktemp("greenmail"))
    yield gm
    gm.stop()
