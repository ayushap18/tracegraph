# Created files that always build, follow a design file, and ask before a costly run

Status: plan, not yet built. Based on the verified audits of run 2750 (forensics, fuzz, cost, design; 28 Sep 2026).
Code references are to HEAD `8ccf286`. The TypeScript contract for this plan is already appended to the end of
`web/src/protocol.ts` and `web/src/api.ts` (it type-checks); builders use it as written.

Five builders (S, A, E, W, F) work in parallel against this document. **Names, fields, rule ids, file paths, signatures
and user-facing strings written here are exact.** Everything is additive for clients: existing SSE events, `/ask`,
stored runs, stored files and evals keep working unless a section below says a test changes.

Ground rules for every builder:

- The user's server on port 8777 is never stopped, restarted or sent a POST. No `pkill`, `killall` or kill by name;
  only kill exact PIDs you started. Live checks use ports 8790 to 8799 with a throwaway `TG_DB`.
- No subscription or paid LLM calls. Tests use fakes (`tests/fakes.py`). Any eval or CLI run passes `--engine none`.
- No git commit, checkout, reset, stash or restore. Temp files only under `$TMPDIR/fr-<role>`.
- User-facing copy is plain English with no em or en dashes (use "to", commas or colons).

---

## 0. Key decisions

1. **Repair, never discard.** `spec.normalize` repairs a malformed block (aliases, inferred type, strings split into
   lists), keeps any text it cannot place as a paragraph, and drops only blocks with no text, each with a `fix` note.
   A file is refused only when the spec holds no text at all (S2), the reply is over 2 MB (new L6), the format is
   unknown (X1) or safety says no (X4). S1, S7, L1 and X6 become `fix`.
2. **Rendering has its own ladder** (`render.render_safe`): render; on failure, find the section that breaks and retry
   it as plain text; then leave it out (new V11) and name it. V1 blocks only when even plain text cannot be written.
3. **The create agent has a repair ladder**: code repair, then one small call that rewrites only the broken sections,
   then a partial file with honest caveats and a Resume button (new V12). Paid content is never thrown away.
4. **Checkpoints**: the long writer saves its state after every paid phase into the run record. A failed, partial or
   timed-out file resumes with `POST /api/created/resume`, which writes only what is missing.
5. **Design files are applied by code at 0 tokens.** `create/design.py` parses a design system (colours by role,
   fonts, radius) into `spec['design']`; `themes.resolve` turns it into a theme dict every renderer uses; contrast is
   repaired to 4.5:1; fonts fall back honestly. The design file is kept out of the model's context.
6. **Cost preflight is keyless and server-enforced.** `jevrouter/estimate.py` prices a draft from the brief, the
   long writer's own call plan and per-engine constants fitted to recorded runs. `POST /api/estimate` shows it;
   `/ask` returns **409** `{needs_confirmation, estimate}` without starting when a run is over the threshold and the
   body lacks `confirm_cost: true`. Keyless runs and evals are never asked. Afterwards the run and the file show
   estimated vs used.
7. **Leaner engine for long files on Auto**: when Auto would send a long file to Antigravity (about 12,400 tokens of
   its own per turn, 3 turns per schema call), the long writer prefers the healthy engine with the lowest estimate.
   The dialog also offers "Use Claude Code instead".
8. **A failed create is recorded honestly**: phases, tokens and the checkpoint are kept on the step, the run gets
   `file_failed`, and the merge uses the template (0 tokens) instead of an LLM call to wrap a failure.

---

## 1. Evidence (short)

Run 2750: *"create the ppt on the how mobile phone is being evolved history past present everything a ppt of 12 slides
using the multiple pictured diagrams and also use the design.md for the design"*, `@create`, mode research, attached
`DESIGN-lovable.md` (17,284 chars).

| Stage | Engine | Time | Notes |
|---|---|---|---|
| LLM planner | Auto lead (agy) | 10.4 s | |
| 2750.1 research | claude-code | 85.8 s | answer 11,017 chars, kept in the merged answer |
| 2750.2 create | agy | 155.3 s | outline + 2 section batches; section 7 block 1 `{"type":"bullets","items":null}`; fit loop hid the S1 (`longdoc.py` 358-362); `finish` normalized again and discarded the deck |
| LLM merge | agy | 18.6 s | an extra call to wrap "No file was made" |
| Run | | 272.5 s | `llm_in` 250,046, `llm_out` 30,492, status `done`, no `created` row, no spec kept |

- 21 of 24 one-bad-block variants of a valid 11-section deck discard the whole file (16 S1 block-level, 2 S1
  section-level, 3 S7). Renderers are robust except for 4 reproduced bugs (section 2.5). `normalize` is the single
  point of loss, called by X4 in `finish`, by `build`, and by `measure`.
- `DOCSPEC_SCHEMA` already requires `bullets.items`; agy's `--json-schema` did not enforce it. Code-side repair is
  needed for every engine and format.
- Only 860 of 17,284 design characters reached the model, as content, after the research answer, under prompts that
  forbid styling. `parse_brief` found no theme or font; nothing said the design was ignored.
- `parse_brief('... multiple pictured diagrams')` gives `images=False` (`brief.py` line 52 misses "pictured").
- agy costs about 12,400 input tokens per turn of its own and a schema call takes about 3 turns: single creates cost
  41,386 input tokens at the median (32K to 89K) for prompts of 1K to 3K. claude-code adds about 0.6K to 1.9K.
- No estimate, confirm or pause exists anywhere (`app.py` routes, `web/src`).
- `make_file` (`pipeline.py` 1101-1130) copies only answer, ok, engine and tokens, so a failed create loses its phases.
  A non-SpecError exception reaches `pipeline.py` 1161 and loses the step's tokens entirely.

---

## 2. Repair, not block (builder S)

### 2.1 Rule changes (`docs/RULES-files.md`, `jevrouter/create/rules.py` `RULES`)

Rule ids are never renumbered. Changed and new rows, exact text and severity:

| ID | Rule | Severity |
|---|---|---|
| S1 | Each block is repaired to the DocSpec schema: known aliases are mapped (list, ul, ol to bullets; content or body to text; points or lines to items), a missing type is inferred from its keys, and a string where a list is expected is split into lines. A block that still cannot be read keeps its text as a paragraph; a block with no text is left out. Every change is noted. | fix |
| S2 | The file has something to show: at least one section with a heading or a non-empty block, not counting page breaks and figures that found no image. A spec with no text at all is refused. | block |
| S7 | Nothing is fetched: link, image and path keys (url, src, href, image, link, path and the like) are removed from the spec with a note, and the file holds only the content given. | fix |
| S8 | Every section has a heading: a missing one is taken from its first line of text, or becomes "Section N" (slides: the previous slide's heading with "(cont.)"). A section given as plain text becomes a heading-only section, or a paragraph when it is longer than 80 characters. | fix |
| L1 | At most 200 KB of spec JSON (table rows are limited by L2), 40 sections and 30 blocks per section. Blocks past 30 continue in a section headed "<heading> (cont.)"; sections past 40, or past 200 KB, are cut from the end and the note says how many. | fix |
| L6 | A model reply larger than 2 MB is not read as a file spec. | block |
| X6 | Every embedded image is a PNG or JPEG from the local asset cache, re-encoded, found under an allowed licence (public domain, CC0, CC BY, CC BY-SA), with a credit line naming its title, author, licence and source. An image without a full credit is left out, never embedded. | fix |
| V10 | When a design file was applied, the file uses its background and text colours (or the answer says what could not be used). | warn |
| V11 | A section the renderer cannot lay out in this format is retried as plain text, then left out, and the answer names it. | fix |
| V12 | The file holds every planned section, or the answer lists the missing ones and offers Resume. | warn |

Also in `RULES-files.md`: the severity list gains the sentence "A file is refused only when nothing in it can be
shown (S2), the reply is too large to read (L6), the format is unknown (X1), the safety check says no (X4), or the
file cannot be written even as plain text (V1)." S6 gains: "Column names are at most 80 characters, cut at a word."
F2 and F3 gain: "The subject property holds at most 255 characters of the subtitle, cut at a word." The "Changing the
rules" paragraph is unchanged.

`rules.py`: `RULES` rows updated to match; `_rule` for S8, L6, V10, V11, V12 added in their groups; `verify` adds V10
(when `spec.get('design')`) and accepts `extra` results for V11 and V12 from `render_safe` and the agent.

### 2.2 New public functions in `jevrouter/create/spec.py` (S adds these first, with final signatures)

```python
def parse_spec(text: str) -> dict | None
    """Tolerant JSON read of a model reply: strips code fences and text around the outermost {...} or [...], removes
    trailing commas, closes brackets and quotes left open by a cut-off reply. A top-level list becomes
    {'sections': list}. None when no JSON object can be read. Raises SpecError('L6') for a reply over 2 MB."""

def repair(spec) -> tuple[dict, list[RuleResult]]
    """The format-independent half of normalize (steps 1 to 6 of 2.3): never raises. Returns
    {'title', 'subtitle', 'sections': [{'heading', 'level', 'blocks', 'notes'}], plus 'theme', 'paper', 'font',
    'design' when present} with every block valid for _block, and the S1, S7, S8, L1 results. Garbage gives
    {'title': '', 'subtitle': '', 'sections': []}."""

def has_text(spec) -> bool
    """True when repair(spec) keeps at least one section with a heading or a non-empty block (the S2 test)."""

def normalize(spec: dict, fmt: str) -> tuple[dict, list[RuleResult]]   # unchanged signature
    """repair(), then the format steps (titles, levels, slides, tables). Raises SpecError only for S2, L6 or X1."""
```

`create/__init__.py` exports `parse_spec`, `repair`, `has_text`, `render_safe`.

### 2.3 The repair algorithm (`repair`, in order)

1. **Clean the tree**: one recursive pass strips `_BAD_CHARS` from every string (fixes the lone surrogate read as
   "not JSON"), turns keys into strings, and stops at depth 40 (deeper values become their JSON text). Size is measured
   after cleaning with `ensure_ascii=False` and `errors='replace'`.
2. **Top level**: a list becomes `{'sections': list}`; `slides`, `pages` or `parts` stand for `sections` when
   `sections` is missing; a dict of sections becomes its values; a string becomes one paragraph section.
3. **Sections**: `title` or `name` stands for `heading`; `content`, `body` or `items` for `blocks`; a dict of blocks
   becomes `[dict]`, a string becomes a paragraph, a list of strings becomes bullets; `bullets` or `points` directly on
   a section become a bullets block; a string section follows S8. Missing headings follow S8.
4. **Blocks**: each block goes through `_coerce(b) -> list[dict]` then `_check_block` and `_block`, inside one
   `try/except Exception` per block. On any exception the block is salvaged (step 5).
   - Types are lowercased and stripped. Aliases: `list, ul, bullet, bulleted_list, points` to bullets; `ol,
     numbered_list, numbered` to bullets with `ordered: true`; `text, para, p, body, markdown, description` to
     paragraph; `blockquote` to quote; `pre, snippet, source_code` to code; `grid, matrix, data_table` to table;
     `graph, plot` and a chart kind used as the type (`bar`, `line`, `pie`, `column`, `donut` ...) to chart;
     `hr, divider, break, pagebreak` to page_break; `picture, image, photo, img, illustration` to figure (their URL key
     is removed by S7; the caption becomes the query); `diagram` by its keys (events: timeline; edges: flow; nodes
     with a parent: tree; other nodes: flow); `heading, header, h1, h2, h3, subheading, title` start a new section with
     that text (a slide on PPTX).
   - A missing type is inferred from keys, first match wins: `items` bullets; `rows` or `columns` table; `series` or
     `data` with `labels` chart; `events` timeline; `edges` flow; `nodes` tree when any node has a parent, else flow;
     `text` or `content` paragraph; `code` or `lang` code; `query` or `caption` figure.
   - Field aliases: paragraph `text` from `content, body, value, paragraph, description`, or a list joined with
     spaces; bullets `items` from `points, list, bullets, lines, content, text`; a string of items is split on list
     markers (`_LIST_LINE`), then newlines, then "; " when that gives 2 or more; dict items give their `text`,
     `label`, `title` or first string value; nested lists are flattened one level. Table `columns` from `headers,
     header, cols`; `rows` from `data, cells, body`; dict rows become cells by key, with the union of keys as columns
     when none are given; a string of rows is split into lines, then on `|` or `,`. Chart `labels` from
     `categories, x, xlabels`; `series` from `datasets, values`; a bare list of numbers is one series; a series dict
     becomes `[dict]`; a series given as a list becomes `{'name': '', 'values': list}`; a series given as a string is
     dropped with an S5 note; `data: [{label, value}]` becomes labels and one series; an unknown kind becomes `bar`
     with an S1 note; a chart with no numeric value becomes a bullets block of its labels (title as the first item).
     Timeline `events` from `items, milestones, entries`; an event string `"1973: first call"`, `"1973 - first call"`
     or `"1973 first call"` becomes `{date, label}`; event keys `year, when, time` mean date and `title, text, event,
     description` mean label. Tree and flow node strings become `{id: slug, label}`; a flow with nodes and no edges is
     chained in order (S6 note); edge strings `a->b`, `a → b`, `a to b` and an edge dict `{"a": "b"}` become edges.
     Quote `text` from `quote, content`; `by` from `author, source` (strings only). Code `text` from `code, content,
     source`.
5. **Salvage**: a block that still fails keeps its text: the string values of the block (skipping `type, kind, lang,
   ordered, level, id, parent, from, to, formats, asset, credit, query` and every `FETCH_KEYS` key), joined, at most
   2,000 characters, as a paragraph, with the S1 note "a block in section N could not be read and was kept as text".
   A block with no text is left out with "an empty block in section N was left out". Figure blocks are never
   salvaged into text (their query is a search, not content).
6. **S7** strips `FETCH_KEYS` everywhere (except table cells) with the note "link or image addresses in section N were
   removed; nothing is fetched". **L1** applies the caps of 2.1: `_fit` first (already splits and folds), then
   continuation sections, then cuts from the end. Notes aggregate per rule with section numbers ("bullets without items
   repaired in sections 7 and 9").
7. `normalize` then runs S2 (`page_break` and unresolved `figure` blocks do not count), titles, levels and the format
   steps as today. The section cap is skipped when `spec.get('format')` is set (the spec is already normalized), so
   normalizing a split 50-slide deck again no longer fails L1.

### 2.4 `render.render_safe` (new, in `jevrouter/create/render.py`)

```python
def render_safe(spec: dict, fmt: str) -> tuple[bytes, list[RuleResult]]
    """render(), and on any exception except SpecError('X1'|'X3'|'X6'): (1) render each section alone to find the ones
    that fail (at most 40 renders, PDF and DOCX; PPTX and XLSX render per section natively), (2) retry those sections
    as plain text (every block to paragraphs and bullets, tables to bullets of 'col: value' pairs, no images, built-in
    fonts), (3) leave out what still fails. Returns the bytes and the V11 results ('fix', naming each section). Raises
    SpecError('V1') only when a text-only file of the whole spec also fails."""
```

`agents/create.py build()` calls `render_safe` instead of `render` and passes its results as `extra`.

### 2.5 Bugs to fix on the way (all reproduced)

| Bug | Fix |
|---|---|
| `number('', loose=True)` and `' '`, `'%'`, `'()'`, `'(%)'` raise IndexError (`spec.py` 229) | `if not s: return None`; `sign = s[0] if s[:1] and s[0] in '+-' else ''` |
| PDF `Heading` subclass crashes when reportlab splits it (`bulletText`), `render.py` 459-465 | accept `(text, style, level=None, **kw)` and pass `kw` to `Paragraph`; outline entry only on the first part; `keepWithNext=1`; PDF headings capped at 120 characters in render |
| PDF `LayoutError` when a table header row is taller than a page | S6 caps column names at 80 characters; `_pdf` catches `LayoutError` for a table and retries with `repeatRows=0` and headers cut to 40 |
| Subtitle of 256 to 300 characters fails DOCX and PPTX (`render.py` 786, 998) | core property `subject = subtitle[:255]` cut at a word |
| A lone surrogate rejects the spec as "not JSON" | step 1 of 2.3 |
| `_unit_columns` with `formats: [['0%']]` raises TypeError | `g if isinstance(g, str) and g in NUMBER_FORMATS else None` |
| `_fix_levels` level `inf` raises OverflowError | catch `OverflowError` too |
| A page_break-only spec builds an empty MD, PDF, DOCX, XLSX | S2 of 2.3 step 7 |

### 2.6 Diagrams (`jevrouter/create/diagram.py`, builder S)

`clean_block` accepts string events and string nodes as in 2.3, and a `radius` from the theme dict (`t.get('radius',
3)`, 0 draws a plain rectangle) in `pdf_drawing`, `png` and `pptx_draw` (rounded rectangle adjustment scaled from it).

### 2.7 Tests (S)

- `tests/test_create_spec.py`: lines 59-75 change: S1 and S7 cases now assert a file spec comes back with a `fix`
  note. New: every one of the 24 probe shapes plus the string-series shape, each inside a valid 11-section spec,
  normalizes for all 5 formats with at least 11 sections kept and the right note; `number` edge cases; surrogate;
  L1 continuation and cut; `repair` on garbage never raises; `has_text`; normalize is idempotent on its own output
  (section count kept) for all formats.
- `tests/test_create_render.py`: Heading split at a page end; 5,000-character header; subtitle 256 and 300;
  `render_safe` with a monkeypatched renderer that raises on one section gives the other sections plus a V11 note;
  design colours in each format (section 4.6).
- `tests/test_create_rules.py`: new rows in `RULES`; severities; V10.

---

## 3. The repair ladder and checkpoints (builder A, with the pipeline hooks by E)

### 3.1 The ladder

Every create path follows the same rungs, in order, and stops at the first that yields a file:

1. **Code repair**: `cf.parse_spec` and `cf.repair` (0 tokens). A reply that is not JSON but has prose is laid out
   with `cf.from_markdown(reply.text)` and the note "The model replied in plain text, so its text was laid out as the
   file." A section that keeps its heading but loses every block counts as missing.
2. **Rewrite only what is missing**: one call per group of at most 10 missing sections, with the request, title,
   outline, the missing headings and the reason ("the last reply for these sections had no usable content"). At most
   `longdoc.MAX_REPAIR_CALLS = 2` per file, and only while `job.deadline - time.monotonic() >=
   longdoc.REPAIR_MIN_SECONDS = 60`. Counted in the `sections` phase and in `Made.repairs`.
3. **Partial file**: build with the sections that exist. `meta['partial'] = {planned, written, missing, resume}`
   (`resume` is filled by the pipeline), a V12 `warn` result, and the caveat, exactly:
   `"{missing} of {planned} planned {slides|sections} could not be written ({headings}). Use Resume on the file card to write them; it reuses everything already written."`
   (headings joined with ", ", at most 5, then "and N more").
4. **No file**: only when nothing renderable exists (S2). The checkpoint is still saved, so Resume can retry.

The single-call path (`from_engine`) uses rungs 1, 2 (one retry call when the reply had no usable content at all,
under the same deadline check) and 4. The zero-token paths use rung 1 only.

`make()` wraps its whole body: any exception becomes `Made("No file was made: {plain reason}", False, ...)` with the
engine, tokens and phases spent so far (a `Tally` on the job, `job.tally`), so the pipeline never loses tokens.

### 3.2 `longdoc.write_long` changes (A)

- New exact helper, used by `write_long` and by `estimate.py` so they cannot drift:
  ```python
  @dataclass
  class CallPlan:
      sections: int          # budget()[1]
      words: int             # budget()[0]
      batches: int           # n_batches
      max_calls: int         # outline + batches + top-up + MAX_REPAIR_CALLS
      token_budget: int
  def plan_calls(fmt: str, brief: Brief) -> CallPlan
  ```
- `write()` parses each batch with `cf.parse_spec` then `cf.repair` (fmt independent), keeps the sections that
  survive, records notes, and reports the parts still missing to rung 2.
- The fit loop never swallows a `SpecError`: it records the note in `caveats` and stops fitting, and the final build
  still renders the valid sections.
- `with_images` runs on the repaired spec only (fixes the helper crashes on odd shapes).
- The design file is excluded from `context()` (4.3). `context()` gives attached content documents at least 25% of
  `LONG_CONTEXT_CHARS` and earlier answers at most 60%, so attachments are never starved.
- Figures dropped because images were not asked for add the note "Pictures were left out because the request did not
  ask for images. Say \"with images\" to add them."

### 3.3 Checkpoints (A writes them, E stores them)

`Job` gains, in `agents/create.py`:

```python
checkpoint: Callable[[dict], None] | None = None   # sink the pipeline provides
deadline: float | None = None                      # time.monotonic() value the run must finish by
resume: dict | None = None                         # a checkpoint to continue from
design_docs: list[tuple[dict, str]] = field(default_factory=list)  # design files from earlier turns (4.3)
tally: object | None = None                        # longdoc.Tally shared by every call this job makes
```

`Made` gains `repairs: dict | None = None`, `partial: dict | None = None`, `checkpoint: dict | None = None`,
`reply: str | None = None` (the raw reply of the single path, at most 200 KB).

The longdoc state, sent to `job.checkpoint` after the outline, after each batch, after each repair call and after the
final build (at most `longdoc.CHECKPOINT_MAX_BYTES = 400_000` bytes; `ctx` is cut first, then the oldest written
sections are replaced by their headings and marked missing):

```json
{"v": 1, "kind": "longdoc", "format": "pptx", "request": "...", "brief": {"...": "FileBrief"},
 "theme": null, "font": null, "design": null, "ctx": "at most 12,000 chars", "title": "...", "subtitle": "...",
 "parts": [{"heading": "...", "level": 1, "words": 45, "hints": [], "diagrams": ["timeline"], "figures": 0}],
 "written": {"0": {"heading": "...", "level": 1, "blocks": [], "notes": ""}},
 "failed": [6], "phase": "sections", "engine": "agy", "tokens_in": 0, "tokens_out": 0, "calls": 0,
 "at": 1790000000.0, "file_id": null}
```

The single path saves `{"v": 1, "kind": "single", "format", "request", "brief", "theme", "font", "design", "reply",
"engine", "tokens_in", "tokens_out", "calls": 1, "at", "phase", "file_id"}` so a later code fix can rebuild it at 0
tokens.

`longdoc.info(state: dict, qid: int, tid: str) -> dict` returns the `CheckpointInfo` summary (protocol.ts).
`resumable` is true when `kind == 'single'` and no file was made, or when `failed` or unwritten parts remain.

`longdoc.resume_long(job, engine, jev, state) -> Made` writes only the parts not in `written` (rung 2 rules, batches
of `BATCH_WORDS`), then runs images, the fit loop and `finish` exactly like `write_long`. A `single` state re-runs
rung 1 and `finish` on the stored `reply` with 0 calls. The new file's meta carries `resumed_from = {qid, tid,
file_id}` and only the new calls' tokens.

### 3.4 Pipeline hooks (E, in `jevrouter/pipeline.py`; kept small)

- **H2 checkpoint sink**: `make_file` passes `checkpoint=lambda s: rec.setdefault('checkpoints_state', {}).__setitem__(st['tid'], s)`
  and `deadline=<run start monotonic + self.deadline(query, engine)>`. The full state lives in
  `record['checkpoints_state']`, saved with the run on every final status (done, timeout, cancelled, error). Every API
  that returns a run replaces it with `checkpoints: [longdoc.info(...)]`; `hello` and SSE never carry the full state.
  Sandbox runs keep it in sandbox memory only.
- **H3 keep what was spent**: `answered` gains `phases`, `llm_in`, `llm_out` and `checkpoint` (the info) for create
  steps, on success and on failure.
- **H4 honest failure**: when the run's primary create step has `ok false` and made no file, the record gets
  `file_failed: true`, the `done` event carries it, and the merge uses `merger.template` (0 tokens) instead of an LLM.
- **H7 partial files**: before `save_created`, when `made.partial` is set, fill `meta['partial']['resume'] = {qid, tid,
  sandbox}`.
- **H6 resume runs**: a run submitted with `extras['resume'] = {'qid', 'tid', 'state'}` skips the planner and
  routing: one subtask `{tid: '1', text: state['request']}` routed to `create` with reason "resume a partial file",
  whose `Job.resume = state` (its deps are not re-run: the state carries `ctx`).

### 3.5 `POST /api/created/resume` (E, in `jevrouter/app.py`)

Body `ResumeBody`. Steps: find the run (store, or sandbox memory with `sandbox_id`); 404 `"no checkpoint for that
file step"`; 409 `"That file is already complete."` when not resumable; price it with `estimate.for_resume`; apply the
guard of 5.4 (409 `NeedsConfirmation`); submit a new run with the original text, source, session and
`extras={'resume': ...}`; respond `ResumeResponse` (202). `GET /api/runs/{qid}/checkpoints` returns
`{checkpoints: CheckpointInfo[]}`.

### 3.6 Tests (A and E)

- `tests/test_create_longdoc.py` (A): fake engine for the run 2750 shape (outline of 11 parts; batch 1 valid; batch 2
  with section 7 as `[{"type":"bullets","items":null}]`): a 12-slide pptx, an S1 note naming section 7, exactly one
  repair call. Variant where the repair call also returns nothing: 11 slides (title plus 10), `partial.missing ==
  [heading of 7]`, V12 warn, the caveat text of 3.1. A batch of prose: laid out, note present. Cancel during batch 2:
  the sink received a state with batch 1 written. `resume_long` from that state calls the engine only for the missing
  parts. The fit loop with a spec that fails S2 mid-loop records a caveat.
- `tests/test_create_agent.py` or existing create tests (A): `from_engine` prose salvage; exception inside `make`
  returns tokens.
- `tests/test_pipeline.py` and `tests/test_create_api.py` (E): failed create keeps phases and tokens on the task;
  `file_failed`; template merge; checkpoints saved on timeout; resume endpoint 404, 409, 202; checkpoint state never
  appears in `hello` or `/api/runs`.

---

## 4. design.md support (builder A; renderers by S)

### 4.1 `jevrouter/create/design.py` (new, A)

```python
@dataclass
class FontSpec:
    family: str                  # 'Camera Plain Variable'
    fallbacks: list[str]         # ['ui-sans-serif', 'system-ui', 'sans-serif']
    category: str                # 'sans' | 'serif' | 'mono'

@dataclass
class DesignTokens:
    name: str                    # the file name
    colors: dict[str, str]       # DesignRole -> 'RRGGBB'
    palette: list[str]           # chart colours, 3 to 6
    hatch: bool                  # fewer than 3 saturated colours: charts use patterns
    heading_font: FontSpec | None
    body_font: FontSpec | None
    mono_font: FontSpec | None
    radius: int | None           # px, clamped 0..24
    confidence: float            # 0..1
    notes: list[str]             # conflicts resolved, colours nudged

MENTION: re.Pattern   # the request asks to use a design file (4.2)
def is_design(meta: dict, text: str, request: str = '') -> float     # 0..1
def split_docs(docs, request) -> tuple[list, list]                  # (design docs, content docs)
def parse_design(text: str, name: str = 'design.md') -> DesignTokens | None
def to_spec(tokens: DesignTokens) -> dict          # the spec['design'] JSON
def clean_design(d) -> dict | None                 # validates a spec['design'] value; used by normalize
def applied(design: dict, theme: dict, fonts_used: dict) -> dict   # the CreatedFile.design (DesignApplied) dict
```

`spec['design']` shape: `{"name", "colors": {role: "RRGGBB"}, "palette": [...], "hatch": bool, "heading_font":
{family, fallbacks, category} | null, "body_font": ..., "mono_font": ..., "radius": int | null, "notes": [...]}`.
`KEYS['spec']` gains `design` (S); `strip_internal` removes `design` from model output like `font` (S); `normalize`
keeps `clean_design(spec.get('design'))` when not None (S calls A's function).

### 4.2 Detection

- `is_design` scores: file name matches `design|style|brand|theme|tokens` (0.4); headings like Colo(u)r, Palette,
  Typography, Spacing, Components (0.1 each, at most 0.3); 5 or more colour values (0.2); role words next to a colour
  (0.2); the request names this file or says "the design file" (0.5). Capped at 1.0. It is a design at 0.5 or more.
- `MENTION` matches `\b(?:design(?:[\s_-]?system)?|style[\s_-]?guide|brand(?:[\s_-]?(?:guide|book|kit))?|theme|tokens)\.(?:md|json|css|txt)\b`
  and `\b(?:use|follow|apply|match|with|using|in)\s+(?:the|my|our|this|attached)\s+(?:design(?:\s+system)?|style\s*guide|brand(?:\s+guide)?|design\s+file)\b`
  and `\bfor the (?:design|style|look)\b`.
- `split_docs`: a doc with `is_design >= 0.5` is a design doc when the request matches `MENTION`, or when
  `is_design >= 0.7` and the request does not ask about the file as content (`summari[sz]e|explain|describe|review|critique|audit|compare|what (?:is|does)`
  near design, style or brand). Otherwise it stays content.
- `Brief` gains `design: str | None` (the design file's name), set by `agents/create.make` after `split_docs`;
  `brief.to_dict` carries it (FileBrief.design). `describe()` does not mention it: the model never sees styling.
- Mentioned but not attached: when `MENTION` names a file and neither `job.docs` nor `job.design_docs` has a design,
  the caveat is "You mentioned {name}, but no design file is attached to this message. Attach it and convert the file
  to apply it (0 tokens)."
- `brief.IMAGES` becomes `\b(?:images?|photos?|photographs?|pictur(?:e|es|ed)|pictorial|illustrat(?:ion|ions|ed)|figures?|visuals?)\b`
  with the same `asked_for` framing (A, `brief.py`); tests for "multiple pictured diagrams" and "an illustrated ppt".

### 4.3 Wiring in the create agent (A) and pipeline (E)

- `make()` splits `job.docs` with `design.split_docs`, parses the first design doc (or the newest of
  `job.design_docs`), and sets `spec['design']` on every path: engine, zero-token, `convert_last` and
  `from_dep_files`. `attached` is computed from content docs only, and `convert_last` / `from_dep_files` take a
  `design` argument so "restyle my last deck with this design.md" re-renders the stored spec with the design at 0
  tokens.
- Design docs are never in `context_text` or `longdoc.context`.
- **H5 (E)**: when the request matches `design.MENTION` and nothing attached to this message is a design, the pipeline
  fills `Job.design_docs` with design-like files attached to earlier turns of the same chat (store files by id from the
  session's runs, text extracted as today), newest first, at most 2.
- An explicit theme word in the request wins over the design's colours ("black and white" keeps mono and the design's
  fonts, note "The design's colours were not used because you asked for black and white."). A font named in the
  request wins over the design's fonts.

### 4.4 Parsing algorithm (`parse_design`, 0 tokens)

1. Collect colour values: `#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb()`, `rgba()`, `hsl()`, CSS custom properties
   (`--color-bg: #...`), JSON design tokens (`{"color": {"background": {"value": "#..."}}}`). Alpha colours are
   composited over the chosen background (a second pass once bg is known).
2. Each colour line gives a **label** (the text before the first colon, or the list item's lead) and a **description**
   (after the colon). Role keywords are read only from the label, and from the description only when it names a role
   ("Page background", "Body text", "Headings", "Borders", "Primary button"). Words in the colour's own name
   ("Muted Gray", "Slate", "Ink") never vote.
3. Role keywords: bg (`background, canvas, page, base`), surface (`surface, card, panel`), text (`text, body, copy,
   foreground, primary text`), heading (`heading, title, display`), muted (`secondary, muted, caption, subtle`),
   accent (`accent, primary, brand, link, cta`), border (`border, divider, rule, stroke, outline`), header_bg
   (`button background, primary button`), header_text (`on primary, button text, on dark`).
4. Lines under a "Quick Color Reference" (or "Quick reference", "Summary") heading vote 5 times. A line that says the
   colour is for focus rings only, or starts with "Don't", "Do not", "Avoid" or "Never", casts no vote and the colour is
   left out of the palette.
5. Conflicts: for `text`, when candidates disagree, the one with the highest contrast on bg wins; the loser becomes
   `muted` when it reaches 4.5:1 and `muted` is unset. The conflict is noted ("The design gives two body text colours;
   used #1C1C1C and kept #5F5F5D for secondary text.").
6. Fonts: `font-family` lines, "Font family:" and "Headings:" lines under a Typography heading; the first family is the
   font, the rest are fallbacks; category from the generic family or the fallbacks (`serif`, `sans-serif`,
   `ui-sans-serif`, `system-ui`, `monospace`, `ui-monospace`). Radius from `radius`, `border-radius` or `rounded` with
   px.
7. Palette: saturated colours that voted for no text role and are not excluded, in order of appearance; when fewer
   than 3, the palette is `text`, `muted`, `mix(text, bg, 0.55)`, `mix(text, bg, 0.75)` and `hatch = True`.
8. `confidence` = share of the roles bg, text and heading that were found, times 0.8, plus 0.2 when a font was found.
   Below 0.34 (no background or text found) `parse_design` returns None and the caveat is "The design file
   {name} did not name its colours or fonts in a way that could be read, so the standard style was used."

### 4.5 Themes and fonts (A: `themes.py`, `fonts.py`)

```python
# themes.py
def clean_design(d) -> dict | None           # re-export of design.clean_design (normalize imports it from here)
def from_design(design: dict, paper: bool = False) -> dict
def resolve(spec: dict, paper: bool = False) -> dict   # the theme dict renderers and rules use
def base_for(design: dict) -> str            # 'dark' when bg luminance < 0.2; 'warm' when bg red exceeds blue by 6+; else 'clean'
```

- `from_design` starts from `THEMES[base_for(design)]`, fills every key the renderers read (`bg, text, muted, heading,
  accent, header_bg, header_text, stripe, code_bg, border, palette, pdf_font, font, heading_font, mono`), derives the
  missing ones (`header_bg` = heading, `header_text` = bg or FFFFFF by contrast, `stripe` = `mix(bg, text, 0.04)`,
  `code_bg` = `mix(bg, text, 0.06)`, `surface` = bg), and sets `hatch` from the design and `radius`.
- Contrast repair: for every pair in `TEXT_PAIRS`, move the foreground's HLS lightness away from its background in
  steps of 0.02 until 4.5:1; chart fills to 3:1 on bg. Each adjusted role is listed in `nudged` with a note ("Muted
  text was darkened slightly to stay readable."). Idempotent.
- `patterns` is split: `hatch` (charts use patterns) and `grey_images` (images are greyscaled, `mono` only).
  `render.py` 563, 957, 1173 read `grey_images`; 607-689, 1117-1131, 1357-1362 read `hatch`. `THEMES['mono']` gets
  both True (S reads, A defines).
- `resolve(spec, paper)`: `from_design(spec['design'], paper)` when a design is present, else `get(spec['theme'],
  paper)`. `worst_contrast` accepts a theme dict as well as a name.
- `fonts.resolve(requested, fmt, theme=None, stack=None, category=None) -> FontChoice`. Precedence: the font named in
  the request, then the design's stack, then `TRACEGRAPH_BODY_FONT`, then the built-in. **PDF** walks the stack for an
  installed static `.ttf` and embeds it, else the category's built-in (Helvetica, Times-Roman, Courier). **Word,
  PowerPoint, Excel** never name a brand font that is not the env font; they name the first stack entry on the Office
  list (`Calibri, Arial, Segoe UI, Georgia, Cambria, Times New Roman, Consolas, Courier New`), else the category's
  default (sans Calibri, serif Georgia, mono Consolas). One note per file: "The design's font {family} isn't available
  here, so the file uses {used}." Markdown: "A Markdown file carries no colours or fonts; convert it to PDF, Word or
  PowerPoint to see the design (0 tokens)."

### 4.6 Per format (S, in `render.py` and `rules.py`)

Every `themes.get(name)` call in `render.py` (412, 779, 988, 1307) and `rules.py` (438, 493, 618) becomes
`themes.resolve(spec, paper=...)`; `diagram.py` already takes the dict.

| Role | PDF | DOCX | PPTX | XLSX | MD |
|---|---|---|---|---|---|
| bg | page fill on every page | `w:background` plus `displayBackgroundShape` when not white (new) | slide background solid fill | none (sheets stay white) | none |
| text, heading, muted | body, headings, captions and page numbers | run colours, Heading 1 to 3 style colours | body, title, notes-free captions | cell font colour, title row | none |
| accent | links and rules | links | accent shapes | first chart series when palette is empty | none |
| header_bg, header_text, border, stripe | tables | tables | tables and diagram outlines | header row fill and font, borders, banding | none |
| palette, hatch | charts and diagrams | chart and diagram pictures | native charts and diagrams | native charts | none |
| fonts | embedded or built-in (4.5) | named | named | named | none |
| radius | diagram boxes | diagram pictures | rounded rectangle adjustment | none | none |

V10 (S, `rules.verify`): PDF has a fill operator with the design bg and text colours; DOCX has the background and a
heading run in the heading colour; PPTX slide 2 has the bg fill; XLSX header fill equals `header_bg`; MD passes with
the note of 4.5.

`agents/create.build()` (A) adds `meta['design'] = design.applied(...)`, and `caveats_for` adds the design notes and a
failed V10.

### 4.7 Tests

- `tests/test_create_design.py` (A) on `evals/fixtures/design_system.md` (F writes it, text in 7.4): bg F7F4ED,
  surface F7F4ED, text 1C1C1C, heading 1C1C1C, muted 5F5F5D, border ECEAE4, header_bg 1C1C1C, header_text FCFBF8,
  body font "Camera Plain Variable" with fallbacks `['ui-sans-serif', 'system-ui', 'sans-serif']`, category sans,
  radius 12, hatch True, 3B82F6 not in the palette, a conflict note present. A variant where the colour names carry no
  role words still gives the same roles. A bad design (text CCCCCC on FFFFFF) is nudged to 4.5:1 with a note.
  `is_design` for the fixture with "use the design.md for the design" is at least 0.9; for `meeting_notes.txt` below
  0.5. `split_docs` keeps the fixture as content for "summarise this design system". Routing: design only with a last
  file restyles at 0 tokens; design only with a prior answer; design plus deps (the 2750 shape); fake engine prompts
  never contain "Color Palette".
- `tests/test_create_render.py` (S): the design in all 5 formats (checks of 4.6), `worst_contrast(resolve(spec)) >=
  4.5`.

---

## 5. Cost preflight (builder E)

### 5.1 `jevrouter/estimate.py` (new, pure: no network, no engine, no store)

```python
@dataclass
class EngineView:
    name: str; label: str; billing: str; web: bool
    healthy: bool = True
    p50_ms: float | None = None

@dataclass
class Draft:
    query: str
    mode: str = 'balanced'
    agent: str | None = None
    files: list[dict] = field(default_factory=list)   # FileInfo metas (name, kind, chars, columns); design files cost 0
    has_answer: bool = False        # the chat has an earlier answer
    last_file: dict | None = None   # the chat's newest CreatedFile meta
    source: str = 'chat'

@dataclass
class Estimate: ...                 # every field of the TS Estimate; to_dict() gives exactly that JSON

def estimate(draft: Draft, engine: EngineView | None, web_engine: EngineView | None, *, deadline_s: float,
             alternatives: list[EngineView] = (), prices: dict | None = None) -> Estimate
def for_resume(state: dict, engine: EngineView | None, *, deadline_s: float) -> Estimate
def combine(estimates: list[Estimate]) -> Estimate    # several engines in one ask
def needs_confirmation(est: Estimate) -> bool          # the thresholds of 5.3
def actual_of(record: dict) -> dict                    # CostActual from a finished run record
```

### 5.2 Model

Steps are guessed from the draft with the same keyless code the run uses: `create.brief.parse_brief`,
`planner.is_file_request`, `planner.candidate_split`, `create.brief.asks_more`, `longdoc.plan_calls`,
`design.is_design` (design files add no tokens).

- **planner**: 1 call when an engine is set and the query splits (`candidate_split` gives 2 or more parts), the mode
  is research, or a file step follows a topic; else 0 (range low 0, high 1).
- **research**: 1 call on `web_engine` when mode is research, or the query asks for a file about a topic with no
  earlier answer, attachment or last file (low 0 in that second case).
- **create**: 0 calls for a zero-token path (last file in another format, earlier answer, attached table); the long
  writer when `asks_more(brief)`: outline + `plan.batches` + top-up (optional) + `MAX_REPAIR_CALLS` (optional);
  else 1 call + 1 repair (optional).
- **other agents**: 1 answer call per non-keyless step.
- **merge**: 1 call when there are 2 or more steps.
- Per call: `tokens_in = turns * (FIXED_IN + prompt + ctx + schema)`; `tokens_out = base_out * OUT_MULT`;
  `seconds = PER_CALL_S + tokens_out / 1000 * PER_KOUT_S`, with `ctx = min(context chars, LONG_CONTEXT_CHARS or
  CREATE_CONTEXT_CHARS) / 4`, `schema = 1,200` for DocSpec and 300 for the outline, `prompt = 300 + request chars / 4`,
  and `base_out` = outline 1,200; sections `words * 1.4 * 1.3 + 600` per batch; top-up 1,000; single create
  `{'pptx': 550, 'pdf': 500, 'docx': 500, 'md': 500, 'xlsx': 300}`; planner 500; merge 800; answer 700.
  Research: `RESEARCH = {'claude-code': (50_000, 3_000, 90), 'default': (40_000, 3_000, 90)}` (in, out, seconds).
  Images add 15 s and 0 tokens. Subscription engines run section batches one at a time, API engines two at a time.
- Engine constants (fitted to the runs in 7.3; `turns` is for a schema call, plain calls take 1, the agy planner 2):

  | Engine | FIXED_IN | turns | OUT_MULT | PER_CALL_S | PER_KOUT_S |
  |---|---|---|---|---|---|
  | agy | 12,400 | 3 | 4.0 | 20 | 3 |
  | codex | 15,200 | 2 | 2.0 | 15 | 6 |
  | claude-code | 1,500 | 1 | 1.5 | 8 | 10 |
  | anthropic | 300 | 1 | 1.0 | 3 | 12 |
  | default | 2,000 | 1 | 1.5 | 10 | 10 |

  When `p50_ms` is known, `PER_CALL_S` is `max(PER_CALL_S / 2, p50_ms / 1000)`.
- Range: low sums non-optional calls with `tokens_in * 0.75`, `tokens_out / 3`, `seconds * 0.7`; high adds optional
  calls with `tokens_in * 1.4`, `tokens_out * 2`, `seconds * 1.5`.
- `dollars` only when billing is `api`: `in * prices['claude_in'] / 1e6 + out * prices['claude_out'] / 1e6` for low
  and high.
- `cheaper`: each healthy alternative that can take every step (web for research) and whose mid tokens are at most
  0.6 of the current engine's, best first, at most 2.
- Worked example (run 2750 on Auto, agy lead, claude-code for web): planner 27.8K, research 50K, outline 47.7K,
  2 batches 102.6K, merge 15.6K: mid 243,700 in (actual 250,046), about 21,800 out (actual 30,492, inside the range),
  about 250 s (actual 272.5 s). The same file on claude-code: about 85,000 in.

### 5.3 Thresholds (E, `jevrouter/config.py`)

```python
COST_CONFIRM = True                      # TG_COST_CONFIRM=0 turns the guard off (read on every request)
COST_CONFIRM_TOKENS = {'agy': 120_000, 'codex': 120_000, 'claude-code': 150_000, 'anthropic': 60_000,
                       'default': 100_000}   # high end of tokens_in + tokens_out
COST_CONFIRM_CALLS = 5                   # mid model calls, planner and merge included
COST_CONFIRM_SECONDS = 180               # mid seconds
COST_DEADLINE_SHARE = 0.8                # high seconds above this share of the deadline
COST_SOURCES = ('chat', 'sandbox', 'you')
LEAN_LONG_FILES = True                   # TG_LEAN_LONG_FILES=0 turns 5.6 off
```

`needs_confirmation` is true when the run is not keyless and any of: high tokens at or above the engine's threshold,
mid calls at or above 5, mid seconds at or above 180, high seconds at or above 0.8 of the deadline.

### 5.4 API (E, `jevrouter/app.py`)

- `POST /api/estimate`, body `EstimateBody`: validates like `/ask` (same `Bad` errors), resolves engines (5.5),
  returns the `Estimate`. Never calls `router.submit` or any engine.
- `/ask` gains `confirm_cost`. After validation and before `submit`, when `COST_CONFIRM` is on, the source is in
  `COST_SOURCES` and `confirm_cost` is not `true`: compute the estimate (combined for `engines`, the run's for
  `retry_of`), and when `needs_confirmation`, return **409** `{"error": "This run needs about N model calls. Confirm to
  go ahead.", "needs_confirmation": true, "estimate": ...}` without starting anything. Keyless runs, `source: 'eval'`
  and `compare` never get a 409. `confirm_cost` that is not a boolean is a 400.
- The estimate (asked for or not) goes into `submit(extras={'estimate': ...})`. The pipeline stores `record['cost'] =
  {'estimate', 'actual': None}`; at the end `actual = estimate.actual_of(record)` (calls = phases calls + 1 for an LLM
  planner + 1 for an LLM merge + 1 per LLM agent step; tokens from `record.tokens`; seconds from `total_ms`). The
  `done` event and `/api/runs` carry `cost`. Each created file's meta gets the same `cost` (its own tokens as actual).
- `POST /api/created/resume` and `GET /api/runs/{qid}/checkpoints`: section 3.5.

### 5.5 Engine resolution and the lean long writer (E)

- `app.engine_views(router, chat, engine) -> (EngineView | None, EngineView | None, list[EngineView])`: a pinned
  engine is itself; Auto gives `router.engine.chain()[0]` and `chain(web=True)[0]`; deep mode gives
  `router.deep_engine()`; keyless gives None. Health from `router.health` (p50, cooling).
- **H8**: `agents.Tuned` gains `prefer: str | None = None` and passes `first=prefer` to `stream` only when the wrapped
  engine is an `AutoEngine`. In `make_file`, when `LEAN_LONG_FILES`, the engine is Auto and `asks_more(brief)`,
  `prefer` is the name of the cheapest healthy engine by `estimate` (ties keep Auto's order). A user-pinned engine is
  never changed. The dialog's "Use {label} instead" sends that engine pinned.

### 5.6 Tests (E)

- `tests/test_estimate.py`: the calibration rows of 7.3 (from `evals/fixtures/estimate_runs.jsonl`): mid `tokens_in`
  and mid `seconds` within 2x of actual, actual `tokens_out` inside the range. Keyless draft gives 0 calls and
  `needs_confirmation` false. The 2750 draft on agy needs confirmation; the same draft on a fake engine with default
  constants and a 1-slide deck does not. `for_resume` of a state with 2 missing parts prices 1 call plus repair.
- `tests/test_cost_api.py`: with a fake engine named `agy`, the 2750 query in research mode gets 409 with the
  estimate, the run count is unchanged and the fake engine saw 0 calls; again with `confirm_cost: true` gives 200 and a
  qid; `/api/estimate` returns the same estimate and starts nothing; keyless gives 200 without confirming;
  `source: 'eval'` is never asked; `engines` of 2 sums them; `record.cost.actual` is filled after the run; resume
  guard.
- `tests/conftest.py` (E): an autouse fixture sets `TG_COST_CONFIRM=0` so existing tests keep their behaviour; the
  new tests turn it on.

---

## 6. Web (builder W)

The contract is in `web/src/protocol.ts` (end) and `web/src/api.ts` (end): `estimateRun`, `askOrConfirm`,
`resumeFile`, `runCheckpoints`, types `Estimate`, `NeedsConfirmation`, `RunCost`, `CheckpointInfo`, `PartialInfo`,
`DesignApplied`.

### 6.1 `web/src/components/chat/CostDialog.tsx` (new)

Built on `components/ui/dialog.tsx`. Props: `estimate: Estimate`, `onContinue()`, `onCancel()`,
`onSwitch(engine: string)`, `remember: boolean`, `onRemember(v: boolean)`. Exact copy (no dashes):

- Title: "This will take several model calls"
- Lead: `estimate.summary` (server text, e.g. "This deck needs about 6 model calls on Antigravity and Claude Code:
  roughly 240,000 tokens and about 4 to 6 minutes.")
- A 4-row grid: "Model calls" `calls` ("up to" high); "Tokens" about mid, "from {low} to {high}"; "Time" "{low} to
  {high} minutes" (seconds under 120 shown as seconds); "Engine" `engine_label`, "uses your subscription" or the dollar
  range for api billing.
- "Why": `reasons[].text` as a short list.
- When `cheaper[0]`: "{label} would use about {tokens} tokens for the same work." with the button
  "Use {label} instead".
- When high seconds are at least 0.8 of `deadline_s`: "This may come close to the {minutes} minute limit. If it runs
  out of time, what was written is kept and you can resume it."
- Checkbox "Don't ask again in this chat". Buttons "Cancel" and "Continue" (Continue has focus). Esc cancels.
- Numbers: `toLocaleString`, tokens rounded to 2 significant figures ("about 240,000 tokens").

### 6.2 Flows

- **Chat** (`pages/Chat.tsx` send and retry): call `askOrConfirm(body)`. On `confirm`, keep the composer state (text,
  files, agent) and open the dialog. Continue: `askOrConfirm({...body, confirm_cost: true})`. Use cheaper:
  `askOrConfirm({...body, engine: cheaper.engine, confirm_cost: true})` (for `engines` bodies, replace the costliest).
  Cancel: restore the composer exactly as the error path does today, and show nothing else. "Don't ask again in this
  chat" is kept per session id in `sessionStorage` (try/catch) and makes later sends carry `confirm_cost: true`.
- **Sandbox** (`pages/Sandbox.tsx`, the 3 `ask` calls): same dialog; the compare path asks once for the combined
  estimate. `sandbox/UsageMeter.tsx` shows "estimated" beside used tokens for the latest run when `cost` is present.
- **Resume**: `resumeFile({qid, tid, sandbox_id?})` with the same confirm flow; on start, scroll to the new run.

### 6.3 File card (`components/chat/CreatedFiles.tsx`) and run detail

- Cost line under the tokens: "Estimated {mid} tokens, used {actual}" (only when `cost.estimate` exists), and
  "{n} repair call(s)" when `repairs`.
- Partial: a warning badge "Partial: {written} of {planned} {slides|sections}" with the missing headings in its
  tooltip, and a primary button "Resume" (hidden when `partial.resume` is null). Button busy text "Starting".
- Design chip: "Design: {name}" with a popover of colour swatches (with their hex text, never colour alone) and the
  fonts used, plus `design.notes`.
- A failed file step (`answered.ok false` with `checkpoint.resumable`): the step shows "No file was made" and the
  "Resume" button, and `file_failed` runs get the failure style instead of success.
- `pages/RunDetail.tsx`: a "Cost" row, estimate vs actual (calls, tokens in and out, seconds) and the checkpoints list.

### 6.4 Checks

`cd web && npx tsc -p . --noEmit` passes. A live check on a port from 8790 to 8799 with a throwaway `TG_DB` and a
fake engine only (never 8777).

---

## 7. Fuzz, evals and fixtures (builder F)

### 7.1 `tests/test_create_fuzz.py` (seeded, deterministic)

- Generator `gen_spec(rng) -> (spec, markers)`: starts from one of 6 valid bases (short doc, 11-section deck, tables
  report, charts, diagrams, mixed) and applies 1 to 4 mutations drawn from the catalog: the 24 probe shapes, the
  string series, the 27 odd shapes of the fuzz audit (ragged tables, tree cycles, dangling edges, string events,
  nested bullets, 5,000-character words, 200-node trees), number strings (`''`, `' '`, `'%'`, `'()'`, `'(%)'`,
  `'$'`), lone surrogates, 130-character headings, 5,000-character column names, subtitles of 256 and 300, 41 sections,
  31 blocks, page-break-only, figure-only, CJK, Arabic, Hindi and emoji text, None or a wrong type at every key, depth
  60 nesting, NaN and infinity, booleans, `FETCH_KEYS` everywhere. Every text field carries a unique marker word
  (`zq` + 6 letters) so preservation can be checked.
- `N = int(os.environ.get('TG_FUZZ_N', '300'))`; seeds `20260928 + i`; `TG_FUZZ_SHARD=k/m` runs every m-th spec so
  the 5,000 run can be split across processes.
- Properties, for every spec and every format in `('pdf', 'docx', 'pptx', 'xlsx', 'md')`:
  - **P1** `normalize` raises nothing but `SpecError`, and only with rule `S2`, `L6` or `X1`.
  - **P2** when the oracle finds any text in a section (headings, notes, block strings outside the skipped keys of
    2.3 step 5 and outside figure blocks), `normalize` does not raise.
  - **P3** `render_safe(norm, fmt)` returns bytes that `verify` reopens (V1 ok) for all 5 formats.
  - **P4** `normalize(normalize(spec, fmt)[0], fmt)` does not raise and keeps the section count.
  - **P5** every marker in a text field of a block (not figure) survives into `spec_text(normalize(spec, 'md')[0])`.
  - **P6** a mutation that drops or converts content leaves at least one `fix` result with `ok` false.
- Also: the run 2750 replay (fake engine, fixture `evals/fixtures/spec_2750_shape.json`) builds a 12-slide deck with an
  S1 note; with the design fixture attached and "use the design.md for the design", slide backgrounds are F7F4ED.

### 7.2 `evals/cases.jsonl` (new cases; the case schema test must pass)

- `cr-2750-deck`: the 2750 query with `files: ["design_system.md"]`, mode research, `expect_agents` research and
  create, `expect_file {format: "pptx", slides: [12, 12], diagrams_min: 2, design_bg: "F7F4ED"}`, tags
  `["create", "pptx", "design", "long"]`.
- `cr-design-pdf`: "make a 2 page pdf about tea using the design.md" with the design fixture, `design_bg`.
- `cr-design-restyle`: two turns, "put that in slides" then "restyle it with the attached design.md" (0 tokens,
  `source: "convert"`).
- `cr-pictured`: "a ppt of 10 slides on bridges using pictured diagrams", `images_min: 1`.
- `cr-12-slides`: "make a 12 slide deck on the history of mobile phones", `slides: [12, 12]`.
- `jevrouter/evals.py` (F, file checks only): `FILE_KEYS` gains `design_bg` (RRGGBB compared to
  `CreatedFile.design.colors.bg`) and `partial_ok` (boolean; false fails a partial file).

### 7.3 Fixtures

`evals/fixtures/estimate_runs.jsonl`, one row per recorded run: `{qid, query, mode, engine, web_engine, planner,
merger, files: [{name, chars}], actual: {tokens_in, tokens_out, seconds}}`. Rows (read with `sqlite3 -readonly`):

| qid | engine (planner / merger) | query (start) | files | in | out | s |
|---|---|---|---|---|---|---|
| 1629 | agy (heuristic / single) | Create a short presentation on the water cycle | | 44,086 | 3,476 | 19.8 |
| 1662 | agy (heuristic / single) | Make slides about solid-state batteries | | 42,376 | 3,035 | 28.3 |
| 1717 | agy (heuristic / single) | Turn these release notes into a short summary file | release_notes.txt 774 | 40,936 | 1,063 | 22.8 |
| 2346 | agy (heuristic / single) | Make slides about solid-state batteries | | 41,386 | 2,158 | 24.0 |
| 2381 | agy (heuristic / single) | Create a short presentation on the water cycle | | 42,125 | 2,644 | 19.2 |
| 2401 | agy (heuristic / single) | Turn these release notes into a short summary file | release_notes.txt 774 | 89,057 | 2,596 | 30.6 |
| 2402 | agy (agy / llm) | Write a short cover letter ... as a Word document | | 38,956 | 2,900 | 35.3 |
| 2403 | agy (agy / llm) | Make a PDF of the decisions and action items ... | meeting_notes.txt 604 | 51,236 | 687 | 23.5 |
| 2405 | agy (agy / single) | put that table in an Excel sheet | | 26,302 | 60 | 11.3 |
| 2749 | Auto: claude-code (llm / llm), research claude-code | pdf on the ai what is ai ... (275 chars) | | 104,690 | 23,781 | 314.8 |
| 2750 | Auto: agy lead (llm / llm), research claude-code | create the ppt on the how mobile phone ... | DESIGN 17,284 (design) | 250,046 | 30,492 | 272.5 |

`evals/fixtures/design_system.md` (exact text):

```markdown
# Design System: Lovable style

## 1. Visual Theme & Atmosphere
A warm, paper-like canvas. Cream background, charcoal text, no saturated accents.

## 2. Color Palette & Roles
- **Cream** (`#f7f4ed`): Page background and primary surface.
- **Cream Surface** (`#f7f4ed`): Cards and panels.
- **Charcoal** (`#1c1c1c`): Primary text and headings.
- **Charcoal 82%** (`rgba(28,28,28,0.82)`): Body copy.
- **Muted Gray** (`#5f5f5d`): Secondary text and captions.
- **Light Cream** (`#eceae4`): Borders and dividers.
- **Off-White** (`#fcfbf8`): Text on dark buttons.
- **Ring Blue** (`#3b82f6`): Focus rings only, never decorative.

## 3. Typography Rules
- Font family: `Camera Plain Variable`, fallbacks: `ui-sans-serif, system-ui, sans-serif`
- Headings: Camera Plain Variable, weight 600
- Code: `ui-monospace, SFMono-Regular, Menlo, monospace`

## 4. Component Stylings
Primary button: charcoal (#1c1c1c) background, off-white (#fcfbf8) text, border-radius 12px.

## 7. Do's and Don'ts
- Don't use pure white (#ffffff) as a background.
- Don't introduce saturated accent colors.

## 9. Agent Prompt Guide
### Quick Color Reference
- Page background: Cream (#f7f4ed)
- Body text: Muted Gray (#5f5f5d)
- Headings: Charcoal (#1c1c1c)
- Borders: Light Cream (#eceae4)
```

`evals/fixtures/spec_2750_shape.json`: the outline (11 parts: title "The Evolution of Mobile Phones", headings from
"Before mobile phones" to "What comes next") and two batch replies, the second with section 7 as
`{"heading": "Smartphones arrive", "level": 1, "blocks": [{"type": "bullets", "items": null}], "notes": ""}`.

---

## 8. File ownership (5 parallel builders)

| Builder | Owns (only these files) |
|---|---|
| **S** spec and rules | `jevrouter/create/spec.py`, `rules.py`, `render.py`, `preview.py`, `diagram.py`, `create/__init__.py`, `docs/RULES-files.md`, `tests/test_create_spec.py`, `tests/test_create_render.py`, `tests/test_create_rules.py`, `tests/test_create_diagram.py` |
| **A** agent and design | `jevrouter/agents/create.py`, `jevrouter/create/longdoc.py`, `create/design.py` (new), `create/themes.py`, `create/fonts.py`, `create/brief.py`, `create/assets.py`, `tests/test_create_longdoc.py`, `tests/test_create_design.py` (new), `tests/test_create_brief.py`, `tests/test_create_assets.py`, `tests/longdoc_stub.py` |
| **E** estimate and API | `jevrouter/estimate.py` (new), `jevrouter/app.py`, `jevrouter/pipeline.py` (hooks H2 to H8 only), `jevrouter/config.py`, `jevrouter/agents/__init__.py` (`Tuned.prefer` only), `tests/test_estimate.py` (new), `tests/test_cost_api.py` (new), `tests/test_pipeline.py`, `tests/test_create_api.py`, `tests/conftest.py` |
| **W** web | `web/**` except `web/src/protocol.ts` (the contract is fixed; `web/src/api.ts` may gain helpers but its appended block keeps its names) |
| **F** fuzz and evals | `tests/test_create_fuzz.py` (new), `evals/cases.jsonl`, `evals/fixtures/*` (new files only), `jevrouter/evals.py` (`FILE_KEYS` and their checks only) |

Nobody else edits `docs/PLAN-files-robust.md` or `web/src/protocol.ts`.

### 8.1 Cross-builder interfaces (write the signature first, fill it later)

| Provider | Function | Used by |
|---|---|---|
| S | `spec.parse_spec`, `spec.repair`, `spec.has_text`, `render.render_safe` | A (agent, longdoc), F |
| A | `themes.resolve`, `themes.clean_design`, `themes.from_design`, the `hatch` / `grey_images` keys, `fonts.resolve(..., stack, category)` | S (render, rules, normalize) |
| A | `longdoc.plan_calls`, `longdoc.info`, `longdoc.MAX_REPAIR_CALLS`, `design.is_design`, `design.MENTION` | E |
| A | `Job.checkpoint`, `Job.deadline`, `Job.resume`, `Job.design_docs`, `Made.partial`, `Made.repairs` | E |
| E | `config.COST_*`, `estimate.estimate`, `/api/estimate`, 409 body, `/api/created/resume` | W, F |
| F | `evals/fixtures/design_system.md`, `spec_2750_shape.json`, `estimate_runs.jsonl` | A, E, S tests |

Order within the first hour: S adds `parse_spec`, `repair` (wrapping today's logic), `has_text` and `render_safe`
(calling `render`) as working stubs; A adds `themes.resolve` (returning `get(spec.get('theme'), paper)`),
`themes.clean_design` (returning None), `longdoc.plan_calls`, `longdoc.info` and the new `Job` and `Made` fields; E adds
the config constants; F writes the three fixtures from this document. After that, each fills in its own side.

---

## 9. Acceptance

| Target | How it is checked | Owner |
|---|---|---|
| 5,000 seeded specs x 5 formats: 0 crashes, 0 whole-file refusals when any block holds text | `TG_FUZZ_N=5000 pytest tests/test_create_fuzz.py` (sharded with `TG_FUZZ_SHARD`), P1 to P6 | F (S and A fix) |
| Run 2750's failing shape gives a 12-slide deck with an S1 note | `test_create_longdoc.py` and the fuzz replay | A, F |
| Its design gives F7F4ED slide backgrounds and 1C1C1C text, design file absent from prompts | `test_create_design.py`, fuzz replay | A, S |
| Design changes colours and fonts in PDF, DOCX, PPTX, XLSX; MD notes it; contrast at least 4.5:1 | `test_create_render.py` design cases | S |
| Estimator within 2x on recorded runs (mid tokens in and seconds; tokens out inside the range) | `test_estimate.py` on `estimate_runs.jsonl` | E |
| 409 flow: no run and no engine call without `confirm_cost`; 200 with it; keyless and evals never asked | `test_cost_api.py` | E |
| Estimated vs used stored on the run and the file | `test_cost_api.py`, UI check | E, W |
| A timed-out or partial long file resumes without repeating paid calls | `test_create_longdoc.py` resume, `test_create_api.py` resume | A, E |
| A failed create keeps its phases and tokens, marks `file_failed`, and uses the template merge | `test_pipeline.py` | E |
| `pytest -q` passes (fakes only) and `cd web && npx tsc -p . --noEmit` passes | whole suite | all |

---

## 10. Not in this plan

- Passing `max_tokens` to CLI engines (they ignore it today) and trimming agy's own tool set. Measuring whether
  `--disable-slash-commands` lowers agy's input needs a real call, which only the user can approve.
- Retrying a pinned engine on "stream was interrupted".
- Better image search queries for descriptive captions (the 2749 image shortfall).
