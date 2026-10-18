# gud

Source control for agent swarms, where the email chain is the repo. Every push is an email carrying a signed diff; blame goes to the agent that pushed; consent is negotiated on the thread; there are no branches and no server-side merge.

- `SPEC.md`: wire format, link rules, consent.
- `api_contract.md`: Python core API and HTTP API.
- `docs/mcp-app.md`: run the live-feed MCP app in Claude, Codex, and Cursor.

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pytest -q
```

Runs fully local with `GUD_MAILBOX=fake` and `GUD_RUNNER=local` (the defaults). Set `PRIMITIVE_API_KEY`, `INSTABOX_API_KEY` and `ARMATURE_API_KEY` in `.env` to use the real services.
