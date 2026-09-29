# Ruleset for created files

Every file TraceGraph creates (PDF, DOCX, PPTX, XLSX, Markdown) is checked against these rules before it is offered
for download. Each rule has an ID. `jevrouter/create/rules.py` enforces the ones marked **enforced** and reports
every result in the run trace and on the file card, so a user can see why a file looks the way it does.

Severity:
- **block**: the file is not produced. The answer says which rule failed and why.
- **fix**: the renderer corrects it automatically and records that it did.
- **warn**: the file is produced; the warning is shown on the file card.

A file is refused only when nothing in it can be shown (S2), the reply is too large to read (L6), the format is unknown (X1), the safety check says no (X4), or the file cannot be written even as plain text (V1).

## 1. Content spec (what the model returns)

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| S1 | Each block is repaired to the DocSpec schema: known aliases are mapped (list, ul, ol to bullets; content or body to text; points or lines to items), a missing type is inferred from its keys, and a string where a list is expected is split into lines. A block that still cannot be read keeps its text as a paragraph; a block with no text is left out. Every change is noted. | fix | yes |
| S2 | The file has something to show: at least one section with a heading or a non-empty block, not counting page breaks and figures that found no image. A spec with no text at all is refused. | block | yes |
| S3 | A title of 1–120 characters. Missing: taken from the first heading or the request. | fix | yes |
| S4 | Text is plain: no HTML, no raw Markdown syntax inside paragraphs (converted to plain text, with bold/italic kept as runs where the format supports them). | fix | yes |
| S5 | Numbers in tables and charts are numbers, not strings ("1,200" → 1200; "$1,200.50" and "12%" become numbers that keep their money or percent format). Non-numeric chart values drop that point. | fix | yes |
| S6 | Every table row has as many cells as there are columns (short rows padded, long rows trimmed). Diagrams are well formed: one root, no loops or cycles, known node names. Column names are at most 80 characters, cut at a word. | fix | yes |
| S7 | Nothing is fetched: link, image and path keys (url, src, href, image, link, path and the like) are removed from the spec with a note, and the file holds only the content given. | fix | yes |
| S8 | Every section has a heading: a missing one is taken from its first line of text, or becomes "Section N" (slides: the previous slide's heading with "(cont.)"). A section given as plain text becomes a heading-only section, or a paragraph when it is longer than 80 characters. | fix | yes |

## 2. Size limits (token and file budgets)

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| L1 | At most 200 KB of spec JSON (table rows are limited by L2), 40 sections and 30 blocks per section. Blocks past 30 continue in a section headed "<heading> (cont.)"; sections past 40, or past 200 KB, are cut from the end and the note says how many. | fix | yes |
| L2 | Tables ≤ 2,000 rows × 30 columns in XLSX; ≤ 200 rows in PDF/DOCX/MD (rest summarised with a note); ≤ 12 rows per slide (split across slides). Diagrams: at most 30 timeline events, 40 tree nodes in 4 levels, 12 flow steps. | fix | yes |
| L3 | Slides: ≤ 6 bullets per slide and ≤ 18 words per bullet; longer content moves to the slide notes. | fix | yes |
| L4 | Output file ≤ 15 MB. | block | yes |
| L5 | The model is asked for the spec once per request; conversions to another format reuse the stored spec (0 LLM tokens). | — | yes |
| L6 | A model reply larger than 2 MB is not read as a file spec. | block | yes |

## 3. Structure and style, per format

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| F1 | PDF: A4 (Letter when the locale is US), 2 cm margins, page numbers, the title in the document properties, headings as PDF outline entries. | fix | yes |
| F2 | DOCX: built-in Heading 1 to 3 styles (so a table of contents works), real Word tables with a header row, core properties set. The subject property holds at most 255 characters of the subtitle, cut at a word. | fix | yes |
| F3 | PPTX: 16:9; a title slide, then one slide per section; native charts (not pictures); speaker notes for overflow text. The subject property holds at most 255 characters of the subtitle, cut at a word. | fix | yes |
| F4 | XLSX: one sheet per table (sheet names ≤ 31 chars, unique, no `[]:*?/\`), a bold frozen header row, column widths fitted, numbers stored as numbers, native charts next to their data. | fix | yes |
| F5 | Markdown: CommonMark with GFM tables; one `#` title; headings never skip a level. | fix | yes |
| F6 | One theme per file (font, colours); body text ≥ 10 pt in PDF/DOCX and ≥ 18 pt on slides. | fix | yes |
| F7 | File names: lowercase words joined by hyphens from the title, the right extension, ≤ 80 characters, no path separators. | fix | yes |

## 4. Safety

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| X1 | No macros or active content: never `.docm`/`.xlsm`/`.pptm`, no embedded scripts, no OLE objects. | block | yes |
| X2 | Spreadsheet formula injection: a cell whose text starts with `=`, `+`, `-`, `@`, tab or CR is stored as text with a leading apostrophe, unless it came from a formula the spec marked as one, built only from cell references and a short allow-list of functions (SUM, AVERAGE, MIN, MAX, COUNT, ROUND). | fix | yes |
| X3 | No remote references; images only as embedded bytes from the asset cache. No external links in formulas, no linked (non-embedded) objects. Hyperlinks are allowed only as `http(s)` text links. | fix | yes |
| X4 | Content Jev would block is not written to a file: the request goes through the same safety check as any question, and so does the spec text. | block | yes |
| X5 | Created files are stored like uploads (by id, never by user path). Sandbox files stay in memory and disappear with the sandbox. | — | yes |
| X6 | Every embedded image is a PNG or JPEG from the local asset cache, re-encoded, found under an allowed licence (public domain, CC0, CC BY, CC BY-SA), with a credit line naming its title, author, licence and source. An image without a full credit is left out, never embedded. | fix | yes |

## 5. Verification (after rendering)

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| V1 | The file reopens with its own library (pypdf, python-docx, python-pptx, openpyxl) or parses as Markdown. | block | yes |
| V2 | The title and every section heading appear in the reopened file's text. | warn | yes |
| V3 | Page, slide or sheet counts match the spec (after the L2/L3 splits). | warn | yes |
| V4 | Every chart and diagram in the spec exists in PPTX/XLSX, or is drawn in PDF, or appears as a table or picture in DOCX/MD. | warn | yes |
| V5 | Pages (or slides) are within the count the request asked for ("asked for 12-13 pages, made 9"). | warn | yes |
| V6 | When images were asked for, at least one licensed image is embedded. | warn | yes |
| V7 | The requested font was used, or the answer names the font used instead. | warn | yes |
| V8 | The file uses the theme the request asked for; a black and white PDF draws only greys (every `rg`/`RG` colour has r = g = b) and its images are DeviceGray. | warn | yes |
| V9 | At least as many diagrams are drawn as the request asked for (2 for "multiple diagrams", 1 for "a diagram"). | warn | yes |
| V10 | When a design file was applied, the file uses its background and text colours (or the answer says what could not be used). | warn | yes |
| V11 | A section the renderer cannot lay out in this format is retried as plain text, then left out, and the answer names it. | fix | yes |
| V12 | The file holds every planned section, or the answer lists the missing ones and offers Resume. | warn | yes |

## 6. Accessibility

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| A1 | Headings are real headings (styles, outline entries, slide titles), never bold paragraphs. | fix | yes |
| A2 | Tables have a header row marked as a header. | fix | yes |
| A3 | Charts carry a text title, and a one-line summary of what they show appears next to them. | warn | yes |
| A4 | Colour is never the only way information is shown; theme colours meet 4.5:1 contrast for text. | fix | yes |

## 7. Design (Studio's visual QA)

Files laid out by the design stage (docs/PLAN-designer.md, on when `TG_STUDIO` names the format) carry a design report.
These rules are read from it: one result each, shown on the file card. Files made without the design stage have none.
The design stage fixes what it can by code first (shrink, rebalance, compact variant, split, re-crop, swap layout,
snap to the grid), so a failure here is what was still not ideal after its last round.

| ID | Rule | Severity | Enforced |
|---|---|---|---|
| D1 | Overflow: no text is taller than its box (0.5 pt of slack) and no line is wider than its box. | warn | yes |
| D2 | Overlap: no two boxes overlap by more than 1 square point, unless the upper one is meant to sit on top (text on an overlay, art behind type). | warn | yes |
| D3 | Readability: text is at least the minimum size (slides 18 pt, captions 12 pt; print 10 pt, captions 8 pt) and meets 4.5:1 contrast (3:1 for large text), measured against the photo under it too. | warn | yes |
| D4 | Density: a slide holds at most 40 words of bullets or 60 of prose, and 25 to 60 percent of each page is white space (15 to 60 percent in print). | warn | yes |
| D5 | Balance: the visual weight of a page sits in its middle third both ways (asymmetric layouts, covers and freeform pages are skipped). | warn | yes |
| D6 | Consistency: text sizes are on the type scale, text left edges sit on grid columns, and the file uses one image treatment. | warn | yes |
| D7 | Variety: no layout is used on more than 3 slides in a row, and a deck of 8 or more slides uses at least 4 layouts. | warn | yes |
| D8 | Images: no picture is enlarged more than 1.5 times (at 96 dpi on slides, 150 dpi in print) and its focal point stays inside the crop. | warn | yes |

When the design stage fails, or the file it painted fails a block rule above, the file is made with the standard
layout instead and the answer says so: a file is never lost to the design stage.

## Changing the rules

A new rule gets the next ID in its group, a severity and a test in `tests/test_create_rules.py`. A rule is never
renumbered, so IDs in old run traces keep their meaning.
