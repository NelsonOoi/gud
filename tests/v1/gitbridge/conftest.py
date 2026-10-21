import shutil

import pytest

if shutil.which("git") is None:  # pragma: no cover
    pytest.skip("git is not installed", allow_module_level=True)


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch):
    """Keep the user's global/system git config out of the tests (and out of GitMirror)."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.autocrlf=false' 'core.symlinks=true'")
