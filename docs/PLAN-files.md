# Create files: PDF, DOCX, PPTX, XLSX and Markdown

Ask in Chat, "make a PDF report of this", "turn this table into an Excel sheet", "slides on solid-state batteries", and
get a real file to download, built from the conversation, attached files or a fresh answer. Every file follows
[`docs/RULES-files.md`](RULES-files.md).

## Token budget: the model writes content once, code does the rest

The expensive part of a document is not the words, it's the layout. So the model never writes layout:

1. **One compact spec per request.** The engine returns a `DocSpec` (JSON: title, sections, blocks of paragraphs,
   bullets, tables, charts) under a JSON schema, with no styling, fonts, coordinates or XML. A 10-slide deck is
   ~1–2 K output tokens instead of the 10–20 K a model spends writing python-pptx code or HTML.
2. **Zero-token paths first.** When the content already exists, no LLM is called:
   - "Put that answer in a PDF/DOCX/MD" → the previous turn's answer is parsed into a spec (headings, lists,
     Markdown tables) by code.
   - "Export this CSV/JSON to Excel", "chart the sales by region" → the attached table becomes the spec.
   - "Now as slides/PDF/Excel" → the stored spec of the last created file is re-rendered in the new format.
   These also work keyless.
3. **Right-sized calls.** The spec call carries only what it needs (the request, the last 3 turns trimmed to
   `CREATE_CONTEXT_CHARS`, table schemas plus first rows instead of whole files) with `max_tokens` per format
   (MD/DOCX/PDF 3 K, PPTX 2.5 K, XLSX 1.5 K since data comes from tables) and `effort: low` unless the request asks
   for research or a long report.
4. **Deterministic renderers** (python-docx, python-pptx, openpyxl, reportlab, plain Markdown) apply one theme,
   the size limits and the safety rules. Converting a file never costs tokens.
5. **Token use is visible:** each created file records the tokens its spec cost (0 for zero-token paths).

## How it fits the pipeline

- A new built-in agent **`create`**: "Create a downloadable file: PDF, Word (DOCX), PowerPoint (PPTX), Excel (XLSX)
  or Markdown". Jev routes to it like any agent; `@create` forces it.
- **Format detection** is keyless (pdf, word/docx, slides/deck/presentation/pptx, excel/spreadsheet/xlsx/sheet,
  markdown/md). No format named: PDF for reports, PPTX for "slides", XLSX when the content is mostly tables, else
  Markdown; the answer says which it chose and offers the others.
- A multi-step query works: "research X and make slides about it" plans a research step, then a `create` step that
  depends on it, so the spec is built from that answer.
- The `create` step's answer is a short text ("Created **battery-report.pdf**, 4 pages") plus the file on the
  `answered` event. Merging keeps the file list.
- Sandbox: created files live in the sandbox's memory only, like its uploads.

## Python interface (fixed; `jevrouter/create/`)

```python
# spec.py
FORMATS = ('pdf', 'docx', 'pptx', 'xlsx', 'md')
DOCSPEC_SCHEMA: dict            # JSON schema handed to engine.stream(schema=...)
def normalize(spec: dict, fmt: str) -> tuple[dict, list[RuleResult]]   # S1-S6, L1-L3 fixes; raises SpecError on block
def from_markdown(text: str, title: str | None = None) -> dict           # zero-token: an answer -> spec
def from_table(meta: dict, rows: list[list], columns: list[str], title: str | None = None) -> dict  # zero-token
def detect_format(text: str) -> str | None
def file_name(title: str, fmt: str) -> str                              # F7

# render.py
def render(spec: dict, fmt: str, theme: str = 'clean') -> bytes         # F1-F6, X1-X3, A1-A4

# rules.py
@dataclass class RuleResult: id: str; severity: str; ok: bool; note: str   # severity: block | fix | warn
def verify(spec: dict, fmt: str, data: bytes) -> list[RuleResult]        # V1-V4, L4
RULES: list[dict]                                                         # {id, group, text, severity, enforced}

# preview.py
def preview(fmt: str, data: bytes) -> dict   # {kind, ...}: md text, outline (headings/slide titles), sheets (first rows), pages
```

`SpecError(rule_id, message)` carries the rule that blocked.

## DocSpec

```json
{"title": "Solid-state batteries", "subtitle": "Pros and cons", "format": "pptx", "theme": "clean",
 "sections": [
   {"heading": "Why they matter", "blocks": [
     {"type": "paragraph", "text": "..."},
     {"type": "bullets", "items": ["...", "..."]},
     {"type": "table", "columns": ["Metric", "Li-ion", "Solid-state"], "rows": [["Energy density", 250, 400]]},
     {"type": "chart", "kind": "bar", "title": "Energy density (Wh/kg)", "labels": ["Li-ion", "Solid-state"],
      "series": [{"name": "Wh/kg", "values": [250, 400]}]},
     {"type": "quote", "text": "...", "by": "..."},
     {"type": "code", "lang": "python", "text": "..."}
   ], "notes": "speaker notes (pptx only)"}]}
```
Chart kinds: `bar`, `line`, `pie`. Themes: `clean` (default), `dark`, `warm`.

## API contract

TypeScript shapes: end of `web/src/protocol.ts` (`CreatedFile`, `FileFormat`, `RuleResult`, `RuleInfo`,
`FilePreview`) and `created_files` on `AnsweredFields`; client in `web/src/api.ts`.

- `answered` for a `create` step carries `created_files: CreatedFile[]`; stored on the task in the run record.
- `GET /api/created?limit=&before=` → `{files: CreatedFile[]}` newest first.
- `GET /api/created/{id}` → `CreatedFile`; `DELETE /api/created/{id}`.
- `GET /api/created/{id}/download` → the bytes, `Content-Disposition: attachment; filename="<name>"`, the right
  MIME type, `X-Content-Type-Options: nosniff`.
- `GET /api/created/{id}/preview` → `FilePreview`.
- `POST /api/created/{id}/convert {format}` → a new `CreatedFile` from the stored spec (0 tokens); 400 for an
  unknown format, 409 if it's the same format.
- `GET /api/rules` → `{rules: RuleInfo[]}`.
- Sandbox: `GET /api/sandbox/{sid}/created/{id}/download` and `/preview`; nothing is written to disk.

Storage: table `created(id, qid, name, format, size, created, spec, tokens, rules, meta)`; bytes under
`data/created/<id>` (never a user-supplied path).

## Work split

| Builder | Files |
|---|---|
| Renderer + rules | `jevrouter/create/{__init__,spec,render,rules,preview,themes}.py`, `tests/test_create_*.py` (render, rules, spec) |
| Integration | agent registration, format detection wiring, spec call with token caps, zero-token paths, store table, endpoints, events, sandbox, `requirements.txt`; `tests/test_create_api.py` |
| Web | `components/chat/CreatedFiles.tsx` (file cards: icon, name, size, pages/slides/sheets, download, convert menu, preview, rule warnings, tokens), Run detail, a **Files** page (`#/files`) listing created files, presets ("PDF report", "Slides from this", "Excel from this table") |
| Evals | cases tagged `create` with `expect_file: {format, contains: [...], min_pages?, max_pages?, sheets?, slides?, rules_ok: true}`; harness checks the created file by reopening it |

Order: renderer first (the integration builder starts once its interface exists); web and evals in parallel against
the contract. Then review (renderer correctness, security of downloads and spreadsheets, token use) and fixes.

## Verification

- Unit: every block type in every format, limits and splits, formula injection, file names, reopen checks, preview.
- End to end (keyless, 0 tokens): "put that in a PDF" after an answer; an attached CSV to XLSX with a chart; convert
  PDF → DOCX → PPTX → MD from one spec.
- One engine run (Antigravity) of the `create` eval cases, reporting spec tokens per file.
- Open the files: page counts, slide titles, sheet headers checked by the harness; a few opened by hand.
