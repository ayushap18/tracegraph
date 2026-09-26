# Sandbox v2: a test bench for engines, routing and agents

The Sandbox (v1, commit 423a065) runs messages through the real pipeline without storing anything. v2 turns it
into a place to experiment: choose the engine per message, see why Jev routed where it did, count tokens, export,
draft an agent before saving it, edit and re-run, compare engines side by side, keep a conversation on purpose,
forget idle sandboxes, and attach files that live only in memory.

## Features

| # | Feature | Server | Web |
|---|---|---|---|
| 1 | Engine picker per sandbox | uses existing `engine` on `/ask` | `EngineSelect` |
| 2 | "Why this agent" (reason, confidence, runner-ups) | none (already in `routed` events) | `RoutingWhy` |
| 3 | Token and cost meter | none (`done` carries `tokens`) | `UsageMeter`, `Run.tokens` |
| 4 | Download conversation (Markdown / JSON) | none | `export.ts` |
| 5 | Agent playground (draft agent, never saved) | `draft_agent` on `/ask` | `Playground` |
| 6 | Edit and re-run, keeping both answers | `replaces` on `/ask` | `TurnGroupView` |
| 7 | Side-by-side engines inside the sandbox | `remember: false` on `/ask` | `SideBySide` |
| 8 | "Keep this": save as a normal chat, on request | `POST /api/sandbox/{id}/keep` | `KeepDialog` |
| 9 | Forget idle sandboxes | TTL sweeper (`TG_SANDBOX_TTL`, default 1800 s) | none |
| 10 | Temporary file attachments | `POST/DELETE /api/sandbox/{id}/files` | `Attachments` |

## API contract (fixed before work starts; every agent codes against this)

### `POST /ask` with `source: "sandbox"`, `sandbox_id`
Existing fields plus:
- `engine?: string`: engine name or `"none"` for keyless (already validated by `pick_engine`).
- `files?: string[]`: ids returned by the sandbox upload endpoint (stored-file ids are rejected).
- `draft_agent?: { name, description, prompt, web? }`: added to Jev's route criteria and the agent registry for
  this run only. Validated like a custom agent (name pattern, lengths, no clash with built-ins or saved customs),
  but not counted against the 12-agent limit and never stored. Needs an LLM engine: `409` if the run is keyless.
- `replaces?: number`: qid of an earlier sandbox turn this run is an edited version of. Its context is the turns
  before that turn; when it finishes it takes that turn's place in the sandbox memory.
- `remember?: boolean` (default `true`): `false` means the run neither reads nor writes the follow-up memory
  (used by side-by-side runs).
- `session_id` is still rejected for sandbox runs.

### Sandbox memory (server, per sandbox id, never on disk)
`{ thread: [{qid, query, answer, record}] (finished turns in order, max 50), files: {id: (meta, text)},
last_used }`. Follow-up context = last 3 thread turns (before `replaces` when given). Files: max 5 per sandbox,
10 MB each, 20 MB total.

### New endpoints
- `POST /api/sandbox/{id}/files` (multipart `file`) → file metadata (same shape as `/api/files`), kept in memory.
- `DELETE /api/sandbox/{id}/files/{fid}` → `{ok: true}`.
- `POST /api/sandbox/{id}/keep` body `{qids?: number[]}` (default: the whole thread) → `{session_id, qids}`.
  Copies those finished records into a new saved chat: new qids from the normal counter, tids rewritten,
  `source: "chat"`, new `session_id`, file references dropped (sandbox files are not saved). The sandbox itself
  is unchanged.
- `DELETE /api/sandbox/{id}` (exists) also drops files and the thread.
- Sweeper: every 60 s, sandboxes idle longer than `TG_SANDBOX_TTL` with no running queries are forgotten.

### Events
Unchanged. The web reducer additionally keeps `tokens` from `done` on each `Run`.

## Work split

| Owner | Scope | Files |
|---|---|---|
| Lead (me) | plan, shared types and API client, `Run.tokens`, integration of the page, end-to-end QA, commit | `docs/PLAN-sandbox.md`, `web/src/pages/sandbox/types.ts`, `web/src/api.ts`, `web/src/useEventStream.ts`, `web/src/protocol.ts`, `web/src/pages/Sandbox.tsx` |
| Agent BE | every server change above + tests | `jevrouter/sandbox.py` (new: sandbox memory), `jevrouter/pipeline.py`, `jevrouter/app.py`, `tests/test_sandbox.py` |
| Agent FE-A | engines and numbers: 1, 3, 4, 7 | `web/src/pages/sandbox/{EngineSelect,UsageMeter,SideBySide}.tsx`, `export.ts` |
| Agent FE-B | insight and iteration: 2, 6 | `web/src/pages/sandbox/{RoutingWhy,TurnGroupView}.tsx` |
| Agent FE-C | agents, files, keep: 5, 8, 10 | `web/src/pages/sandbox/{Playground,Attachments,KeepDialog}.tsx` |

Agents own only their files, code against the contract and the prop interfaces in `sandbox/types.ts`, and do not
edit shared files. The lead wires everything into `Sandbox.tsx`.

## Order
1. Lead: plan, contract, shared types, API client, `Run.tokens` (so all agents compile against real types).
2. BE, FE-A, FE-B, FE-C in parallel.
3. Lead: integrate `Sandbox.tsx`, restart a test server on a throwaway DB, test every feature end to end in the
   browser (light, dark, 390 px), run `pytest` and `npm run build`, commit.

## Verification
- `pytest`: storage untouched by sandbox, draft agent routed and never stored, `replaces` context and memory swap,
  `remember: false`, keep creates a real chat with new qids, sweeper forgets idle sandboxes, file limits,
  sandbox files usable by file agents and never written to disk.
- Browser: each feature once; saved-run list and stats unchanged throughout; Keep lands in Chat as a normal chat.
