# gud ui

The dashboard of a gud v1 host. `gud host serve` serves it at `/` on the host's own port and the page
reads the host's UI view API (`/api/v1/view/*`, `gud/host_view.py`, documented in
`docs/INTERFACES.md` under "gud/host_view.py"). Everything on it is derived from the host's
replay of its log: the chain with carried links and tip verdicts, proposals with their consent plan,
window and grace countdowns, holds, objections (reason or evidence, confirmed or refuted) and
negotiation rounds, escalations with their deadline and default, the red tip with `first_bad` and its
fix task, tasks with claims, leases and overlaps, members and join requests, blame with live and
expired ownership, the host log and its self-audit, and every §19 event live over SSE.

## Run

```bash
cd ui
npm ci
npm run build              # tsc + vite build into ui/dist
cd ..
gud host serve --dir <host dir> --ui-home <a human member's GUD_HOME>
# or: python -m gud.cli host serve ...
```

Open the URL it prints (default [http://127.0.0.1:8787](http://127.0.0.1:8787)). `--ui-home` (repeatable,
default: the serving GUD_HOME) names member homes whose keys the page may sign with. With a human's
key there the page can open tasks, approve, hold, object, veto, comment, accept tasks and admit join
requests as that human; those actions are loopback-only and are signed v1 messages logged like any
mail. Without one the page is read-only. `--no-ui` serves no page, `--ui-dist DIR` (or
`GUD_UI_DIST`) serves another build.

From loopback the view API needs no signature. From anywhere else every request is a signed member
request (§21), which a browser cannot make by itself, so remote viewers go through a signing proxy.

`npm run dev` serves [http://localhost:5173](http://localhost:5173) with hot reload and proxies `/api`
to a host on port 8787.

## Offline

[`?mock=1`](http://localhost:5173/?mock=1), or no host answering, shows an in-memory v1 story
(`src/mock/`) with a **mock data** badge: a red tip whose fix task is offered to `@api-agent`, a
carried push, a negotiation escalated after two rounds, an evidence objection the tester confirmed,
two claims overlapping on `app/auth.py`, a pending join request and genesis lines whose ownership
has expired. Over about 30 seconds an approval links the windowed push, the tester passes the new
tip (green again, the fix task is done) and a new push opens a window. Actions apply to the story as
`@nelson`.
