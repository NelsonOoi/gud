# gud demo

About four minutes. The screen is the gud dashboard of a live host while four reference agents build a small todo system together. Say the opening line, start the swarm, then point at the beats as they happen. Nothing on screen is staged: every panel is the host's own replay of its log.

## Opening line

"This repo is an email address. Four agents are working on it right now. Every push, claim, vote and test result you see is a signed email; the host only numbers them in a signed log. Any member can replay that mail and get exactly this screen. There are no branches and no merge queue: a change that touches nobody's lines just lands, and a change to someone else's lines gives them a window to object."

## Before the demo

Build the dashboard once and check the agents can authenticate:

```bash
cd ui && npm ci && npm run build && cd ..
claude auth status
```

### Option A: the eval harness (one command, read-only dashboard)

The harness sets up a host, a tester, four members and the task, and runs four reference agents against it:

```bash
.venv/bin/python evals/v1/run.py --arm gud-ref --spec todo-stack --agents 4 --budget-usd 1.2 --timeout 900 --keep-work
```

It prints `work root <dir>`. The host's ready line in `<dir>/logs/host.log` has the dashboard URL (`dashboard http://127.0.0.1:<port>/`). This dashboard is read-only, because the host has no human key to sign with. Use option B for beats 4 and 6.

### Option B: by hand (interactive dashboard)

Follow the README quickstart with four agents (`@a1` … `@a4`), `@tester` and `@nelson` (human), and seed the repo from `evals/v1/specs/todo-stack/seed/`. Then:

```bash
gud --home homes/host --handle @host host serve --dir hostdir --bind 127.0.0.1:8787 --ui-home homes/nelson
(cd tester-wc && gud --home ../homes/tester --handle @tester tester) &
for k in 1 2 3 4; do (cd a$k-wc && gud --home ../homes/a$k --handle @a$k agent --budget-usd 1) & done
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). The page can act as `@nelson` (from this machine only). Open the task from **Tasks → New task** with the text of `evals/v1/specs/todo-stack/spec.md` and the acceptance command `python -m pytest -q`, or with `gud task new`.

If the live run fails or the network is down, open the dashboard with `?mock=1` (or `cd ui && npm run dev`, then `/?mock=1`). It plays a scripted story with a **mock data** badge: a red tip with its fix task, a carried push, an escalation, a confirmed evidence objection and overlapping claims. Say that it is the mock.

## Beats

Each beat is one sentence while the panel is on screen. Skip 4 and 5 if the clock is short; do not skip 1, 2, 3 or 6.

1. **The task fans out without a lead.** In **Tasks**, one agent claims T1 and posts subtasks (library, CLI, API, docs), each with a file scope; the other agents claim them. Say: "Nobody assigned anything. A claim is an email with a lease, and splitting is just posting subtasks under a claim you hold."
2. **New files land immediately.** In **Chain**, under Proposals, a push that only adds files links with no window: nobody owns those lines. The chain grows; links that were pushed on an older tip show as carried. Say: "That push named an old parent. Another link landed first, touched different files, so gud carried it forward. Nobody rebased."
3. **Touching someone's lines opens a window.** When one agent edits a function another agent wrote (the library is the usual one), the proposal shows its consent plan: who owns the lines, the window and grace countdown, and who has approved. Say: "Ownership comes from blame. The owner gets a window to object, on the same thread. If they approve, it lands now; if they're silent, it lands when the window closes."
4. **A human can object or veto from here.** Click **Object** (or **Veto**) on an open proposal and give a reason. The author's agent is woken, answers with a revision or a comment, and the revision links. Say: "That button sent a signed email as @nelson. It's logged and replayed like any other message."
5. **A broken tip is bisected and handed back.** If a link breaks the tests, the tester's tip run turns the chain red: the header shows `first_bad`, its author, and a fix task offered to that author first. Pushes keep linking while red; the task can't be done until it's green. Say: "No one is blocked by a red build, and the agent who broke it is the one asked to fix it." (If it doesn't happen live, show it in `?mock=1`.)
6. **Done is derived, and everyone agrees.** When the subtasks are done and the acceptance command passes on the tip, T1 turns done. Then, in a terminal, `gud verify --from-genesis` as any member, and open **Host**: the log verifies and its self-audit is clean. Say: "Any member can throw away its cache and replay the mail from genesis. Same tip, same blame, same done. The host can withhold mail, but it can't rewrite history without its own signatures proving it."

## Numbers, if asked

From `evals/v1/` (real Claude Code agents, N=4, 2026-10-20; see the README's Evals section for the caveats):

- Every run, git or gud, passed every hidden acceptance test. The comparison is about coordination, not correctness.
- On this todo system, after the coordination fixes, both gud arms split the task into three subtasks in every run and four agents landed work. Headless Claude Code on gud finished in 78 s (58–98) against 466 s before the fixes, and git took 184 s (128–240) in the first grid.

**Do not claim**

- That gud beat git on quality: hidden-test pass rates were identical.
- Speed over real email: the evals ran on the local fake transport. The live Primitive smoke linked a new file 2.9 s after `gud push`; an edit to owned lines waits for its window (20 s plus grace in that run).
- That every provider works live: only Primitive has passed live; IMAP ran against GreenMail; JMAP, SES, Postmark and Gmail ran against mocks only.
