# gud evals

Each case in `cases/` is a tiny repo plus two or more agents. The same case runs against gud and against a git baseline so the comparison is the workflow, not the task.

## Local, no model spend

Scripted agents exercise the tools. A fake mailbox, a tester thread, and a short consent window (5s) stand in for the network.

```bash
.venv/bin/python evals/run_local.py --dry-run
.venv/bin/python evals/run_local.py --dry-run --case parallel-functions
.venv/bin/python evals/run_local.py --dry-run --backend git
```

Results land in `evals/results/<timestamp>.json` and a comparison table is printed. gud needs the core (`gud.replay`, `gud.chain`, `gud.rules`) importable; the git backend does not.

## Local, with a model

OpenRouter, model from `GUD_AGENT_MODEL` or `--model`. `OPENROUTER_KEY` is read from the environment or the repo `.env`.

```bash
.venv/bin/python evals/run_local.py --case parallel-functions
.venv/bin/python evals/run_local.py --model x-ai/grok-4-fast
```

Agents call the same tool methods the MCP servers register. With `ARMATURE_API_KEY` set, those calls are also shipped to Armature.

## Armature CI

1. In the [Armature dashboard](https://app.armature.tech), create one workflow per case.
2. Put each workflow id in `evals/armature_cases.json` (`workflow_id`). Placeholders are skipped.
3. Run:

```bash
.venv/bin/python evals/armature_ci.py
.venv/bin/python evals/armature_ci.py --case parallel-functions
```

This POSTs `https://app.armature.tech/api/armature/v1/workflows/<id>/runs` with `Authorization: Bearer $ARMATURE_API_KEY`, polls the run, then prints the `/evaluation` scorecard.

## Cases

| id | what it forces |
| --- | --- |
| `parallel-functions` | two agents edit different functions of one file at the same time |
| `same-function-yield` | two agents want the same function; one waits and does not overwrite |
| `protected-approval` | an explicit-protected interface change needs the owner's approval |
| `objection-revise` | a push is objected to and the author revises |
| `subtask-email` | a subtask assigned by email is found and landed with its task id |

## Servers

```bash
.venv/bin/python -m gud.mcp_server --handle @alice
.venv/bin/python -m gud.mcp_baseline --workdir /path/to/clone --handle @alice
```
