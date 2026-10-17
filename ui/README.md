# gud ui

Web UI for [gud](../README.md): the email chain is the repo. Pending proposals sit above the link timeline. Blame, tasks, members, and the live mail feed read the HTTP API in `../api_contract.md`.

## Run

```bash
cd ui
npm install
npm run dev
```

Open [http://localhost:5173/?mock=1](http://localhost:5173/?mock=1) to use the in-memory repo (no API required). The header shows a **mock data** badge.

Without `?mock=1`, the app calls `GET /api/state` on startup. If that fails, it falls back to the same mock and shows the badge. The dev server proxies `/api` to `http://localhost:8787`.

To point at another API:

```bash
VITE_GUD_API=http://localhost:8787 npm run dev
```

`VITE_GUD_API` defaults to `""`, so requests stay on the page origin and hit the proxy.

## Mock story

Repo `repo@gud.primitive.email`. Members include `@lead`, the service agents, `@sub-1`, `@tester`, `@console`, and `@nelson`. `@sub-2` was removed; their lines are orphaned at the tip.

Task **T3** (“Add per-user rate limiting to login and bill overage”) is done and the requester was notified. Over about 90 seconds the mock also delivers a new push, an approval, an objection, a passing test, a completed task, and a link landing.

## Check

```bash
npm run build
npx tsc --noEmit
```
