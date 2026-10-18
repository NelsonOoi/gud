import { App, applyDocumentTheme, applyHostStyleVariables, type McpUiHostContext } from "@modelcontextprotocol/ext-apps";
import "./style.css";

type FeedEvent = {
  id: string;
  kind: string;
  time: number;
  actor: string;
  task?: string | null;
  text: string;
  target?: string | null;
  bug?: string | null;
  added?: number;
  removed?: number;
};

type Subtask = { id: string; title: string; assignee: string; status: string };
type Task = { id: string; title: string; status: string; subtasks: Subtask[]; requester?: string };
type Bug = { id: string; subject: string; status: string; task_id?: string | null };
type Need = { id: string; title: string; kind: string; verbs: string[]; status?: string };
type Tip = { seq: number; id: string; author: string; subject: string; state: string } | null;
type Line = { n: number; text: string; owner: string };
type Mail = { id: string; from: string; subject: string; body: string; kind: string | null; received_at: number };
type Focus =
  | { kind: "file"; path: string; lines: Line[] }
  | { kind: "thread"; id: string; diff: string; emails: Mail[] }
  | { kind: "missing"; id: string; text?: string; path?: string };

type Snapshot = {
  delta?: boolean;
  tip?: Tip;
  tasks?: Task[];
  bugs?: Bug[];
  events?: FeedEvent[];
  needs_you?: Need[];
  cursor?: string;
  focus?: Focus;
};

type ToolResult = {
  isError?: boolean;
  structuredContent?: Snapshot;
  content?: { type: string; text?: string }[];
};

const KIND: Record<string, string> = {
  task: "opened",
  subtask: "assigned",
  bug: "reported",
  "bug-accepted": "accepted",
  "bug-rejected": "rejected",
  push: "proposed",
  "test-pass": "passed",
  "test-fail": "failed",
  approve: "approved",
  object: "objected",
  veto: "vetoed",
  superseded: "superseded",
  link: "landed",
  "no-change": "no change",
  "task-done": "done",
};

const TONE: Record<string, string> = {
  "test-pass": "ok",
  approve: "ok",
  "bug-accepted": "ok",
  link: "ok",
  "no-change": "ok",
  "task-done": "ok",
  "test-fail": "bad",
  object: "bad",
  veto: "bad",
  "bug-rejected": "bad",
  push: "warn",
  superseded: "warn",
  bug: "warn",
};

const app = new App({ name: "gud", version: "0.1.0" }, {}, { autoResize: true });

let cursor = "";
let tip: Tip = null;
let tasks: Task[] = [];
let bugs: Bug[] = [];
let events: FeedEvent[] = [];
let needs: Need[] = [];
let focus: Focus | null = null;
let note = "";
let fresh = new Set<string>();
let busy = false;

function esc(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function textOf(result: ToolResult | null): string {
  return result?.content?.find((block) => block.type === "text")?.text || "";
}

function payload(result: ToolResult | null): Snapshot | null {
  if (!result || result.isError) return null;
  if (result.structuredContent && typeof result.structuredContent === "object") return result.structuredContent;
  const text = textOf(result);
  if (!text) return null;
  try {
    return JSON.parse(text) as Snapshot;
  } catch {
    return null;
  }
}

function applyHost(ctx: McpUiHostContext | undefined) {
  if (!ctx) return;
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
}

function merge(data: Snapshot) {
  if (data.tip !== undefined) tip = data.tip;
  if (data.needs_you) needs = data.needs_you;
  if (data.cursor) cursor = data.cursor;
  if (data.focus) focus = data.focus;
  if (data.delta) {
    const known = new Set(events.map((event) => event.id));
    for (const event of data.events || []) {
      if (known.has(event.id)) continue;
      events.push(event);
      fresh.add(event.id);
    }
    for (const task of data.tasks || []) {
      const index = tasks.findIndex((item) => item.id === task.id);
      if (index >= 0) tasks[index] = task;
      else tasks.unshift(task);
    }
    for (const bug of data.bugs || []) {
      const index = bugs.findIndex((item) => item.id === bug.id);
      if (index >= 0) bugs[index] = bug;
      else bugs.unshift(bug);
    }
  } else {
    const known = new Set(events.map((event) => event.id));
    events = data.events || [];
    tasks = data.tasks || [];
    bugs = data.bugs || [];
    if (known.size) {
      for (const event of events) {
        if (!known.has(event.id)) fresh.add(event.id);
      }
    }
  }
  events.sort((a, b) => b.time - a.time || (a.id < b.id ? 1 : -1));
}

function parentOf(event: FeedEvent): string {
  const tid = event.task || "";
  if (tid) {
    const parent = tasks.find((task) => task.id === tid || task.subtasks?.some((sub) => sub.id === tid));
    if (parent) return parent.id;
    return tid.includes(".") ? tid.split(".", 1)[0] : tid;
  }
  if (event.bug) return `bug:${event.bug}`;
  return "repo";
}

function groupTitle(key: string): string {
  if (key === "repo") return "Repo";
  if (key.startsWith("bug:")) {
    const bug = bugs.find((item) => item.id === key.slice(4));
    return bug ? bug.subject : "Bug report";
  }
  const task = tasks.find((item) => item.id === key);
  return task ? `${task.id} ${task.title}` : key;
}

function progress(task: Task): number {
  const subs = task.subtasks || [];
  if (task.status === "done") return 1;
  if (!subs.length) return 0;
  return subs.filter((sub) => sub.status === "landed").length / subs.length;
}

function when(time: number): string {
  const date = new Date(time * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function render() {
  const tipEl = document.querySelector("#tip");
  const taskEl = document.querySelector("#tasks");
  const needsEl = document.querySelector("#needs");
  const feedEl = document.querySelector("#feed");
  const detailEl = document.querySelector("#detail");
  const noteEl = document.querySelector("#note");
  if (!tipEl || !taskEl || !needsEl || !feedEl || !detailEl || !noteEl) return;

  tipEl.innerHTML = tip
    ? `#${esc(tip.seq)} · ${esc(tip.state)} · ${esc(tip.author)} · ${esc(tip.subject)}`
    : "No tip yet";

  const open = tasks.filter((task) => task.status !== "done");
  taskEl.innerHTML = open.length
    ? open.map((task) => {
        const subs = task.subtasks || [];
        const done = subs.filter((sub) => sub.status === "landed").length;
        const width = Math.round(progress(task) * 100);
        return `<div class="task"><div><b>${esc(task.id)}</b> ${esc(task.title)}</div><div class="quiet">${done}/${subs.length} landed</div><div class="barline"><span style="width:${width}%"></span></div></div>`;
      }).join("")
    : "";

  needsEl.innerHTML = needs.length
    ? `<div class="needs">${needs.map((item) => `<div class="need"><span class="quiet">${esc(item.kind)}</span><span class="title">${esc(item.title)}</span><span class="verbs">${item.verbs.map((verb) => `<button type="button" data-verb="${esc(verb)}" data-target="${esc(item.id)}" ${busy ? "disabled" : ""}>${esc(verb)}</button>`).join("")}</span></div>`).join("")}</div>`
    : "";

  const groups = new Map<string, FeedEvent[]>();
  for (const event of [...events].sort((a, b) => a.time - b.time || (a.id < b.id ? -1 : 1))) {
    const key = parentOf(event);
    const list = groups.get(key) || [];
    list.push(event);
    groups.set(key, list);
  }
  const ordered = [...groups.entries()].sort((a, b) => {
    const at = a[1][a[1].length - 1]?.time || 0;
    const bt = b[1][b[1].length - 1]?.time || 0;
    return bt - at;
  });
  feedEl.innerHTML = ordered.length
    ? ordered.map(([key, rows]) => {
        const head = groupTitle(key);
        const body = rows.map((event) => {
          const focusId = event.target || event.id;
          const tone = TONE[event.kind] || "";
          const label = KIND[event.kind] || event.kind;
          return `<button type="button" class="event${fresh.has(event.id) ? " rise" : ""}" data-focus="${esc(focusId)}"><span class="kind ${tone}">${esc(label)}</span><span class="text"><b>${esc(event.actor)}</b> ${esc(event.text)}</span><time>${esc(when(event.time))}</time></button>`;
        }).join("");
        return `<section class="group"><header><span>${esc(head)}</span><span class="quiet">${rows.length}</span></header>${body}</section>`;
      }).join("")
    : `<p class="empty">Nothing in the mailbox yet.</p>`;

  detailEl.innerHTML = focusHtml(focus);
  noteEl.textContent = note;
  window.setTimeout(() => fresh.clear(), 700);
}

function focusHtml(item: Focus | null): string {
  if (!item) return "";
  if (item.kind === "missing") {
    return `<section class="detail"><header>${esc(item.path || item.id)}</header><pre class="mail">${esc(item.text || "Nothing matches")}</pre></section>`;
  }
  if (item.kind === "file") {
    const lines = (item.lines || []).map((line) => `<span class="quiet">${esc(line.n)}</span><span class="owner">${esc(line.owner)}</span><span>${esc(line.text)}</span>`).join("");
    return `<section class="detail"><header><span>${esc(item.path)}</span><span class="quiet">blame at the tip</span></header><pre class="code blame">${lines}</pre></section>`;
  }
  const mail = (item.emails || []).map((email) => `<pre class="mail"><b>${esc(email.kind || "human")}</b> ${esc(email.from)}  ${esc(email.subject)}\n${esc(email.body)}</pre>`).join("");
  const diff = item.diff ? `<pre class="code">${diffHtml(item.diff)}</pre>` : "";
  return `<section class="detail"><header><span>Thread</span><button type="button" id="close-detail">Close</button></header>${diff}${mail}</section>`;
}

function diffHtml(diff: string): string {
  return diff.split("\n").map((line) => {
    const tone = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : "";
    return `<div class="${tone}">${esc(line) || " "}</div>`;
  }).join("");
}

function tokenValue(): string {
  const input = document.querySelector<HTMLInputElement>("#token");
  const value = input?.value || sessionStorage.getItem("gud-token") || "";
  if (input && !input.value && value) input.value = value;
  if (value) sessionStorage.setItem("gud-token", value);
  return value;
}

async function call(name: string, args: Record<string, unknown>, quiet = false): Promise<ToolResult | null> {
  if (!quiet) {
    busy = true;
    render();
  }
  try {
    const result = (await app.callServerTool({ name, arguments: args })) as ToolResult;
    if (result.isError) {
      note = textOf(result) || "Request failed";
      return null;
    }
    note = "";
    return result;
  } catch (error) {
    note = error instanceof Error ? error.message : "Request failed";
    return null;
  } finally {
    busy = false;
  }
}

function stamp(): string {
  return JSON.stringify([
    cursor,
    tip,
    needs,
    tasks.map((task) => [task.id, task.status, ...(task.subtasks || []).map((sub) => sub.status)]),
    events.map((event) => event.id),
  ]);
}

async function refresh() {
  if (document.visibilityState === "hidden" || busy) return;
  const args: Record<string, unknown> = {};
  if (cursor) args.since = cursor;
  const before = stamp();
  const result = await call("gud", args, true);
  const data = payload(result);
  if (data) merge(data);
  if (!result || stamp() !== before) render();
}

async function decide(target: string, verdict: string) {
  const result = await call("gud_decide", { target, verdict, token: tokenValue() });
  if (!result) {
    render();
    return;
  }
  await refresh();
}

async function openFocus(id: string) {
  const args: Record<string, unknown> = { focus: id };
  if (cursor) args.since = cursor;
  const result = await call("gud", args);
  const data = payload(result);
  if (data) merge(data);
  render();
}

document.querySelector("#app")?.addEventListener("click", (event) => {
  const target = event.target as HTMLElement;
  if (target.id === "close-detail") {
    focus = null;
    render();
    return;
  }
  const verb = target.closest<HTMLElement>("[data-verb]");
  if (verb?.dataset.verb && verb.dataset.target) {
    void decide(verb.dataset.target, verb.dataset.verb);
    return;
  }
  const row = target.closest<HTMLElement>("[data-focus]");
  if (row?.dataset.focus) void openFocus(row.dataset.focus);
});

document.querySelector("#file-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const path = String(new FormData(event.target as HTMLFormElement).get("path") || "").trim();
  if (path) void openFocus(path);
});

document.querySelector("#task-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const data = new FormData(event.target as HTMLFormElement);
  const title = String(data.get("title") || "").trim();
  if (!title) return;
  void (async () => {
    const result = await call("gud_task", {
      title,
      description: String(data.get("description") || ""),
      token: tokenValue(),
    });
    if (!result) {
      render();
      return;
    }
    (event.target as HTMLFormElement).reset();
    tokenValue();
    await refresh();
  })();
});

app.ontoolresult = (params) => {
  const data = payload(params as ToolResult);
  if (!data) return;
  merge(data);
  render();
};

app.onhostcontextchanged = (ctx) => applyHost(ctx);

tokenValue();
render();

void app.connect().then(() => {
  applyHost(app.getHostContext());
  window.setInterval(() => {
    if (document.visibilityState === "visible") void refresh();
  }, 2500);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void refresh();
  });
  if (!cursor) void refresh();
}).catch((error: unknown) => {
  note = error instanceof Error ? error.message : "Waiting for the chat host";
  render();
});
