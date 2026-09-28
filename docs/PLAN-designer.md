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

`CreatedFile` gains `design: {preset, fonts, score, notes}`. The file card gets a **Design** panel: thumbnail strip,
preset picker, font picker with live previews, per-slide layout override, "print version", and the design report.

## 6. Build order (each phase leaves the app working)

1. **Foundations:** `fontTools` dependency, font manager with open-licence downloads and cache, workspace, DesignSystem
   + presets, design.md v2, prompt-word detection (fixes "dark design" today).
2. **Layout engine + PPTX painter:** the 16 slide layouts, fitting, art director (keyless rules first, then the model
   call), thumbnails + QA. Presentations are where design matters most for students.
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
- Token budget: art direction ≤ 2K tokens out; restyle and re-layout 0 tokens.

## 8. Risks and limits

- **PowerPoint and Word can't embed fonts via python-pptx/python-docx.** Mitigated by measuring against the wider of
  the chosen font and its fallback, and by telling the user which free font to install.
- **Thumbnails are Studio's own rendering**, not PowerPoint's; small differences are possible. The PDF path is exact.
- **Licensing:** only openly licensed fonts, icons and images, with the licence stored next to each asset.
- **Cost of assets:** font and image downloads are cached and capped; offline mode degrades to installed fonts and
  code-drawn art, never to a failed file.
- New dependencies: `fontTools` (MIT). Optional later: `pypdfium2` (Apache-2.0/BSD) to rasterize the real PDF for QA.
