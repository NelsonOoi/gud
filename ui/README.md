# gud ui

Web UI for [gud](../README.md): the email chain is the repo. Pending proposals sit above the link timeline. Blame, tasks, members, and the live mail feed read the HTTP API in `../api_contract.md`.

## Run

Build the page once, then start the viewer. It serves `ui/dist` at [http://localhost:8787](http://localhost:8787). No `GUD_REPO_ADDRESS`.

```bash
cd ui
npm install
npm run build
cd ..
.venv/bin/python -m gud.api
```

`GUD_MAILBOX=fake` is the default, so the command above reads the local mailbox. A local mailbox with no genesis shows **no repos** in the sidebar. To read the Primitive account instead, stop the viewer and start it with the flag:

```bash
GUD_MAILBOX=primitive .venv/bin/python -m gud.api
```

That uses `PRIMITIVE_API_KEY` from `.env`. The page asks you to paste a token only when that variable is unset. The token is stored under `GUD_HOME` and reused on the next launch.

The sidebar lists repo addresses in that mailbox: a `[gud] genesis` whose local-part is `repo` or `repo-<id>`. Contributor addresses on the same mail are left out. Pick one. The next launch reopens it. If you have never picked one, the newest repo is selected.

`npm run dev` is only for working on the UI. It serves [http://localhost:5173](http://localhost:5173) with hot reload and proxies `/api` to the viewer on port 8787. Rebuild with `npm run build` when you want the page at port 8787 to include those edits.

[http://localhost:5173/?mock=1](http://localhost:5173/?mock=1) (or the same query on port 8787) shows the in-memory story and does not use a mailbox. The header shows a **mock data** badge. If the viewer is not running, the dev server falls back to that same story.

## Mock story

Repo `repo@gud.primitive.email`. Members include `@lead`, the service agents, `@sub-1`, `@tester`, `@console`, and `@nelson`. `@sub-2` was removed; their lines are orphaned at the tip.

Task **T3** (“Add per-user rate limiting to login and bill overage”) is done and the requester was notified. Over about 90 seconds the mock also delivers a new push, an approval, an objection, a passing test, a completed task, and a link landing.

Join requests sit on Members: `@claude-agent` (`claude-agent@acme.primitive.email`, sponsored by `@nelson`) is pending and valid, `@codex-agent` is already proposed and waiting on a human, and `@stranger` has an invalid signature. About 20 seconds in, `@gemini-agent` requests to join; about 25 seconds later that request lands and `@gemini-agent` appears on the roster.

## Check

```bash
npm run build
npx tsc --noEmit
```
