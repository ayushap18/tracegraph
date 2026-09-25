# Jev Router v2 — plan and build contract

v1 (`server.py` + `index.html`, commit d282950): one Jev call routes one query to one of 7 keyless agents; a hand-written SVG graph animates it over SSE.

v2 adds three things:

1. **Multi-agent chains.** One query can hold several requests ("weather in Paris and convert 100 EUR to INR"). A planner splits it into subtasks, Jev routes each subtask in parallel, agents run in parallel, and a merger combines the answers.
2. **Claude-powered agents.** With `ANTHROPIC_API_KEY` set, Claude (`claude-opus-5`) plans the split, writes answers for the `code`, `knowledge`, `chat`, and new `research` agents (streamed token by token), and writes the merged answer. Without a key everything still works: heuristic planner, v1 keyless agents, and a plain concatenating merger. `research` is only offered to Jev when Claude is available.
3. **React + D3 frontend.** A Vite + React + TypeScript app in `web/` with a D3 force-directed graph (zoom, pan, drag, click-to-inspect), D3 charts, and streaming answer text. aiohttp serves the built `web/dist`.

Jev stays the router: every routing decision is a Jev `system_one` call. Claude never picks the agent.

## Layout

```
server.py                 entry point: loads .env, builds the app, serves web/dist (falls back to legacy index.html if dist is missing)
jevrouter/__init__.py
jevrouter/config.py       env loading, constants (AGENTS, GUARDS, thresholds, SAMPLES, prices)
jevrouter/events.py       Broadcaster (SSE fan-out) and the event helpers below
jevrouter/jev.py          Jev questions and route_one(text) -> RouteDecision
jevrouter/planner.py      plan(query) -> list[str] subtasks (Claude when available, else heuristic split + Jev "multi" noul)
jevrouter/agents/__init__.py   registry: name -> async run(text, emit_delta) -> AgentResult
jevrouter/agents/tools.py      keyless agents from v1 (math, weather, time, currency, knowledge, code, chat); parsing helpers stay pure functions
jevrouter/agents/claude.py     Claude agents (code, knowledge, chat, research) with streaming; used only when key present
jevrouter/merger.py       merge(query, results, emit_delta) -> str
jevrouter/pipeline.py     handle(query, source): plan -> route all (parallel) -> run all (parallel) -> merge; emits events
jevrouter/app.py          aiohttp routes: GET / (dist), GET /events, POST /ask, POST /control, GET /api/config
tests/                    pytest: parsers, planner heuristic, pipeline with Jev and Claude faked, HTTP endpoints
web/                      Vite + React + TS + d3 app; `npm run build` -> web/dist; vite dev proxies /events,/ask,/control,/api to :8777
index.html                v1 page, kept as fallback
```

## Behaviour

- **Planner (keyless):** split on ` and `, `;`, ` then `, ` also `, `, and ` only when both sides are ≥ 3 words or contain a digit/capitalized place, AND Jev's noul `multi` ("The query contains two or more separate requests that need different kinds of answers") is ≥ 0.5. Max 4 subtasks. Otherwise one subtask = the whole query.
- **Planner (Claude):** `client.messages.parse`-style structured output `{subtasks: string[]}` (1–4 items, each self-contained, preserve numbers/places), `effort: "low"`. On any Claude error fall back to the heuristic.
- **Routing:** per subtask, v1 questions (`route` choice, `urgency` score, `unsafe` noul, `clear` noul) in one Jev call; same guard rules (unsafe ≥ 0.7 → `blocked`; confidence < 0.45 or clear < 0.25 → `clarify`). Subtasks route concurrently.
- **Agents:** when Claude is available, `code`/`knowledge`/`chat`/`research` use Claude; `knowledge` first fetches the DuckDuckGo abstract and gives it to Claude as grounding; `research` uses the `web_search_20260209` server tool (max_uses 3). `math`/`weather`/`time`/`currency` stay keyless (they're exact). Every agent gets `emit_delta(text)` to stream partial output; keyless agents emit their whole answer once.
- **Merger:** 1 subtask → its answer as-is (no extra call). ≥ 2 → Claude streams a short combined answer when available, else join as `**agent**: answer` blocks.
- **Claude calls:** model `claude-opus-5`, `thinking: {type: "adaptive"}`, `output_config.effort` = `"low"` for planner/chat/merger and `"medium"` for code/knowledge/research, streaming via `client.beta.messages.stream(...)` with `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`; check `stop_reason == "refusal"` and surface it as a failed agent result. Catch `anthropic.RateLimitError` → `anthropic.APIStatusError` → `anthropic.APIConnectionError`, most specific first.
- **Cost tracking:** stats carry `jev_input_tokens` and `claude_input_tokens` / `claude_output_tokens`; prices: Jev $0.042/M input; Opus 5 $5/M in, $25/M out.

## SSE event protocol (server → browser, `data: <json>\n\n` on GET /events)

Every event has `type`. `qid` is an int per query; `tid` is `"<qid>.<n>"` per subtask (n from 1).

| type | fields |
|---|---|
| `hello` | `agents` {name: description} (only the agents active now), `guards` ["clarify","blocked"], `claude` bool, `state` {autopilot, interval}, `stats`, `history` (list of `record`), `samples` [str], `prices` {jev_in, claude_in, claude_out} |
| `state` | `state` |
| `query` | `qid`, `text`, `source` ("you"\|"autopilot") |
| `plan` | `qid`, `planner` ("claude"\|"heuristic"), `subtasks` [{`tid`, `text`}], `multi` float\|null (Jev's multi noul if computed), `ms` |
| `routed` | `qid`, `tid`, `agent`, `pick`, `reason`, `probabilities` {agent: p} sorted desc, `confidence`, `urgency`, `unsafe`, `clear`, `jev_ms`, `model` |
| `delta` | `qid`, `tid` (or `"merge"`), `text` (append-only chunk) |
| `answered` | `qid`, `tid`, `agent`, `agent_ms`, `answer` (full text), `ok` bool, `source` str\|null, `engine` ("claude"\|"keyless") |
| `merged` | `qid`, `answer`, `engine` ("claude"\|"concat"\|"single"), `ms` |
| `done` | `qid`, `total_ms`, `stats` |
| `error` | `qid` (nullable), `tid` (nullable), `message` |

`record` (in `hello.history`, newest last, max 60): `{qid, text, source, at, plan: {planner, subtasks}, tasks: [routed ∪ answered fields per tid], merged: {answer, engine}, total_ms}`.

`stats`: `{queries, subtasks, errors, jev_input_tokens, claude_input_tokens, claude_output_tokens, by_agent: {agent: count}}` — `by_agent` counts subtasks and includes guards.

HTTP: `POST /ask {query}` → `{ok, qid}` (400 on empty, text capped 500 chars). `POST /control {autopilot?, interval?}` → state (interval clamped 1–15). `GET /api/config` → same body as `hello` minus history.

## Frontend (web/)

- Vite + React 18 + TypeScript (strict) + `d3` v7. No UI kit; CSS variables with light/dark via `prefers-color-scheme`. Agent colors: math #4f7cff, weather #17a9bd, time #9466ff, currency #23a864, knowledge #d99a06, code #e8622f, chat #d4549f, research #0ea5a4, clarify #8a8f9c, blocked #e0443a.
- `useEventStream()` hook owns the EventSource and a reducer keyed by qid/tid; reconnect resets from `hello`.
- **Force graph** (D3 `forceSimulation`, rendered to SVG via React refs, zoom/pan with `d3.zoom`, drag nodes): fixed-ish anchors for `query` (left), `planner`, `Jev` (center), agent nodes (right arc), `merger`, `answer` (far right). For each live query, subtask nodes appear between planner and Jev; links Jev→agent width = routing probability; particles travel query→planner→subtask→Jev→agent→merger→answer as events arrive; parallel subtasks visibly branch. Node size grows with traffic. Clicking any node opens an inspector (agent: count, avg confidence, recent queries; subtask: probabilities + answer).
- **Panels:** ask box + sample chips + autopilot toggle/interval; "Latest run" card showing the plan, each subtask's chip/probability bars/streaming answer, and the merged answer streaming; KPIs (queries, subtasks, avg Jev ms, avg confidence, Jev $ + Claude $); D3 charts: confidence timeline (dots colored by agent, 0.45 threshold line), traffic by agent (bars), latency per query (stacked Jev/agent/merge); routing log table.
- Mobile: single column below 1000px, 16px gutters, no horizontal scroll.

## Done means

- `.venv/bin/pytest -q` passes; `cd web && npm run build` passes with no TS errors.
- Server starts with only `TYPESAFE_API_KEY`; `POST /ask "weather in Paris and convert 100 EUR to INR"` produces plan with 2 subtasks, routed to weather + currency, both answered, merged (engine concat), done.
- Claude paths covered by tests with a fake Anthropic client (planner, streaming agent, merger, refusal, error fallback).
