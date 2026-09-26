# TraceGraph v4: build contract

Branch `v4`. Three builders work in parallel against this document. **Names, routes, fields and event types here are
exact**: the backend and the two frontend builders never see each other's code, so any deviation breaks integration.
Everything is additive: existing SSE events, `/ask`, `/control`, `/api/config` and every existing test keep working.

Ownership (never edit outside your area):
- **backend**: `jevrouter/**`, `server.py`, `tests/**`, `evals/**`, `requirements*.txt`, `.gitignore`
- **frontend-core**: everything in `web/**` except the three files owned by frontend-pages
- **frontend-pages**: `web/src/pages/Compare.tsx`, `web/src/pages/Evals.tsx`, `web/src/pages/Agents.tsx` only

Nobody commits. Nobody prints or copies the TypeSafe key in `.env`. Don't touch the server running on port 8777. Use other
ports (8790-8799) for live checks, and kill what you start.

---

## 1. Backend

### 1.1 Persistence (SQLite, stdlib `sqlite3`)
- DB file: `data/tracegraph.db` (env `TG_DB`; tests use a temp path or `:memory:`). Add `data/` to `.gitignore`.
- Tables (at minimum): `runs(qid INTEGER PRIMARY KEY, session_id TEXT, compare_id TEXT, source TEXT, text TEXT, at REAL,
  status TEXT, engine TEXT, total_ms INTEGER, record TEXT /* JSON, same shape as a hello.history record */)`,
  `sessions(id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL)`, `agents(...)` (1.6), `files(...)` (1.7),
  `evals(...)` (1.9). Run DB writes in a thread (`asyncio.to_thread`) or keep them tiny; never block the event loop long.
- On startup: qid counter continues from `MAX(qid)+1`; the last 60 finished runs load into `history`, so `hello` replays them after a restart.
- A run record gains fields: `status` (`running|done|cancelled|timeout|error`), `engine` (engine name or null),
  `session_id` (or null), `compare_id` (or null), `files` (list of file ids). Plan subtasks gain `depends_on` (1.5).

### 1.2 Run control
- `POST /api/runs/{qid}/cancel` → `200 {"ok": true}` if it was running, `404` unknown, `409 {"error": ...}` if already finished.
  Cancelling cancels the run's asyncio task (and so kills any CLI child process via the engine runner), then emits
  `{"type":"cancelled","qid"}` followed by the normal `done` event.
- Per-run deadline `TG_RUN_TIMEOUT` (seconds, default 300) → status `timeout`, with an `error` event then `done`.
- The `done` event gains `status` (additive). Subtasks that never answered get an `error` event with their tid.

### 1.3 `/ask` additions
Body: `{"query", "session_id"?: str, "engine"?: str, "files"?: [file_id], "source"?: "you"|"chat"|"compare"|"eval"}`.
- `engine`: use that engine **for this run only**. Unknown → 400; unavailable → 409.
- Response: `{"ok": true, "qid", "session_id"}`. A `session_id` is created if `source == "chat"` and none was given.
- The `query` event gains `session_id`, `compare_id`, `engine`, `files` (additive; null or [] when absent).

### 1.4 Chat sessions (follow-ups)
- When a run has a `session_id`, the LLM planner receives the last 3 turns of that session (question and final
  answer, truncated to 600 chars each). It rewrites follow-ups into self-contained subtasks: "and in GBP?" after
  "convert 100 USD to EUR" becomes "convert 100 USD to GBP". The keyless planner ignores context, and
  `worth_llm_plan` is true for any session run that has at least one prior turn.
- `GET /api/sessions?limit=30` → `{"sessions": [{"id","title","created","updated","turns"}]}`, newest first
  (title = first query, truncated to 60 chars).
- `GET /api/sessions/{id}` → `{"id","title","runs": [record...]}`, runs in qid order. `DELETE /api/sessions/{id}` → `{"ok":true}`.

### 1.5 Dependent steps (DAG)
- LLM planner schema: `{"subtasks":[{"text": str, "depends_on": [int]}]}`, where `depends_on` holds 0-based indices of
  earlier subtasks only. Tolerate the old `["text", ...]` shape. The keyless planner always has no dependencies.
- The `plan` event's subtasks become `{"tid","text","depends_on":[tid...]}`.
- Execution runs in waves: a subtask starts once its dependencies have answered. Its text sent to Jev and to the
  agent is its own text plus `"\n\nContext from earlier steps:\n- <dep text>: <dep answer (≤500 chars)>"`.
  Independent subtasks still run in parallel. If a dependency failed, the dependent subtask still runs, with the
  failure noted in its context.

### 1.6 Custom agents
- `GET /api/agents` → `{"agents":[{"name","description","kind":"builtin"|"custom"|"guard","engine_required":bool,"available":bool,"prompt"?:str,"web"?:bool}]}`.
- `POST /api/agents {"name","description","prompt","web"?:false}` → 201 with the agent. Validation: name
  `^[a-z][a-z0-9_-]{1,23}$`, no clash with a builtin or guard, description of 10–200 chars, prompt of 10–4000 chars,
  at most 12 custom agents. Errors return `400 {"error"}`.
- `DELETE /api/agents/{name}` → `{"ok":true}` (custom only, else 400).
- Custom agents are offered to Jev (in the route Choice criteria) only while an engine is active. They run on the
  engine with `system = ABOUT + the custom prompt`, and `web` is honoured only if the engine supports it. Every
  change emits a `config` event.

### 1.7 Files, and the document and data agents
- `POST /api/files` (multipart field `file`, ≤10 MB; `.txt .md .csv .json .pdf`; PDF text via `pypdf`, added to
  requirements) → `201 {"id","name","size","kind":"text"|"csv"|"pdf"|"json","chars","rows"?,"columns"?}`. Stored under
  `data/files/<id>` with extracted text. `GET /api/files` → `{"files":[...]}`. `DELETE /api/files/{id}` → `{"ok":true}`.
- New built-in agents, offered to Jev **only for runs that attach files**:
  - `document`: "Questions about the attached documents or files". Keyless: keyword/BM25-style passage search over
    ~800-char chunks, returning the top 3 passages with the file name. With an engine: answer grounded in the top 6
    passages, citing file names.
  - `data`: "Statistics, totals, averages or analysis of an attached CSV/JSON table". Keyless: row count, columns,
    numeric column min/mean/max via `csv` (no pandas). With an engine: those stats plus the first 30 rows, then the
    engine answers.
- The planner is told the attached file names.

### 1.8 Heavy agents (engine required)
- `report`: "Long-form written reports, essays, comparisons or summaries, with sources". Uses the engine with
  `web=True` when supported, effort `high`, Markdown of 400–900 words with a Sources section.
- `run`: "Write and execute code to compute or produce something". Only offered when the active engine is
  **codex**. It runs Codex in its own OS sandbox (`-s workspace-write`, network off by default) inside a fresh scratch
  dir, and asks it to write the script, run it and report the output. Add `supports_exec = True` to
  `CodexEngine` and an `exec` option to its `stream(...)` that switches `-s read-only` to `-s workspace-write`. Other
  engines don't offer `run`.
- The `Engine.stream` signature gains an optional `exec: bool = False` (ignored by engines without exec support).

### 1.9 Compare engines
- `POST /api/compare {"query","engines":[name,...]}` (2–4 available engines) → `{"compare_id","runs":[{"engine","qid"}]}`.
  It submits one run per engine with that engine override, `source:"compare"`, and a shared `compare_id`.
- `GET /api/compare/{compare_id}` → `{"compare_id","query","runs":[record...]}`.

### 1.10 Evals
- `evals/cases.jsonl`: at least the 30 stress-test questions from `docs/PLAN-v3.md` Appendix A, plus 10 easy
  controls. One JSON per line: `{"id","query","expect_agents"?:[...] /* each must be chosen for some subtask */,
  "expect_outcome"?:"clarify"|"blocked"|"answer", "must_match"?: "<regex, case-insensitive, on the final answer>",
  "must_not_match"?: "<regex>", "tags":[...]}`.
- `POST /api/evals/run {"engine"?: name|"none"}` → `{"eval_id"}`. Cases run sequentially through the real pipeline
  with `source:"eval"` and that engine override, at a concurrency of 2.
- Progress: SSE `{"type":"eval_progress","eval_id","done","total","passed"}` after each case, and
  `{"type":"eval_done","eval_id","passed","total","accuracy"}` at the end. `POST /api/evals/{id}/cancel` stops it.
- `GET /api/evals` → `{"evals":[{"eval_id","at","engine","status","passed","total","accuracy","silent_wrong"}]}`
  (`silent_wrong` = failed cases whose answers all had `ok=true`).
- `GET /api/evals/{id}` → the summary plus `"cases":[{"id","query","tags","pass","reasons":[str],"agents":[...],"answer","ms","qid"}]`.
- Also a CLI: `.venv/bin/python -m jevrouter.evals [--engine NAME]` prints a table and exits non-zero on regressions
  against `evals/baseline.json` if it exists.

### 1.11 Engine test
`POST /api/engines/{name}/test` → `{"ok":bool,"ms":int,"text"?:str,"error"?:str}`. It sends a tiny prompt ("Reply with
exactly: ok"), with a 60s timeout.

### 1.12 Config additions
`config`/`hello` gain `"features": {"files": true, "compare": true, "evals": true, "custom_agents": true, "exec": <active engine supports exec>}`.

### 1.13 Tests
Keep all 112 existing tests green. Add tests for every endpoint above, the DAG wave order and context passing,
cancellation (including a CLI child being killed through a fake engine that sleeps), session follow-up context
reaching the planner, file upload and extraction (txt/csv/pdf; generate a tiny PDF in the test with `pypdf` or ship
a fixture), document/data keyless answers, custom agent validation and routing criteria, compare fan-out, eval
scoring, and DB persistence across a Router restart. Use fakes, not live engines.

---

## 2. Frontend

Hash routing with no router dependency. Current routes are `#/` (Chat), `#/live`, `#/runs`, `#/runs/:qid`, `#/compare`,
`#/compare/:id`, `#/evals`, `#/evals/:id`, `#/agents`, `#/settings`, `#/about`. Unknown routes fall back to Chat.

### 2.1 Shared (frontend-core owns and creates these; frontend-pages imports them and does not edit them)
- `web/src/api.ts`: typed fetch helpers for every endpoint in §1, returning parsed JSON or throwing `ApiError{status,message}`:
  `ask(body)`, `cancelRun(qid)`, `listRuns({limit, before, q})`, `getRun(qid)`, `listSessions()`, `getSession(id)`,
  `deleteSession(id)`, `listAgents()`, `createAgent(a)`, `deleteAgent(name)`, `uploadFile(file)`, `listFiles()`,
  `deleteFile(id)`, `compare(query, engines)`, `getCompare(id)`, `runEval(engine?)`, `cancelEval(id)`, `listEvals()`,
  `getEval(id)`, `testEngine(name)`, `setEngine(name)`, `control(body)`.
- `web/src/protocol.ts`: add types for every new field and event (`cancelled`, `eval_progress`, `eval_done`, `status` on
  `done`, `depends_on`, `session_id`/`compare_id`/`engine`/`files` on `query`, `features`), plus a `RunRecord` type.
- `web/src/store.tsx`: `<StoreProvider>` wrapping the existing `useEventStream`, plus `useStore()` returning
  `{ store, subscribe }`, and `useRun(qid)`, which returns the live Run if it's in the store, otherwise fetches
  `getRun`.
- `web/src/ui.tsx`: small shared primitives: `Button`, `IconButton`, `Card` (with title/icon/actions slot), `Badge`,
  `Skeleton`, `EmptyState` (icon, title, text, action), `Tabs`, `Spinner`, `Toggle`, `Field` (label plus input/textarea),
  `useToast()`, `ProgressBar`, and `navigate(path)` for hash routing.
- Icons: only through `web/src/icons.tsx`. frontend-core adds UI icons for every page and action (chat, live, runs,
  compare, evals, agents, settings, about, stop, upload, paperclip, share/link, copy, trash, plus, play, external,
  file, history).

### 2.2 frontend-core builds
- **App shell**: a left sidebar (logo, nav items with icons and labels, active state, collapsible to icons, the
  engine picker, connection dot, theme toggle) and a top bar with the page title, ⌘K hint and contextual actions. On
  mobile (<760px) the sidebar becomes a bottom tab bar with the 5 main items (Chat, Live, Runs, Evals, More).
- **Chat (home `#/`)**:
  - Session list on the left (new chat, recent sessions, delete) and a conversation thread in the centre.
  - Each assistant turn streams its merged answer (Markdown), shows agent chips with confidence and a timing, and has a
    "View trace" link to `#/runs/:qid`.
  - A composer with auto-growing textarea, Enter to send (Shift+Enter for a newline), an attach-files button (uploads
    via `uploadFile`, shows removable file chips), a **Stop** button while running (`cancelRun`), and sample prompts on
    an empty session.
  - A collapsible right panel shows the live trace graph (the existing `Graph`) for the selected turn.
  - Uses `source:"chat"` and `session_id`.
- **Live `#/live`**: the existing dashboard (KPIs, ask box, trace graph, latest run, timeline, analytics, log), moved into
  a page and kept working.
- **Runs `#/runs`**: a searchable, filterable table (source, status, engine) with "Load more" pagination, status badges,
  agent chips and relative times.
- **Run detail `#/runs/:qid`**: header (query, status, engine, total time, Share button that copies the URL, Stop if
  running, Replay button that re-asks the query) and tabs Answer · Trace (Graph) · Timeline (Waterfall) · Subtasks
  (cards with probabilities) · Raw (JSON).
- **Settings `#/settings`**: engine cards with status, billing, a Test button (`testEngine`) and a Set-active button;
  theme; timeouts (read-only info); a keyboard shortcuts reference; data section (counts).
- **About `#/about`**: a polished landing page with a hero (logo, tagline, "Open app" CTA, GitHub link
  https://github.com/ayushap18/tracegraph), an animated mini trace illustration (pure SVG/CSS), a feature grid,
  "how it works" steps, the engine logos row, and the tech stack.
- **Command palette** (⌘K / Ctrl+K): fuzzy search over pages, "Switch engine: X", "New chat", sample queries, and the
  10 most recent runs, with arrow keys, Enter and Esc.
- **Polish**:
  - The Inter font via Google Fonts `<link>` in `index.html`, with a system fallback.
  - An 8px spacing scale and one type scale.
  - Skeletons while loading, empty states everywhere, toasts for errors and successes.
  - Smooth page transitions (CSS), `prefers-reduced-motion` respected, light/dark via the existing tokens, focus-visible
    styles, no horizontal scroll at 375px.
- Integrate pages from frontend-pages: `import Compare from './pages/Compare'`, and the same for `Evals` and `Agents`.
  Each page is a default export taking `{ params: Record<string,string> }` (route params, e.g. `id`).

### 2.3 frontend-pages builds (using only `api.ts`, `ui.tsx`, `store.tsx`, `icons.tsx`, `protocol.ts` and existing components)
- **Compare `#/compare`, `#/compare/:id`**: query input, engine checkboxes (available engines from the store; 2–4), a
  Run button, then side-by-side columns per engine showing streaming answers (from store runs by qid), agents chosen,
  plan, time, tokens, status and a "fastest" badge. There's a history of past comparisons in this browser
  (localStorage, try/catch).
- **Evals `#/evals`, `#/evals/:id`**:
  - The list page has a Run eval button with an engine select, a live progress bar (from `eval_progress` via
    `subscribe`), and a history table with accuracy, silent_wrong and a trend sparkline.
  - The detail page has a summary, pass/fail filter chips, a tag filter, and a table of cases (query, expected vs got
    agents, reasons, answer excerpt, link to the run).
- **Agents `#/agents`**: cards for built-in agents (icon, description, whether it needs an engine, availability),
  guards, and custom agents; a create-agent form with live validation mirroring §1.6; and delete with confirm-in-place
  (no `window.confirm`).

Because frontend-core creates the shared modules at the same time, frontend-pages must write against the exact names
in §2.1. If a helper isn't there yet, frontend-pages writes against the spec and does not create the shared file itself.
