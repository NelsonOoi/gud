# Testing the gud MCP app

The MCP app is the live feed a chat host shows for one mailbox. It is not the agent server. `python -m gud.mcp_server` is the stdio server an agent uses to read, push, and rebase. `python -m gud.mcp_app` is the feed: three tools and one widget.

| Tool | What it does |
| --- | --- |
| `gud` | Snapshot of the tip, tasks, bugs, and recent mail. `focus` is a file path, a task id, or a message id. `since` is the cursor from the previous call. This tool carries the widget. |
| `gud_decide` | One human reply: `approve`, `object`, or `veto` on a push, `approve` or `reject` on a bug. |
| `gud_task` | Open a task. `@console` sends the signed task email. |

The widget is `ui://gud/feed.html`, MIME type `text/html;profile=mcp-app`. Claude renders that in the conversation. Codex CLI and Cursor call the same tools and show the text result. A host that does not implement MCP Apps still tests the server. It just does not draw the feed.

## 1. Build the widget and a mailbox

From the `gud` directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
npm install
npm run build:widget
```

`resources/dist/feed.html` must exist before you start the server. If it does not, the resource body is the sentence "gud feed is not built."

Use a mailbox that already has a genesis. A fresh home keeps this off any mailbox you already use:

```bash
export GUD_HOME="$PWD/.gud-home-mcp"
export GUD_MAILBOX=fake
export GUD_DOMAIN=gud.local
export GUD_REPO_ADDRESS=repo@gud.local
export GUD_MCP_TOKEN=dev-token   # omit this to leave writes open

.venv/bin/gud init --from demo_repo
```

`gud init` sends genesis and prints the message id. The feed's tip is that link. `GUD_MCP_TOKEN` is optional. When it is set, `gud` stays open and `gud_decide` / `gud_task` reject a missing or wrong token. The token may be the tool argument `token`, an `Authorization: Bearer` header, or an `X-Gud-Token` header.

## 2. Start the server

Stdio, for a host that launches the process itself:

```bash
.venv/bin/python -m gud.mcp_app --stdio
```

HTTP, for a host that connects to a URL. The process listens on `0.0.0.0:$PORT` (default `8000`) at `/mcp`. It exits if that port is taken. It does not move to another port.

```bash
PORT=8000 .venv/bin/python -m gud.mcp_app
```

The URL you give every host is `http://127.0.0.1:8000/mcp`. Leave this process running. Pass the same `GUD_HOME`, `GUD_MAILBOX`, and `GUD_MCP_TOKEN` you used for `gud init`.

After either start, a working server lists `gud`, `gud_decide`, and `gud_task`, and lists the resource `ui://gud/feed.html`. Calling `gud` with `focus` set to `auth.py` returns the tip and that file with blame.

## 3. Claude

Claude on the web and Claude Desktop render the widget. Claude Code calls the tools. The text fallback is the same feed, one line per event.

**Claude Desktop, local HTTP.** Current desktop builds still want a `command`. Point `mcp-remote` at the server you already started, and force HTTP so it does not probe SSE and drop the widget capability:

`~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "gud": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8000/mcp",
        "--allow-http",
        "--transport",
        "http-only"
      ]
    }
  }
}
```

Quit Claude with Cmd-Q and reopen it. Closing the window is not enough. Desktop caches the widget HTML. Rebuild the widget, restart `gud.mcp_app`, then Cmd-Q again.

**Claude Desktop, stdio.** Skip the HTTP process. Desktop starts the app itself:

```json
{
  "mcpServers": {
    "gud": {
      "command": "/absolute/path/to/gud/.venv/bin/python",
      "args": ["-m", "gud.mcp_app", "--stdio"],
      "env": {
        "GUD_HOME": "/absolute/path/to/gud/.gud-home-mcp",
        "GUD_MAILBOX": "fake",
        "GUD_DOMAIN": "gud.local",
        "GUD_REPO_ADDRESS": "repo@gud.local",
        "GUD_MCP_TOKEN": "dev-token"
      }
    }
  }
}
```

**Claude Code.** In the project, or via `claude mcp add`:

```json
{
  "mcpServers": {
    "gud": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

**claude.ai.** A custom connector needs a public HTTPS URL. `http://127.0.0.1:8000/mcp` is invisible to it. Tunnel the HTTP server, then add that `https://…/mcp` URL as a connector. The widget loads from a sandbox on `*.claudemcpcontent.com`. A policy that sets `disableNonessentialServices` blocks that origin and Claude shows the text feed instead.

In a new chat, ask Claude to call `gud` and show the feed. You should see the genesis tip, a file field, and a task form. Type `auth.py` and Read. The blame view is the tip of that file. Open a task from the form. With `GUD_MCP_TOKEN` set, the Open button fails until the write-token field is `dev-token`. The field is kept in the widget's session storage.

## 4. Codex

The CLI, the IDE extension, and the ChatGPT desktop app share `~/.codex/config.toml`. A project-scoped `.codex/config.toml` works in a trusted project. The CLI prints the text feed. It does not draw the iframe. The ChatGPT app renders an MCP App when that host build supports one.

Do not set `transport`. A `url` is the streamable HTTP server. `codex mcp add` can write the stdio form:

```bash
codex mcp add gud --env GUD_HOME=/absolute/path/to/gud/.gud-home-mcp --env GUD_MAILBOX=fake --env GUD_MCP_TOKEN=dev-token -- /absolute/path/to/gud/.venv/bin/python -m gud.mcp_app --stdio
```

Or edit the file. Stdio:

```toml
[mcp_servers.gud]
command = "/absolute/path/to/gud/.venv/bin/python"
args = ["-m", "gud.mcp_app", "--stdio"]
cwd = "/absolute/path/to/gud"
startup_timeout_sec = 20

[mcp_servers.gud.env]
GUD_HOME = "/absolute/path/to/gud/.gud-home-mcp"
GUD_MAILBOX = "fake"
GUD_DOMAIN = "gud.local"
GUD_REPO_ADDRESS = "repo@gud.local"
GUD_MCP_TOKEN = "dev-token"
```

HTTP, with the server from section 2 already running:

```toml
[mcp_servers.gud]
url = "http://127.0.0.1:8000/mcp"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

`codex mcp list` should show `gud`. In the TUI, `/mcp` lists it. Start a thread and ask Codex to call `gud` with `focus` `auth.py`. The reply should name the tip and the file. Ask it to call `gud_task` with the token. `gud status` in another shell, same `GUD_HOME`, should then list that task.

If Codex tries an OAuth login against this server, there is nothing to log in to. Leave `url` with no `bearer_token_env_var` and no `auth` override. The server accepts a connection with no credentials. Reads do not need the token.

## 5. Cursor

Cursor reads `.cursor/mcp.json` in the project and `~/.cursor/mcp.json` for every project. The project file wins on a name clash. Restart Cursor after editing, or reload MCP from Settings.

Stdio, so Cursor owns the process and you do not keep a terminal open:

```json
{
  "mcpServers": {
    "gud": {
      "type": "stdio",
      "command": "/absolute/path/to/gud/.venv/bin/python",
      "args": ["-m", "gud.mcp_app", "--stdio"],
      "env": {
        "GUD_HOME": "/absolute/path/to/gud/.gud-home-mcp",
        "GUD_MAILBOX": "fake",
        "GUD_DOMAIN": "gud.local",
        "GUD_REPO_ADDRESS": "repo@gud.local",
        "GUD_MCP_TOKEN": "dev-token"
      }
    }
  }
}
```

HTTP:

```json
{
  "mcpServers": {
    "gud": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer dev-token"
      }
    }
  }
}
```

The header satisfies `GUD_MCP_TOKEN` for `gud_decide` and `gud_task`. The widget's own token field is only needed when the host does not send that header.

In a new agent chat, ask Cursor to call `gud` and then `gud` again with `focus` set to `billing.py`. The tool result is the text feed and, when the call returns structured content, the file lines with owners. Cursor's MCP settings page should show the server connected and the three tools enabled. Treat a rendered iframe as a bonus of that Cursor build. The pass condition here is the tool result, not the widget.

## 6. What to check

Use the same mailbox for every host so the answers match.

- The server is connected and the three tools are listed.
- `gud` names the genesis tip (`@lead`, subject `[gud] genesis`) and a file count.
- `gud` with `focus` `auth.py` returns blame lines, not "Nothing matches".
- `gud` with `since` set to the previous cursor returns no older events.
- `gud_task` with the token opens a task. A second `gud` lists it. Without the token, and with `GUD_MCP_TOKEN` set, the tool errors with "token required".
- `gud_decide` on a real pending push id records the reply. On a made-up id it errors. It does not invent a mail.
- In Claude, the widget shows the same tip, a row opens the file or the thread, and the task form calls `gud_task`. After a widget edit, Cmd-Q Claude and restart the server.

`python -m gud.mcp_server` is the other server. Connecting a host to that one gives `read`, `push`, `rebase`, and `wait`, and no feed widget. For this guide the command is `gud.mcp_app`.
