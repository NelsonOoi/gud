"""The gud v1 arms' tester process: `gud.tester_v1.Tester` in a loop that survives errors.

    PYTHONPATH=<repo> python gud1_tester.py --home H --handle @tester --host local:<dir> --repo repo@gud.local \\
        --mail <fake mail root> --state <dir> [--extra-path <venv bin>] [--poll 2]

It follows the repo through `gud.wc.remote.RepoClient` (local host mode over the shared FakeMailbox
root) and signs `test` messages with the local runner (`python -m pytest -q -x` per the repo's
`.gud/test`, in a fresh temp dir per run). `Tester.run_forever` would stop on the first unexpected
exception; here a failed step is logged and retried, so one bad read cannot stall the run. SIGTERM
stops it.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main(argv: list[str] | None = None) -> int:
    from gud.tester_v1 import Tester, TesterIdentity, make_runner
    from gud.transport.fake import FakeMailbox
    from gud.wc.remote import RepoClient, load_key

    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--handle", default="@tester")
    ap.add_argument("--host", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--mail", required=True, help="the FakeMailbox root")
    ap.add_argument("--address", default=None)
    ap.add_argument("--state", required=True)
    ap.add_argument("--extra-path", action="append", default=[])
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args(argv)
    home = Path(args.home)
    handle = args.handle if args.handle.startswith("@") else "@" + args.handle
    key = load_key(home, handle)
    domain = args.repo.rpartition("@")[2]
    address = args.address or f"{handle.lstrip('@')}@{domain}"
    client = RepoClient(args.host, handle, key, send_via=FakeMailbox(Path(args.mail), address),
                        repo_address=args.repo, home=home)
    tester = Tester(client, make_runner("local", extra_path=list(args.extra_path)),
                    identity=TesterIdentity(handle, key), state_dir=Path(args.state))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(f"tester {handle} up", flush=True)
    steps = errors = 0
    while not stop.is_set():
        try:
            sent = tester.step()
            steps += 1
            if sent:
                print(f"{time.strftime('%H:%M:%S')} sent {len(sent)}: {', '.join(s[:12] for s in sent)}", flush=True)
        except Exception:
            errors += 1
            print(f"{time.strftime('%H:%M:%S')} tester step failed:\n{traceback.format_exc()}", flush=True)
            stop.wait(1.0)
        stop.wait(args.poll)
    print(f"tester stopped after {steps} steps, {errors} errors", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
