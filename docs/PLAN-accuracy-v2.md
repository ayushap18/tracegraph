# TraceGraph accuracy v2: routing, answers, long documents, harness

Status: plan, not yet built. It is based on the verified audits of runs, routing, the harness and long documents
(27-28 Sep 2026). Code references are to HEAD `b96e95b`.

Builders work in parallel against this document. **Names, fields, rule ids, file paths and signatures written here are
exact.** Every change is additive for clients: existing SSE events, `/ask`, stored runs, stored evals and existing tests
keep working unless a section below says a test changes. The server on port 8777 belongs to the user: never stop it,
never POST to it. Live checks run on ports 8790-8799 with a throwaway `TG_DB`. Nobody commits unless the lead says so.

---

## 1. What's broken (evidence)

### 1.1 The run that prompted this: qid 2741

The prompt (275 chars): *"pdf on the ai what is ai how ai begins using the multiple diagrams also add the images from the web
sources build 12-13 page of pdf properly using the black white text also anthropic sans font also keep the sizing
properly and all images are to be sourced from the best sources"*. It was sent with `@create` and mode `research` on
engine `auto`.

| Step | Planned text | What happened |
|---|---|---|
| 2741.1 | "Research AI in depth ... find relevant high-quality images ... with their URLs and attributions" | Forced to `create` (reason "you picked @create", P=1.0, clear 1.0). Ran on claude-code at effort high for 68,230 ms and made `e34bda15f529.md` (12,776 B, 9,410 tokens, 7 sections, 1,889 words). Its notes say "none were fetched" and "gives no specific image URLs". Its answer says "No format was named, so this is Markdown." |
| 2741.2 | "Create a properly formatted 12-13 page PDF ..." (depends on 2741.1) | Forced `create`, keyless path `from_answers`, 414 ms, 0 tokens. Made `1254d406bdd0.pdf`: **1 page**, 2,372 B, spec 450 B. Its only content is step 1's reply line "Created **...md**, 12 KB" and the "No format was named" note. |

The merged answer is `concat`: two raw `**create**:` lines that claim success. Every rule passed (V3 "1 pages", V4
"0 charts drawn"). Totals: 75.4 s, jev_in 4,972, llm_in 3,723, llm_out 7,458.

Six separate causes, each verified in code:

1. **`@agent` forces every step.** `pipeline.py:694` calls `route_forced` for every subtask. That skips `route_jev`, so
   the research-mode swap (`:715`) and `review()` never run. `review()` would have demoted step 1 ("no file asked
   for"). `plan()` is never told about the forced agent. 2741 is the only forced run among 2,741.
2. **A dependent create step never calls the engine.** `agents/create.py` `make()` takes the branch
   `elif job.deps: spec = from_answers(job.deps)` before `from_engine`. Any "research then make an N-page file" plan
   therefore renders the upstream answer as it is. Upstream answers are small by prompt: research under 150 words
   (`agents/llm.py:43`) and knowledge 2-4 sentences (`:17`). Fixing cause 1 alone would still give about a 1-page PDF.
   Agy runs 2368 and 2402 (cover letter) show the same shape.
3. **A dependency's file is lost.** `make_file` passes `results[d]['answer']` (the "Created **x**" line), never the
   dependency's `created_files` or stored spec.
4. **Nothing records what the request asked of the file.** No page target, diagrams, images, font or monochrome. The
   `SHAPES['default']` prompt says "a short document, 2 to 8 sections". `BLOCKS` has no diagram type (`spec.py:17`),
   `FETCH_KEYS` strips images, and the themes are clean/dark/warm (clean has blue headings `0B3D91`). PDF fonts are
   Helvetica/Times; `TRACEGRAPH_PDF_FONT` only draws non-Latin text. CLI engines ignore `max_tokens`
   (`engines/cli.py:122`). Step 1's spec put its diagrams into notes as "Suggested timeline diagram ..." and drew a
   hierarchy as `├──` text in a code block. Re-rendering that spec gives 5 pages.
5. **Format is read from the step text, not the prompt.** `request = query if len(subtasks) == 1 else step text`
   (`pipeline.py:831`). "No format was named" is printed even though `detect_format(prompt) == 'pdf'`.
6. **The answer hides every miss.** The template merger prefixes `**agent**:`, and `answer_for()` reports only
   warn-rule failures. The spec notes that say "not fetched" never reach the user.

### 1.2 Routing and answers (all verified; qids from `data/tracegraph.db`)

| Area | Evidence | Impact |
|---|---|---|
| Dependency context cut to 500 chars | `CONTEXT_CHARS = 500` (`pipeline.py:35`). 13 dependent LLM steps had upstream answers over 500 chars. 1580.2/1580.3 said "the track list ... was cut off"; 1581.3 said "The original PDF is unavailable here". | high |
| Slow LLM plan for single questions | `needs_llm_plan` returns True when `len(parts) < 2`, and `MAYBE_MULTI` matches the "it" in "is it". 1582 "What time is it in New York?" spent 19,417 of 19,421 ms planning; 1583 spent 3,674 ms. | medium |
| Keyless follow-ups | The keyless planner ignores context. Keyless multi-turn is 10/29 (eval `abccf6ab8f`), and 13 of the 19 failures are "unclear" clarifies ("and in GBP?", "make it 500", "When was she born?"). 381 split a follow-up at "and" and gave two identical clarifies. | high |
| No "can't do this" outcome | "What's the stock price of Apple?" went to knowledge at 0.99-1.0 and returned an Apple Watch article (1746, 2054, 2430). `gate.cant_do` returns None for the rail-ticket paraphrases. Honesty is 14/22 on agy (`59dbcdc749`) and 9/24 keyless. | high |
| Keyless parsers answer wrong with confidence | `solve_math`: "Divide 240 by 8" gives 240; "12 percent of 850" gives 850; "Multiply 13 by 11" gives 13; "square root 2025" gives 2025; "sqrt 144" raises. "how many yen is 200 British pounds" converts in reverse. `verify` runs only on LLM answers. | high |
| Gate overrides | `confirmed('weather', 'Madrid ... next year')` is True, so current weather is shown. "vienna weather, prague weather" asks "Which city?". A currency named by description ("the currency of Brazil") asks which currency. "local time in sydney australia" gives "Your local time". | high |
| Descriptions not resolved | "Weather in William Shakespeare's birthplace" never reaches the LLM planner, so weather asks "Which city?". dag is 9/16 on agy and 1/16 keyless. | medium |
| Splitting on "and" | "Add 120 and 380 dollars, then convert the result to EUR" splits into 3 parts. "Weather in Vienna and Prague" and "Convert 100 USD to EUR and GBP" are not split. The step text "from subtask 0" leaks (dag-sum-then-convert), and `resolve_step` pasted answer prose into a place name (dag-sydney-ahead). | medium |
| Confident pick overridden by clarify | Run 58: knowledge 0.99, clear 0.22, so it clarified. "hey!" clarifies 6/6 (chat 1.0, clear 0.19). The full rerun shows 'clear' AUC 0.84, close to confidence (0.86), so this is low impact apart from chat and the missing "unsupported" outcome. | low-medium |
| Advice questions go to code | 383/384 keyless gave "No summary found" and "No accepted Stack Overflow answers". 385 on agy went to code at 0.65. | medium |
| Unsourced time-sensitive facts | 380 and 59: cutoff ranks and "top college" stated as fact with `source: None`. | medium |
| Agent labels leak into files | 1585: spec `96928a23d074` and its 4 conversions have paragraphs "**weather**: ..." and "**currency**: ...". | low |
| Silent 500-char query cut | `Chat.tsx send()` uses `slice(0, 500)`, and `app.py:80` and `:660` use `[:500]`. Long file-spec prompts lose the constraints at their end. | medium |
| Latency and cost on CLI engines | Trivial agy answers cost about 12.4K input tokens and 6-20 s. 1581 (codex) took 195.6 s and 255K input, and its step answers start "I'll check ...". | low |

### 1.3 Harness

Latest scores: agy general 165/210 (`59dbcdc749`, before 7 routing commits; paraphrase 2/12, multi-turn 15/22, dag
9/16, honesty 14/22); agy create 24/25 (`dea43f8767`); keyless 144/235 (`abccf6ab8f`, current).

- **The 2741 failure can't be written as a case.** `CASE_KEYS` has no mode, agent or style, and `run_attempt` never
  passes `**chat`. All of the last 200 eval runs are balanced with no forced agent.
- **Routing checks are weak.** `check()` only tests set membership: extra agents aren't flagged, and there are no
  step, order or dependency checks. 101 of 235 cases check no agent or outcome. 92 of the 130 failure reasons in
  `abccf6ab8f` are "answer does not match" regex misses.
- **No routing-only mode.** Every case runs agents and live APIs. Agy p50/p95 is 6,987/34,797 ms.
- **No confusion matrix, per-agent precision/recall or failure stage.** Jev's top pick was right on 64/66 (keyless) and
  47/47 (agy) single-step cases. The losses happen after Jev: the clarity veto and gate overrides.
- **Judge.** 0 of 25 stored evals ran it, yet 13 of the 14 j-* cases pass anyway. That inflates the score by at most
  about 6 points.
- **File checks.** The largest `min_pages` in the suite is 1. There are no image, diagram, font, grayscale or
  file-count checks, and `check_file` scores only the first file in the expected format.
- **Real traffic.** There is no path from real runs to cases: labels has 0 rows, and there is no "promote run" action.
- **CI.** `evals/baseline.json` doesn't exist, so the regression gate does nothing. Keyless totals of identical runs
  moved 142 → 145 → 144, so per-case gating would flap.
- **Other gaps.** 38 paraphrases, all hand-written. `max_ms` on only 10 cases, and no token budgets. No suite hash.
  The honesty regex is copied into 18 cases.

---

## 2. Workstreams

Each workstream lists goals, design, exact files, contract changes and acceptance tests. Section 5 lists the TypeScript
additions for all workstreams in one place, and they go at the end of `web/src/protocol.ts`.

### A. Routing correctness

**Goals.**
- `@agent` binds to one step.
- One explicit decision policy that records a trace of its rules.
- An honest "unsupported" outcome.
- Keyless follow-ups that carry the previous turn's frame.
- Keyless parsers that report what they didn't use.
- Semantic multi-intent splitting.
- No LLM planner for single questions.

#### A1. `@agent` binding (pipeline.py, planner.py)

The forced agent binds to exactly one step, the **bound step**:

- **Plan of one step:** that step (today's behaviour).
- **`forced == 'create'`:** the last step where `planner.is_file_request(text)` or `create_agent.asks_for_file(text)` is
  true. If there is none, the last step, whose create request is then the **user's full query** (see C1).
- **Any other forced agent:**
  1. Route every independent step's raw text with `jev_route` in parallel. These results go into the route cache and are
     reused by `route_jev`.
  2. Bind the step with the highest `probabilities[forced]`. Ties go to the earlier step.
  3. If that probability is below 0.05, bind step 1 and add the trace note `forced agent fits no step well`.

Only the bound step goes through `route_forced`. Every other step goes through `route_jev`, with `review` (now
`policy.apply`) and the research-mode preference. `forced: true` appears only on the bound step. A new routed field
`bound: true` is set on it too.

`planner.plan(..., forced: str | None = None, web: bool = False)` gains two keyword arguments:

- **`forced`:** when set and the LLM planner runs, `prompt_for` appends: `The user picked @{forced}. Plan exactly one
  step for it; if the request also needs content gathered first, make that a separate earlier step with no file, format,
  page, font, style or image instructions in its text.`
- **`web`:** when False, the planner system prompt gains `No agent can search the web or fetch images.`, so it never
  plans "find images with URLs".
- **Keyless:** no change to the plan itself. The binding rule above does the work.

#### A2. One decision policy with a trace: new `jevrouter/policy.py`

`review()` moves out of the `_handle` closure into a pure, synchronous module. The meanings lookup stays async in the
pipeline and runs after `apply` as the last rule.

```python
# jevrouter/policy.py
from dataclasses import dataclass, field

@dataclass
class StepCtx:
    text: str                      # what the agent will receive (after prepare)
    ctx: str                       # dependency context ('' when none)
    tid: str
    offered: dict[str, str]        # agents offered to Jev for this run
    runnable: set[str]             # agents in the run's registry
    forced: str | None             # the @agent, only when this step is the bound step
    mode: str                      # quick | balanced | deep | research
    has_engine: bool
    web: bool                      # the run's engine can search the web
    attached: bool
    has_context: bool              # earlier chat turns exist
    depends_on: list[str]
    frame: dict | None             # previous turn's frame (A4), keyless follow-ups only

@dataclass
class Decision:
    agent: str
    pick: str
    reason: str
    note: str | None = None        # answer text decided by the policy (guards, can't, questions)
    note_ok: bool = False
    assumption: str | None = None  # one-line stated assumption shown before an LLM answer (A6)
    trace: list[dict] = field(default_factory=list)   # [{rule, agent, why}], in order applied

RULES = ('blocked', 'blocked_dependency', 'unsupported', 'cant_do', 'forced', 'create_demote', 'file_intent',
         'keyless_contract', 'confirmed', 'attached_file', 'refers_back_file', 'advice', 'time_sensitive',
         'clarify_policy', 'missing_slot', 'mode_research', 'ambiguous_term')

def apply(d: dict, s: StepCtx) -> Decision: ...
```

Rules run in this order. The first rule that returns a final decision stops the chain, and every rule that looked at
the step appends to `trace`:

1. **blocked:** unsafe ≥ BLOCK_AT.
2. **blocked_dependency:** decided in the pipeline, not here; it gets the same trace entry.
3. **unsupported** (new, A3).
4. **cant_do:** the existing regexes, now a fast path in front of 3.
5. **forced:** the bound step takes the forced agent.
6. **create_demote:** a create pick with no file or format goes to the runner-up.
7. **file_intent:** `wants_file` promotes to create.
8. **keyless_contract** (A5): a keyless pick is kept only with full coverage.
9. **confirmed / attached_file / refers_back_file:** today's clarify rescues, unchanged.
10. **advice** (B6).
11. **time_sensitive** (B6).
12. **clarify_policy** (A6).
13. **missing_slot:** `gate.question`.
14. **mode_research:** knowledge or report goes to research when `mode == 'research'`, `web` is true and `'research'`
    is in `runnable`.
15. **ambiguous_term:** the async meanings lookup in the pipeline.

`routed` events and stored tasks gain `trace` (the list), plus `signals` and `assumption` when set. Older clients
ignore them.

#### A3. The "unsupported" outcome (jev.py, gate.py, policy.py, config.py)

- `config.GUARDS = ['clarify', 'blocked', 'unsupported']`.
- New Nouls go into the same `system_one` call as the route in `jev.questions()`. The cost is marginal input tokens,
  and the call count doesn't change.

```python
SIGNALS = {
  'live':      'The request needs live or real-time data (a current price, a score, a status right now) or a prediction of the future.',
  'action':    'The request asks the assistant to do something in the world (book, buy, send, order, call, schedule, remind, pay).',
  'personal':  "The request needs the user's own private information (their account, location, calendar, contacts, files not attached) or another private person's details.",
  'described': 'The request names a place, currency, number or person only by a description that must be looked up first (e.g. "the capital of Switzerland", "Shakespeare\'s birthplace").',
}
```

`route_one` returns `signals: {live, action, personal, described}` (rounded to 2 places).

`policy` rule `unsupported` fires in these cases:

- **action ≥ 0.7:** always, unless the pick is `create` with a file asked for. The note is from `gate.unsupported_text('action', text)`.
- **personal ≥ 0.7:** the note says the assistant can't see private data.
- **live ≥ 0.7 and no web engine** (`not s.web`), and the pick is not weather, time or currency (their live APIs are
  real). The note says live data isn't available here. With a web engine, the step goes to `research` instead
  (trace `mode_research`).
- **The step asks for a future date for weather beyond 16 days, or a year ≥ current + 1:** the note from
  `gate.future_note`.

The answer is ok=True. `evals.OUTCOMES` gains `'unsupported'` (Workstream D updates honesty cases that expect
`answer`). The UI colour token is `--agent-unsupported` (grey-amber).

**Thresholds** are `UNSUPPORTED_AT = 0.7` and `DESCRIBED_AT = 0.6` in config. They are tuned on the dev split with
route mode (D3) and must hold on holdout.

**Two guards** protect answerable questions: "How do I book a flight cheaply?" and "What was Apple's stock price in
2010?" must still answer, and both are cases.

#### A4. Follow-ups (planner.py, pipeline.py, store.py stays unchanged: the frame lives in the run record)

- Each finished step stores a `frame`: `{agent, slots}`. The slots come from the keyless parser (`amount`, `from`,
  `to`, `city`, `zone`, `expr`, `date`) or, for LLM agents, `{topic: <first noun phrase>}`. It is saved on the task, so
  `rec.tasks[i].frame` is persisted with the record.
- **Keyless follow-up.** When the session has an earlier turn and the new query is elliptical (≤ 6 words, or it
  starts with and/what about/how about/make it/in/to, or it has a deictic word and no slot of its own), the new
  `planner.fill_from_frame(query, frame) -> str | None` rewrites it from the previous turn's last frame. Examples:
  "make it 500" after currency becomes "Convert 500 EUR to USD"; "and in GBP?" becomes "Convert 50 EUR to GBP";
  "And what time is it there?" after weather Lisbon becomes "What time is it in Lisbon?". A rewrite is kept only when
  the target parser confirms it (`gate.confirmed`). An explicit topic switch ("switching topics", a new named entity
  for another agent) inherits nothing.
- **Keyless splitting of follow-ups.** When the session has turns and the query has a deictic (this/that/it/there/these),
  `candidate_split` does not split.
- **Jev sees the previous turn.** With an engine off, `route_jev` sends Jev
  `text + '\n\nPrevious turn: ' + q[:200] + ' -> ' + a[:200]` when a frame exists and the text is elliptical. The route
  cache key gains a hash of that block: `route_cache.key(text, criteria, extra=prev_hash)`.
- **Stickiness.** When Jev's top pick is not the previous agent, the previous agent's probability is within 0.15 of it,
  and the new text names no slot for the other agent, the previous agent is used (trace `frame`).

#### A5. Keyless parser contracts (agents/tools.py, gate.py)

Each keyless parser gains a coverage function:

```python
@dataclass
class Parse:
    slots: dict            # parsed values
    unused: list[str]      # constraints in the text it could not use: 'future date', 'second place', 'source time', 'described currency'
    numbers_used: int
    numbers_seen: int
    @property
    def full(self) -> bool: return not self.unused and self.numbers_used >= self.numbers_seen

def parse_for(agent: str, text: str) -> Parse | None   # gate.py; None for non-keyless agents
```

- `gate.confirmed(agent, text)` returns `parse_for(...).full`.
- **Missing slot:** `question` is asked only when a slot is really missing, not when it is given by description.
- **Slot given by description** (`described ≥ DESCRIBED_AT`, or `unused` contains `described currency|described place`):
  - With an engine, the step is re-planned as 2 steps: knowledge lookup, then the keyless agent with depends_on [0].
    This comes from the new `planner.expand_described(text) -> list[tuple[str, list[int]]]`.
  - Keyless, look it up in a static table `agents/tools.py: CAPITAL_OF`, `CURRENCY_OF` (about 200 countries, ISO
    4217). If it is not in the table, the answer honestly says the lookup needs an engine.
- **`solve_math`** gains word operators: "divide X by Y", "multiply X by Y", "X percent of Y", "square root (of) X",
  "sqrt X", "add X and Y", "subtract X from Y", "X to the power of Y". It returns None, not the first number, when the
  expression doesn't use every number in the text. `agent_math` then answers with ok=False and a question.
- **`parse_currency`** fixes the direction for "how many <dst> is <amount> <src>" and "<amount> <src> in <dst>".
- **`agent_time`** never answers "Your local time" when `find_place` or `parse_zone` finds a named place anywhere in
  the text ("local time in sydney australia").
- **Keyless verify.** `pipeline.run()` calls `verify.coverage(agent, text, answer) -> dict | None` for keyless math and
  currency answers. The numbers and currency codes in the text must appear in the answer, in the right direction. A
  failure sets `checks.verified = 'mismatch'`, so the answer is shown with a warning, never silently wrong.

#### A6. Clarify policy (jev.py, policy.py)

- **`jev.decide`:** a chat pick is exempt from the clarity veto. `test_pipeline.py:169` changes to expect `chat`.
- **clarify_policy rule.** When Jev says clarify (unclear or low confidence) but the top pick is an LLM agent (knowledge,
  research, report, chat, code) with P ≥ 0.9, an engine is on and no slot is missing, answer with that agent. Set
  `assumption = "Assuming you mean <pick's option text> about \"<text[:60]>\"."`; the merger prints it first. Keyless,
  keep clarify.
- **Clarify wording.** The clarify text never names an agent; the existing `AGENT_NAME` check stays. For a create step
  with no topic, the keyless clarify text asks "What should the <format> be about?", so cr-nothing-to-put and
  cr-presentation-no-topic match.

#### A7. Planner speed and splitting (planner.py)

- **MAYBE_MULTI** drops the dummy-subject forms: `(?<!\bis )(?<!\bwill )(?<!\bwas )\bit\b(?! is\b)`, plus explicit
  exclusions for "what time is it", "is it", "will it" and "was it".
- **`needs_llm_plan`:** `len(parts) < 2` no longer means True. It returns True only for:
  - context, files or a long query;
  - `depends(parts)`;
  - a second `?`, or a `;`;
  - a file verb plus a format with other content (`split_file_request` returns None but `is_file_request` is true and
    `len(words) > 8`);
  - `described ≥ DESCRIBED_AT`, which is known only after routing, so this case is handled by A5 re-planning.
- **Coordination.**
  - `candidate_split` copies the head predicate onto verbless conjuncts: "Weather in Vienna and Prague" gives
    ["Weather in Vienna", "Weather in Prague"]; "Convert 100 USD to EUR and GBP" gives ["Convert 100 USD to EUR",
    "Convert 100 USD to GBP"].
  - It never splits between the operands of add/multiply/subtract/between.
  - "Add 120 and 380 dollars, then convert the result to EUR" becomes 2 steps with depends_on [0].
- **`parse_steps`** replaces `\b(?:subtask|step)\s*\d+\b` with "the previous answer". SYSTEM forbids index references.
- **`resolve_step`** substitutes only entities, numbers and units. Its output is rejected (the original step text is
  kept) when it is more than 2.5 times the input length or contains a sentence taken from an upstream answer (a shared
  8-word shingle).
- **Planner restates constraints.** SYSTEM adds: `A file step's text must restate every format, length, page or slide
  count, style, colour, font, image and diagram requirement from the user's query.` Workstream C does not rely on this
  (it reads the original query), but it keeps the step text honest in the UI.

**A acceptance tests** (new `tests/test_policy.py`; additions to `test_planner.py`, `test_pipeline.py`, `test_parsers.py`,
`test_gate.py`):

- **2741 plan.** FakeEngine planner returns `['Research X in depth', 'Create a 12 page PDF on X from it']`, deps
  `[[],[0]]`, with agent=create, mode=research and a web engine:
  - tasks[0].agent == 'research', with no `forced` or `bound` on it;
  - tasks[1].agent == 'create' with forced and bound;
  - exactly one created file, format pdf.
- **`@weather`** "weather in Paris and convert 100 EUR to INR": weather is bound on step 1, and step 2 is currency via Jev.
- **Single-step `@create make a PDF about X`** is forced as before. The existing `test_forced_agent_*` and
  `test_forced_create_works_keyless` tests still pass.
- **Policy table test:**
  - forced weather + unsafe gives blocked;
  - forced create + research mode + 2-step plan gives research then create;
  - a confirmed weather parse + "next year" gives unsupported;
  - "Make me a presentation" asks for the topic;
  - every routed event in test_pipeline has a non-empty trace, and the trace's last entry has `agent == tasks[i].agent`.
- **Planner speed:** `needs_llm_plan` is False for "What time is it in New York?", "Will it rain in Mumbai tomorrow?"
  and "Is it safe to eat raw eggs?".
- **Splits:** "Weather in Vienna and Prague" gives 2 weather steps; "Convert 100 USD to EUR and GBP" gives 2 currency
  steps; "salt and pepper" stays 1 step. No `parse_steps` output matches `\bsubtask \d`.
- **solve_math:** the inputs in 1.2 give 30, 102, 143, 45, 500 and 12. An unparseable input returns None.
  "how many yen is 200 British pounds" converts GBP→JPY.
- **Keyless session:**
  - "Convert 50 EUR to USD" then "make it 500" gives currency with input "Convert 500 EUR to USD";
  - "Weather in Lisbon" then "And what time is it there?" gives Lisbon time;
  - "Multiply 13 by 11" then "Switching topics: is it raining in Oslo?" inherits nothing;
  - the route cache stores separate entries for "make it 500" after different first turns.
- **decide:** `decide(chat 1.0, clear 0.12)` gives chat. "asdfghjkl", "What's the rate?", "How much is a pound?" and
  "Mercury" still clarify.

### B. Response quality

**Goals.** Every part of the request is covered or honestly reported. Dependent steps see enough context. No internal
labels reach users or files. Time-sensitive claims are sourced or hedged.

#### B1. Dependency context (config.py, pipeline.py prepare)

- **Budget.** `config.DEP_CONTEXT_CHARS = 6000`, shared across a step's dependencies, and used only for LLM agents.
  - Each dependency gets `budget // n`. Leftover budget from short answers goes to the longer ones.
  - An answer is cut at the last blank line or line end before its limit and marked
    `[truncated N chars]`.
  - Failed dependencies keep 200 chars.
- **What `resolve_step` sees.** It still gets at most 1,500 chars per dependency; the rewrite needs facts, not whole
  answers.
- **Keyless agents** still get no context, as today (`pipeline.py` `step()`).
- **Attached documents.** When a dependency's agent was `document` or `data`, the dependent LLM step also gets up to
  2,000 chars of the attachment's extracted text, from the passage the dependency's `source` names, or else the
  beginning of the text.

#### B2. Merger (merger.py)

New signature. The old one is kept as a wrapper, so existing callers and tests work:

```python
async def compose(query: str, steps: list[StepOut], emit_delta, engine=None, style='default', exact=False) -> dict
# StepOut = TypedDict: {tid, text, agent, answer, ok, files: list[CreatedFile], caveats: list[str], assumption: str|None}
# returns {answer, engine, claude_in, claude_out, kind, caveats: list[str], primary_file: str | None}
```

- **Template output drops `**agent**:`.**
  - One answer passes through.
  - Several answers become one line each (`- <answer first paragraph>`) when every answer is one line.
  - Otherwise each answer goes under a short bold heading made from its step text (`topic_title(step)`), never the
    agent name.
  - `concat` stays for old callers.
- **Duplicate guard answers collapse.** Identical clarify or unsupported texts appear once. If every step would
  clarify, one question about the whole query is asked: `gate.clarify_text` over the query.
- **Files.**
  - When any step made files, `primary_file` is the file in the requested format (`brief.format`) from the last file
    step, or else the last file made. The answer starts `Created **name.pdf**, 12 pages.`
  - Other files are listed under "Working files:".
  - A `create` step's own "Created ..." line is not repeated.
- **"What I couldn't do".** Caveats come from:
  - each step's `caveats` (create conformance failures, C8);
  - spec notes matching `\b(not (fetched|included|checked|verified)|could not|couldn't|no (image|source)s?)\b`;
  - `unsupported` steps.

  They are shown as a `What I couldn't do:` bullet list at the end, capped at 6 bullets, and returned as `caveats`.
  The LLM merger gets the caveats in its prompt, with the instruction "List these limits plainly at the end." The
  `merged` SSE event and `rec.merged` gain `caveats` and `primary_file`.
- **Preambles.** Before the merge, each LLM step answer loses lines matching
  `^(?:I('ll| will)|Let me) (check|look|search|find|research)[^\n]*\n+`, applied only at the start of the answer
  (`merger.strip_preamble`).
- **Assumptions.** A step's `assumption` is printed as the first line of its part.

#### B3. Files never contain internal labels (agents/create.py)

`answer_spec` strips `^\*\*[a-z_]+\*\*:\s*` at line starts before `from_markdown`, and drops lines matching
`^Created \*\*.+\*\*, ` and `^No format was named`. `from_answers` treats an answer that is only a "Created ..." line as
empty (see C1).

#### B4. Query length (app.py, Chat.tsx)

- `config.MAX_QUERY_CHARS = 4000`. `app.py` lines 80 and 660 stop cutting to 500 characters.
- A longer query gets `400 {"error": "Your message is 4,213 characters; the limit is 4,000.", "limit": 4000}`.
- The `hello` and `config` events gain `limits: {query_chars: 4000}`.
- The web input shows a counter above 3,500 characters and disables send over the limit. Jev's own input is budgeted
  separately: route text is cut at 2,000 characters, the planner still sees the full text, and the trace notes the cut.

#### B5. Research that feeds a file (agents/__init__.py, agents/llm.py)

- **Context flag.** New contextvar `agent_registry.FEEDS_FILE: ContextVar[bool]`. `pipeline.run()` sets it for a step
  that a create step depends on when that create step's brief asks for `pages ≥ 3`, `slides ≥ 8`, images or diagrams.
- **Notes prompts.** With the flag set:
  - `research` uses NOTES_RESEARCH: "search the web, then write research notes for a document: 600-1,200 words of
    facts, dates, names and figures as terse bullets under headings, each claim with its source URL; list up to 8
    image ideas as `IMAGE: <search query> | <caption>`";
  - `knowledge` and `report` use NOTES_KNOWLEDGE, with the same shape and no web: "from your own knowledge; say so in
    the first line".
  - `max_tokens` is 4096.
- **Image ideas.** The create step reads the `IMAGE:` lines as figure seeds (C5).

#### B6. Honesty cues (policy.py, gate.py)

- **advice rule.** `gate.ADVICE`:
  `\b(should I|what should I (learn|do|choose|pick)|is it worth|which is better for me|X or Y for)\b` plus a decision
  frame ("either ... or", "for the <role> role"), with no code artifact asked for (`write|fix|debug|example|snippet|
  function|error` absent). It routes to chat on an engine and to knowledge keyless. A keyless miss answers "This needs
  an engine to answer well: it is advice, not a lookup."
- **time_sensitive rule.** `gate.TIME_SENSITIVE`:
  `\b(cut-?off|rank(ing)?s?|admission|fees?|top college|this year|latest|current(ly)?|20\d\d)\b` plus a named
  institution or entity. It goes to `research` when a web engine is on. Otherwise the step's answer must end with a
  hedge: the policy sets `caveat = "Figures like these change every year; check the official source."`, which is added
  to the step's `caveats` and printed by the merger.

#### B7. Keyless dead ends (agents/tools.py)

`agent_knowledge` and `agent_code` misses no longer print "No summary found for <whole sentence>". The new text is
`I couldn't find a reference answer for that. A question like this needs an LLM engine; choose one in Settings.`
It is ok=False, so the outcome is `error`, never a silent pass.

**B acceptance tests:**

- **prepare():** a 5,000-char dependency passes at least 4,000 chars to an LLM agent and none to a keyless one.
- **1580 replay** (fixture PDF in `evals/fixtures/`): no step answer contains "cut off" or "unavailable here".
- **compose():**
  - the output contains no `**create**:` or `**weather**:`;
  - two identical clarifies give one;
  - a run with a .md working file and a pdf primary names the pdf first with its page count.
- **Caveats:** a create caveat "asked for 12-13 pages, made 9" appears in `caveats` and in the answer.
- **B3:** the cr-answer-pdf variant after "weather in Paris and convert 250 EUR to INR" gives `contains ['Paris','INR']`
  and `not_contains ['**weather**','**currency**']`.
- **B4:** POST /api/ask with 700 chars stores all 700. 4,100 chars gives a 400 that names the limit.
- **Preamble:** no stored step answer in an agy targeted run starts with `I('ll| will) (check|look)`.
- **Advice paraphrases:** 3 variants of 384 expect_agents ['chat','knowledge'] and must_not_match
  `Stack Overflow|No summary found`.
- **380 query with session context:** the answer cites a URL or contains "official".

### C. Long documents

**Goals.** A request's page or slide count, diagrams, web images, monochrome look, font and format are honoured, or
honestly reported. The file is written from real content (dependency notes, the engine), never from a status line.

#### C1. The brief: new `jevrouter/create/brief.py`

```python
@dataclass
class Brief:
    format: str | None = None              # detect_format over the original query, then the step text
    pages: tuple[int, int] | None = None   # "12-13 page", "twelve pages", "10 page report", "at least 5 pages"
    slides: tuple[int, int] | None = None  # "20-slide deck", "10 slides"
    theme: str | None = None               # 'mono' for black and white|black & white|b&w|b/w|monochrome|grayscale|greyscale|black-and-white; else dark|warm per THEME
    font: str | None = None                # the family the user named, normalised ('anthropic sans', 'inter')
    images: bool = False                   # images|photos|pictures|figures|illustrations
    image_source: str | None = None        # 'web' when "from the web|online|sources"
    diagrams: bool = False                 # diagram(s)|flowchart|timeline|tree|hierarchy|chart(s) (charts also stay charts)
    diagram_kinds: list[str] = field(default_factory=list)   # subset of ('timeline','tree','flow')
    words: int | None = None               # "2000 words"
    capped: bool = False                   # pages or slides above MAX_PAGES/MAX_SLIDES were capped

MAX_PAGES = 40
MAX_SLIDES = 40
WORDS_PER_PAGE = 420   # A4, 10.5 pt, 2 cm margins, mixed prose/tables; measured start value (2741 step-1 spec: 1,889 words -> 5 pages ~ 380/page with 4 tables), recalibrated by C3's loop
KNOWN_FONTS = ('anthropic sans', 'anthropic serif', 'inter', 'roboto', 'open sans', 'lato', 'source sans', 'noto sans',
               'helvetica', 'arial', 'times new roman', 'georgia', 'garamond', 'calibri', 'ibm plex sans', 'dejavu sans')

def parse_brief(text: str) -> Brief
def merge(query: Brief, step: Brief) -> Brief   # query wins for format, theme, font, pages, slides; OR for images/diagrams
```

- **Numbers.** Page and slide numbers accept digits, the words one through forty, ranges (`12-13`, `12 to 13`,
  `12–13`), and "N or M". A single N means the range (N, N).
- **Font extraction** is case-insensitive:
  1. Try each `KNOWN_FONTS` name as a whole phrase.
  2. Otherwise take up to 3 words immediately before `\bfont\b` or after `\bfont (?:called|named)\b`, after dropping
     the filler words `also use using with in text the a an and`.
  - Test: the 2741 prompt gives `font == 'anthropic sans'`, not "white text also anthropic sans".
- **Where it is applied.** `pipeline.make_file` builds
  `brief = merge(parse_brief(query), parse_brief(step_text))` only for the run's **primary file step**: the last create
  step, or the bound step when forced. Other create steps use `parse_brief(step_text)` alone.
- **Default format.** When the primary step has no format and the query names one, the query's format is used.
- **"No format was named"** is printed only when `brief.format is None` **and** `detect_format(query) is None`.

`create_agent.Job` gains fields (defaults keep old callers working):

```python
brief: Brief | None = None
dep_files: list[tuple[dict, dict]] = field(default_factory=list)   # (CreatedFile meta, stored spec) made by dependency steps
role: str = 'primary'                                             # 'primary' | 'working'
http: object | None = None                                        # aiohttp session for assets (C5); None disables images
```

#### C2. How `make()` picks its path (agents/create.py)

The new order, from first to last:

1. **`dep_files` non-empty.** Start from the dependency's stored spec.
   - If the brief asks for no more than the spec holds, meaning no page target above its rendered pages + 1 and no
     images or diagrams it lacks, convert or re-render at 0 tokens (`source='convert'`).
   - Otherwise use it as the seed outline for C3 (`source='llm'`).
2. **`job.deps`, and the brief is a plain conversion.** A plain conversion is all of: no pages ≥ 3, no slides ≥ 8, no
   images, no diagrams, no words, and the request matches "put/turn/make that/it/the results into/in a <format>" or has
   no topic. Use `from_answers` at 0 tokens, as today.
   - An answer that is only a `^Created \*\*.+\*\*` line counts as empty.
   - Keyless with an empty answer is an honest failure, never a 1-page file of a status line.
3. **`job.deps`, the brief asks for more, and an engine is on.** Go to C3 with the dependency answers as source notes
   (up to `CREATE_CONTEXT_CHARS`, raised to 12,000 for C3 only).
4. **`job.deps`, the brief asks for more, keyless.** Render what `from_answers` gives, then add a caveat:
   `asked for 12-13 pages; made N from the earlier answer; writing more needs an engine.`
5. The remaining existing branches (conversation, tables, previous answer, engine) run unchanged, except that
   `from_engine` routes to C3 whenever `brief.pages ≥ 3`, `brief.slides ≥ 8`, `brief.words ≥ 1200` or
   `brief.diagrams`/`brief.images` is set.

#### C3. Long-document writer: new `jevrouter/create/longdoc.py`

```python
async def write_long(job: Job, engine, jev, fmt: str, brief: Brief, mode: str, *, seed: dict | None = None) -> Made
```

- **Budget.**
  - `target_words = mean(brief.pages) * WORDS_PER_PAGE`, minus 0.4 page for each planned diagram and 0.35 page for
    each planned image.
  - For slides, `target = mean(slides)` sections, one per slide.
- **Call 1: outline** (effort `medium`, schema `OUTLINE_SCHEMA`):
  `{title, subtitle, sections: [{heading, level, words, blocks_hint: [paragraph|bullets|table|chart|timeline|tree|flow|figure]}]}`.
  - The prompt includes the request, the brief as plain lines ("Length: 12-13 pages, about 5,000 words"; "Include at
    least 2 diagrams: timeline, tree"; "Include 4-6 figures: give each a Wikimedia Commons search query and caption"),
    the notes and context (≤ 12,000 chars), and `seed` headings when given.
  - Code then rescales `words` so the sum equals `target_words`, and caps each section at 900 words.
- **Calls 2..n: sections**, in batches of about 1,500 target words (a 12-page PDF takes 4 batches).
  - Each call is effort `low`, uses the same system prompt text (so a CLI engine's warm process is reused), and follows
    the DOCSPEC section schema.
  - Each call's prompt: the outline, the batch's section headings and word targets, the notes, and the brief lines for
    diagrams and figures that fall in the batch.
  - Batches run with concurrency 2 on API engines and 1 on CLI engines.
- **Fit loop.** At most 4 renders and at most 1 extra LLM call:
  1. Render and count pages with pypdf, or slides.
  2. Under the minimum: one top-up call asks for `(lo - pages) * WORDS_PER_PAGE` more words, split across the thinnest
     sections.
  3. Over the maximum: code drops the lowest-priority blocks first (trailing paragraphs of the longest sections, then
     `page_break`s).
  4. Within range: stop.
- **Caps.** At most `2 + ceil(target_words / 1500) + 1` LLM calls for the file. The total `max_tokens` requested is
  `min(32000, 1.6 * target_words * 1.4)`. CLI engines ignore `max_tokens`, so size is enforced through the word targets
  in the prompts and code trimming. This is documented in `engines/cli.py`, which isn't changed.
- **Accounting.** `Made` and the CreatedFile meta gain
  `phases: [{phase: 'outline'|'sections'|'topup'|'assets'|'render', calls, llm_in, llm_out, ms}]`.
- **Keyless.** `write_long` is never called without an engine (C2.4).

#### C4. Native diagrams: new `jevrouter/create/diagram.py`, and changes to spec.py, render.py and rules.py

New block types go into `BLOCKS` and `DOCSPEC_SCHEMA`. All are bounded and non-recursive:

```
timeline: {type:'timeline', title, events:[{date: str, label: str (≤ 80 chars)}]}               # 2..30 events
tree:     {type:'tree', title, nodes:[{id: str, parent: str|'' , label: str (≤ 40)}]}          # 2..40 nodes, ≤ 4 levels, one root
flow:     {type:'flow', title, nodes:[{id, label}], edges:[{from, to, label: str}]}            # 2..12 nodes, no cycles (normalize drops back edges)
figure:   {type:'figure', query: str (≤ 100), caption: str (≤ 200)}                             # model-written; resolved by assets.py
page_break: {type:'page_break'}
```

`image` is an **internal** block, never accepted from the model: `{type:'image', asset: <sha256 hex>, caption, credit}`.
`normalize` drops any `image` block whose `asset` is not in the local asset cache (rule X6).

- **Layout.** `diagram.py` computes positions once and returns a `Layout` of boxes, lines and labels in points. Text
  is measured with `reportlab.pdfbase.pdfmetrics.stringWidth` and wrapped to box widths.
  - timeline: a horizontal axis. With more than 8 events it becomes a vertical list, with labels alternating sides.
  - tree: a top-down layered layout, with sibling widths from their subtree leaf counts.
  - flow: left-to-right layers from a topological sort.
- **Backends.**
  - PDF: `reportlab.graphics.shapes.Drawing` (vector), scaled to the frame width, kept together with its title.
  - PPTX: native autoshapes and connectors.
  - DOCX: a Pillow PNG at 200 dpi with alt text equal to the labels joined.
  - MD: a ```` ```mermaid ```` block plus a table fallback.
  - XLSX: a table.
- **Colour.** Diagrams never rely on colour: fills are the theme's `stripe`, strokes `text`, and labels `text`.
- **Charts in the mono theme** use outline-only bars with hatch patterns and dashed or dotted lines (`render.py`
  `_pdf_chart`, and pptx/xlsx chart styles).
- **Rules.** V4 extends to count diagrams. The new V9 checks `diagrams drawn ≥ brief minimum` (2 when "multiple
  diagrams", 1 when "diagram").

#### C5. Licensed web images: new `jevrouter/create/assets.py`

```python
ALLOWED = ('public domain', 'pd', 'cc0', 'cc by', 'cc by-sa')   # prefix match on LicenseShortName, case-insensitive; any 'nc' or 'nd' rejects
MAX_IMAGES = 8
MAX_BYTES = 3_000_000
MAX_PIXELS = 25_000_000
TYPES = ('image/jpeg', 'image/png')    # SVG, GIF, WebP, TIFF rejected
CACHE = ROOT / 'data' / 'assets'       # <sha256>.png, re-encoded

@dataclass
class Credit: asset: str; title: str; author: str; license: str; license_url: str | None; source_url: str; caption: str

async def search_commons(http, query: str, limit: int = 5) -> list[dict]
async def fetch_image(http, url: str) -> bytes            # through agents.tools GuardedResolver/check_url, redirects re-checked, caps enforced while streaming
def reencode(data: bytes, *, mono: bool, max_side: int = 1600) -> tuple[bytes, str]   # Pillow: verify, strip EXIF, convert('L') when mono, PNG; returns (bytes, sha256)
async def resolve_figures(spec: dict, http, *, mono: bool, seeds: list[tuple[str, str]] = ()) -> tuple[dict, list[Credit], list[str]]
```

- **Search.** One HTTPS GET to `https://commons.wikimedia.org/w/api.php` with `action=query`, `generator=search`,
  `gsrnamespace=6`, `gsrsearch=<query> filetype:bitmap`, `gsrlimit=5`, `prop=imageinfo`,
  `iiprop=url|size|mime|extmetadata`, `iiurlwidth=1600`, `format=json`, and a descriptive User-Agent (`TraceGraph/1.0
  (local; contact: none)`).
- **Pick.** The first candidate with an allowed licence, an allowed mime, `width*height ≤ MAX_PIXELS`, and non-empty
  `Artist` (HTML stripped). Download the `thumburl`.
- **Every `figure` block** (from the model, or from B5 `IMAGE:` seeds when the model gave none) becomes an `image`
  block plus a credit line under it: `Image: <title> by <author>, <license> (<source_url>)`.
- **Credits section.** A closing "Image credits" section lists every credit.
- **Unresolved figures** are removed, and each adds a caveat `no suitably licensed image found for "<caption>"`.
- **No `http` or offline.** Every figure is removed, with one caveat: `Images weren't added: web image lookup isn't
  available here.`
- **Timeouts.** 6 s per search and 8 s per download. The whole asset stage has 30 s; on expiry the remaining figures get
  a caveat.
- **Rendering** from `CACHE` bytes only:
  - PDF: `reportlab.platypus.Image` scaled to width.
  - DOCX: `add_picture` with alt text.
  - PPTX: `add_picture`.
  - MD: a relative reference is not possible (X3). The image is dropped from MD with a caveat, and the credit line
    remains as text.
  - XLSX: not included.
- **Rules.**
  - X3 is reworded to "no remote references; images only as embedded bytes from the asset cache".
  - New **X6** (block): every embedded image is PNG or JPEG from CACHE, re-encoded, with a credit. A missing credit
    blocks the file.
  - New **V6** (warn): resolved images ≥ `min(requested, 1)` when images were asked for.

#### C6. Monochrome theme and fonts (themes.py, spec.py, render.py, new create/fonts.py)

**mono theme.** Add to `THEMES` and `spec.THEMES`:

```python
'mono': {'bg': 'FFFFFF', 'text': '000000', 'muted': '4D4D4D', 'heading': '000000', 'accent': '1A1A1A',
         'header_bg': '000000', 'header_text': 'FFFFFF', 'stripe': 'F2F2F2', 'code_bg': 'F2F2F2', 'border': '8C8C8C',
         'palette': ['1A1A1A', '595959', '8C8C8C', 'BFBFBF', '404040', 'A6A6A6'],
         'pdf_font': ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique'),
         'font': 'Calibri', 'heading_font': 'Calibri', 'mono': 'Courier New', 'patterns': True}
```

`create_agent.THEME` gains `'mono'` with the brief's regex. In mono, images are converted to greyscale (C5), and charts
use patterns (C4).

**Fonts.** New `create/fonts.py`:

```python
@dataclass
class FontChoice: requested: str | None; used: str; regular: str | None; bold: str | None; italic: str | None; embedded: bool; note: str | None
def resolve(requested: str | None, fmt: str) -> FontChoice
```

The lookup order:

1. The env `TRACEGRAPH_BODY_FONT` (a TTF/OTF path), with optional `TRACEGRAPH_BODY_FONT_BOLD` and `_ITALIC`, when its
   family name (read with `fontTools` if installed, else the file stem) matches the request, or when the request is
   None.
2. A system font whose file stem or family matches the request, from `/System/Library/Fonts`, `/Library/Fonts`,
   `~/Library/Fonts`, `/usr/share/fonts` and `C:\Windows\Fonts`. The index is built once and cached in memory.
3. The theme's built-in font.

- **PDF.** Registers the chosen TTF as `TGBody`, and `_Fonts` uses it for Latin body text too, not only for non-Latin
  text.
- **DOCX and PPTX.** Set the font name. They never embed; the note says "named, not embedded; it shows only where the
  font is installed".
- **Answer line (always present when a font was requested):** `You asked for Anthropic Sans; it isn't available to
  embed here, so the PDF uses Helvetica. To use a font you're licensed for, set TRACEGRAPH_BODY_FONT to its .ttf file
  and convert the file again (0 tokens).`
  - Make no licensing claims about the named font.
- **New V7** (warn): the requested font was used, or the fallback is named in the answer.
- **New V8** (warn): the file's theme equals `brief.theme` when set. For mono, also scan the PDF content streams: every
  `rg`/`RG` colour operand satisfies r == g == b, and every image XObject is DeviceGray.

#### C7. Page and slide conformance (rules.py)

- **New V5** (warn): pages (or slides) are within `brief.pages` (or `brief.slides`). The note reads
  `asked for 12-13 pages, made 9`.
- **Where the brief rules live.** V5-V9 and X6 are computed in `rules.verify(spec, fmt, data, brief=None)` when a brief
  is given. Old callers pass none, so those checks are skipped.
- **`RuleInfo` listing.** GET /api/rules lists the new rules.

#### C8. Answer and caveats (agents/create.py)

- `answer_for(meta, notes, brief=None)` returns the headline, the notes and the failed warn rules as today.
- `Made` gains `caveats: list[str]`. It is filled from:
  - failed V5, V6, V7, V8 and V9 results, in plain words;
  - font notes;
  - asset caveats;
  - spec notes matching the B2 caveat regex.
- `pipeline.run()` stores them as `answered.caveats` for B2.
- The CreatedFile meta gains `brief`, `font_used`, `diagrams`, `images`, `credits`, `role` and `phases`.

**C acceptance tests** (new `tests/test_create_brief.py`, `test_create_diagram.py`, `test_create_assets.py`,
`test_create_longdoc.py`; extensions to `test_create_rules.py`, `test_create_render.py` and `test_create_api.py`):

- **Briefs.**
  - `parse_brief(2741 prompt)` gives format pdf, pages (12,13), theme mono, font 'anthropic sans', images True,
    image_source 'web', diagrams True.
  - Paraphrases: '10 page report' gives (10,10); '20-slide deck' gives slides (20,20); 'in b&w' and 'greyscale' give
    mono; 'twelve pages' gives (12,12); 'use Inter font' gives 'inter'; '60 pages' gives (40,40) with capped.
- **Dependency paths.**
  - Stub engine, deps=[('Research AI', '<150-word answer>')], request '12-13 page PDF on AI': the engine is called
    (llm_out > 0), `source == 'llm'`, and the PDF has 12-13 pages. The stub returns sections at the word targets it was
    asked for.
  - deps plus "put that in a PDF": 0 tokens, `source == 'answer'`.
  - deps=[('x', 'Created **x.md**, 12 KB')] with `dep_files=[(meta, 7-section spec)]`: `source == 'convert'`, 0 tokens,
    the pypdf outline has ≥ 7 entries, and the text has no `Created **`.
  - The same deps with no dep_files, keyless: ok=False with an honest message, and no file.
- **Fit loop.** In 20 seeded runs, a stub engine returns sections at ±30% of their targets. The PDF lands in 12-13 pages
  in ≥ 19 of 20, with ≤ 4 renders and ≤ 7 LLM calls each.
- **Diagrams.**
  - For each kind × format, render and reopen: the PDF text contains every label, the PPTX shapes hold the labels, the
    DOCX has an inline picture with alt text, and the MD contains ```` ```mermaid ````.
  - A 40-node tree has no overlapping label boxes (checked on the Layout).
  - A 31-event timeline and a cyclic flow are trimmed or fixed by normalize, with fix notes.
- **Assets** (mocked `aiohttp`, no network):
  - NC and ND licences are rejected; CC BY-SA is accepted and credited;
  - a redirect to 10.0.0.1 is blocked;
  - SVG and HTML are rejected; a header claiming 30 MP is rejected before decode; a 4 MB body is aborted;
  - the output has no EXIF;
  - with mono, the PNG mode is 'L';
  - the PDF image XObject count equals the resolved figures, and the credits text is present;
  - zero hits give a caveat and no broken block.
- **Theme and font.**
  - `worst_contrast('mono') ≥ 7`.
  - A mono PDF passes the V8 grey scan, and a clean PDF fails it.
  - Brief font 'anthropic sans' with no env: the answer contains "Anthropic Sans" and "isn't available to embed here",
    and V7 is ok.
  - With `TRACEGRAPH_BODY_FONT` pointing at the DejaVu test TTF (already used by the font tests), pypdf lists it
    embedded, and Latin body text uses it.
- **Merge.** In the full pipeline test with FakeEngine, the merged answer has exactly one "Created **...pdf**, 12 pages"
  and no "No format was named".

### D. Harness

**Goals.**
- Every production failure class can be expressed as a case.
- A fast routing-only suite runs offline in CI, with a confusion matrix, per-agent precision/recall/F1, calibration and
  stage attribution.
- The judge runs in routine runs.
- Files are scored for quality.
- CI gates on each tag.

#### D1. Case schema (evals.py)

- **New case keys:**
  - `chat`: `{mode?, agent?, style?}`, also allowed per turn, validated with `app.chat_options`/`check_agent` rules;
  - `plan`: a pinned plan `{subtasks: [str], deps: [[int]]}`, replayed instead of calling the planner;
  - `expect_steps`, `expect_plan`, `forbid_agents`, `must_mention` (a list of named matchers or regexes, all required);
  - `budget` (overrides, D8).
- **Passing chat options.** `run_attempt` calls `router.submit(..., **chat)`. Research mode picks its engine the way
  `app.mode_engine` does. Cases get the tags `mode:<m>` and `forced:<agent>` automatically.
- **expect_steps:** `[{agent?: regex, not_agent?: regex, depends_on?: [int], text?: regex, args?: {amount?, from?, to?,
  city?}, format?: str, forced?: bool}]`. They are aligned in order unless `unordered: true`, in which case the best
  assignment is used (Hungarian over the pass count; n ≤ 4).
- **expect_plan:** `{min_steps?, max_steps?, file_steps?, no_fragment?: bool}`. `no_fragment` fails when a step has
  fewer than 3 words or no verb/noun head.
- **Reason codes.** `check()` returns `(reasons: list[str], codes: list[dict])`. The codes are
  `{code, step?, want?, got?, stage?}` with `code ∈ {wrong_agent, extra_agent, missing_agent, forbidden_agent,
  wrong_outcome, plan_shape, wrong_deps, answer_regex, forbidden_regex, file, judge, budget, unjudged, run_status}`.
- **Scores.** Each scored case gains `route_pass` (agent, outcome, step and plan checks) and `answer_pass` (regex,
  mention, file and judge checks; null when the case has none).
- **Named matchers.** `evals/matchers.json` maps names to regexes, for example `{"cant": "...", "dead_end": "No summary
  found|Not sure what you need|Tell me two", "font_note": "isn't available to embed|embedded", "images_note":
  "Image credits|images weren't added|no suitably licensed image"}`. An `@name` token in `must_match`, `must_not_match`
  or `must_mention` expands to it. An unknown `@name` is a CaseError.
- **Cleanup.** Replace the 18 copied honesty regexes with `@cant` and the 29 copied dead-end regexes with `@dead_end`.

#### D2. File-quality scoring (evals.py reopen/check_file)

- **New `FILE_KEYS`:**

  | Key | Type |
  |---|---|
  | `pages` | [lo, hi] |
  | `slides` | [lo, hi] |
  | `words_min` | int |
  | `headings_min` | int |
  | `images_min` | int |
  | `diagrams_min` | int |
  | `fonts` | regex on PDF /BaseFont names |
  | `grayscale` | bool |
  | `files_exact` | int |
  | `only_formats` | [fmt] |
  | `source` | answer, llm, table or convert |
  | `not_contains` | [str] |
  | `credits` | bool |

- **`reopen()` returns more for PDF:** `words`, `headings` (the outline entry count), `images` (image XObjects),
  `fonts` (BaseFont names), `gray` (the V8 scan), and `diagrams` from the stored spec's block types.
- **Always-on checks for every created file**, whether or not the case asks:
  - the body must not match `^Created \*\*.+\*\*, \d` or `No format was named`;
  - `files_exact` defaults to 1 for cases with `expect_file` and without `expect_steps` naming several files.
- **`check_file` scores every file.** The primary is the one in `format`; the extras count toward `files_exact` and
  `only_formats`.

#### D3. Routing-only suite (pipeline.py hook, evals.py, new jevrouter/cassette.py)

- **Pipeline hook** (Workstream A implements it): `submit(..., extras={'dry_run': 'route', 'jev': <jev-like>, 'plan':
  <pinned>})`.
  - The run plans (from the pinned plan if given, else `planner.plan`), then prepares and routes every step in
    dependency order, applying the policy.
  - It stops before any agent runs. Dependent steps get the context `(not run: route mode)`, and `resolve_step` is
    skipped.
  - The record is stored with `status 'done'`, `rec.dry_run = 'route'`, and tasks carrying ROUTED_KEYS plus `trace`,
    `signals` and `hard`.
  - No agent, merger, engine or HTTP call is made. A test with raising stubs checks this.
- **`cassette.JevCassette(inner, path, mode)`**, where mode is `'replay' | 'record' | 'live'`.
  - It wraps any object with `system_one(text, questions)`, keyed by
    `sha256(text + json(sorted question names and their instruction/criteria))`.
  - It stores `{key, text, answers: {name: {choice?, probabilities?, confidence?, noul?, score?}}, model,
    input_tokens}` in `evals/cassettes/jev.jsonl`, append-only, one line per key.
  - In replay, a miss raises `CassetteMiss`. The case then scores `unrecorded`: it doesn't pass or fail and is reported
    separately.
- **CLI:** `python -m jevrouter.evals --mode route --jev replay|record|live [--tags ...] [--split ...]`.
  - The plan is pinned by `plan` when present. Otherwise the heuristic planner runs, since route mode has no engine.
  - The multi-score call is also cassetted.
- **API:** `POST /api/evals/run` accepts `mode: 'full' | 'route'` and `jev: 'live' | 'replay' | 'record'`. The API
  defaults to live, and cassette recording happens only from the CLI.
- **Report** (in the summary, `EXTRA_KEYS` gains these):
  - `confusion`: `{expected: {got: n}}`, over steps with an expected agent from `expect_steps`, or single-step cases'
    `expect_agents[0]`, and never over bags;
  - `per_agent`: `[{agent, tp, fp, fn, precision, recall, f1, support}]`;
  - `stages`: a histogram of failure stages, `plan_text | plan_shape | jev_pick | clarity | confidence |
    gate_missing_detail | gate_cant | gate_wants_file | unsupported | forced | frame | agent_answer | file | judge |
    budget`. The stage is taken from the first trace entry that moved the step away from its expected agent;
  - `calibration` (D4);
  - `route_pass` and `answer_pass` TagScores;
  - `mode`, `jev`, `suite_sha`, `unrecorded`.
- **Speed target.** The full suite in route replay takes under 15 s on a laptop.

#### D4. Calibration (evals.py, new `calibrate` subcommand)

- **Reliability.** For `confidence` and for `clear`, the route-mode report bins steps into 10 equal-width bins and
  gives `{lo, hi, n, accuracy, mean}` per bin, plus ECE. Accuracy means the final agent equals the expected one; for
  `clear`, it means the expected outcome is not clarify.
- **`python -m jevrouter.evals calibrate --jev replay`** sweeps `MIN_CLEAR ∈ {0.10..0.40 step 0.05}`, `CONFIRM_AT ∈
  {0.5..0.8 step 0.1}`, `UNSUPPORTED_AT ∈ {0.5..0.9 step 0.1}` and `DESCRIBED_AT` over the cassette.
  - It re-runs only `jev.decide` and `policy.apply` on recorded outputs, and prints clarify rate, wrong-route rate,
    unsupported precision/recall and honesty pass rate for each point.
  - Thresholds are chosen on dev and reported on holdout. The builder changes config values only when holdout confirms
    the gain. The sweep takes under 2 s.

#### D5. Judge in routine runs (evals.py, judge.py)

- **Unjudged cases.** A case with a `judge` rubric that ran without a judge gets `unjudged: true`, code `unjudged`, and
  `pass: null`. It is left out of passed/total and counted in `unjudged`. The API headline shows the count; the CLI
  already prints it.
- **Picking the judge.** `--judge auto` (and `judge: 'auto'` in `RunEvalBody`) uses the first healthy engine in
  `STRONGEST` that differs from the engine under test (`judge_mod.avoiding`, which already exists).
  - The keyless full suite uses no judge; rubric cases are unjudged.
  - Targeted agy runs use `auto`.
- **Gold set.** `evals/judge_gold.jsonl` holds 30 human-scored `{question, answer, rubric, scores}` rows, written by
  the lead from real runs (2741, 1580, 380, 59, 385 and 25 others).
  - `python -m jevrouter.evals judge-calibrate --judge <engine>` reports mean absolute error per criterion and the
    pass/fail agreement at `JUDGE_MIN`.
  - The judge is trusted for gating only when agreement is ≥ 0.85.
- **File-aware judging.** When the run made files, the judge prompt gets a `FILE:` block: format, pages, headings list,
  image and diagram counts, fonts, and 1,500 chars sampled from the first, middle and last pages. `judge.py` gains
  `grade(..., file_summary: str | None = None)`.

#### D6. Real-traffic cases (app.py, evals.py, new jevrouter/suspects.py (owned by B), labels.py untouched)

- **`suspects.suspects(rec) -> list[{code, note}]`.** It is computed at the end of every run and stored as
  `rec.suspects`. The codes:

  | Code | Meaning |
  |---|---|
  | `pages_short` | a created file's pages are below brief.pages lo |
  | `forced_non_file` | forced create on a step with no file request |
  | `reply_template_body` | a file body matches the reply template |
  | `extra_format` | a file was made in a format the query didn't ask for |
  | `unfulfilled` | caveats are non-empty |
  | `dup_clarify` | identical guard texts appear |
  | `agent_label_leak` | `**agent**:` appears in a file or answer |
  | `dead_end` | the `@dead_end` matcher hit |
  | `cut_off` | the answer matches `cut off\|unavailable here\|truncated` |

- **`GET /api/runs?suspect=1`** filters to runs with suspects.
- **`POST /api/runs/{qid}/promote`** writes a draft case to `evals/cases.local.jsonl` (gitignored):
  - `id` is `run-<qid>`, with query, `chat` from the record, files by fixture name, and the pinned `plan`;
  - `expect_steps` is drafted from the record's agents with every step marked `forced` as stored, and the suspects as
    comments in `note`;
  - it returns `{case_id, created, path}`;
  - it returns 409 if the id exists and 404 for an unknown qid.
- **`python -m jevrouter.evals harvest --since 30d [--db PATH]`** lists runs with suspects, newest first, read-only. It
  reads `data/tracegraph.db` read-only, so it is safe against the user's server.
- **Committed cases** go in `evals/cases.jsonl`, tag `real-traffic`, written by the D builder by hand:

  | Case | Setup | Expectation |
  |---|---|---|
  | `rt-2741-forced-longpdf` | literal prompt; chat {mode: research, agent: create}; pinned 2-step plan from 2741 | step 0 agent `research\|knowledge` and not create; step 1 create forced; `expect_file {format: pdf, pages: [12,13], diagrams_min: 2, grayscale: true, files_exact: 1, not_contains: ['Created **','No format was named'], credits: true}`; `must_mention ['@font_note','@images_note']` |
  | `rt-2741-unforced` | the same prompt without @create | the same file expectations |
  | `rt-2741-keyless` | keyless | outcome answer or error; must_match `needs an? (LLM )?engine`; `files_exact` 0 or not_contains checks |
  | `rt-1580-context` | fixture PDF `evals/fixtures/hackathon-brief.pdf`, made by the builder from a public-domain text with 3 tracks; 3-step plan | must_not_match `cut off\|unavailable here` |
  | `rt-381-followup-keyless` | 2 turns | at most one clarify; no repeated sentence (new check `no_repeat: true`) |
  | `rt-383`, `rt-384`, `rt-385` | 3 advice paraphrases | per B acceptance |
  | `rt-1582`, `rt-1583` | timing, `budget: {planner: ['single','heuristic'], plan_ms: 800}` | as set |
  | `rt-58-assume` | the run-58 question | engine-on `expect_outcome answer`, `must_not_match \bagents?\b` |
  | `rt-1585-labels` | 2 turns | not_contains labels |

- **More new cases:**
  - `create-long`: a 10-slide deck with a timeline; an unforced "research the history of X and make a 5-page Word
    doc"; "twelve pages" and "b&w" paraphrases;
  - `boundary`: 6 or more per confusable pair (knowledge/research, knowledge/report, report/create, document/create,
    chat/currency statement, chat/knowledge translation), split between dev and holdout;
  - `honesty`: 3 holdout paraphrases per h-* case.
- **Settle `cr-default-report-pdf` once.** "Write a report on the history of the Eiffel Tower" names no file, so per
  53c9a5a it expects `report` on an engine and `knowledge` keyless, with no file. Update the case to match.

#### D7. CI gates (.github/workflows/ci.yml, evals/gates.json, evals/baseline.json)

- **`evals/gates.json`:**
  `{"route": {"<tag>": {"min": 0.9}}, "keyless": {"<tag>": {"min": 0.8, "max_drop": 0.1}}, "agy": {...}}`.
  - A tag gate passes when the Wilson 95% lower bound of its pass rate is ≥ `min`, **or** the pass rate is no more than
    `max_drop` below the baseline rate.
  - `safety`, `injection` and `control` have `min: 1.0` on route and keyless.
- **`evals/baseline.json`** holds pass rates per tag (not per case) from the first green run of each suite after this
  work, plus `suite_sha`.
- **CI job `route-evals`**, on every PR with no secret: `pytest -q`, then
  `python -m jevrouter.evals --mode route --jev replay --gates evals/gates.json`. It fails on any gate, on `unrecorded
  > 2%`, or on a route_sha mismatch without a cassette refresh (route_sha hashes only the cases route mode runs, so a
  case added for cli or api engines alone doesn't need one). The existing keyless live suite runs nightly
  (`schedule`), only when `TYPESAFE_API_KEY` is set.
- **Suite size.** `tests/test_evals_harness.py:109` replaces `180 <= len(cases) <= 260` with per-kind floors
  (single ≥ 120, multi_turn ≥ 30, file ≥ 60, judge ≥ 14, real-traffic ≥ 12) and a ceiling of 600.

#### D8. Paraphrases, budgets, determinism (new jevrouter/paraphrase.py, evals/budgets.json)

- **`python -m jevrouter.evals paraphrase --seed 7 --n 3 --tags ... > evals/paraphrases.gen.jsonl`** generates
  deterministic variants. The transforms: lowercase with punctuation removed; 1-2 keyboard typos in words of 5 or more
  letters; filler words ("hey", "pls", "quickly"); currency surface forms ($, "bucks", "USD", "dollars"); clause
  reorder; trailing style instructions ("in bullets"); an `@agent` prefix; and a long, instruction-dense version (40-80
  words, with format, length and style constraints appended).
  - The same seed gives byte-identical output.
  - The route report has a robustness table per transform.
- **`evals/budgets.json`:** `{"<kind>": {"<engine_class: keyless|cli|api>": {"p95_ms", "llm_out", "file_tokens"}}}`.
  - Each case result records `tokens: {jev_in, llm_in, llm_out}` and `timings`.
  - Going over budget is a warning, and a failure under `--strict-budgets`.
  - Starting values: a keyless single step has p95 2,000 ms; the create-long file has file_tokens 25,000 on cli.
- **Determinism.**
  - Summaries store `suite_sha` (sha256 of the sorted case lines) and a per-case `case_sha`.
  - `compare()` warns when either differs.
  - `python -m jevrouter.evals flaky --last 10` lists cases whose results flip across stored evals of the same
    suite_sha.

**D acceptance tests:**

- **Stub router:** `run_attempt` passes `case['chat']` into submit.
- **Route mode:**
  - with raising agent stubs, no agent is called;
  - replay twice gives identical JSON;
  - `rt-2741-forced-longpdf` with its pinned plan fails today with `wrong_agent` on step 0 (stage `forced`).
- **Codes:** s05-three-part fails with `extra_agent knowledge`.
- **Stages:** dag-euro-capital-convert is attributed to `gate_missing_detail`, and dag-sydney-ahead step 1 to `clarity`.
- **Unjudged:** a keyless eval headline shows `unjudged 14`, and j-* cases are not counted as passed.
- **Files:** the 2741 case on today's code fails with `1 pages, outside 12-13`, `extra file .md` and `file body is a
  create reply template`.
- **Matchers:** an unknown `@name` is rejected.
- **Harvest:** `harvest --since 30d` lists 2741 first.
- **CI:** a PR that breaks `planner.SEP` fails the route gate with no secret.

### E. UI surfaces (web/)

All data comes from the fields in section 5. Every new field is optional, so old runs render as before.

1. **Chat input** (`pages/Chat.tsx`, `components/chat/ChatOptions.tsx`):
   - Remove the 500-char `slice`.
   - Show a character counter from 3,500, reading `limits.query_chars` from hello/config (default 4000), and disable
     send over the limit.
   - When `@agent` is set and the run's plan has more than one step, the turn shows which step it bound to (the `bound`
     field).
2. **Turn answer** (`components/chat/Turn.tsx`, `AnswerBadges.tsx`, `CreatedFiles.tsx`):
   - The **primary file** card first, with its pages or slides next to the brief target (`12 pages · asked 12-13`), plus
     chips for theme, font used, diagrams and images.
   - Working files collapsed under "Working files".
   - A **"What I couldn't do"** box from `merged.caveats`, amber, never hidden.
   - The `assumption` line in muted text above the step's answer.
   - `unsupported` steps get their own badge colour.
3. **File card and preview** (`CreatedFiles.tsx`, `pages/Files.tsx`): an "Image credits" disclosure lists `credits`
   with licence links; a "Brief" row shows ✓/✗ for each V5-V9 conformance; `phases` token breakdown in the details.
4. **Run detail** (`pages/RunDetail.tsx`, `components/TraceView.tsx`):
   - Per step, a **policy trace** list (rule → agent, why).
   - Signals bars (live, action, personal, described).
   - `frame` used, and `bound`/`forced` badges.
   - Run-level suspects as warnings, with a **"Make eval case"** button that calls `POST /api/runs/{qid}/promote` and
     shows the path.
5. **Runs list** (`pages/Runs.tsx`): a "Suspect" filter (`suspect=1`) and a suspect-count badge column.
6. **Evals** (`pages/Evals.tsx`, `pages/evals/*`):
   - `RunOptions` gets a mode selector (Full / Routing only) and Jev (Live / Replay; replay is disabled in the UI unless
     the server reports a cassette).
   - Headline shows route_pass, answer_pass and unjudged separately.
   - New `pages/evals/Confusion.tsx`: a heatmap of `confusion`, with rows expected and columns got, and cells linking to
     the case list filtered by that pair.
   - New `pages/evals/AgentScores.tsx`: a per-agent P/R/F1 table sorted by F1.
   - New `pages/evals/Calibration.tsx`: reliability diagrams for confidence and clear, plus ECE.
   - A stage histogram bar.
   - A gates table (tag, min, pass rate, Wilson lower bound, baseline, ok).
   - The case row shows `codes` as chips and the file score (pages/images/diagrams/gray).
7. **Settings** (`pages/Settings.tsx`): a read-only "Document fonts" line showing `TRACEGRAPH_BODY_FONT` status
   (`fonts: {body: string|null}` in the config event), with the embed caveat.

**E acceptance:** `npm run build` and `tsc` are clean. Rendering a stored 2741-like record from a fixture shows exactly
one primary file card and the caveats box. The Evals page renders an eval with and without the new fields.

---

## 3. Contract details

### 3.1 Python interfaces (summary; the full signatures are in the sections above)

| Module | New or changed |
|---|---|
| `jevrouter/policy.py` (new) | `StepCtx`, `Decision`, `RULES`, `apply(d, s) -> Decision` |
| `jevrouter/jev.py` | `SIGNALS`; `route_one` returns `signals`; `decide` exempts chat |
| `jevrouter/planner.py` | `plan(..., forced=None, web=False)`, `fill_from_frame(query, frame)`, `expand_described(text)`, coordination in `candidate_split`, `needs_llm_plan` fix |
| `jevrouter/gate.py` | `Parse`, `parse_for(agent, text)`, `unsupported_text(kind, text)`, `future_note(text)`, `ADVICE`, `TIME_SENSITIVE` |
| `jevrouter/agents/tools.py` | `solve_math` word operators and full-coverage rule, `parse_currency` direction, `CAPITAL_OF`, `CURRENCY_OF`, `agent_time` named-place rule, keyless miss texts |
| `jevrouter/agents/__init__.py` | `FEEDS_FILE: ContextVar[bool]` |
| `jevrouter/agents/llm.py` | `NOTES_RESEARCH`, `NOTES_KNOWLEDGE` used when `FEEDS_FILE` is set |
| `jevrouter/merger.py` | `compose(...)`, `strip_preamble(text)`; `merge()` kept as a wrapper |
| `jevrouter/verify.py` | `coverage(agent, text, answer) -> dict \| None` |
| `jevrouter/suspects.py` (new) | `suspects(rec) -> list[dict]` |
| `jevrouter/create/brief.py` (new) | `Brief`, `parse_brief`, `merge`, `MAX_PAGES`, `MAX_SLIDES`, `WORDS_PER_PAGE`, `KNOWN_FONTS` |
| `jevrouter/create/longdoc.py` (new) | `write_long(job, engine, jev, fmt, brief, mode, *, seed=None) -> Made`, `OUTLINE_SCHEMA` |
| `jevrouter/create/diagram.py` (new) | `layout(block) -> Layout`, `pdf_drawing(block, theme, width)`, `png(block, theme, width_px) -> bytes`, `mermaid(block) -> str`, `pptx_draw(slide, block, theme, box)` |
| `jevrouter/create/assets.py` (new) | `ALLOWED`, `Credit`, `search_commons`, `fetch_image`, `reencode`, `resolve_figures` |
| `jevrouter/create/fonts.py` (new) | `FontChoice`, `resolve(requested, fmt)` |
| `jevrouter/create/spec.py` | block types `timeline`, `tree`, `flow`, `figure`, `page_break` (and internal `image`); `THEMES += ('mono',)` |
| `jevrouter/create/rules.py` | V5-V9, X6, X3 reworded; `verify(spec, fmt, data, brief=None)` |
| `jevrouter/agents/create.py` | `Job.brief`, `Job.dep_files`, `Job.role`, `Job.http`; `Made.caveats`, `Made.phases`; new path order (C2); label stripping (B3) |
| `jevrouter/cassette.py` (new) | `JevCassette(inner, path, mode)`, `CassetteMiss` |
| `jevrouter/paraphrase.py` (new) | `generate(cases, seed, n, transforms) -> list[dict]` |
| `jevrouter/evals.py` | the schema keys in D1/D2; `check() -> (reasons, codes)`; route mode, calibrate, harvest, flaky, paraphrase and judge-calibrate subcommands; gates |
| `jevrouter/config.py` | `GUARDS += 'unsupported'`, `DEP_CONTEXT_CHARS = 6000`, `MAX_QUERY_CHARS = 4000`, `UNSUPPORTED_AT = 0.7`, `DESCRIBED_AT = 0.6`, `FORCED_MIN = 0.05` |

### 3.2 HTTP

| Method and path | Change |
|---|---|
| `POST /ask`, `POST /api/ask` | query ≤ 4,000 chars, else `400 {error, limit}` |
| `GET /api/runs?suspect=1` | new filter |
| `POST /api/runs/{qid}/promote` | new; `200 PromoteRunResponse`, 404, 409 |
| `POST /api/evals/run` | body adds `mode`, `jev`, and `judge: 'auto'` |
| `GET /api/evals/{id}` | summary and cases gain the D fields |
| `GET /api/rules` | lists V5-V9 and X6 |
| hello/config SSE | gain `limits` and `fonts` |

---

## 4. Work split (disjoint file ownership) and order

### 4.1 Phase 0: contracts (the lead, before any builder starts; about half a day)

1. Append the section 5 block to `web/src/protocol.ts`, exactly as written.
2. Add the config constants from 3.1 to `jevrouter/config.py`.
3. Create the new Python modules as stubs whose functions keep today's behaviour:
   - `policy.apply` wraps the current `review` logic;
   - `brief.parse_brief` returns `Brief()`;
   - `longdoc.write_long` raises `NotImplementedError`;
   - `suspects.suspects` returns `[]`;
   - `merger.compose` calls `merge`;
   - `FEEDS_FILE` is defined;
   - `Job` and `Made` get the new fields with defaults.
4. Export a read-only fixture `tests/fixtures/run2741.json`: the record plus both created specs, from
   `sqlite3 -readonly data/tracegraph.db`.
5. `pytest -q` must be green before phase 1 starts.

### 4.2 Phase 1: builders in parallel (each owns only the files listed)

| Builder | Owns (edits nothing else) | Delivers |
|---|---|---|
| **R: routing** | `jevrouter/pipeline.py`, `planner.py`, `jev.py`, `gate.py`, `policy.py`, `cache.py`, `agents/tools.py`, `agents/__init__.py`, `config.py`, `store.py`; tests `test_pipeline.py`, `test_planner.py`, `test_gate.py`, `test_parsers.py`, `test_chat_variety.py`, `test_speed.py`, `test_new_tools.py`, new `test_policy.py` | All of A. **Pipeline wiring for B, C and D:** B1 prepare budget, calling `merger.compose` with StepOut and caveats, `FEEDS_FILE` set in `run()`, the `brief`/`dep_files`/`role`/`http` passed to `create_agent.Job`, `answered.caveats`, `suspects.suspects(rec)` at the end of the run, and the `dry_run`, `jev` and `plan` extras for D3. The D3 hook comes **first** (day 1) so D can build route mode against it. |
| **Q: answers** | `jevrouter/merger.py`, `agents/llm.py`, `verify.py`, new `suspects.py`; tests new `test_merger.py`, new `test_suspects.py`, `test_claude.py` | B2, B5 (llm side), `verify.coverage` (A5), `suspects` (D6 logic), B6 hedge wording texts supplied to R as constants in `merger.py` |
| **C: documents** | `jevrouter/agents/create.py`, `jevrouter/create/**` (including new `brief.py`, `longdoc.py`, `diagram.py`, `assets.py`, `fonts.py`), `requirements.txt` (optional `fontTools` only if needed); tests `test_create_*.py`, new `test_create_brief.py`, `test_create_diagram.py`, `test_create_assets.py`, `test_create_longdoc.py`, `test_evals_create.py` | All of C, and B3 |
| **H: harness and API** | `jevrouter/evals.py`, `judge.py`, `labels.py`, `app.py`, new `cassette.py`, new `paraphrase.py`, `evals/**` (cases, matchers.json, gates.json, budgets.json, judge_gold.jsonl, cassettes/, fixtures/), `.github/workflows/ci.yml`, `.gitignore`; tests `test_evals.py`, `test_evals_harness.py`, `test_evals_judge.py`, `test_api.py`, `test_runs.py`, `test_labels.py`, `test_learning.py` | All of D; B4 (app.py limit and hello `limits`/`fonts` passthrough from `Router.config()`, which R adds the keys to); the promote and suspect endpoints; the case updates for `unsupported`, `cr-default-report-pdf` and the matcher dedupe |
| **W: web** | `web/**` except `web/src/protocol.ts` (frozen after phase 0; changes go through the lead) | All of E, plus `api.ts` functions `promoteRun(qid)`, `listRuns({suspect})` and `runEvalWith({mode, jev, judge})` |

**Handoffs** are by signature only (section 3):
- R calls C (`Job` fields and `Made.caveats`), Q (`compose`, `suspects`, `coverage`) and H (none).
- H calls R (the `dry_run` hook) and reads C's meta fields.
- Each builder tests against the phase 0 stubs or fakes, not against another builder's work in progress.

**Order inside phase 1:**
1. Day 1: R lands the D3 hook and A1 (binding). H lands D1 (chat options) and D3 route mode. C lands C1 (brief) and C2
   (path order). Q lands B2 (compose).
2. Day 2: R lands A2-A7. C lands C4 (diagrams), C6 (mono and fonts) and C7. H lands D2, D5 and D6. W builds against
   fixtures.
3. Day 3: C lands C3 (longdoc) and C5 (assets). H lands D4, D7 and D8, and records the cassette with `--jev record`
   (Jev tokens only, cheap; TypeSafe key).

### 4.3 Phase 2: integration (the lead)

1. Merge in order: R → Q → C → H → W.
2. Run `pytest -q`, `cd web && npm run build`.
3. Re-record the cassette for new cases, then run the route suite and fix gate misses.
4. Commit only when the user asks.

---

## 5. TypeScript additions (append to the end of `web/src/protocol.ts`, verbatim)

Interfaces with the same name merge with the existing declarations (TS declaration merging), so the existing types
gain optional fields without edits above. The one exception is `HistoryRecord.merged`, whose type changes to
`MergedRecord | null`, as the comment in the block says.

```ts
// ---------- accuracy v2 (docs/PLAN-accuracy-v2.md) ----------
export type GuardOutcome = 'clarify' | 'blocked' | 'unsupported'
export type PolicyRule =
  | 'blocked' | 'blocked_dependency' | 'unsupported' | 'cant_do' | 'forced' | 'create_demote' | 'file_intent'
  | 'keyless_contract' | 'confirmed' | 'attached_file' | 'refers_back_file' | 'advice' | 'time_sensitive'
  | 'clarify_policy' | 'missing_slot' | 'mode_research' | 'ambiguous_term' | 'frame'
/** One rule the decision policy applied to a step, in order. `agent` is the agent after this rule. */
export interface PolicyStep { rule: PolicyRule; agent: string; why: string }
/** Jev's extra scores from the same route call (0..1). */
export interface RouteSignals { live?: number; action?: number; personal?: number; described?: number }
/** What a finished step is about, carried to the next turn (keyless follow-ups). */
export interface TurnFrame { agent: string; slots: Record<string, string | number> }

export interface RoutedFields {
  trace?: PolicyStep[]
  signals?: RouteSignals
  bound?: boolean          // this step took the run's @agent (only one step per run)
  assumption?: string      // stated before the answer instead of asking a clarifying question
  frame_used?: TurnFrame   // the previous turn's frame this step was completed from
}
export interface AnsweredFields {
  caveats?: string[]       // what this step could not do, in plain words
  frame?: TurnFrame
}
export interface MergedEvent { caveats?: string[]; primary_file?: string | null } // primary_file: CreatedFile id
/** The stored merged answer. Phase 0 also changes the one existing line in HistoryRecord from
 *  `merged: { answer: string; engine: MergeEngine } | null` to `merged: MergedRecord | null`
 *  (the only edit above this block: TS can't redeclare a merged property with a different type). */
export interface MergedRecord { answer: string; engine: MergeEngine; caveats?: string[]; primary_file?: string | null }

export type SuspectCode =
  | 'pages_short' | 'forced_non_file' | 'reply_template_body' | 'extra_format' | 'unfulfilled' | 'dup_clarify'
  | 'agent_label_leak' | 'dead_end' | 'cut_off'
export interface SuspectCheck { code: SuspectCode; note: string }
export interface RunRecord { suspects?: SuspectCheck[]; dry_run?: 'route' | null }
export interface PromoteRunResponse { case_id: string; created: boolean; path: string }

export interface Limits { query_chars: number }
export interface FontsInfo { body: string | null } // TRACEGRAPH_BODY_FONT family, or null
export interface HelloEvent { limits?: Limits; fonts?: FontsInfo }

// Created files: the request's brief and how the file met it.
export type ThemeName = 'clean' | 'dark' | 'warm' | 'mono'
export type DiagramKind = 'timeline' | 'tree' | 'flow'
export interface FileBrief {
  format: FileFormat | null
  pages: [number, number] | null
  slides: [number, number] | null
  theme: ThemeName | null
  font: string | null
  images: boolean
  image_source: 'web' | null
  diagrams: boolean
  diagram_kinds: DiagramKind[]
  words: number | null
  capped: boolean
}
export interface ImageCredit {
  asset: string; title: string; author: string; license: string; license_url: string | null
  source_url: string; caption: string
}
export interface FilePhase { phase: 'outline' | 'sections' | 'topup' | 'assets' | 'render'; calls: number; llm_in: number; llm_out: number; ms: number }
export interface CreatedFile {
  brief?: FileBrief | null
  role?: 'primary' | 'working'
  theme?: ThemeName
  font_used?: string | null
  diagrams?: number
  images?: number
  credits?: ImageCredit[]
  phases?: FilePhase[]
}

// Evals
export interface ChatOpts { mode?: ChatMode; agent?: string; style?: AnswerStyle }
export type EvalMode = 'full' | 'route'
export type JevSource = 'live' | 'replay' | 'record'
export type FailStage =
  | 'plan_text' | 'plan_shape' | 'jev_pick' | 'clarity' | 'confidence' | 'gate_missing_detail' | 'gate_cant'
  | 'gate_wants_file' | 'unsupported' | 'forced' | 'frame' | 'agent_answer' | 'file' | 'judge' | 'budget'
export type ReasonCodeName =
  | 'wrong_agent' | 'extra_agent' | 'missing_agent' | 'forbidden_agent' | 'wrong_outcome' | 'plan_shape' | 'wrong_deps'
  | 'answer_regex' | 'forbidden_regex' | 'file' | 'judge' | 'budget' | 'unjudged' | 'run_status' | 'unrecorded'
export interface ReasonCode { code: ReasonCodeName; step?: number; want?: string; got?: string; stage?: FailStage }
export interface StepRoute {
  tid: string; agent: string; pick: string; confidence: number; clear: number
  expected?: string | null; stage?: FailStage | null; trace?: PolicyStep[]
}
export interface FileScore {
  format: FileFormat; pages: number | null; slides: number | null; words: number; headings: number
  images: number; diagrams: number; fonts: string[]; grayscale: boolean | null
  source: CreatedFile['source']; files: number
}
export interface AgentPR {
  agent: string; tp: number; fp: number; fn: number
  precision: number | null; recall: number | null; f1: number | null; support: number
}
export interface CalibrationBin { lo: number; hi: number; n: number; accuracy: number | null; mean: number }
export interface Calibration { signal: 'confidence' | 'clear'; bins: CalibrationBin[]; ece: number | null }
export interface TagGate {
  tag: string; min: number; passed: number; total: number; rate: number; lower: number
  baseline: number | null; ok: boolean
}
export interface EvalCaseExtra {
  route_pass?: boolean
  answer_pass?: boolean | null
  unjudged?: boolean
  codes?: ReasonCode[]
  steps?: StepRoute[]
  file?: FileScore | null
  tokens?: RunTokens
  chat?: ChatOpts
}
export interface EvalSummaryExtra {
  mode?: EvalMode
  jev?: JevSource
  suite_sha?: string
  route_pass?: TagScore
  answer_pass?: TagScore
  by_tag_route?: Record<string, TagScore>
  unjudged?: number
  unrecorded?: number
  confusion?: Record<string, Record<string, number>> // expected -> got -> count
  per_agent?: AgentPR[]
  stages?: Partial<Record<FailStage, number>>
  calibration?: Calibration[]
  gates?: TagGate[]
  tokens?: RunTokens
}
export interface RunEvalBody { mode?: EvalMode; jev?: JevSource }
```

(`RunEvalBody.judge` already accepts `string | null`; the value `'auto'` is a string.)

---

## 6. Verification protocol

The lead runs every step on a throwaway server (`TG_DB=/tmp/tg-verify.db python server.py --port 8791` or the
project's equivalent) or in the in-process CLI. Nothing touches port 8777. Subscription engines (agy, claude-code) run
only in steps 4 and 5, and only with the user's go-ahead, because they use the user's quota.

1. **Unit and build.** `pytest -q` green; `cd web && npm run build` clean.
2. **Routing-only suite.** `python -m jevrouter.evals --mode route --jev replay --gates evals/gates.json` finishes in
   under 15 s with unrecorded ≤ 2%. Report the confusion matrix, per-agent P/R/F1, stage histogram, calibration (ECE)
   and gate table. Targets:
   - every agent with support ≥ 5 has F1 ≥ 0.9;
   - `boundary` ≥ 90% on dev and holdout, with the regex overrides still on;
   - `safety`, `injection` and `control` at 100%;
   - `rt-2741-*` route checks pass.
3. **Keyless full suite.** `python -m jevrouter.evals --engine none --save --tags all` (in-process, temp DB), repeated
   3 times. Report per-tag median and Wilson bounds. Targets against `abccf6ab8f` (144/235):
   - total ≥ 180 of the old 235, counted separately from the new cases;
   - multi-turn ≥ 22/29 on expect_agents;
   - paraphrase ≥ 9/12;
   - dag ≥ 6/16;
   - honesty ≥ 18/24;
   - control 10/10, over-block ≥ 3/4;
   - no tag drops more than its `max_drop`;
   - zero `@dead_end` hits on answer-expected cases where an honest "needs an engine" text is expected instead.
4. **Antigravity targeted runs.** On the throwaway server, engine `agy`, judge `auto` (a different healthy engine),
   tags `paraphrase,multi-turn,dag,honesty,create,create-long,real-traffic,boundary,mode:research,forced:*`. Targets:
   - paraphrase ≥ 10/12, multi-turn ≥ 19/22, dag ≥ 14/16, honesty ≥ 19/22;
   - create ≥ 24/25;
   - all `create-long` and `real-traffic` cases pass;
   - judge agreement on the gold set ≥ 0.85 before judge results count.

   Then run the full agy general suite once on HEAD as the new baseline (the old `59dbcdc749` predates the routing
   commits). Target ≥ 190/210 on the original 210.
5. **Replay of run 2741's prompt.** Send the exact prompt with `agent: 'create'` and `mode: 'research'`, on:
   - (a) `agy`;
   - (b) `claude-code` if healthy;
   - (c) keyless.

   The lead records, for each run: the plan, each step's agent, forced and bound flags, and trace; the created files;
   pypdf page count; outline entries; image XObjects and their colour spaces; `rg`/`RG` operand scan result; BaseFont
   names; the credits section text; the merged answer; `caveats`; and `suspects` (must be `[]`).

   **Must hold on (a) and (b):**
   - the plan's first step is research (b) or knowledge (a, no web), not forced;
   - exactly one created file, a PDF, `role: 'primary'`, 12 or 13 pages;
   - V5, V8 and V9 ok; X6 ok when images are present;
   - theme `mono`, and the V8 grey scan passes, images included;
   - at least 2 drawn diagrams (V9; at least a timeline plus a tree or flow);
   - images:
     - (b): at least 1 embedded Wikimedia Commons image under an allowed licence, each with an author, licence and
       source URL credit line, and an "Image credits" section;
     - (a): the same when the asset stage can reach Commons (keyless code fetches it, not the model); otherwise the
       caveat `Images weren't added: ...`;
   - the answer names the font: "Anthropic Sans ... isn't available to embed here, so the PDF uses <font>";
   - no "No format was named", no `**create**:`, and no `Created **` inside the file;
   - "What I couldn't do" lists every unmet part, and nothing else.

   **Keyless (c):** no 1-page PDF; an honest answer that a 12-13 page document needs an LLM engine (the research step
   answers with its keyless knowledge summary or an honest miss).

   **Token cost is reported** as a table per engine:
   - jev_in, llm_in and llm_out for the run;
   - per file phase (outline, sections, topup, assets, render): calls, llm_in, llm_out and ms, from `CreatedFile.phases`;
   - upstream step tokens;
   - wall time, split into plan, route, agents and merge.

   Budget targets:
   - create phases ≤ 25K tokens on agy; median ≤ 22K over 5 runs;
   - the page range hit in ≥ 4 of 5 runs;
   - total wall time ≤ 4 min on agy.

   A run outside budget is a finding to fix, not a pass.
6. **Rollback check.** With `TG_POLICY_TRACE=0` (a debug flag R adds that hides `trace` from events), old clients and
   the stored-run replay (`hello.history` from before this work) render without errors.
