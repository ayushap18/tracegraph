# Ruleset for created files

Every file TraceGraph creates (PDF, DOCX, PPTX, XLSX, Markdown) is checked against these rules before it is offered
for download. Each rule has an ID. `jevrouter/create/rules.py` enforces the ones marked **enforced** and reports
every result in the run trace and on the file card, so a user can see why a file looks the way it does.

Severity:
- **block**: the file is not produced. The answer says which rule failed and why.
- **fix**: the renderer corrects it automatically and records that it did.
- **warn**: the file is produced; the warning is shown on the file card.

## 1. Content spec (what the model returns)

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| S1 | The spec validates against the DocSpec schema: known block types only, required fields present. | block | yes |
| S2 | At least one section with at least one non-empty block. | block | yes |
| S3 | A title of 1–120 characters. Missing: taken from the first heading or the request. | fix | yes |
| S4 | Text is plain: no HTML, no raw Markdown syntax inside paragraphs (converted to plain text, with bold/italic kept as runs where the format supports them). | fix | yes |
| S5 | Numbers in tables and charts are numbers, not strings ("1,200" → 1200). Non-numeric chart values drop that point. | fix | yes |
| S6 | Every table row has as many cells as there are columns (short rows padded, long rows trimmed). | fix | yes |
| S7 | Facts in the file come from the conversation, attached files or the model's answer in this run; the spec never asks the renderer to fetch anything. | block | yes |

## 2. Size limits (token and file budgets)

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| L1 | Spec at most 60 KB of JSON; sections ≤ 40; blocks per section ≤ 30. | block | yes |
| L2 | Tables ≤ 2,000 rows × 30 columns in XLSX; ≤ 200 rows in PDF/DOCX/MD (rest summarised with a note); ≤ 12 rows per slide (split across slides). | fix | yes |
| L3 | Slides: ≤ 6 bullets per slide and ≤ 18 words per bullet; longer content moves to the slide notes. | fix | yes |
| L4 | Output file ≤ 15 MB. | block | yes |
| L5 | The model is asked for the spec once per request; conversions to another format reuse the stored spec (0 LLM tokens). | — | yes |

## 3. Structure and style, per format

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| F1 | PDF: A4 (Letter when the locale is US), 2 cm margins, page numbers, the title in the document properties, headings as PDF outline entries. | fix | yes |
| F2 | DOCX: built-in Heading 1–3 styles (so a table of contents works), real Word tables with a header row, core properties set. | fix | yes |
| F3 | PPTX: 16:9; a title slide, then one slide per section; native charts (not pictures); speaker notes for overflow text. | fix | yes |
| F4 | XLSX: one sheet per table (sheet names ≤ 31 chars, unique, no `[]:*?/\`), a bold frozen header row, column widths fitted, numbers stored as numbers, native charts next to their data. | fix | yes |
| F5 | Markdown: CommonMark with GFM tables; one `#` title; headings never skip a level. | fix | yes |
| F6 | One theme per file (font, colours); body text ≥ 10 pt in PDF/DOCX and ≥ 18 pt on slides. | fix | yes |
| F7 | File names: lowercase words joined by hyphens from the title, the right extension, ≤ 80 characters, no path separators. | fix | yes |

## 4. Safety

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| X1 | No macros or active content: never `.docm`/`.xlsm`/`.pptm`, no embedded scripts, no OLE objects. | block | yes |
| X2 | Spreadsheet formula injection: a cell whose text starts with `=`, `+`, `-`, `@`, tab or CR is stored as text with a leading apostrophe, unless it came from a formula the spec marked as one, built only from cell references and a short allow-list of functions (SUM, AVERAGE, MIN, MAX, COUNT, ROUND). | fix | yes |
| X3 | No external references: no remote images, no external links in formulas, no linked (non-embedded) objects. Hyperlinks are allowed only as `http(s)` text links. | fix | yes |
| X4 | Content Jev would block is not written to a file: the request goes through the same safety check as any question, and so does the spec text. | block | yes |
| X5 | Created files are stored like uploads (by id, never by user path). Sandbox files stay in memory and disappear with the sandbox. | — | yes |

## 5. Verification (after rendering)

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| V1 | The file reopens with its own library (pypdf, python-docx, python-pptx, openpyxl) or parses as Markdown. | block | yes |
| V2 | The title and every section heading appear in the reopened file's text. | warn | yes |
| V3 | Page, slide or sheet counts match the spec (after the L2/L3 splits). | warn | yes |
| V4 | Every chart in the spec exists in PPTX/XLSX, or is drawn in PDF, or appears as a table in DOCX/MD. | warn | yes |

## 6. Accessibility

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| A1 | Headings are real headings (styles, outline entries, slide titles), never bold paragraphs. | fix | yes |
| A2 | Tables have a header row marked as a header. | fix | yes |
| A3 | Charts carry a text title, and a one-line summary of what they show appears next to them. | warn | yes |
| A4 | Colour is never the only way information is shown; theme colours meet 4.5:1 contrast for text. | fix | yes |

## Changing the rules

A new rule gets the next ID in its group, a severity and a test in `tests/test_create_rules.py`. A rule is never
renumbered, so IDs in old run traces keep their meaning.
