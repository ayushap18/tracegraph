# Routing that learns: feedback, review queue, examples, engine health

Today TraceGraph shows every routing decision but never learns from one. When Jev sends "10 km in miles" to
`knowledge` instead of `math`, the only fix is to hand-edit an agent description and hope. The evals are a fixed
40 cases in `evals/cases.jsonl`, so accuracy is measured against the questions we thought of, not the ones people ask.

This plan closes the loop: **mark a wrong route → it becomes a labelled example → it becomes an eval case → it
steers Jev's next decision → the evals prove it helped.** Two supporting pieces make the loop trustworthy: an
engine health view (so answer failures aren't blamed on routing) and CI (so a change that hurts routing can't land
quietly).

## Why this has the most impact

- **It compounds.** Every correction improves routing and grows the eval suite at the same time. Nothing else on
  the list gets better with use.
- **It uses data we already store.** Each run's `record` in SQLite already holds, per subtask, the chosen agent, the
  full `probabilities` map, `confidence`, `unsafe` and `clear` (`jevrouter/jev.py` `route_one`). The review queue
  can be built from existing history on day one, with no new tracking.
- **The lever exists.** Jev's `Choice` criteria accept a JSON object per label, not just a string
  (`typesafe_sdk/_core/question_types.py`: `criteria: Mapping[str, JSONContent | None]`). So an agent's criterion
  can carry `{"description": ..., "examples": [...], "not": [...]}` built from confirmed corrections, without
  changing the router or training anything.

## Features

| # | Feature | Server | Web |
|---|---|---|---|
| 1 | Mark a route right or wrong, per subtask | `labels` table, `POST/DELETE /api/labels` | `RouteFeedback` on Run detail and Chat trace |
| 2 | Review queue: runs where routing was shaky | `GET /api/review` (computed from `runs.record`) | new **Review** page |
| 3 | Promote labels to eval cases | `POST /api/labels/{id}/promote`, `evals/cases.local.jsonl` | "Add to evals" on a label; Evals shows source |
| 4 | Examples steer routing (behind a switch) | `criteria()` builds JSON criteria from labels; `TG_ROUTE_EXAMPLES`, `/control {route_examples}`, `GET /api/agents/examples` | Settings switch; Agents page shows each agent's examples |
| 5 | Prove it: A/B eval, examples on vs off | `POST /api/evals/run` gains `examples: bool`; `GET /api/evals/compare` | Evals compare view: two runs, per-case diff |
| 6 | Engine health | Auto engine counters, `GET /api/engines/health` | Health card on Live and Settings |
| 7 | CI on every push | `.github/workflows/ci.yml` | none |

Order of value: 1 → 2 → 3 ship the loop even if 4 turns out not to help. 4 and 5 ship together, because 4 is
only on by default if 5 shows it wins.

## Data

### New table `labels`
```sql
CREATE TABLE IF NOT EXISTS labels(
  id TEXT PRIMARY KEY, qid INTEGER, tid TEXT, text TEXT,   -- the subtask text Jev saw
  picked TEXT,                                            -- agent Jev chose (or clarify/blocked)
  correct TEXT,                                           -- agent the user says was right; = picked when confirmed
  verdict TEXT,                                           -- 'right' | 'wrong'
  confidence REAL, margin REAL,                           -- copied from the run so the queue survives run deletion
  note TEXT, at REAL, promoted TEXT                       -- eval case id once promoted, else NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS labels_run_task ON labels(qid, tid);
```
One label per subtask; marking again replaces it. Sandbox runs have no `qid` in SQLite, so they can't be
labelled (consistent with "nothing is kept"); Keep a sandbox chat first if you want to label it.

### Derived signals (no storage)
- `margin` = top probability − second probability for the subtask.
- A subtask is **shaky** if any of: `confidence < 0.6`, `margin < 0.15`, outcome `clarify`, the agent returned
  `ok: false`, or the user re-asked within the same session in under 60 s (a proxy for "that answer was wrong").
  Thresholds live in `config.py` next to `MIN_CONFIDENCE`.

## API contract

### `POST /api/labels`
`{ qid, tid, verdict: "right" | "wrong", correct?: string, note?: string }`
- `correct` is required when `verdict = "wrong"` and must be an offered agent name, `clarify` or `blocked`.
- The server copies `text`, `picked`, `confidence` and `margin` from the stored run; the client never sends them.
- 404 when the run or subtask doesn't exist. Returns the label.

### `DELETE /api/labels/{id}` — removes it (and nothing else; a promoted eval case stays).

### `GET /api/labels?agent=&verdict=&limit=` — newest first.

### `GET /api/review?limit=50&reason=`
Shaky, unlabelled subtasks from saved runs, newest first:
`[{ qid, tid, text, picked, confidence, margin, runner_up, reasons: ["low margin", ...], at }]`.
Computed by scanning `runs.record` (bounded to the last 2,000 runs; fast enough in SQLite, no index needed yet).

### `POST /api/labels/{id}/promote`
Appends a case to `evals/cases.local.jsonl`:
`{"id": "u-<label id>", "query": text, "expect_agents": [correct], "tags": ["user"]}`
(`expect_outcome` instead of `expect_agents` when `correct` is `clarify`/`blocked`). `load_cases()` reads both
files; `cases.local.jsonl` is git-ignored by default so personal questions don't get committed. Idempotent.

### Examples in routing (feature 4)
`criteria(agents, labels)` in `jev.py` turns each agent description into:
```json
{"description": "Arithmetic, percentages, ...",
 "examples": ["10 km in miles", "what's 7% of 340"],
 "not": ["what is the speed of light"]}
```
- `examples`: up to **3** most recent `correct = agent` labels. `not`: up to **2** recent labels where Jev picked
  this agent and the user said wrong. Agents with no labels keep a plain string (identical request to today).
- Off unless `TG_ROUTE_EXAMPLES=1` or the Settings switch is on. `route_one` records `examples: true|false` in
  the `routed` event so runs and evals show which mode made the decision.
- Guard against a bad label poisoning an agent: an example is only used if its `text` isn't also a `not` for the
  same agent, and a label is dropped from examples after it's deleted.

### Switch and inspection
- `POST /control {route_examples: bool}` flips it for new runs (default from `TG_ROUTE_EXAMPLES`); `/api/config` and
  the `hello`/`config` events carry `route_examples`.
- `GET /api/agents/examples` → `{enabled, agents: [{agent, examples, not}]}`: exactly what Jev would see now.

### `POST /api/evals/run` gains `examples?: boolean`; `GET /api/evals/compare?a=&b=`
The eval forces examples on or off for its own runs only (the global switch is untouched), and the stored summary
records `examples`. Compare returns `{a, b, cases: [{id, query, a_pass, b_pass}]}` where each side has accuracy,
passed, total, silent-wrong and `mean_jev_tokens`. The TypeScript shapes are in `web/src/protocol.ts` (end of file)
and the client in `web/src/api.ts`; both are fixed before work starts.

### `GET /api/engines/health`
Per engine since server start: `calls, ok, fallbacks_from, fallbacks_to, p50_ms, p95_ms, last_error, cooling_until`.
Counters live in `engines/auto.py`, which already knows every attempt and cooldown; memory only, reset on restart.

## Web

- **RouteFeedback** (Run detail → Subtasks tab, and the Chat trace panel): 👍 / 👎 per subtask; 👎 opens an agent
  picker pre-sorted by Jev's probabilities, so the likely right answer is one click. Shows the saved label after.
- **Review page** (Workspace nav, after Runs): table of shaky subtasks with the reason chips, confidence bar and
  runner-up; inline 👍 / 👎 so you can clear 20 in a few minutes. Keyboard: j/k to move, y/n to mark.
- **Agents page**: each agent shows its current examples and "not" list, with remove buttons.
- **Evals**: a "user" tag filter, and a compare view (two runs side by side, flips highlighted).
- **Health card**: one row per engine with a status dot, success rate, p95 and "cooling until 18:35".

## Order and work split

1. **M1 Feedback** (server + RouteFeedback): labels table, `POST/DELETE/GET /api/labels`, tests.
2. **M2 Review** (server + page): `/api/review`, shaky reasons, keyboard flow. Backfills from existing runs.
3. **M3 Promote**: `cases.local.jsonl`, `load_cases()` merge, Evals tag filter.
4. **M4 Examples + A/B**: `criteria()`, the switch, eval `examples` flag, compare view. **Decision gate:** turn it
   on by default only if, on the keyless suite plus user cases, accuracy goes up, no committed-suite case
   regresses, and mean Jev tokens per call rise by less than 30%.
5. **M5 Health**: counters in `auto.py`, endpoint, card.
6. **M6 CI**: GitHub Actions running `pytest`, `cd web && npm ci && npm run build` (the web app has no test script yet), and the eval suite
   with `--engine none` against `evals/baseline.json` (it already exits 1 on a regression). No secrets needed.

M1–M3 and M5–M6 are independent after the `labels` schema is fixed, so they can run in parallel the way the
Sandbox v2 work did. M4 depends on M1 and M3.

## Verification

- Server tests: label validation (unknown agent, missing `correct`, sandbox qid), one-per-subtask replace,
  review reasons against hand-built run records, promote idempotency, `criteria()` output with 0 / 1 / many labels
  and the poisoning guard, health counters across a forced fallback.
- End to end in the browser: ask "10 km in miles" → mark 👎 → pick `math` → it appears in Review as labelled and on
  the Agents page as an example → promote → run evals with examples off and on → compare shows the flip.
- Token check: log Jev `input_tokens` with examples on vs off for the same 40 cases.

## Risks

- **Examples may not help.** Whether Jev uses `examples` inside a criterion object is an assumption until M4's A/B
  says so. The plan is built so M1–M3 still pay off (a growing, real eval suite and a triage queue) if it doesn't.
- **Token cost.** Examples lengthen every routing call (currently ~500 input tokens). Capped at 3 + 2 per agent
  and measured at the decision gate.
- **Overfitting to one person's habits.** Labels are local to this install, and the committed eval suite stays the
  regression gate, so a local tweak can't silently break the shared cases.
- **Privacy.** Labelled questions are user text; `cases.local.jsonl` is git-ignored and never exported by default.

## Not in this plan

Automatic description rewriting by an LLM, training or fine-tuning a router, and sharing labels across installs.
Each is a sensible next step once there are enough labels to know whether it's worth it.
