"""Test runners. `LocalRunner` executes pytest on the host; `InstaBoxRunner` uses an InstaBox VM.

`make_runner` picks one from `Config.runner` (`local` or `instabox`).
"""

from __future__ import annotations

import base64
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .config import Config
from .types import RunResult, Tree
from .wire import tree_hash

# stdin on POST /v1/machines/{id}/exec is capped at 1 MiB; stay under it.
_STDIN_BUDGET = 900_000
_API_DEFAULT = "https://api.instabox.sh"
_WORKDIR = "/data/gud-work"
_BOOTSTRAP = (
    "if python3 -c 'import pytest' >/dev/null 2>&1; then exit 0; fi; "
    "if command -v uv >/dev/null 2>&1; then "
    "uv venv /data/gud-venv && uv pip install --python /data/gud-venv/bin/python pytest; "
    "else python3 -m pip install --user pytest; fi"
)


class RunnerUnavailable(RuntimeError):
    """InstaBox cannot be used: no credentials, or the account rejected them."""


class InstaBoxError(RuntimeError):
    """An InstaBox API call failed after credentials were accepted."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"instabox {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _check_tree_path(path: str) -> Path:
    rel = Path(path)
    if not path or path.startswith(("/", "\\")) or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe tree path: {path}")
    return rel


def _tree_tarball(tree: Tree) -> bytes:
    """Gzipped tar of `tree`, paths relative, mtime fixed so the bytes are stable."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(tree):
            _check_tree_path(path)
            payload = tree[path].encode("utf-8")
            info = tarfile.TarInfo(name=path)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("PYTEST_"):
            del env[key]
    return env


def _kill_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _materialize(root: Path, tree: Tree) -> None:
    base = root.resolve()
    for path, content in tree.items():
        dest = (base / _check_tree_path(path)).resolve()
        if not dest.is_relative_to(base):
            raise ValueError(f"unsafe tree path: {path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


class LocalRunner:
    """Run the suite in a fresh temp directory on this machine.

    The default command is pytest, with a leading `python` / `python3` replaced
    by `sys.executable` so the venv that launched gud is the one that runs.
    On timeout the process group is killed and `passed` is false; the log
    contains the word `timeout`.
    """

    def __init__(self, timeout: float = 120, command: tuple[str, ...] = ("python", "-m", "pytest", "-q", "-x")):
        self.timeout = timeout
        self.command = tuple(command)
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def _argv(self) -> list[str]:
        argv = [str(part) for part in self.command]
        if argv and Path(argv[0]).name in {"python", "python3"}:
            argv[0] = sys.executable
        return argv

    def run(self, tree: Tree) -> RunResult:
        started = time.monotonic()
        digest = tree_hash(tree)
        with tempfile.TemporaryDirectory(prefix="gud-run-") as tmp:
            root = Path(tmp)
            _materialize(root, tree)
            passed, log = self._execute(root)
        return RunResult(passed=passed, log=log, tree_hash=digest, seconds=time.monotonic() - started,
                         runner="local")

    def _execute(self, root: Path) -> tuple[bool, str]:
        proc = subprocess.Popen(
            self._argv(), cwd=root, env=_child_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        with self._lock:
            self._procs.add(proc)
        try:
            try:
                out, _ = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                _kill_process(proc)
                out, _ = proc.communicate()
                return False, (out or "") + "\ntimeout\n"
            return proc.returncode == 0, out or ""
        finally:
            with self._lock:
                self._procs.discard(proc)

    def close(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _kill_process(proc)


@dataclass(frozen=True)
class _Auth:
    mode: str   # "api_key" or "bearer"
    secret: str


class InstaBoxRunner:
    """Run the suite in an InstaBox Firecracker VM (4 vCPU / 8 GB, max 5 per account).

    Verified against published sources on 2026-10-17 (no live VM; this environment
    has no InstaBox key, so nothing below was executed against api.instabox.sh):

    - ``npx --yes instabox@latest --help`` from npm ``instabox@0.6.0``. That bin is a
      sign-in and installer wrapper. ``npx instabox@latest <command>`` delegates to the
      native ``instabox`` CLI; the wrapper's own ``--help`` does not list subcommands.
    - npm package ``lib/credentials.mjs`` and README: a CLI login is stored at
      ``$XDG_CONFIG_HOME/instabox/credentials.json`` or ``~/.config/instabox/credentials.json``
      with ``accessToken``, ``accessExpiresAt``, ``refreshToken``, ``refreshExpiresAt``.
      ``POST /v1/auth/refresh`` rotates the refresh token; the old one stops working.
    - OpenAPI ``https://api.instabox.sh/v1/openapi.json`` (``instabox API`` 1.0.0):
      ``X-API-Key`` or ``Authorization: Bearer``; ``POST /v1/machines`` (exactly one of
      ``templateId`` / ``snapshotId``, ``name`` required, ``metadata``, ``timeoutSeconds``,
      ``onTimeout``, ``autoSuspendAfterSeconds``, ``vault``) returns 202 and an operation;
      poll ``GET /v1/operations/{id}?wait=25s``. ``POST /v1/machines/{id}/exec`` runs
      ``bash -lc`` (``stdin`` ≤ 1 MiB, ``timeoutSeconds`` ≤ 600) and returns
      ``stdout`` / ``stderr`` / ``exitCode`` / ``timedOut`` / ``truncated``; a non-zero
      exit is still HTTP 200; timeout kills the process group and sets exit 124.
      ``Idempotency-Key`` is required on create, start, delete, and snapshot.
      ``POST /v1/machines/{id}/wake`` resumes an idle suspend. Account cap is 5 machines
      (``429 quota_exceeded``). There is no HTTP file-write route, only
      ``GET /v1/machines/{id}/files``.
    - Docs and the npm ``instabox`` skill: default template ``universal`` is "dev plus
      Docker, Python (uv), Go and Rust"; home is ``/data/home`` on the persistent
      ``/data`` disk; ``exec`` default timeout is 60s and the max is 600s; snapshot
      names are ``[a-z0-9-]`` (no underscore) while ids look like ``sn_...``.

    Assumed, because it was not run on a real box:

    - ``image`` is the API ``templateId`` (default ``universal``). ``snapshot`` is a
      snapshot id, or a name resolved with ``GET /v1/snapshots``.
    - The tree is uploaded as a gzipped tarball: base64 into ``exec`` stdin (chunked
      under 1 MiB) and extracted to ``/data/gud-work``. The SDK writes files as base64
      over the same command channel; this matches that, using a tarball.
    - ``universal`` has ``python3`` and ``uv`` but not pytest. The first use of a VM
      runs ``uv venv /data/gud-venv && uv pip install pytest``, or ``python3 -m pip``
      if ``uv`` is missing.
    - Warm pool: VMs tagged ``gud=runner`` plus the image/snapshot are reused and left
      running (``autoSuspendAfterSeconds: 0``, ``vault: false``, 1h ``timeoutSeconds``
      then pause). ``after="destroy"`` deletes the VM; ``after="snapshot"`` saves
      ``snapshot_name`` once (names must be unique on the account) and keeps the VM.
      ``close()`` deletes VMs this process created.
    - Exec concurrency defaults to 4 on the account (OpenAPI). Extra calls retry
      ``429 quota_exceeded``. Metadata values use ``-`` when snapshot/image is absent
      so we never send an empty label.
    """

    def __init__(self, api_key: str | None = None, image: str = "universal", snapshot: str | None = None, *,
                 api_url: str | None = None, timeout: float = 120,
                 command: tuple[str, ...] = ("python3", "-m", "pytest", "-q", "-x"),
                 max_machines: int = 5, workdir: str = _WORKDIR,
                 after: str = "pool", snapshot_name: str = "gud-pytest"):
        if after not in {"pool", "destroy", "snapshot"}:
            raise ValueError("after must be 'pool', 'destroy', or 'snapshot'")
        if max_machines < 1:
            raise ValueError("max_machines must be at least 1")
        self.api_key = api_key
        self.image = image
        self.snapshot = snapshot
        self.api_url = (api_url or os.environ.get("INSTABOX_API_URL") or _API_DEFAULT).rstrip("/")
        self.timeout = timeout
        self.command = tuple(command)
        self.max_machines = max_machines
        self.workdir = workdir
        self.after = after
        self.snapshot_name = snapshot_name
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._dead: set[str] = set()
        self._created: set[str] = set()
        self._prepared: set[str] = set()
        self._starting = 0
        self._closing = False
        self._checkpoint_done = False
        self._client: httpx.Client | None = None
        self._auth_mode: str | None = None

    def run(self, tree: Tree) -> RunResult:
        digest = tree_hash(tree)
        mid = self._acquire()
        failed = False
        try:
            result = self._execute(mid, tree, digest)
            if self.after == "snapshot" and result.passed:
                self._checkpoint(mid)
            return result
        except Exception:
            failed = True
            raise
        finally:
            self._release(mid, destroy=failed or self.after == "destroy")

    def close(self) -> None:
        with self._lock:
            self._closing = True
            ids = list(self._created)
            self._created.clear()
            self._busy.clear()
        for mid in ids:
            self._delete_quiet(mid)
        if self._client is not None:
            self._client.close()
            self._client = None

    def save_snapshot(self, name: str, machine_id: str) -> str:
        """Save a snapshot of `machine_id`. Returns the snapshot id. Polls until it is ready."""
        snap = self._request(
            "POST", f"/v1/machines/{machine_id}/snapshots",
            json_body={"name": name, "description": "gud pytest base"}, idempotent=True,
        )
        sid = snap["id"]
        deadline = time.monotonic() + 180
        while snap.get("status") == "pending":
            if time.monotonic() >= deadline:
                raise InstaBoxError(504, "snapshot_timeout", f"snapshot {sid} stayed pending")
            time.sleep(2)
            snap = self._request("GET", f"/v1/snapshots/{sid}")
        if snap.get("status") == "failed":
            raise InstaBoxError(500, "snapshot_failed", str(snap.get("error") or "snapshot failed"))
        return sid

    # -- credentials -------------------------------------------------------

    def _credentials_path(self) -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        return base / "instabox" / "credentials.json"

    def _load_cli_credentials(self) -> dict | None:
        path = self._credentials_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("accessToken"):
            return None
        server = str(data.get("server") or "").rstrip("/")
        if server and server != self.api_url:
            return None
        return data

    def _save_cli_credentials(self, creds: dict) -> None:
        """Atomic replace. Refresh rotates the token, so the file must move with it."""
        path = self._credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "server": creds.get("server") or self.api_url,
            "accessToken": creds["accessToken"],
            "accessExpiresAt": creds.get("accessExpiresAt") or "",
            "refreshToken": creds.get("refreshToken") or "",
            "refreshExpiresAt": creds.get("refreshExpiresAt") or "",
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _resolve_auth(self) -> _Auth:
        key = (self.api_key or "").strip() or (os.environ.get("INSTABOX_API_KEY") or "").strip()
        if key:
            return _Auth("api_key", key)
        creds = self._load_cli_credentials()
        if not creds:
            raise RunnerUnavailable(
                "InstaBox runner is unavailable: set INSTABOX_API_KEY, pass api_key=, "
                "or sign in with `instabox login` "
                "(~/.config/instabox/credentials.json). InstaBox is an invite-only beta."
            )
        if _token_stale(str(creds.get("accessExpiresAt") or "")):
            creds = self._refresh_tokens(str(creds.get("refreshToken") or ""))
            creds["server"] = self.api_url
            self._save_cli_credentials(creds)
        return _Auth("bearer", creds["accessToken"])

    def _refresh_tokens(self, refresh_token: str) -> dict:
        if not refresh_token:
            raise RunnerUnavailable("InstaBox CLI session has no refresh token; run `instabox login`")
        try:
            response = httpx.post(f"{self.api_url}/v1/auth/refresh", json={"refreshToken": refresh_token},
                                  timeout=30.0)
        except httpx.HTTPError as exc:
            raise RunnerUnavailable(f"InstaBox token refresh failed: {exc}") from exc
        if response.status_code != 200:
            raise RunnerUnavailable(
                "InstaBox CLI session could not be refreshed; run `instabox login` or set INSTABOX_API_KEY"
            )
        data = response.json()
        if not data.get("accessToken"):
            raise RunnerUnavailable("InstaBox token refresh returned no access token")
        return data

    def _force_refresh(self) -> None:
        creds = self._load_cli_credentials() or {}
        fresh = self._refresh_tokens(str(creds.get("refreshToken") or ""))
        fresh["server"] = creds.get("server") or self.api_url
        self._save_cli_credentials(fresh)
        if self._client is not None:
            self._client.close()
            self._client = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            auth = self._resolve_auth()
            self._auth_mode = auth.mode
            headers = {"Accept": "application/json", "User-Agent": "gud-tester"}
            if auth.mode == "api_key":
                headers["X-API-Key"] = auth.secret
            else:
                headers["Authorization"] = f"Bearer {auth.secret}"
            self._client = httpx.Client(base_url=self.api_url, headers=headers, timeout=60.0)
        return self._client

    # -- HTTP --------------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: dict | None = None, params=None,
                 idempotent: bool = False, timeout: float | None = None, _retry_auth: bool = True):
        headers = {}
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        try:
            response = self._http().request(method, path, json=json_body, params=params, headers=headers,
                                            timeout=timeout)
        except RunnerUnavailable:
            raise
        except httpx.HTTPError as exc:
            raise InstaBoxError(0, "transport", str(exc)) from exc
        if response.status_code == 401 and _retry_auth and self._auth_mode == "bearer":
            self._force_refresh()
            return self._request(method, path, json_body=json_body, params=params, idempotent=idempotent,
                                 timeout=timeout, _retry_auth=False)
        if response.status_code >= 400:
            err = _error_body(response)
            if response.status_code in {401, 403}:
                raise RunnerUnavailable(f"InstaBox rejected credentials ({err['code']}): {err['message']}")
            raise InstaBoxError(response.status_code, err["code"], err["message"])
        if not response.content:
            return {}
        return response.json()

    def _wait_op(self, op_id: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstaBoxError(504, "operation_timeout", f"operation {op_id} did not finish")
            wait = max(1, min(25, int(remaining)))
            op = self._request("GET", f"/v1/operations/{op_id}", params={"wait": f"{wait}s"},
                               timeout=wait + 15)
            state = op.get("state")
            if state == "succeeded":
                return op
            if state == "failed":
                err = op.get("error") or {}
                code = err.get("code") if isinstance(err, dict) else "failed"
                message = err.get("message") if isinstance(err, dict) else "operation failed"
                if code == "quota_exceeded":
                    raise RunnerUnavailable(message or "InstaBox quota exceeded")
                raise InstaBoxError(500, code or "failed", message or f"operation {op_id} failed")

    def _exec(self, mid: str, cmd: str, *, cwd: str | None = None, stdin: str | None = None,
              timeout: float = 60) -> dict:
        body: dict = {"cmd": cmd, "timeoutSeconds": max(1, min(600, int(timeout)))}
        if cwd:
            body["cwd"] = cwd
        if stdin is not None:
            if len(stdin) > 1_048_576:
                raise ValueError("instabox stdin exceeds 1 MiB")
            body["stdin"] = stdin
        delay = 0.5
        for attempt in range(5):
            try:
                return self._request("POST", f"/v1/machines/{mid}/exec", json_body=body,
                                     timeout=body["timeoutSeconds"] + 30)
            except InstaBoxError as exc:
                if exc.code == "machine_not_running" and attempt == 0:
                    self._ensure_running(mid)
                    continue
                if exc.code != "quota_exceeded" or attempt == 4:
                    raise
                time.sleep(delay)
                delay *= 2
        raise InstaBoxError(429, "quota_exceeded", "exec stayed over the concurrency cap")

    def _checked(self, mid: str, cmd: str, **kwargs) -> dict:
        result = self._exec(mid, cmd, **kwargs)
        if result.get("timedOut") or result.get("exitCode") not in (0, None):
            detail = ((result.get("stderr") or "") + (result.get("stdout") or ""))[-2000:]
            raise InstaBoxError(int(result.get("exitCode") or 1), "exec_failed", detail or cmd)
        return result

    # -- pool --------------------------------------------------------------

    def _meta(self) -> dict[str, str]:
        return {"gud": "runner", "gud-image": self.image or "-", "gud-snapshot": self.snapshot or "-"}

    def _matches(self, machine: dict) -> bool:
        meta = machine.get("metadata") or {}
        if meta.get("gud") != "runner":
            return False
        if (meta.get("gud-image") or "") != (self.image or "-"):
            return False
        if (meta.get("gud-snapshot") or "") != (self.snapshot or "-"):
            return False
        return machine.get("state") not in {"deleted", "deleting", "error"}

    def _list_pool(self) -> list[dict]:
        found: list[dict] = []
        cursor = None
        while True:
            params: list[tuple[str, str]] = [("limit", "100"), ("metadata", "gud:runner")]
            if cursor:
                params.append(("cursor", cursor))
            page = self._request("GET", "/v1/machines", params=params)
            found.extend(page.get("machines") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                return found

    def _acquire(self) -> str:
        machines = self._list_pool()
        with self._lock:
            if self._closing:
                raise RunnerUnavailable("InstaBox runner is closing")
            chosen = None
            for machine in machines:
                mid = machine.get("id") or ""
                if not mid or mid in self._busy or mid in self._dead or not self._matches(machine):
                    continue
                self._busy.add(mid)
                chosen = mid
                break
            if chosen is None:
                if len(self._busy) + self._starting >= self.max_machines:
                    raise RunnerUnavailable(
                        f"InstaBox machine cap reached ({self.max_machines}). "
                        "Delete one with `instabox rm`, or wait for a warm machine to free."
                    )
                self._starting += 1
        if chosen is not None:
            try:
                self._ensure_running(chosen)
            except Exception:
                self._release(chosen, destroy=True)
                raise
            return chosen
        try:
            mid = self._create_and_boot()
        except Exception:
            with self._lock:
                self._starting -= 1
            raise
        with self._lock:
            self._starting -= 1
            if self._closing:
                orphan = mid
            else:
                orphan = None
                self._busy.add(mid)
                self._created.add(mid)
        if orphan is not None:
            self._delete_quiet(orphan)
            raise RunnerUnavailable("InstaBox runner is closing")
        return mid

    def _release(self, mid: str, *, destroy: bool) -> None:
        with self._lock:
            self._busy.discard(mid)
            if destroy:
                self._created.discard(mid)
                self._prepared.discard(mid)
                self._dead.add(mid)
        if destroy:
            self._delete_quiet(mid)

    def _create_and_boot(self) -> str:
        body: dict = {
            "name": "gud-" + uuid.uuid4().hex[:12],
            "metadata": self._meta(),
            "timeoutSeconds": 3600,
            "onTimeout": "pause",
            "autoSuspendAfterSeconds": 0,
            "vault": False,
            "desiredState": "running",
        }
        if self.snapshot:
            body["snapshotId"] = self._snapshot_ref(self.snapshot)
        else:
            body["templateId"] = self.image or "universal"
        try:
            op = self._request("POST", "/v1/machines", json_body=body, idempotent=True)
        except InstaBoxError as exc:
            if exc.code == "quota_exceeded":
                raise RunnerUnavailable(
                    "InstaBox refused a new machine (max 5 per account). "
                    "Remove one with `instabox rm`."
                ) from exc
            raise
        mid = op.get("machineId")
        try:
            done = self._wait_op(op["id"], timeout=180)
            mid = done.get("machineId") or mid
            if not mid:
                raise InstaBoxError(500, "create_failed", "create operation returned no machine id")
            self._ensure_running(mid)
            return mid
        except Exception:
            if mid:
                self._delete_quiet(mid)
            raise

    def _snapshot_ref(self, ref: str) -> str:
        # Snapshot names cannot contain "_"; ids are `sn_...` or legacy `im_...`.
        if "_" in ref:
            return ref
        data = self._request("GET", "/v1/snapshots")
        for snap in data.get("snapshots") or []:
            if snap.get("name") == ref or snap.get("id") == ref:
                return snap["id"]
        raise RunnerUnavailable(f"no InstaBox snapshot named {ref!r}")

    def _ensure_running(self, mid: str) -> None:
        deadline = time.monotonic() + 180
        nudged = False
        while time.monotonic() < deadline:
            machine = self._request("GET", f"/v1/machines/{mid}")
            state = machine.get("state")
            runtime = machine.get("runtime") or {}
            if state == "running" and runtime.get("ready", True):
                return
            if not nudged and state in {"suspended", "suspending"}:
                self._nudge(f"/v1/machines/{mid}/wake", idempotent=False)
                nudged = True
            elif not nudged and state == "stopped":
                self._nudge(f"/v1/machines/{mid}/start", idempotent=True)
                nudged = True
            elif state in {"deleted", "deleting", "error"}:
                raise InstaBoxError(409, "machine_unavailable", f"machine {mid} is {state}")
            time.sleep(1)
        raise InstaBoxError(504, "boot_timeout", f"machine {mid} did not reach running")

    def _nudge(self, path: str, *, idempotent: bool) -> None:
        try:
            result = self._request("POST", path, idempotent=idempotent)
        except InstaBoxError as exc:
            if exc.code == "operation_conflict":
                return
            raise
        if isinstance(result, dict) and result.get("type") in {"start", "resume", "create"} \
                and result.get("state") in {"queued", "running"}:
            self._wait_op(result["id"], timeout=180)

    def _delete_quiet(self, mid: str) -> None:
        try:
            op = self._request("DELETE", f"/v1/machines/{mid}", idempotent=True)
        except (InstaBoxError, RunnerUnavailable):
            return
        if isinstance(op, dict) and op.get("type") == "delete" and op.get("state") in {"queued", "running"}:
            try:
                self._wait_op(op["id"], timeout=120)
            except (InstaBoxError, RunnerUnavailable):
                return

    def _checkpoint(self, mid: str) -> None:
        if self._checkpoint_done:
            return
        self.save_snapshot(self.snapshot_name, mid)
        self._checkpoint_done = True

    # -- the suite ---------------------------------------------------------

    def _execute(self, mid: str, tree: Tree, digest: str) -> RunResult:
        started = time.monotonic()
        self._upload(mid, tree)
        self._ensure_pytest(mid)
        result = self._exec(mid, self._pytest_cmd(), cwd=self.workdir, timeout=self.timeout)
        log = (result.get("stdout") or "") + (result.get("stderr") or "")
        timed_out = bool(result.get("timedOut")) or result.get("exitCode") == 124
        if timed_out and "timeout" not in log.lower():
            log += "\ntimeout\n"
        if result.get("truncated"):
            log += "\n[instabox truncated output to 1 MiB]\n"
        passed = result.get("exitCode") == 0 and not timed_out
        return RunResult(passed=passed, log=log, tree_hash=digest, seconds=time.monotonic() - started,
                         runner="instabox")

    def _ensure_pytest(self, mid: str) -> None:
        with self._lock:
            ready = mid in self._prepared
        if ready:
            return
        self._checked(mid, _BOOTSTRAP, cwd="/data/home", timeout=180)
        with self._lock:
            self._prepared.add(mid)

    def _pytest_cmd(self) -> str:
        argv = " ".join(shlex.quote(part) for part in self.command)
        work = shlex.quote(self.workdir)
        return (
            'if [ -x /data/gud-venv/bin/python ]; then export PATH="/data/gud-venv/bin:$PATH"; fi; '
            f"cd {work} && {argv}"
        )

    def _upload(self, mid: str, tree: Tree) -> None:
        blob = base64.b64encode(_tree_tarball(tree)).decode("ascii")
        chunks = [blob[i:i + _STDIN_BUDGET] for i in range(0, len(blob), _STDIN_BUDGET)] or [""]
        self._checked(
            mid,
            "rm -rf /data/gud-work /data/gud-work.b64 /data/gud-work.tar.gz && "
            "mkdir -p /data/gud-work && : > /data/gud-work.b64",
            timeout=60,
        )
        for chunk in chunks:
            self._checked(mid, "cat >> /data/gud-work.b64", stdin=chunk, timeout=60)
        self._checked(
            mid,
            "base64 -d /data/gud-work.b64 > /data/gud-work.tar.gz && "
            "tar -xzf /data/gud-work.tar.gz -C /data/gud-work && "
            "rm -f /data/gud-work.b64 /data/gud-work.tar.gz",
            timeout=60,
        )


def _token_stale(expires_at: str) -> bool:
    text = (expires_at or "").strip()
    if not text:
        return True
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        exp = datetime.fromisoformat(text)
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= datetime.now(timezone.utc) + timedelta(seconds=60)


def _error_body(response: httpx.Response) -> dict[str, str]:
    try:
        body = response.json()
    except Exception:
        return {"code": str(response.status_code), "message": response.text[:500]}
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return {"code": str(response.status_code), "message": response.text[:500]}
    return {
        "code": str(err.get("code") or response.status_code),
        "message": str(err.get("message") or ""),
    }


class RunnerPool:
    """Thread pool over a `Runner`. Default width is 5, InstaBox's machine cap."""

    def __init__(self, runner, max_concurrent: int = 5):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.runner = runner
        self.max_concurrent = max_concurrent
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="gud-run")
        self._closed = False

    def submit(self, tree: Tree) -> Future[RunResult]:
        return self._executor.submit(self.runner.run, tree)

    def shutdown(self, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def close(self) -> None:
        self.shutdown(wait=False)


def make_runner(cfg: Config):
    """`local` -> `LocalRunner`. `instabox` -> `InstaBoxRunner` using `cfg.instabox_api_key`."""
    if cfg.runner == "local":
        return LocalRunner()
    if cfg.runner == "instabox":
        return InstaBoxRunner(api_key=cfg.instabox_api_key)
    raise ValueError(f"unknown runner {cfg.runner!r} (expected 'local' or 'instabox')")
