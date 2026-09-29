# Studio: a designer for every file TraceGraph makes

Status: plan, not built. Audience: **students** (class presentations, lab reports, posters, revision notes), so the bar
is "looks like a good designer made it, reads well, prints well, and is honest about what it couldn't do".

## 1. Why this is needed (evidence)

Run 2808 asked for "a 12 slide PPT … bold and creative with a dark design, a strong cover, diagrams, a chart … and
real pictures". It got 12 correct slides, 5 credited photos and a chart, but:

- a **white** background ("dark design" was not recognised as the dark theme);
- a plain cover: title and subtitle on white;
- **one** layout for every slide: title, then content stacked below;
- 1 diagram where 2+ were asked for.

The cause is structural. Content (the DocSpec) goes straight to four renderers, each with its own fixed layout. Nothing
decides *where* things go, *how big* they are, or *which* layout suits a slide. There are 4 colour themes and 2 slide
layouts, no font downloads, no way to measure text, and no way to look at the result. "Creative" in a prompt changes
nothing because nothing can act on it.

## 2. The idea: a design stage between content and rendering

```
content (DocSpec) ──► Studio ──────────────────────────────────────────► painters (pptx, pdf, docx, md, xlsx)
                        │ 1. art direction (1 small LLM call, or keyless rules)
                        │ 2. design tokens (preset + design.md + prompt words)
                        │ 3. assets: fonts, images, icons, diagrams, illustrations → workspace
                        │ 4. layout engine: per page/slide, boxes with exact positions and sizes
                        │ 5. visual QA: render thumbnails, measure, auto-fix (≤ 3 passes)
                        ▼
                  DesignPlan (positioned, format-aware) + thumbnails + design report
```

Today's renderers become **painters**: they draw a positioned DesignPlan and make no layout decisions of their own
(PPTX and PDF). DOCX and Markdown are flowing formats, so for them Studio decides styles, spacing, figure placement and
page breaks, not coordinates.

**Token budget.** Layout, fonts, QA and restyling are code. The only model call is art direction: about 1–2K tokens
out for a 12-slide deck. **Restyle** (another preset, another font, dark vs light) and **re-layout** cost 0 tokens,
because the content is already stored.

## 3. Components

### 3.1 Art director (`jevrouter/studio/direct.py`)

Input: the DocSpec outline (headings, block types, counts, image slots), the brief (format, pages/slides, mood words,
audience), design tokens. Output, under a JSON schema:

```json
{"preset": "bold-dark", "mood": ["bold", "cinematic"],
 "pages": [{"layout": "cover-hero", "image": 0, "emphasis": "title"},
           {"layout": "section-divider"},
           {"layout": "big-number", "focus": "block:1"},
           {"layout": "image-left-text", "image": 2}, …]}
```

- Keyless fallback: rules by content shape (a section with one stat → big-number; with an image and ≤ 4 bullets →
  image-left-text; a timeline → full-width-diagram; the first slide → cover; every 3–4 slides in long decks → divider).
- The director only chooses from the layout library and presets; it never writes coordinates, colours or fonts.

### 3.2 Design tokens (`studio/tokens.py`, extends `create/design.py`)

One `DesignSystem` built from, in priority order: an attached **design.md** (or brand/style/theme file), explicit prompt
words ("dark", "minimal", "playful", "pastel", "Poppins"), the chosen **preset**, then defaults.

- Colour roles (existing) plus: gradient pairs, overlay tints for text on photos, chart palette (colour-blind safe),
  accent shapes.
- Typography: display/heading/body/caption/mono families, a **modular type scale** (ratio 1.25–1.5 by preset),
  line-height, letter-spacing for display, minimum sizes (slides ≥ 18 pt body, print ≥ 10 pt).
- Space: an 8-point grid, margins, gutters, a 12-column grid per format and paper size.
- Shape language: corner radius, stroke weight, shadow on/off, image treatment (full-bleed, rounded, framed, duotone).
- design.md v2 reads type scales, spacing units, radii, imagery rules and "do / don't" lines, not only colours and fonts.

**Presets** (each a complete DesignSystem, tuned for students):

| Preset | For |
|---|---|
| `bold-dark` | pitches, space/tech topics, strong covers |
| `editorial` | history, literature, essays; serif display, generous white space |
| `minimal` | science, maths; lots of air, one accent |
| `vibrant` | school projects, younger students; playful shapes, bright palette |
| `pastel` | biology, wellbeing, revision notes |
| `academic` | lab reports, research posters; print-first, strict hierarchy |
| `mono` | print and photocopy friendly (exists today) |
| `high-legibility` | dyslexia-friendly: Atkinson Hyperlegible / Lexend, larger spacing, no justified text |

### 3.3 Font manager (`studio/fonts.py`, replaces the lookup in `create/fonts.py`)

- **Sources, in order:** fonts already cached; the system's installed fonts; **open-licensed families downloaded on
  demand** from Fontsource (jsDelivr CDN) or the google/fonts repository.
- **Only open licences** (SIL OFL 1.1, Apache 2.0, Ubuntu Font Licence): the licence is read from the source's
  metadata before download and stored with the font. Anything else is never downloaded.
- **Requests for fonts that can't be downloaded** (proprietary: "Anthropic Sans", "Helvetica Neue", "SF Pro", a brand
  font) get the closest open alternative from a curated similarity table (category, x-height, width, contrast), and
  an honest note: "Anthropic Sans isn't openly licensed, so this uses Inter, the closest open match". A user-supplied
  `.ttf`/`.otf` (upload or `TRACEGRAPH_BODY_FONT`) is used as-is.
- **Measuring:** fontTools reads real advance widths, ascender/descender and x-height, so the layout engine knows
  exactly how much space text needs.
- **Embedding:**
  - PDF: subset and embed (reportlab TTF), so it looks identical everywhere.
  - PPTX and DOCX: python-pptx and python-docx can't embed fonts. The file names the font; the file card says "install
    Inter (free) for the exact look" with a link, and PowerPoint falls back to a safe equivalent chosen by Studio.
    Measurements use the fallback's metrics when they are wider, so text still fits if the font isn't installed.
- **Cache:** `data/cache/fonts/<family>/<style>.ttf` plus `LICENSE` and `meta.json`, LRU-capped (200 MB), shared
  across files. Downloads go through the existing egress guard (http(s) only, public addresses, size caps).

### 3.4 Assets and illustrations (`studio/assets.py`, `studio/icons.py`, `studio/art.py`, `create/diagram.py`)

- **Photos:** the existing Commons search (licence allow-list, credits), plus **smart crop**: a focal point from an
  edge/entropy map (Pillow) so faces and subjects aren't cut off in a full-bleed cover; minimum resolution per slot.
- **Icons:** a bundled open icon set (Lucide, ISC licence, about 150 curated icons: science, history, maths,
  geography, tech, arrows, UI). Drawn as native vector shapes in PPTX/PDF and picked by keyword ("battery", "planet",
  "dna") for bullets, stat cards and diagrams.
- **Illustrations without photos:** seeded, code-drawn decorative art: geometric patterns, gradient meshes, blobs,
  orbit rings, grids, wave dividers. Consistent with the preset, never random noise.
- **Diagrams:** keep timeline, tree and flow; add **cycle**, **venn** (2–3 sets), **pyramid**, **2×2 matrix**,
  **mind map**, **process arrows**, **comparison cards**, **labelled figure** (callouts on an image), **stat cards**
  and **scatter**. All drawn natively (editable shapes in PPTX), styled by the design tokens.
- Every rendered asset (diagram PNG/vector, cropped photo, icon) goes into the workspace (3.6) and is reused across
  conversions and restyles.

### 3.5 Layout engine (`studio/layout.py`, `studio/library.py`)

Deterministic. For each page or slide: take the layout from the library, fill its slots, then **fit**.

**Layout library (slides, 16):** cover-hero (full-bleed photo + overlay + title), cover-type (big display type +
art), section-divider, title-bullets, image-left-text, image-right-text, full-bleed-image-caption, big-number,
stat-cards (2–4), quote, two-column, comparison, full-width-diagram, chart-focus, timeline-strip, closing / thank-you.

**Page templates (PDF/print, 8):** cover, chapter opener, text with side figure, two-column text, full figure page, pull
quote, summary / key-points box, references and credits.

**Fitting, in order, until everything fits:**
1. Measure every text run with the font's real metrics; wrap to the slot width.
2. Shrink within the type scale (never below the preset's minimum).
3. Rebalance: move a caption, shorten to the slide's bullet limit (overflow goes to speaker notes), switch to the
   layout's compact variant.
4. Split: continue on a new slide/page with "(continued)".

Images: `cover` or `contain` per slot, focal-point crop, never upscaled past 1.5×. Alignment snaps to the grid; every
text box keeps its safe margin; reading order is kept for screen readers.

### 3.6 Workspace, the "temporary memory" (`studio/workspace.py`)

A per-file working directory, `data/cache/design/<file-id>/`: the DesignPlan (JSON), cropped images, rendered
diagrams, measured text, thumbnails and a manifest. Fonts live in the shared font cache (3.3).

- Restyle, convert and resume read from it, so nothing is fetched or measured twice.
- LRU-capped (1 GB) with a 14-day TTL; a file deleted by the user takes its workspace with it.
- Sandbox files keep their workspace in a temp dir that is removed when the sandbox is cleared.

### 3.7 Visual QA (`studio/qa.py`)

Studio draws its own thumbnail of every page/slide from the DesignPlan with Pillow, using the same fonts and geometry
the painters use (LibreOffice isn't installed, and exact PowerPoint rendering isn't available here). It checks and
auto-fixes (≤ 3 passes):

| Check | Rule |
|---|---|
| Overflow / clipping | no text box's measured height exceeds its slot |
| Overlap | no two boxes intersect except by design (text on overlay) |
| Readability | body ≥ minimum size; contrast ≥ 4.5:1 (3:1 for display ≥ 24 pt), including text over photos |
| Density | words per slide ≤ 40 (bullets) / 60 (text); white-space ratio 25–60% |
| Balance | visual weight centre within the middle third (except intentional asymmetric layouts) |
| Consistency | one type scale, aligned left edges, consistent image treatment |
| Variety | no layout used on more than 3 consecutive slides; a deck of 8+ slides uses 4+ layouts |
| Images | resolution per slot, focal point inside the crop |

The results are a **design report** (score 0–100 plus notes) stored with the file, shown on the file card, and
checked by new ruleset entries (D1–D8 in `docs/RULES-files.md`). Thumbnails are served to the UI.

### 3.8 The design agent loop (`studio/agent.py`)

Studio is an agent, not a single pass. For each file it runs a bounded loop:

```
direct ─► assemble assets ─► lay out ─► render thumbnails ─► QA checks ─┬─► pass: paint the file
   ▲                                                                   │
   └──── revise (code fixes, then targeted re-direction) ◄── critique ◄─┘  (≤ 3 rounds, token budget)
```

1. **Code fixes first** (0 tokens): every QA failure maps to a deterministic fix (shrink, rebalance, compact variant,
   split, re-crop, swap layout for the next best in the same family).
2. **Design critic** (optional, vision): when the active engine can read images (Claude Code; the Anthropic API) and
   the mode is Deep or the user pressed **Polish**, the slide thumbnails (downscaled, contact sheet of 6 per image) go
   to the model with the design report. It returns structured edits only, never free text:
   `{"page": 4, "action": "change_layout|emphasize|reduce_text|swap_image|recolor_accent|enlarge_title|add_icon", "arg": …}`.
   Edits are validated against the layout library and tokens, applied by code, and re-checked by QA. Budget: ≤ 2
   critic rounds, ≤ 6K tokens in total; engines without vision skip it with a note.
3. **Free-form hatch** for at most 2 pages per file (the cover, a poster panel, an infographic): the director may mark
   a page `freeform`. The model then returns a small **shape program**, a JSON list of primitives (`rect`, `ellipse`,
   `line`, `path`, `text`, `image`, `icon`, `diagram`) in grid units with token colour names, never raw coordinates in
   points or raw colours. Studio validates it (bounds, overlap, contrast, minimum sizes, only workspace assets), fits the
   text with real metrics, and paints it natively in PPTX and PDF. If it fails QA twice, the page falls back to the
   closest library layout. Budget ≤ 2K tokens out per freeform page.
4. **Stop rules:** QA pass, the round limit, or the token budget, whichever comes first. The design report says which,
   and lists anything still not ideal.

Every step (direction, assets, layout decisions, QA results, critic edits, fallbacks) is recorded in the workspace
and summarised in the design report, so a student can see why the file looks the way it does.

## 4. Student-first features

- **Templates** by task: class presentation, lab report, research poster (A3/A2), revision notes, infographic,
  book report, science-fair board. Each is a preset + a page sequence + tone rules.
- **Print-friendly:** A4/Letter, grayscale-safe palettes, a "print version" restyle at 0 tokens.
- **Accessibility:** minimum sizes, contrast AA, alt text on every image/diagram/icon, reading order, a
  high-legibility preset, no text baked into images.
- **Honest sourcing:** image credits, citations slide/page from research sources, no unlicensed fonts or images.
- **Speaker notes** for every slide and an optional **handout PDF** (slides + notes) at 0 tokens.

## 5. API and UI

| Interface | Purpose |
|---|---|
| `GET /api/design/presets` | presets and templates with preview thumbnails |
| `GET /api/fonts/search?q=` | open-licensed families (name, category, licence, preview) |
| `POST /api/created/{id}/restyle` `{preset?, fonts?, dark?, template?, layouts?}` | re-layout and re-render from the stored content, 0 tokens |
| `GET /api/created/{id}/thumbs` | page/slide thumbnails (PNG) from the workspace |
| `GET /api/created/{id}/design` | the design report and the DesignPlan summary |
| `POST /api/created/{id}/polish` `{engine?, confirm_cost?}` | one critic round on the stored design (vision engines only), edits applied by code; goes through the cost estimate |

`CreatedFile` gains `design: {preset, fonts, score, notes}`. The file card gets a **Design** panel: thumbnail strip,
preset picker, font picker with live previews, per-slide layout override, "print version", and the design report.

## 6. Build order (each phase leaves the app working)

1. **Foundations:** `fontTools` dependency, font manager with open-licence downloads and cache, workspace, DesignSystem
   + presets, design.md v2, prompt-word detection (fixes "dark design" today).
2. **Layout engine + PPTX painter + agent loop:** the 16 slide layouts, fitting, art director (keyless rules first,
   then the model call), thumbnails + QA, code fixes, the vision critic and the freeform hatch. Presentations are where
   design matters most for students.
3. **PDF page templates + painter**, with embedded subset fonts.
4. **Icons, illustrations, new diagram kinds, smart crop.**
5. **DOCX/Markdown styling from the same DesignSystem; XLSX chart palette.**
6. **UI:** Design panel, restyle, font and preset pickers, thumbnails.
7. **Evals:** golden decks per preset, design-report thresholds, before/after thumbnails, token and time budgets.

## 7. Verification

- Seeded fuzz of DocSpecs × presets × formats: 0 crashes, 0 QA overflow/overlap failures after fitting.
- Golden set: 10 student prompts (history deck, biology poster, physics lab report, …) × 3 presets; every result
  scores ≥ 80 on the design report, uses ≥ 4 layouts per 8+ slides, and passes contrast and minimum-size checks.
- Replay run 2808 ("bold and creative with a dark design"): a dark preset, a photo cover with an overlay, ≥ 4 layouts,
  ≥ 2 diagrams, thumbnails shown; restyle to `minimal` costs 0 tokens.
- Fonts: requesting Poppins downloads it once (OFL recorded) and embeds it in the PDF; "Anthropic Sans" gets an open
  alternative and an honest note; offline, everything falls back to installed fonts without failing.
- Token budget: art direction ≤ 2K tokens out; restyle and re-layout 0 tokens; critic ≤ 6K per file; freeform ≤ 2K
  out per page.
- Agent loop: a deliberately cramped deck is fixed by code within 3 rounds; a critic edit that breaks QA is rolled
  back; a freeform page that fails QA twice falls back to a library layout; engines without vision skip the critic.

## 8. Risks and limits

- **PowerPoint and Word can't embed fonts via python-pptx/python-docx.** Mitigated by measuring against the wider of
  the chosen font and its fallback, and by telling the user which free font to install.
- **Thumbnails are Studio's own rendering**, not PowerPoint's; small differences are possible. The PDF path is exact.
- **Licensing:** only openly licensed fonts, icons and images, with the licence stored next to each asset.
- **Cost of assets:** font and image downloads are cached and capped; offline mode degrades to installed fonts and
  code-drawn art, never to a failed file.
- New dependencies: `fontTools` (MIT). Optional later: `pypdfium2` (Apache-2.0/BSD) to rasterize the real PDF for QA.

## 9. Contract (exact)

Phase 0 froze this contract as code: `jevrouter/studio/` exists with every module below as a behaviour-free stub
(unbuilt functions raise `NotImplementedError('… builder X')`; data tables, dataclasses and schemas are real), and
`tests/test_studio_contract.py` checks every signature, dataclass field, schema and table here. **Changes are additive
only** (a new optional parameter, a new field at the end of a dataclass, a new optional JSON key) and are announced to
every builder; the contract test fails on removals, renames, reordering or changed defaults. Units are PDF points
(1/72 in), top-left origin; colours inside Studio are **token names**, never hex, except in `DesignSystem.colors`.

### 9.1 Package layout and public API

`jevrouter/studio/__init__.py` re-exports `design, enabled, paint, polish, restyle`, the plan dataclasses and schemas,
`SLIDE_LAYOUTS, PAGE_TEMPLATES, PRESETS, TEMPLATES, DEFAULT_PRESET, DesignSystem`. Importing it has no side effects.

| Module | Owner | Public API (Python signatures; `async` marked) |
|---|---|---|
| `plan.py` | frozen (L amends) | dataclasses `BoxStyle, Box, PagePlan, FontUse, AssetUse, QaResult, DesignPlan, PageDirection, ArtDirection, CriticEdit, ShapeProgram, RestyleOptions, DesignResult` (all with the fields in 9.3–9.4); `design_plan_schema() -> dict`; `art_schema(llm: bool = True) -> dict`; `critic_schema() -> dict`; `freeform_schema(cols=12, rows=12) -> dict`; `report_schema() -> dict`; `validate(value, schema, where='$') -> list[str]` (JSON-Schema subset, `[]` = valid); `DIAGRAM_BLOCK_SCHEMAS`, `DIAGRAM_KINDS_NEW`; constants `BOX_KINDS, TYPE_STEPS, TOKEN_COLORS, CHECK_IDS, CRITIC_ACTIONS, FREEFORM_PRIMITIVES, STOP_REASONS, PAINTED=('pptx','pdf')` |
| `tokens.py` | F | dataclasses `TypeScale, Families, Spacing, Shape, MinSizes, DesignSystem` (+ `size(step, fmt) -> float`, `color(name) -> str`, `to_dict()`, `from_dict(d)`); `build_system(*, preset=None, design=None, prompt='', fmt='pptx', font=None, dark=None) -> DesignSystem` (design.md > prompt words > preset > default; never raises); `with_dark(ds, dark) -> DesignSystem`; `print_version(ds) -> DesignSystem`; `from_design(design: dict) -> dict` (partial overrides from a cleaned design.md); `legacy_theme(ds, fmt) -> dict` (a `create/themes` theme dict so legacy renderers use the same tokens) |
| `presets.py` | F | data `PRESETS, DEFAULT_PRESET='minimal', LEGACY_THEME, THEME_OF, PROMPT_WORDS, DARK_MOOD, TEMPLATES`; dataclasses `PresetInfo, TemplateInfo, PromptStyle`; `get(preset_id) -> DesignSystem` (unknown → default); `list_presets() -> list[PresetInfo]`; `list_templates() -> list[TemplateInfo]`; `template(id) -> TemplateInfo \| None`; `read_prompt(text) -> PromptStyle` (no LLM); `for_theme(theme) -> str` (built) |
| `fonts.py` | F | data `CACHE_DIR=data/cache/fonts, CACHE_BYTES=200 MB, STYLES, LICENCES_OK=('OFL-1.1','Apache-2.0','UFL-1.0'), SOURCES, SIMILAR`; dataclasses `FontFace, FontMetrics (+advance(text, size_pt)), FontResolution, FontInfo`; `resolve_local(family) -> FontResolution \| None` (cache → system, no network); `async ensure(family, http, *, styles=STYLES) -> FontResolution` (cache → system → Fontsource/jsDelivr → google/fonts, open licences only, via the egress guard; never raises); `alternative(family) -> (open_family, note) \| None`; `metrics(path) -> FontMetrics` (fontTools); `measure(text, family, size_pt, *, bold=False, italic=False, fmt='pptx') -> float` (pptx/docx: wider of the family and its office fallback); `wrap(text, family, size_pt, width_pt, *, bold=False, italic=False, fmt='pptx') -> list[str]`; `line_metrics(family, size_pt, line_height=1.2) -> (ascent, descent, advance)`; `async search(q, http, limit=20) -> (list[FontInfo], offline)`; `preview_png(family, text='The quick brown fox', px=32) -> bytes \| None`; `prune(cap_bytes=CACHE_BYTES) -> int` |
| `workspace.py` | F | `DESIGN_DIR=data/cache/design, CAP_BYTES=1 GB, TTL_DAYS=14`; `class Workspace(file_id, root=None)` with `exists()`, `save_plan(plan)` (old one → `plan.prev.json`), `load_plan(previous=False)`, `save_report(report)`, `load_report()`, `put(kind, name, data) -> 'ws:<kind>/<name>'`, `get(ref) -> bytes \| None`, `path_of(ref) -> Path`, `save_thumb(page0, png) -> ref`, `thumbs() -> list[Path]`, `log(event)`, `events()`, `manifest()`, `copy_to(file_id) -> Workspace`, `delete()`; `open_workspace(file_id, *, sandbox=None)`; `drop(file_id)`; `drop_sandbox(sandbox_id)`; `prune(cap_bytes, ttl_days) -> int`. On disk: `plan.json report.json manifest.json events.jsonl images/ diagrams/ art/ thumbs/<001>.png text/measure.json` |
| `direct.py` | L | dataclasses `PageOutline, Outline`; `outline_of(spec, fmt) -> Outline`; `direct_keyless(outline, ds, *, dark=None) -> ArtDirection` (rules of 3.1 + D7 variety); `async direct_llm(outline, ds, engine, *, mood=(), max_tokens=2000, effort='low') -> (ArtDirection, usage)` (falls back to keyless on any failure); `validate_direction(raw, outline, fmt) -> (ArtDirection, notes)` |
| `library.py` | L | data `SLIDE_GRID=(12,12), PAGE_GRID=(12,16)`, dataclasses `Slot, LayoutDef`, `SLIDE_LAYOUTS` (16), `PAGE_TEMPLATES` (8) — 9.5; `get(id) -> LayoutDef`; `slide_layouts()`; `page_templates()`; `for_format(fmt) -> tuple[str, ...]`; `slot_kinds(id) -> {slot: kinds}` (built); `next_best(id, avoid=frozenset(), *, needs=()) -> str \| None` |
| `layout.py` | L | `SLIDE=(960,540)`, `PAPER{a4,letter,a3,a2}`, `FIX_ACTIONS=('shrink','rebalance','compact','split','recrop','swap_layout','snap','enlarge')`, dataclass `FitResult`; `page_size(fmt, paper=None)` (built); `grid_rect(area, size, ds, *, bleed=False, fmt='pptx') -> (x,y,w,h)`; `fit_text(text, family, step, ds, w, h, fmt, *, bold=False, line_height=None) -> FitResult`; `lay_out(spec, fmt, ds, direction, ws, *, file_id, paper=None) -> DesignPlan`; `lay_out_page(i, spec, fmt, ds, direction, ws, size) -> list[PagePlan]`; `refit(plan, spec, page, action, ds, ws, *, box=None, arg=None) -> bool`; `relayout(plan, spec, ds, ws, *, layouts=None) -> DesignPlan`; `flow_styles(spec, fmt, ds) -> dict` (docx/md/xlsx, `FLOW_SCHEMA`); `box_text(box, spec) -> str` |
| `paint_pptx.py`, `paint_pdf.py` | L | `paint(plan, spec, ws) -> bytes` each. No layout decisions; F/A/X rules hold as in `create/render.py`; PDF subsets and embeds fonts |
| `qa.py` | Q | data `CHECKS, THRESHOLDS, WEIGHTS, PASS_SCORE=80, FIXES, EMPTY_FIXES` (9.8); `check(plan, ws=None, *, thumbs=None) -> list[QaResult]`; `run_check(id, plan, ws=None, *, thumbs=None)`; `score(results) -> int` (built); `report(plan, results, *, critic=None, fallbacks=None, notes=None, thumbs=0) -> dict`; `contrast(fg, bg) -> float` (built) |
| `thumbs.py` | Q | `THUMB_PX=480, SHEET_PX=1200, 3×2 per sheet`; `render_page(plan, page, ws, *, width_px=480, spec=None) -> png`; `render_all(plan, ws, *, width_px=480, spec=None) -> list[ref]`; `contact_sheet(pngs, *, cols=3, rows=2, width_px=1200, first_page=1) -> png`; `sample(png, box, page_size) -> list[hex]`; `preset_thumb(preset_id, *, width_px=320) -> png` |
| `critic.py` | Q | dataclass `CriticReply`; `can_see(engine) -> bool` (built: `engine.supports_vision`); `async critique(plan, report, sheets, engine, *, budget_tokens=6000, effort='medium') -> CriticReply` (never raises); `validate_edits(raw, plan, ds) -> (edits, dropped)`; `apply_edit(plan, edit, spec, ds, ws) -> bool` |
| `freeform.py` | Q | `async compose(page, section, ds, engine, ws, *, fmt, refs=(), budget_tokens=2000) -> (ShapeProgram \| None, usage)`; `validate(raw, ds, ws, *, page, fmt, refs=()) -> (ShapeProgram \| None, problems)`; `to_boxes(program, size, ds, spec, *, fmt, ws) -> list[Box]` |
| `agent.py` | Q | `enabled(fmt) -> bool` (built); `async design(spec, fmt, *, file_id, brief=None, request='', tokens_src=None, engine=None, http=None, mode='balanced', sandbox=None, deadline=None, preset=None) -> DesignResult`; `restyle(spec, fmt, ws, options, *, file_id) -> DesignResult` (0 tokens); `async polish(spec, fmt, ws, engine, *, file_id, deadline=None) -> DesignResult` (raises `ValueError('no-vision')`, `LookupError('no-plan')`); `paint(plan, spec, ws) -> bytes` (built dispatcher); `meta_design(result) -> dict` |
| `icons.py` | V | `DATA=studio/data, ICONS_JSON=data/lucide.json, LICENSE=data/LICENSE-lucide, VIEWBOX=24, STROKE=2`, dataclass `Icon(name, tags, nodes)`; `names()`; `get(name)`; `pick(text, *, used=None) -> name \| None`; `polylines(icon, size_pt, *, tolerance=0.25) -> list[list[(x,y)]]`; `png(name, color, px) -> bytes` |
| `art.py` | V | `ART_KINDS=('dots','grid','gradient-mesh','blobs','orbit-rings','waves','stripes','corner-arcs')`; `draw(kind, seed, w, h, ds) -> list[primitive]` (deterministic); `pick(ds, mood, seed) -> kind`; `png(kind, seed, w_px, h_px, ds) -> bytes` |
| `assets.py` | V | `MIN_DPI={'pptx':96,'pdf':150,'docx':150}, MAX_UPSCALE=1.5`; `focal_point(data) -> (fx, fy)`; `crop_for(size_px, focal, box_w, box_h, fit='cover') -> (x,y,w,h) fractions`; `upscale(size_px, crop, box_w, box_h, fmt) -> float`; `prepare(asset, box_w, box_h, ws, *, fmt, fit='cover', mono=False, duotone=None) -> (ref, crop, focal)` |

The async functions are exactly `fonts.ensure, fonts.search, direct.direct_llm, critic.critique, freeform.compose,
agent.design, agent.polish`. Every `usage` dict is `{calls, llm_in, llm_out, ms}`; every `DesignResult.phases` entry
is `{phase, calls, llm_in, llm_out, ms}` with `phase` in `plan.PHASES` = `direct, assets, layout, thumbs, qa, fix,
critic, freeform, paint`, and is also stored as `report['phases']`. `Made.phases`/`CreatedFile.phases` keep their
existing phase names (the TS `FilePhase` union is not widened): Studio's totals are added to the `render` entry.

### 9.2 DesignSystem, presets and prompt words

`DesignSystem(id, name, dark, colors, chart_palette, gradients=[], overlay_alpha=0.55, families=Families(),
scale=TypeScale(), spacing=Spacing(), shape=Shape(), image='full-bleed', min_sizes=MinSizes(), hatch=False,
grey_images=False, justify=False, do=[], dont=[], source=[], notes=[])`

- **Colours by role** (`colors`, RRGGBB): `bg surface text heading muted accent accent2 border header_bg header_text
  stripe code_bg overlay on_accent`; `chart_palette` 6 colour-blind-safe colours, addressed as `chart1..chart6`.
  `TOKEN_COLORS` = those 14 roles + `chart1..6`; boxes, freeform shapes and critic edits may name only these.
- **Type scale**: `TypeScale(ratio=1.25, line_height=1.25, display_line_height=1.05, display_tracking=-0.01)`, steps
  `caption body lead h3 h2 h1 display` = `base × ratio^(-1..5)`, base 18 pt (pptx), 10.5 (pdf), 11 (docx/md/xlsx).
  `size(step, fmt)` never goes below `MinSizes(slide_body=18, slide_caption=12, print_body=10, print_caption=8)`.
- **Families by role**: `Families(display, heading, body, caption, mono)` (default Inter / JetBrains Mono).
- **Spacing grid**: `Spacing(unit=8, columns=12, slide_margin=48, page_margin=56.7, gutter=16, rows_slide=12,
  rows_page=16)`.
- **Shape language**: `Shape(radius=8, stroke=1, shadow=False, accent_shape in bar|circle|blob|corner|underline|none)`.
- **Image treatment**: `image in full-bleed|rounded|framed|duotone` (one per file, D6).
- **design.md v2** (F, `create/design.py`): `DesignTokens` gains optional fields (defaults keep today's parse) and
  `spec['design']` may carry `system: {scale_ratio, spacing_unit, radius, image, do[], dont[]}`; `clean_design` keeps
  it when valid; `tokens.from_design` maps it.

**Presets** (`PRESETS`, in this order): `bold-dark, editorial, minimal, vibrant, pastel, academic, mono,
high-legibility`. Default `minimal`. Legacy theme → preset: `clean→minimal, dark→bold-dark, warm→editorial,
mono→mono`; preset → `CreatedFile.theme`: `bold-dark→dark, editorial→warm, mono→mono`, others `clean`. Templates
(`TEMPLATES`): `class-presentation, lab-report, research-poster, revision-notes, infographic, book-report,
science-fair`, each `TemplateInfo(id, name, description, format, preset, paper, sequence, tone)`.

**Prompt words → presets/overrides** (`presets.PROMPT_WORDS`, `read_prompt(text) -> PromptStyle(preset, dark, mood,
fonts, print_version, words)`), strongest first:
1. an exact preset id/name ("bold-dark", "high legibility") → that preset;
2. accessibility and print words always win over mood: *black and white, monochrome, greyscale, photocopy,
   print-friendly, no colours* → `mono` + `print_version`; *dyslexia, high legibility, large print, easy to read,
   visually impaired* → `high-legibility`;
3. the first mood word in the text: *cinematic, dramatic, pitch deck* → `bold-dark`; *editorial, elegant, magazine,
   literary, serif* → `editorial`; *minimal(ist), clean look, sleek, simple design* → `minimal`; *vibrant, colourful,
   playful, fun, bright, for kids, creative, bold* → `vibrant`; *pastel, soft colours, calm, gentle* → `pastel`;
   *academic, lab report, research poster, scientific paper, thesis, formal* → `academic`;
4. *dark (design/theme/mode/background/slides/…)*, or bare *dark* → `dark=True`; *light …, white background* →
   `dark=False`; `vibrant` + dark → `bold-dark` (`DARK_MOOD`; run 2808's "bold and creative with a dark design"),
   any other preset + dark → `with_dark(preset, True)`;
5. font names (`create/brief.font_of`) → `fonts` override for body (or the role named: "headings in Poppins").

Priority of sources in `build_system`: design.md > prompt words > explicit `preset` argument (restyle/template) >
legacy spec theme (`for_theme`) > `DEFAULT_PRESET`.

### 9.3 DesignPlan JSON (`plan.design_plan_schema()`)

```json
{"version": 1, "file_id": "3f2a…(12 hex)", "format": "pptx", "preset": "bold-dark",
 "system": {…DesignSystem.to_dict()…}, "direction": {…ArtDirection…},
 "pages": [{"index": 0, "layout": "cover-hero", "w": 960, "h": 540, "section": null, "variant": "default",
            "freeform": false, "background": "bg", "continued": false, "notes": "speaker notes",
            "boxes": [{"id": "p0.image", "kind": "image", "x": 0, "y": 0, "w": 960, "h": 540, "z": 0,
                       "slot": "image", "content": "ws:images/cover.png", "fit": "cover",
                       "crop": [0.1, 0, 0.8, 1], "focal": [0.5, 0.4], "alt": "…", "bleed": true,
                       "style": {"fill": null, "stroke": null, "stroke_w": 0, "text_color": null, "radius": 0,
                                 "opacity": 1, "shadow": false}},
                      {"id": "p0.title", "kind": "text", "x": 48, "y": 300, "w": 640, "h": 120, "z": 2,
                       "slot": "title", "content": "spec:title", "font": "Inter", "step": "display", "size": 54,
                       "bold": true, "italic": false, "line_height": 1.05, "align": "left", "valign": "bottom",
                       "lines": ["Photosynthesis"], "overlay_ok": true, "style": {"text_color": "text"}}]}],
 "flow": null,
 "fonts": [{"family": "Inter", "role": "body", "source": "cache", "licence": "OFL-1.1", "embedded": false,
            "fallback": "Calibri", "requested": null, "note": null}],
 "assets": [{"ref": "ws:images/cover.png", "kind": "image", "source": "commons", "licence": "CC BY-SA 4.0",
             "credit": "…"}],
 "notes": [], "qa": [QaResult…], "score": 92, "rounds": 2, "stop": "pass",
 "tokens": {"direct_in": 0, "direct_out": 0, "critic_in": 0, "critic_out": 0, "freeform_in": 0, "freeform_out": 0}}
```

- `kind ∈ text|image|icon|diagram|chart|table|shape`; `x, y, w, h` points from the page's top-left; `z` paint order
  (low first); `style.*` colours are `TOKEN_COLORS` names; `font` a family resolved by `studio/fonts`; `size` points
  after fitting; `step` the scale step it came from; `align ∈ left|center|right|justify`, `valign ∈ top|middle|bottom`;
  `lines` the measured wrap (text boxes); `overlay_ok` marks intended overlap; `bleed` may touch the page edge.
- **Content refs** (`content`): `spec:title | spec:subtitle | spec:<s>/heading | spec:<s>/notes | spec:<s>/<b> |
  spec:<s>/<b>/items[i:j] | text:<literal ≤120 chars, generated labels only> | asset:<sha256> | ws:<kind>/<name> |
  icon:<lucide name> | art:<kind>:<seed> | none`. Content text never lives in the plan, so X4 still covers it.
- `pages` is empty for docx/md/xlsx; they carry `flow` (`FLOW_SCHEMA`: `styles` per step, `figures[{ref, placement:
  inline|full|side}]`, `breaks[section]`, `palette[hex]`).
- Plan ↔ JSON: `DesignPlan.to_dict()` / `DesignPlan.from_dict(d)` round-trip exactly.

### 9.4 Art direction, critic edits, freeform programs, results

**Art direction** (`plan.art_schema(llm)`; keyless and LLM produce the same shape): `{"preset": PRESETS id, "mood":
[≤4 words], "dark": bool|null, "pages": [{"layout": library id, "image": int|null (index into the spec's image blocks),
"emphasis": title|image|number|quote|diagram|chart|null, "focus": "block:<n>"|null, "variant": default|compact,
"freeform": bool}]}`. The LLM schema (`llm=True`) forbids `source`/`notes`; code adds `source ∈ keyless|llm|restyle`
and `notes`. pptx: one entry per slide (cover and closing included). pdf: one entry per section start (the page
template it opens with; long sections continue on the same template's flow slots). At most 2 `freeform: true`.

**Critic edits** (`plan.critic_schema()`): `{"edits": [{"page": ≥1, "action": change_layout|emphasize|reduce_text|
swap_image|recolor_accent|enlarge_title|add_icon, "arg": …}]}`, ≤ 12 edits, no other keys (no prose). `arg`:
`change_layout` → layout id; `emphasize` → slot name; `reduce_text` → max words 5..60; `swap_image` → image index or
null (next candidate); `recolor_accent` → token colour; `enlarge_title` → 1..2 steps; `add_icon` → Lucide name.
Pages are 1-based as shown on the contact sheet.

**Freeform shape program** (`plan.freeform_schema(cols=12, rows=12)`; pptx 12×12, pdf 12×16): `{"shapes": [{"type":
rect|ellipse|line|path|text|image|icon|diagram, "x","y","w","h": grid units in [0, cols|rows], multiples of 0.5, "z":
0..40, "fill"/"stroke": TOKEN_COLORS|null, "stroke_w": 0..4 (× preset stroke), "radius": 0..1 (fraction of the short
side), "opacity": 0..1, "text": ≤200 chars, "ref": content ref (image/diagram: only refs the page was offered; icon:
`icon:<name>`), "step": type step, "align", "weight": regular|bold, "points": [[x,y]…] ≤32 in grid units (line/path),
"alt": ≤200 chars}]}`, 1..40 shapes. Never points or hex. Budget ≤ 2K tokens out per page, ≤ 2 pages per file.

**New diagram kinds** (`plan.DIAGRAM_BLOCK_SCHEMAS`, builder V adds them to `create/diagram.KINDS` and the DocSpec):
`cycle{steps[3..8]}`, `venn{sets[2..3]{label, items}, shared}`, `pyramid{levels[3..6] top first}`,
`matrix{x_axis, y_axis, quadrants[4]{label, items} TL,TR,BL,BR}`, `mindmap{nodes[{id,label,parent}]}` (tree-shaped),
`process{steps[2..7]{label, detail}}`, `comparison{columns[2..3]{label, items}}`, `labelled{image: sha256,
callouts[1..8]{label, x, y ∈ 0..1}}`, `stat-cards{stats[2..4]{value, label, icon?}}`, `scatter{x_label, y_label,
points[2..200]{x, y, label?}}`; each also has `type` and `title`. Legacy renderers must draw every new kind (at worst
as `diagram.table_of`) so a converted file never loses content.

**QaResult**: `{id: D1..D8, ok, note, page (0-based)|null, box|null, value|null, threshold|null, fixed}`.

### 9.5 Layout library (`studio/library.py`, data frozen)

Slides: 960×540 pt, 12 cols × 12 rows inside the preset's slide margin; pages: 12 × 16 inside the page margin. Area
= `(col, row, cols, rows)`; `bleed` slots use the same grid over the whole page. Text slot steps in brackets; `*`
required; `fit` c = cover, n = contain; `ov` = overlay_ok; `flow` = print text that continues on the next page.
**Compact variant** (all layouts): the first h1/h2 text slot loses a row, slots below move up one row and grow by one,
every text slot one step smaller (never below the minimum).

| Slide layout | Family | Slots |
|---|---|---|
| `cover-hero` | cover | image* image (0,0,12,12) bleed c · overlay shape (0,0,12,12) bleed ov · title* [display] (0,6,10,3) ov · subtitle [lead] (0,9,10,1) ov · credit [caption] (8,11,4,1) ov right |
| `cover-type` (asym) | cover | art shape (7,0,5,12) bleed · kicker [caption] (0,2,7,1) · title* [display] (0,3,7,4) · subtitle [lead] (0,7,7,2) |
| `section-divider` (asym) | divider | number [display] (0,3,3,4) · title* [h1] (3,4,9,3) · accent shape (3,7,2,1) |
| `title-bullets` | text | title* [h2] (0,0,12,2) · body* [body] (0,2,12,10) ≤40 words |
| `image-left-text` (asym) | image-text | image* (0,0,6,12) bleed c · title* [h2] (7,0,5,2) · body [body] (7,2,5,9) · caption [caption] (7,11,5,1) |
| `image-right-text` (asym) | image-text | title* [h2] (0,0,5,2) · body [body] (0,2,5,9) · caption [caption] (0,11,5,1) · image* (6,0,6,12) bleed c |
| `full-bleed-image-caption` | image | image* (0,0,12,12) bleed c · overlay shape (0,9,12,3) bleed ov · caption [lead] (0,9,12,2) ov · credit [caption] (0,11,12,1) ov right |
| `big-number` (asym) | number | title [h3] (0,0,12,2) · number* [display] (0,2,7,6) · label [lead] (0,8,7,2) · context [body] (7,3,5,6) |
| `stat-cards` | number | title* [h2] (0,0,12,2) · cards* diagram `stat-cards` (0,3,12,7) · note [caption] (0,11,12,1) |
| `quote` | quote | mark icon (0,1,1,1) · quote* [h1] (1,2,10,6) · attribution [lead] (1,8,10,1) |
| `two-column` | columns | title* [h2] (0,0,12,2) · left* [body] (0,2,6,10) accepts table/chart/image/diagram · right* [body] (6,2,6,10) same |
| `comparison` | columns | title* [h2] (0,0,12,2) · left_head* [h3] (0,2,6,1) · left* [body] (0,3,6,9) · right_head* [h3] (6,2,6,1) · right* [body] (6,3,6,9) |
| `full-width-diagram` | figure | title* [h2] (0,0,12,2) · figure* diagram (0,2,12,9) n accepts chart/table/image · caption [caption] (0,11,12,1) |
| `chart-focus` (asym) | figure | title* [h2] (0,0,12,2) · chart* chart (0,2,8,10) n accepts table · takeaway [lead] (8,2,4,10) |
| `timeline-strip` | figure | title* [h2] (0,0,12,2) · timeline* diagram (0,3,12,6) n · body [caption] (0,10,12,2) |
| `closing` | closing | art shape (0,0,12,12) bleed · title* [display] (0,3,12,3) center ov · subtitle [lead] (0,6,12,2) center ov · credits [caption] (0,10,12,2) center ov |

| Page template | Family | Slots |
|---|---|---|
| `cover` | cover | image image (0,0,12,9) bleed c · title* [display] (0,10,12,3) · subtitle [lead] (0,13,12,1) · meta [caption] (0,15,12,1) |
| `chapter-opener` | opener | number [display] (0,1,3,3) · title* [h1] (0,4,12,2) · intro [lead] (0,6,12,3) · body [body] (0,9,12,7) flow |
| `text-side-figure` (asym) | figure | heading [h2] (0,0,12,1) · body* [body] (0,1,7,15) flow · figure* image (7,1,5,6) n accepts diagram/chart/table · caption [caption] (7,7,5,1) |
| `two-column-text` | text | heading [h2] (0,0,12,1) · col1* [body] (0,1,6,15) flow · col2 [body] (6,1,6,15) flow |
| `full-figure` | figure | heading [h2] (0,0,12,1) · figure* image (0,1,12,13) n accepts diagram/chart/table · caption [caption] (0,14,12,2) |
| `pull-quote` | text | body* [body] (0,0,12,6) flow · quote* [h2] (1,6,10,3) · body2 [body] (0,9,12,7) flow |
| `key-points` | summary | heading [h2] (0,0,12,1) · box shape (0,1,12,6) · points* [lead] (0,1,12,6) ov · body [body] (0,7,12,9) flow |
| `references` | references | heading* [h2] (0,0,12,1) · refs* [caption] (0,1,12,15) flow |

### 9.6 Layout, painters and fitting

`layout.lay_out` fills slots from the direction, then fits in the order of 3.5 (wrap with real metrics → shrink down
the scale to the minimum → rebalance: caption moves, bullets over the slot's `max_words` go to speaker notes, compact
variant → split with "(continued)"). Images: `cover`/`contain`, focal crop via `studio/assets.prepare`, ≤ 1.5×
upscale. Left edges snap to the grid; text keeps the safe margin unless `bleed`; box order within a page is reading
order. Painters (`paint_pptx.paint`, `paint_pdf.paint`) draw exactly the plan, raise on library failure, and keep
F1/F3, A1–A4 and X1–X3 exactly as `create/render.py` does (headings as real titles/outline entries, table headers
marked, chart titles and summaries, alt text on every image/diagram/icon).

### 9.7 The agent loop and budgets (`studio/agent.py`, `config.py`)

`design()`: `tokens.build_system` → `direct_llm` (engine given and mode ≠ quick) or `direct_keyless` → assets (fonts
`ensure`, `assets.prepare`, icons, art) → `layout.lay_out` → `thumbs.render_all` → `qa.check` → per failing check the
`qa.FIXES` actions via `layout.refit` (0 tokens) → critic (mode `deep` or Polish, `critic.can_see(engine)`) →
freeform pages (≤ 2) → stop on pass / `STUDIO_ROUNDS` / budget / deadline → `agent.paint` → `ws.save_plan`,
`ws.save_report`. Budgets in `config.py`: `STUDIO_ROUNDS=3, STUDIO_DIRECT_TOKENS=2000, STUDIO_CRITIC_ROUNDS=2,
STUDIO_CRITIC_TOKENS=6000, STUDIO_FREEFORM_PAGES=2, STUDIO_FREEFORM_TOKENS=2000, STUDIO_TIME_BUDGET=60 s`. A critic
edit that lowers the score or adds a D1–D3 failure is rolled back; a freeform page failing QA twice falls back to
`library.next_best` of its direction's layout (recorded in `report.fallbacks`). Vision: engines expose
`supports_vision = True` and accept `images=[png bytes]` in `Engine.stream` (additive keyword; Q edits
`engines/base.py`, `anthropic_api.py`, `claude_code.py`); others skip the critic with a note.

### 9.8 QA checks and the design report

| Id | Name | Weight | Threshold (`qa.THRESHOLDS`) | Code fixes (`qa.FIXES`) |
|---|---|---|---|---|
| D1 | Overflow | 20 | measured text height ≤ box h + 0.5 pt; no line wider than the box | shrink, rebalance, compact, split |
| D2 | Overlap | 15 | no intersection > 1 pt² unless the upper box is `overlay_ok` | snap, compact, swap_layout |
| D3 | Readability | 20 | size ≥ minimum (slides 18/12 caption, print 10/8); contrast ≥ 4.5:1, ≥ 3:1 at ≥ 24 pt or ≥ 18.66 pt bold; over photos against the worst sampled thumbnail pixel | shrink, recrop |
| D4 | Density | 10 | slides ≤ 40 words with bullets, ≤ 60 prose; white space 25–60 % (print 15–60 %) | rebalance, split, compact (crowded); enlarge (looks empty: `qa.EMPTY_FIXES`, the page's table, chart or diagram grows toward the bottom of its slot) |
| D5 | Balance | 5 | visual-weight centre in the middle third both ways (skipped: asymmetric layouts, covers, freeform) | swap_layout |
| D6 | Consistency | 10 | sizes on the scale ± 0.5 pt; text left edges on grid columns ± 2 pt; one image treatment | snap |
| D7 | Variety | 10 | no layout > 3 consecutive slides; 8+ slides use ≥ 4 layouts (pptx) | swap_layout |
| D8 | Images | 10 | upscale ≤ 1.5× at 96 dpi (pptx) / 150 dpi (pdf); focal point inside the crop | recrop, swap_layout |

`score = 100 − Σ weight of every check with an unfixed failure` (weights sum to 100; `PASS_SCORE = 80` is the golden
bar). **Design report** (`plan.report_schema()`, stored as `report.json`, served by `GET /api/created/{id}/design`):
`{version: 1, score, preset, format, fonts: [FontUse], rounds, stop ∈ pass|rounds|budget|deadline|error|keyless,
tokens, checks: [8 × {id, name, ok, failures, note}] in D1..D8 order, results: [≤ 50 failing QaResult], layouts: {id:
count}, fallbacks: [{page, from, to, why}], critic: {ran, why, edits, rolled_back}, thumbs: n, phases: [{phase, calls,
llm_in, llm_out, ms}], notes: [str]}`.
Ruleset (builder I, `create/rules.py` + `docs/RULES-files.md`): D1–D8 as **warn** rules read from the report
(`verify(..., design_report=report)`), one merged RuleResult each, absent for files made without Studio.

### 9.9 HTTP API (builder I; types in `web/src/protocol.ts`, clients in `web/src/api.ts`)

Errors are `{error: string}`. Every `/api/created/{fid}/…` route also exists as `/api/sandbox/{id}/created/{fid}/…`
for sandbox files (workspace in a temp dir).

| Method, path | Request | 200 response | Other statuses |
|---|---|---|---|
| `GET /api/design/presets` | — | `{presets: DesignPresetInfo[], templates: DesignTemplateInfo[]}` | — |
| `GET /api/design/presets/{id}/thumb` | — | `image/png` | 404 unknown preset |
| `GET /api/fonts/search?q=&limit=20` | q ≤ 64 chars, limit 1..50 | `{fonts: FontInfo[], offline: bool}` (open licences only; empty q = curated list) | 400 bad q/limit |
| `GET /api/fonts/preview?family=&text=` | text ≤ 60 chars | `image/png` | 404 family not cached/installed or not open |
| `POST /api/created/{fid}/restyle` | `RestyleBody {preset?, fonts?: {display?, heading?, body?}, dark?, template?, layouts?: {"<page0>": id}, print?}` | the new `CreatedFile` (`source: 'convert'`, `from_id` = fid, `tokens: 0`) | 400 unknown preset/template/layout, non-open font, empty body; 404 no such file; 409 no stored spec; 422 `{error, rule}` a block rule failed |
| `GET /api/created/{fid}/thumbs` | — | `{thumbs: Thumb[], reason?}` (`Thumb {page (1-based), url, w, h, layout}`; `[]` + reason for non-Studio files) | 404 no such file |
| `GET /api/created/{fid}/thumbs/{n}.png` | n 1-based | `image/png` | 404 |
| `GET /api/created/{fid}/design` | — | `{report: DesignReport \| null, plan: DesignPlanSummary \| null}` | 404 no such file |
| `POST /api/created/{fid}/polish` | `PolishBody {engine?, confirm_cost?}` | the new `CreatedFile` | 409 `{error, needs_confirmation: true, estimate}` (cost guard, as `/ask`); 409 `{error}` no design plan; 400 engine can't read images; 404; 503 engine unavailable |

`CreatedFile.design` (existing `DesignApplied`) gains optional `studio, preset, fonts: DesignFontUse[], score,
thumbs` (TS interface merging); for a Studio file without a design file, `name` is `preset:<id>`, `colors` are the
preset's roles and `confidence` is 1. The per-phase breakdown is `DesignReport.phases` (`FilePhase` is unchanged).

### 9.10 Integration and the feature flag

- **Flag**: `TG_STUDIO` read on every file by `config.studio_formats()`: unset/empty → `STUDIO_DEFAULT`; `0/off/
  false/no` → none; `1/on/true/yes` → `STUDIO_ON = ('pptx', 'pdf')`; otherwise a comma list of formats. Phase 0 shipped
  `STUDIO_DEFAULT = ''` (off); with L and Q built it is now **`STUDIO_DEFAULT = 'pptx,pdf'`** (on for slides and
  PDFs). `studio.enabled(fmt)` is the only check.
- **agents/create.py** (builder I): `finish()` pre-generates the file id (`uuid4().hex[:12]`) and, when
  `studio.enabled(fmt)`, after X4 calls `await studio.agent.design(norm_spec, fmt, file_id=…, brief=brief,
  request=job.request, tokens_src=spec.get('design'), engine=engine, http=job.http, mode=mode, sandbox=…,
  deadline=job.deadline)`; `build()` gains `studio: DesignResult | None = None` and `file_id: str | None = None`: with
  `painted_bytes` it skips `render_safe` and runs `verify()` on those bytes (plus D1–D8 from the report); without, it
  renders as today. The result's LLM calls, tokens and ms are added to `Made.phases`' `render` entry; `meta['design']` merges `agent.meta_design(result)`; the
  art-direction tokens add to `meta['tokens']`. Engine and job plumbing (`finish` gets `engine`, `mode`, `http`) is I's.
- **Fallback**: any exception from `design()`/`paint()`, or `painted_bytes is None` for a painted format → the legacy
  `render_safe` path, `fell_back = True`, and one caveat "The design stage failed (…), so the file uses the standard
  layout." A file is never lost to Studio.
- **create/render.py**: `render()`, `render_safe()` and `_draw()` stay the legacy renderers, byte-identical with the
  flag off (tested under reportlab's invariant mode and frozen zip/xlsxwriter clocks, the only time-dependent bytes). I adds `render_designed(plan: DesignPlan, spec, fmt, ws) -> bytes` = `studio.agent.paint` wrapped
  in the same V1/L4 handling as `_draw`; convert (`POST /api/created/{id}/convert`) into pptx/pdf with the flag on
  calls `design()` keyless (engine None, 0 tokens), reusing the source file's workspace via `Workspace.copy_to`.
- **Workspaces**: deleting a created file calls `workspace.drop(fid)`; clearing a sandbox calls `drop_sandbox(id)`;
  `prune()` for fonts and workspaces runs at startup (I).

### 9.11 Ownership (six parallel builders)

| Builder | Owns (writes) | Tests |
|---|---|---|
| **F** fonts, workspace, tokens, presets | `studio/fonts.py`, `studio/workspace.py`, `studio/tokens.py`, `studio/presets.py`; `create/design.py` (design.md v2 fields), `create/fonts.py` (adapter over `studio/fonts`, same `resolve()` signature), `create/brief.py` style words (`DARK` fix for "dark design"); `requirements.txt` (fontTools already listed) | `tests/test_studio_fonts.py` |
| **L** layout | `studio/library.py`, `studio/layout.py`, `studio/direct.py` (keyless rules and the LLM call), `studio/paint_pptx.py`, `studio/paint_pdf.py`, amendments to `studio/plan.py` | `tests/test_studio_layout.py` |
| **Q** QA, thumbs, agent loop, critic, freeform | `studio/qa.py`, `studio/thumbs.py`, `studio/agent.py`, `studio/critic.py`, `studio/freeform.py`; the additive vision keyword in `engines/base.py`, `engines/anthropic_api.py`, `engines/claude_code.py` | `tests/test_studio_qa.py` |
| **V** visuals | `studio/icons.py` + `studio/data/lucide.json` + `studio/data/LICENSE-lucide` (ISC), `studio/art.py`, `studio/assets.py`; new diagram kinds in `create/diagram.py` and their DocSpec entries in `create/spec.py` (diagram kinds only) and `create/brief.py` `DIAGRAM_KINDS`/`KIND_WORDS` (coordinate the brief.py edit with F) | `tests/test_studio_visuals.py` |
| **I** integration and API | `agents/create.py`, `app.py` endpoints (9.9), `config.py`, `create/render.py` (`render_designed`), `create/rules.py` D1–D8, `docs/RULES-files.md`, `store.py`/sandbox hooks for workspace drop | `tests/test_studio_api.py` |
| **W** web | `web/**` except `web/src/protocol.ts` (types frozen by phase 0; W may append, never edit) — the Design panel, pickers, thumbnails, `scripts/mock-server.mjs` routes | `web` typecheck/build |

Shared files: `studio/plan.py` and `tests/test_studio_contract.py` change only by contract amendment. Nobody edits a
file they don't own; a needed change goes to the owner. Every builder keeps `.venv/bin/pytest -q` green and the flag-off
byte-identity test passing.

### 9.12 Amendments (additive, made while building)

All of these add to the contract; nothing was removed, renamed or reordered, and `tests/test_studio_contract.py` still
checks the original signatures and fields.

- **`PagePlan.direction`** (L): a new last field, `int | None = None`, the index of the art-direction page a page plan
  came from (a split slide or page keeps its source's index). `design_plan_schema()` accepts it as an optional
  nullable integer.
- **Ref grammar** (L): a `spec:` ref may slice and pick a field, resolved by `layout.box_text`:
  `spec:<s>/<b>/rows[<i>:<j>]` (table rows, header always drawn), `.../words[<i>:<j>]` (a word range of a paragraph,
  quote or item), and `...#<field>` with `plan.REF_FIELDS = caption, credit, by, title, summary, stat, rest,
  continued, footer`.
- **`PageOutline` fields** (L): three new last fields, `parallel: bool = False` (two headed bullet lists, a
  comparison), `texts: int = 0` (paragraphs, bullet items and quotes) and `table_cols: int = 0` (columns of the widest
  table).
- **`fonts.prune(cap_bytes=CACHE_BYTES, keep=())`** (F): `keep` names families that are never evicted (the ones the
  current file uses).
- **`freeform.compose(..., budget_tokens=2000, feedback=())`** (Q): `feedback` is the list of problems from the last
  invalid program, sent back to the model on its one retry.
- **Engines** (Q): `Engine.supports_vision` and the `images=` keyword on `stream()`; `AutoEngine.supports_vision` is
  true only while its chain has a healthy engine that can read images, and Auto routes an image call to that engine.
- **Keyless at 0 tokens** (I): a file made with no writer call (an earlier answer, an attached table, a conversion,
  a checkpoint rebuild) is designed keyless too, so "put that in a PDF" still costs 0 tokens.
- **Slide and page counts** (I): `agent.count_pages(spec, fmt, *, brief=None, request='', tokens_src=None,
  preset=None) -> int` lays a spec out keyless in a throwaway workspace; the long writer's fit loop measures with it
  when Studio designs the format. A deck held to a slide count gets no Studio-made closing slide when that slide
  would go over the count (`agent._hold_count`; the layout engine keeps a direction without one on a restyle).
- **Flag**: `STUDIO_DEFAULT = 'pptx,pdf'`, so Studio is on for slides and PDFs; `TG_STUDIO=off` turns it off and
  still gives the legacy renderers' exact bytes.
