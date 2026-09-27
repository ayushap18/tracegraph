# Faster, stronger backend · harder evals · Chat modes

Three tracks. Each starts from a measurement of what TraceGraph does today, so every change can be shown to help.

## Where we are (last 400 saved runs)

| Engine | Runs | Total p50 | Total p90 | Jev routing p50 | Slowest agent p50 | Slowest agent p90 |
|---|---|---|---|---|---|---|
| Keyless | 120 | 0.7 s | 2.0 s | 0.36 s | 0.16 s | 1.7 s |
| Claude Code | 47 | 4.7 s | 9.8 s | 0.36 s | 0.8 s | 6.1 s |
| Codex | 41 | 7.7 s | 21 s | 0.37 s | 1.1 s | 10.7 s |
| Antigravity | 167 | 7.2 s | 34 s | 0.38 s | 1.5 s | 13 s |

What this says:
- **Jev is not the bottleneck** (under 0.5 s). The agents are fast too at p50.
- **The gap is serial LLM calls around the agents.** With an engine, a run is planner call → routing → agent → merger
  call. At p50 on Antigravity, planner + merger take about 5 s of the 7.2 s, for questions like "time in Tokyo and
  15% of 380" whose parts need no LLM at all.
- **The tail is the CLI engines** (p90 21–34 s): cold starts, retries, and quota errors that arrive late.
- **Stage timings aren't stored** in the run record (only `total_ms`), so today this breakdown is partly inferred.
  Fixing that is step one.

The eval suite is 40 cases, single-turn, one pass each, scored by regex. Antigravity now passes 40/40, so the suite
can no longer tell a good change from a great one. It needs to get harder.

---

## Track A: fast and powerful backend

### A1. Measure every stage (first, small)
Store `timings: {plan_ms, route_ms, agents_ms, merge_ms, queue_ms}` and per-call engine latency in each run record;
show a stacked bar on Run detail and p50/p90 per stage on Live. Every later item is judged against this.

### A2. Skip LLM calls that add nothing (biggest win)
- **Plan without the LLM when the query is simple.** Jev's `multi` score plus the keyless splitter already handle
  most "A and B" queries. Call the LLM planner only for dependent steps, long queries, files or low split confidence.
- **Merge without the LLM when every answer is deterministic** (math, time, currency, weather, blocked, clarify):
  a template join. Use the LLM merger only when an answer is prose from an LLM agent or answers must be reconciled.
- **Route the whole query in parallel with planning** (speculative): if the plan comes back as one step, the route
  is already done.
- Expected: multi-part keyless-able questions on an engine drop from ~7 s to ~1 s.

### A3. Stream sooner
Start streaming the first finished agent's answer while slower ones run, and stream the merger token by token (the
CLIs already emit deltas). Time-to-first-token becomes the headline latency metric next to total time.

### A4. Cache what is safe to cache
- Jev decisions for identical subtask text + agent set (in-memory LRU, invalidated when agents or labels change).
- Live data with short TTLs: currency rates 10 min, weather 10 min, Wikipedia/DuckDuckGo facts 24 h.
- Never cache LLM answers across sessions (privacy, staleness). Every cache hit is marked in the trace.

### A5. Pick the right engine per step, not per run ("powerful")
- **Difficulty routing:** Jev already scores urgency; add a `hard` score. Easy steps go to the fastest healthy engine
  with `effort: low`; hard ones (reports, code, multi-hop) go to the strongest with `effort: high`.
- **Health-aware Auto:** order engines by live p50 and success rate from engine health, not only the fixed order.
- **Hedged requests for the tail:** if the first engine hasn't produced a token by its p90, start the second-best
  engine in parallel and keep whichever answers first (capped, off by default because it spends plan quota).

### A6. Stronger answers
- **Verify step for facts and numbers:** after an LLM agent answers, a cheap check (keyless recompute for math and
  currency; a second-engine "does the answer match the sources?" for knowledge/research) and a visible warning
  instead of a silent wrong answer.
- **More tools for LLM agents:** a URL reader (with the private-IP guard PLAN-v3 requires), unit conversion, date
  maths, and SQL over attached CSVs.

**Targets:** p50 on an engine under 3 s, p90 under 12 s, time to first token under 1.5 s, no accuracy loss on the
eval suite (gated in CI).

---

## Track B: harder, richer evals

### B1. New case types (the suite grows from 40 to ~200)
| Type | Example | Scored by |
|---|---|---|
| Multi-turn | "convert 100 USD to INR" → "and in GBP?" → "which is more?" | per-turn expectations, context kept |
| Dependent chains (DAG) | "capital of the 2022 World Cup winner, then its time zone, then the time there" | step order, values flow |
| Files | CSV stats, PDF passage questions (fixtures in `evals/fixtures/`) | exact numbers, cited passage |
| Paraphrase robustness | 5 phrasings per core case | same outcome for all |
| Safety and injection | harmful part hidden in the 2nd/3rd clause, "ignore previous instructions" inside a file | blocked, never leaked |
| Unsupported and honesty | crypto, bookings, real-time data it can't have | honest "can't", no invented answer |
| Open-ended quality | "explain X simply", reports | rubric judged by an LLM judge |
| Latency budgets | "time in Tokyo" must finish in < 2 s keyless | `max_ms` per case |

### B2. Better scoring
- **LLM-as-judge with a rubric** for open-ended answers (correct, complete, grounded, concise; 1–5 each), run on a
  different engine from the one being tested, with the judge prompt and score stored per case.
- **Repeat runs** (`--repeat 3`) to measure flakiness; a case is "flaky" if its outcomes differ.
- **Per-tag scorecards** (routing, safety, parsing, files, multi-turn), latency p50/p95 and tokens per eval run.
- **Engine matrix:** one command runs keyless + every available engine and prints a table.

### B3. Keep it honest
- Split: `dev` cases you may tune against and a `holdout` set that is only scored (never used for fixes or labels),
  so improvements aren't overfitting.
- CI runs the keyless dev + holdout suites; engine runs stay manual because they spend subscription quota.

---

## Track C: more variety in Chat

"Variety" can mean several things; these are the options, most useful first. Pick the ones you want.

1. **Modes** (a switch above the composer):
   - **Quick**: keyless where possible, low effort, short answers.
   - **Balanced** (default): today's behaviour.
   - **Deep**: strongest engine, high effort, the verify step on.
   - **Research**: web research + sourced answer with citations.
2. **Answer styles**: Concise, Detailed, Bullet points, Step by step, Explain like I'm new, Table. Applied by the
   merger, so they work with every engine.
3. **Several answers to choose from**: "Try another engine" on any answer, or send once to 2–3 engines and pick the
   best one to keep in the conversation (reuses the Compare and Sandbox side-by-side code).
4. **Prompt presets**: one-click starters such as Summarize, Translate, Explain code, Compare two things, Plan a
   trip, each filling the composer with a template.
5. **Agent picker**: "@weather", "@code" or any custom agent in the composer to skip routing and ask one agent.
6. **Rich answers**: tables, charts for numeric answers, copy and export per answer, follow-up suggestions.

---

## API contract (fixed before work starts)

TypeScript shapes: end of `web/src/protocol.ts` (`ChatMode`, `AnswerStyle`, `RunTimings`, `AnswerChecks`,
`TimingsSummary`, `RunEvalBody`, `EvalSummaryExtra`, `EvalCaseExtra`) plus new fields on `AskBody`, `AskResponse`,
`RunRecord`, `DoneEvent`, `RoutedFields`, `AnsweredFields`; client in `web/src/api.ts` (`chooseRun`, `getTimings`,
`runEvalWith`). All new fields are optional so older records and clients keep working.

### `POST /ask` additions
- `mode` (default `balanced`):
  - `quick`: heuristic planner only, template merge, keyless agents where one exists, LLM agents at `effort: low`.
  - `balanced`: today's behaviour plus the A2 skips.
  - `deep`: the healthiest strongest engine, LLM planner, `effort: high`, verify step on.
  - `research`: fact and knowledge steps go to the `research` agent (web search, sources cited); 400 when no engine
    with web search is available.
- `style`: passed to the merger (and to single LLM agents) as an instruction; the template merger applies what it can
  (`bullets`, `table` for multi-part answers, `concise`). Stored on the run.
- `agent`: must be an offered agent (400 otherwise); routing is skipped (`routed.forced: true`, confidence 1). Guards
  still run: an unsafe step is blocked even with `@agent`.
- `engines` (2–3 names, each validated like `engine`): one run per engine, same `group_id`, same session; response
  `{ok, qid: <first>, qids, group_id, session_id}`. The first run is `chosen` until the user picks.
- `retry_of` + `engine`: a new run for that run's question in its group (creating the group if it had none);
  not chosen until picked.
- Follow-up context uses, per group, only the chosen run.

### New endpoints
- `POST /api/runs/{qid}/choose` → marks it chosen and its group siblings not; 404 unknown, 409 no group.
- `GET /api/timings?engine=&limit=` → `TimingsSummary` from stored `timings` (runs without timings are skipped).
- `POST /api/evals/run` accepts `RunEvalBody` (`split`, `tags`, `repeat` 1–5, `judge` engine name or null).

### Run record and events
- Every run stores `timings: RunTimings`, also sent on `done`.
- `routed` may carry `cached` and `forced`; `answered` may carry `checks` (`verified`, `verify_note`, `cached`,
  `effort`).

### Eval cases (`evals/cases.jsonl`, new optional keys; old cases unchanged)
- `split`: `dev` (default) or `holdout`.
- `turns`: list of `{query, expect_agents?, expect_outcome?, must_match?, must_not_match?}` for multi-turn cases,
  run in one session in order.
- `files`: fixture names under `evals/fixtures/` attached to the case.
- `judge`: rubric text for open-ended answers, scored 1–5 on correct, complete, grounded and concise by the judge
  engine; the case passes when the mean is at least `judge_min` (default 3.5).
- `max_ms`: latency budget; over budget is reported, and fails the case only when `strict_ms` is true.
- `paraphrases`: extra phrasings, each run and scored as its own attempt with the same expectations.

## Order

1. **A1 measure** → **A2 skip LLM calls** → **A3 stream sooner** (fastest visible win, 1–2 days of work).
2. **B1 + B2 in parallel** with step 1 (evals don't touch the pipeline), then **B3**.
3. **C: the options you choose.** Modes and styles first (small), then several answers and the agent picker.
4. **A4 cache, A5 engine per step, A6 verify**, each gated by the harder eval suite from Track B.

Work split for parallel builders: backend (A1–A3), evals (B1–B3, new files under `evals/` and `jevrouter/evals.py`),
web (C, plus the A1 timing views), with the API contract fixed first as in the last two plans.

## Verification

- Speed: before/after p50, p90 and time to first token on the same 40 + new cases, keyless and one engine.
- Accuracy: the new suite on keyless and Antigravity, dev and holdout reported separately, no regression allowed.
- Chat: each mode and style checked in the browser on desktop and phone, in both themes.

## Costs to keep in mind

- Engine eval runs spend your subscription quota (~50 calls per 40 cases today; ~250 for a 200-case suite, more with
  `--repeat` or the LLM judge). The judge and hedged requests are opt-in for that reason.
- Deep and Research modes spend more quota per question by design; Quick spends almost none.
