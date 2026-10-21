"""`InstaBoxRunner`: run the `.gud/test` command in an InstaBox Firecracker VM.

Ported from the v0 InstaBox runner (tag `v0-final`). The API
notes, in short (OpenAPI `https://api.instabox.sh/v1/openapi.json`):
`X-API-Key` or `Authorization: Bearer`; `POST /v1/machines` (202 + operation, `Idempotency-Key`
required), `GET /v1/operations/{id}?wait=25s`, `POST /v1/machines/{id}/exec` runs `bash -lc`
with `stdin` <= 1 MiB and `timeoutSeconds` <= 600 and returns `stdout`/`stderr`/`exitCode`/
`timedOut`/`truncated` (a timeout kills the process group, exit 124); `wake`/`start` resume a
machine; 5 machines per account (`429 quota_exceeded`).

Changes from the v0 runner, for v1 trees and policies:

- The tree is uploaded as a byte-exact tarball (`runners.base.tree_tarball`): binary contents,
  modes `100755`, and symlinks survive. It is base64'd into `exec` stdin in chunks under 1 MiB.
- The command is the `.gud/test` command (a shell string) plus test-path arguments, not a fixed
  pytest argv. `bootstrap` (default: install pytest if missing, as v0 did) runs once per VM.
- With an `image`, the command runs inside `docker run <image>` on the VM (the default
  `universal` template ships Docker); without one it runs on the template directly.
- `exec` caps a call at 600 s. A policy timeout above that cannot be honoured in one call, so a
  run that hits the 600 s cap raises `InstaBoxError` (the tester then signs nothing) instead of
  reporting a timeout the policy did not impose.

Credentials: `INSTABOX_API_KEY` / `api_key=`, else a CLI login at
`$XDG_CONFIG_HOME/instabox/credentials.json` (refreshed and rotated as in v0). Not exercised
here: the test suite runs it only when `INSTABOX_API_KEY` is set.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

import httpx

from gud.core.types import Mode

from .base import LOG_LIMIT, RunResult, RunnerUnavailable, shell_command, tail, tree_tarball

_STDIN_BUDGET = 900_000          # exec stdin is capped at 1 MiB
_EXEC_CAP_S = 600
_API_DEFAULT = "https://api.instabox.sh"
_WORKDIR = "/data/gud-work"
DEFAULT_BOOTSTRAP = (
    "if python3 -c 'import pytest' >/dev/null 2>&1; then exit 0; fi; "
    "if command -v uv >/dev/null 2>&1; then "
    "uv venv /data/gud-venv && uv pip install --python /data/gud-venv/bin/python pytest; "
    "else python3 -m pip install --user pytest; fi"
)


class InstaBoxError(RuntimeError):
    """An InstaBox API call failed after credentials were accepted."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"instabox {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Auth:
    mode: str   # "api_key" or "bearer"
    secret: str


class InstaBoxRunner:
    name = "instabox"

    def __init__(self, api_key: str | None = None, template: str = "universal", snapshot: str | None = None, *,
                 api_url: str | None = None, max_machines: int = 5, workdir: str = _WORKDIR,
                 after: str = "pool", bootstrap: str | None = DEFAULT_BOOTSTRAP) -> None:
        if after not in {"pool", "destroy"}:
            raise ValueError("after must be 'pool' or 'destroy'")
        if max_machines < 1:
            raise ValueError("max_machines must be at least 1")
        self.api_key = api_key
        self.template = template
        self.snapshot = snapshot
        self.api_url = (api_url or os.environ.get("INSTABOX_API_URL") or _API_DEFAULT).rstrip("/")
        self.max_machines = max_machines
        self.workdir = workdir
        self.after = after
        self.bootstrap = bootstrap
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._dead: set[str] = set()
        self._created: set[str] = set()
        self._prepared: set[str] = set()
        self._starting = 0
        self._closing = False
        self._client: httpx.Client | None = None
        self._auth_mode: str | None = None

    # -- Runner ------------------------------------------------------------

    def run(self, files: Mapping[str, tuple[Mode, bytes]], command: str, *, image: str | None,
            timeout_ms: int, extra_args: Sequence[str] = ()) -> RunResult:
        mid = self._acquire()
        failed = False
        try:
            return self._execute(mid, files, command, image, timeout_ms, extra_args)
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

    def test_command(self, command: str, image: str | None, extra_args: Sequence[str] = ()) -> str:
        """The bash line exec runs (public for tests)."""
        line = shell_command(command, extra_args)
        work = shlex.quote(self.workdir)
        if image:
            inner = shlex.quote(line)
            return (f"cd {work} && docker run --rm --network none -v {work}:/work -w /work "
                    f"{shlex.quote(image)} /bin/sh -c {inner}")
        return ('if [ -x /data/gud-venv/bin/python ]; then export PATH="/data/gud-venv/bin:$PATH"; fi; '
                f"cd {work} && {line}")

    # -- the run -----------------------------------------------------------

    def _execute(self, mid: str, files: Mapping[str, tuple[Mode, bytes]], command: str, image: str | None,
                 timeout_ms: int, extra_args: Sequence[str]) -> RunResult:
        started = time.monotonic()
        self._upload(mid, files)
        self._ensure_bootstrap(mid)
        wanted = max(1, -(-timeout_ms // 1000))
        result = self._exec(mid, self.test_command(command, image, extra_args), cwd=self.workdir,
                            timeout=min(wanted, _EXEC_CAP_S))
        out = ((result.get("stdout") or "") + (result.get("stderr") or "")).encode("utf-8", "replace")
        timed_out = bool(result.get("timedOut")) or result.get("exitCode") == 124
        if timed_out and wanted > _EXEC_CAP_S:
            raise InstaBoxError(504, "exec_cap", f"run hit the {_EXEC_CAP_S}s exec cap before the policy "
                                                 f"timeout of {timeout_ms // 1000}s")
        code = None if timed_out else result.get("exitCode")
        last = (f"\n[gud tester] timeout after {timeout_ms / 1000:g}s\n" if timed_out
                else f"\n[gud tester] exit {code}\n")
        if result.get("truncated"):
            last = "\n[instabox truncated output to 1 MiB]" + last
        log = tail(out, LOG_LIMIT - len(last.encode())) + last
        return RunResult(passed=(code == 0 and not timed_out), log=log, seconds=time.monotonic() - started,
                         runner=self.name, timed_out=timed_out, exit_code=code)

    def _ensure_bootstrap(self, mid: str) -> None:
        if not self.bootstrap:
            return
        with self._lock:
            ready = mid in self._prepared
        if ready:
            return
        self._checked(mid, self.bootstrap, cwd="/data/home", timeout=180)
        with self._lock:
            self._prepared.add(mid)

    def _upload(self, mid: str, files: Mapping[str, tuple[Mode, bytes]]) -> None:
        blob = base64.b64encode(tree_tarball(files)).decode("ascii")
        chunks = [blob[i:i + _STDIN_BUDGET] for i in range(0, len(blob), _STDIN_BUDGET)] or [""]
        work = shlex.quote(self.workdir)
        self._checked(mid, f"rm -rf {work} {work}.b64 {work}.tar.gz && mkdir -p {work} && : > {work}.b64",
                      timeout=60)
        for chunk in chunks:
            self._checked(mid, f"cat >> {work}.b64", stdin=chunk, timeout=60)
        self._checked(mid, f"base64 -d {work}.b64 > {work}.tar.gz && tar -xzf {work}.tar.gz -C {work} && "
                           f"rm -f {work}.b64 {work}.tar.gz", timeout=60)

    # -- credentials (as v0) -------------------------------------------------

    def _credentials_path(self) -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        return base / "instabox" / "credentials.json"

    def _load_cli_credentials(self) -> dict | None:
        try:
            data = json.loads(self._credentials_path().read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("accessToken"):
            return None
        server = str(data.get("server") or "").rstrip("/")
        if server and server != self.api_url:
            return None
        return data

    def _save_cli_credentials(self, creds: dict) -> None:
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
            raise RunnerUnavailable("InstaBox runner is unavailable: set INSTABOX_API_KEY, pass api_key=, or sign "
                                    "in with `instabox login`")
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
            raise RunnerUnavailable("InstaBox CLI session could not be refreshed; run `instabox login`")
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
            headers = {"Accept": "application/json", "User-Agent": "gud-tester-v1"}
            if auth.mode == "api_key":
                headers["X-API-Key"] = auth.secret
            else:
                headers["Authorization"] = f"Bearer {auth.secret}"
            self._client = httpx.Client(base_url=self.api_url, headers=headers, timeout=60.0)
        return self._client

    # -- HTTP (as v0) --------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: dict | None = None, params=None,
                 idempotent: bool = False, timeout: float | None = None, _retry_auth: bool = True):
        headers = {"Idempotency-Key": str(uuid.uuid4())} if idempotent else {}
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
            op = self._request("GET", f"/v1/operations/{op_id}", params={"wait": f"{wait}s"}, timeout=wait + 15)
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
        body: dict = {"cmd": cmd, "timeoutSeconds": max(1, min(_EXEC_CAP_S, int(timeout)))}
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

    # -- pool (as v0) ----------------------------------------------------------

    def _meta(self) -> dict[str, str]:
        return {"gud": "runner-v1", "gud-template": self.template or "-", "gud-snapshot": self.snapshot or "-"}

    def _matches(self, machine: dict) -> bool:
        meta = machine.get("metadata") or {}
        mine = self._meta()
        if any((meta.get(k) or "") != v for k, v in mine.items()):
            return False
        return machine.get("state") not in {"deleted", "deleting", "error"}

    def _list_pool(self) -> list[dict]:
        found: list[dict] = []
        cursor = None
        while True:
            params: list[tuple[str, str]] = [("limit", "100"), ("metadata", "gud:runner-v1")]
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
                    raise RunnerUnavailable(f"InstaBox machine cap reached ({self.max_machines})")
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
            orphan = mid if self._closing else None
            if orphan is None:
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
            "name": "gud-" + uuid.uuid4().hex[:12], "metadata": self._meta(), "timeoutSeconds": 3600,
            "onTimeout": "pause", "autoSuspendAfterSeconds": 0, "vault": False, "desiredState": "running",
        }
        if self.snapshot:
            body["snapshotId"] = self._snapshot_ref(self.snapshot)
        else:
            body["templateId"] = self.template or "universal"
        try:
            op = self._request("POST", "/v1/machines", json_body=body, idempotent=True)
        except InstaBoxError as exc:
            if exc.code == "quota_exceeded":
                raise RunnerUnavailable("InstaBox refused a new machine (max 5 per account)") from exc
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
    return {"code": str(err.get("code") or response.status_code), "message": str(err.get("message") or "")}
