"""The gud arm's tester process: ``gud.tester.Tester`` in a loop that survives errors.

    GUD_HOME=... GUD_MAILBOX=fake python gud_tester.py --handle @tester

Same tester the swarm uses (gud.build.LocalRunner: ``python -m pytest -q -x`` in a fresh temp dir per
push). ``Tester.run_forever`` stops on the first exception; here a failed step is logged and retried,
so one bad read cannot stall the whole run. SIGTERM stops it.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main() -> None:
    from gud import compose, config, sig
    from gud.build import make_runner
    from gud.mailbox import open_member_mailbox
    from gud.tester import Tester

    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", default="@tester")
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args()
    cfg = config.load()
    handle = args.handle if args.handle.startswith("@") else f"@{args.handle}"
    identity = compose.Identity(handle, cfg.address(handle), sig.load_private(cfg.key_path(handle)))
    mailbox = open_member_mailbox(cfg)
    tester = Tester(mailbox, identity, make_runner(cfg), cfg.window_seconds)
    stop = {"now": False}

    def _stop(*_):
        stop["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_seen = 0.0
    print(f"tester {handle} up", flush=True)
    while not stop["now"]:
        try:
            tester.step()
            fresh = mailbox.wait(recipient=identity.address, since=last_seen, timeout=args.poll)
            if fresh:
                last_seen = max(last_seen, max(item.received_at for item in fresh))
        except Exception:
            print("tester step failed:\n" + traceback.format_exc(), flush=True)
            time.sleep(1.0)
    tester.close()
    print("tester stopped", flush=True)


if __name__ == "__main__":
    main()
