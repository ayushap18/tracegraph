"""Visual QA (docs/PLAN-designer.md 3.7 and 9.8): checks D1-D8 over a DesignPlan (and its thumbnails for contrast over
photos), a 0-100 score and the design report. Also the ruleset entries D1-D8 in create/rules.py (builder I) read
`report['checks']`.

Owner: builder Q. CHECKS, THRESHOLDS and WEIGHTS are contract data.
"""
from __future__ import annotations

import re
from dataclasses import asdict

from .plan import CHECK_IDS, PLAN_VERSION, STOP_REASONS, TYPE_STEPS, Box, DesignPlan, PagePlan, QaResult
from .thumbs import hex_of, measure, system_of, text_of
from .tokens import MinSizes, Spacing
from .workspace import Workspace

# id -> (name, weight in the score, what it checks)
CHECKS: dict[str, tuple[str, int, str]] = {
    'D1': ('Overflow', 20, "no text box's measured height exceeds its box (tolerance 0.5 pt); no line wider than the box"),
    'D2': ('Overlap', 15, 'no two boxes intersect by more than 1 pt² unless the upper one is overlay_ok'),
    'D3': ('Readability', 20, 'text size >= the format minimum; contrast >= 4.5:1, >= 3:1 for >= 24 pt (18.66 pt bold), '
                              'text over images measured against the darkest/lightest sampled thumbnail pixel'),
    'D4': ('Density', 10, 'slides: <= 40 words with bullets, <= 60 words of prose; white space 25-60% of the page '
                          '(print pages 15-60%)'),
    'D5': ('Balance', 5, 'visual weight centre within the middle third both ways (skipped for asymmetric layouts, '
                         'covers and freeform pages)'),
    'D6': ('Consistency', 10, 'every text size is a type-scale step (±0.5 pt); text left edges snap to grid columns '
                              '(±2 pt); one image treatment per file'),
    'D7': ('Variety', 10, 'no layout on more than 3 consecutive slides; decks of 8+ slides use 4+ layouts (pptx only)'),
    'D8': ('Images', 10, 'no image upscaled more than 1.5x at the target dpi (96 pptx, 150 pdf); focal point inside '
                         'the crop'),
}
THRESHOLDS = {
    'overflow_tolerance_pt': 0.5,
    'overlap_area_pt2': 1.0,
    'contrast_text': 4.5, 'contrast_large': 3.0, 'large_pt': 24.0, 'large_bold_pt': 18.66,
    'words_bullets': 40, 'words_text': 60,
    'white_min_slide': 0.25, 'white_min_page': 0.15, 'white_max': 0.60,
    'balance_low': 1 / 3, 'balance_high': 2 / 3,
    'scale_tolerance_pt': 0.5, 'snap_tolerance_pt': 2.0,
    'max_run': 3, 'variety_slides': 8, 'variety_layouts': 4,
    'max_upscale': 1.5, 'dpi_pptx': 96, 'dpi_pdf': 150,
}
WEIGHTS = {k: v[1] for k, v in CHECKS.items()}
PASS_SCORE = 80            # the golden-set bar (section 7); the loop stops early only when every check passes
# the code fix the agent loop tries first for each failing check (layout.FIX_ACTIONS), in order
FIXES = {'D1': ('shrink', 'rebalance', 'compact', 'split'), 'D2': ('snap', 'compact', 'swap_layout'),
         'D3': ('shrink', 'recrop'), 'D4': ('rebalance', 'split', 'compact'), 'D5': ('swap_layout',),
         'D6': ('snap',), 'D7': ('swap_layout',), 'D8': ('recrop', 'swap_layout')}
# a page that "looks empty" (D4 above white_max) gets these instead: the crowded-page fixes would empty it more
EMPTY_FIXES = ('enlarge',)

assert sum(WEIGHTS.values()) == 100


# ---------- helpers ----------

_PHOTO_PREFIX = ('ws:', 'asset:', 'art:')
_FIGURE_KINDS = ('image', 'diagram', 'chart', 'table', 'icon')
_NOT_COUNTED = ('credit', 'credits', 'number', 'kicker', 'mark')   # labels, not reading text (D4 words)
_AIRY = ('cover', 'divider', 'quote', 'closing', 'number')          # families meant to be airy (D4 upper bound)
_BULLET = re.compile(r'^\s*[•·▪●*-]\s')


def _unit(plan: DesignPlan, i: int | None) -> str:
    word = 'Slide' if plan.format == 'pptx' else 'Page'
    return f'{word} {i + 1}' if i is not None else 'The file'


def _layout(layout_id: str):
    from . import library
    try:
        return library.get(layout_id)
    except Exception:
        return None


def _ordered(page: PagePlan) -> list[Box]:
    return [b for _, b in sorted(enumerate(page.boxes), key=lambda t: (t[1].z, t[0]))]


def _inter(a: Box, b: Box) -> float:
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return w * h if w > 0 and h > 0 else 0.0


def _size(box: Box, ds, fmt: str) -> float:
    if box.size:
        return float(box.size)
    if ds is not None and box.step in TYPE_STEPS:
        return ds.size(box.step, fmt)
    return 18.0 if fmt == 'pptx' else 10.5


def _minimum(box: Box, ds, fmt: str) -> float:
    ms = getattr(ds, 'min_sizes', None) or MinSizes()
    if fmt == 'pptx':
        return ms.slide_caption if box.step == 'caption' else ms.slide_body
    return ms.print_caption if box.step == 'caption' else ms.print_body


def _is_photo(b: Box) -> bool:
    ref = b.content or ''
    return b.kind in ('image', 'diagram', 'chart', 'table') or (b.kind == 'shape' and ref.startswith(_PHOTO_PREFIX))


def _blend(fg: str, bg: str, alpha: float) -> str:
    a = max(0.0, min(1.0, alpha))
    f = [int(fg[i:i + 2], 16) for i in (0, 2, 4)]
    g = [int(bg[i:i + 2], 16) for i in (0, 2, 4)]
    return ''.join('%02X' % round(a * x + (1 - a) * y) for x, y in zip(f, g))


class _Ctx:
    """What every check needs, computed once per check() call."""

    def __init__(self, plan: DesignPlan, ws, thumbs):
        self.plan, self.ws, self.thumbs = plan, ws, thumbs or []
        self.ds = system_of(plan)
        self.fmt = plan.format
        self.bgs: dict[int, bytes | None] = {}

    def background(self, i: int) -> bytes | None:
        if i not in self.bgs:
            try:
                from .thumbs import render_background
                self.bgs[i] = render_background(self.plan, i, self.ws)
            except Exception:
                self.bgs[i] = None
        return self.bgs[i]


def _fail(cid: str, note: str, *, page=None, box=None, value=None, threshold=None) -> QaResult:
    return QaResult(cid, False, note, page, box, None if value is None else round(float(value), 2),
                    None if threshold is None else round(float(threshold), 2))


# ---------- D1 overflow ----------


def _d1(c: _Ctx) -> list[QaResult]:
    tol, out = THRESHOLDS['overflow_tolerance_pt'], []
    for i, p in enumerate(c.plan.pages):
        for b in p.boxes:
            if b.kind != 'text' or not b.lines:
                continue
            size = _size(b, c.ds, c.fmt)
            need = len(b.lines) * size * (b.line_height or 1.2)
            if need > b.h + tol:
                out.append(_fail('D1', f'{_unit(c.plan, i)}: the text in {b.slot or b.id} needs {need:.0f} pt of height '
                                       f'but its box is {b.h:.0f} pt tall.', page=i, box=b.id, value=need,
                                 threshold=b.h))
                continue
            widest = max(measure(line, b.font, size, bold=b.bold, italic=b.italic, fmt=c.fmt) for line in b.lines)
            if widest > b.w + tol:
                out.append(_fail('D1', f'{_unit(c.plan, i)}: a line in {b.slot or b.id} is wider than its box.',
                                 page=i, box=b.id, value=widest, threshold=b.w))
    return out


# ---------- D2 overlap ----------


def _d2(c: _Ctx) -> list[QaResult]:
    lim, out = THRESHOLDS['overlap_area_pt2'], []
    for i, p in enumerate(c.plan.pages):
        boxes = [b for b in _ordered(p) if b.w > 0 and b.h > 0]
        for j, upper in enumerate(boxes):
            if upper.overlay_ok:
                continue
            for lower in boxes[:j]:
                area = _inter(upper, lower)
                if area > lim:
                    out.append(_fail('D2', f'{_unit(c.plan, i)}: {upper.slot or upper.id} overlaps '
                                           f'{lower.slot or lower.id}.', page=i, box=upper.id, value=area,
                                     threshold=lim))
                    break
    return out


# ---------- D3 readability ----------


def _backdrop(c: _Ctx, i: int, page: PagePlan, box: Box):
    """(solid colour or None, translucent layers bottom-up, photo box or None) under the centre of a text box."""
    order = _ordered(page)
    cx, cy = box.x + box.w / 2, box.y + box.h / 2
    below = order[:next(k for k, b in enumerate(order) if b is box)]
    layers: list[tuple[str, float]] = []
    for b in reversed(below):
        if not (b.x <= cx <= b.x + b.w and b.y <= cy <= b.y + b.h) or b.kind == 'text' or b.kind == 'icon':
            continue
        if _is_photo(b):
            return None, list(reversed(layers)), b
        if b.kind == 'shape' and b.style.fill:
            colour = hex_of(b.style.fill, c.ds, 'bg')
            if b.style.opacity >= 0.999:
                return colour, list(reversed(layers)), None
            layers.append((colour, b.style.opacity))
    bg = page.background or 'bg'
    if ':' in bg:
        return None, list(reversed(layers)), Box('background', 'image', 0, 0, page.w, page.h, content=bg)
    return hex_of(bg, c.ds, 'bg'), list(reversed(layers)), None


def _d3(c: _Ctx) -> list[QaResult]:
    out = []
    for i, p in enumerate(c.plan.pages):
        for b in p.boxes:
            if b.kind != 'text' or not (b.lines or (b.content or 'none') != 'none'):
                continue
            size, low = _size(b, c.ds, c.fmt), _minimum(b, c.ds, c.fmt)
            if size < low - 0.01:
                out.append(_fail('D3', f'{_unit(c.plan, i)}: {b.slot or b.id} is {size:g} pt, below the {low:g} pt '
                                       f'minimum.', page=i, box=b.id, value=size, threshold=low))
                continue
            large = size >= THRESHOLDS['large_pt'] or (b.bold and size >= THRESHOLDS['large_bold_pt'])
            need = THRESHOLDS['contrast_large'] if large else THRESHOLDS['contrast_text']
            fg = hex_of(b.style.text_color or 'text', c.ds)
            solid, layers, photo = _backdrop(c, i, p, b)
            if photo is None:
                bg = solid
                for colour, a in layers:
                    bg = _blend(colour, bg, a)
                bgs = [bg]
            else:
                bgs = _photo_backdrop(c, i, p, b, fg, photo, layers)
                if not bgs:
                    continue
            worst = min(contrast(fg, bg) for bg in bgs)
            if worst < need - 1e-9:
                over = ' over the picture' if photo is not None else ''
                out.append(_fail('D3', f'{_unit(c.plan, i)}: {b.slot or b.id} has contrast {worst:.1f}:1{over}, '
                                       f'below {need:g}:1.', page=i, box=b.id, value=worst, threshold=need))
    return out


def _photo_backdrop(c: _Ctx, i, page, box, fg, photo: Box, layers) -> list[str]:
    """Colours under text that sits on a photo or art: sampled from a text-free render of the page when the picture can
    be read, else from the stored thumbnail (glyph pixels ignored), else the overlay over black and over white."""
    from .thumbs import load_image, sample
    rect = (box.x, box.y, box.w, box.h)
    readable = load_image(photo.content, c.ws, ds=c.ds, size_px=(64, 64)) is not None
    if readable:
        png = c.background(i)
        if png:
            return sample(png, rect, (page.w, page.h))
    if i < len(c.thumbs) and c.thumbs[i]:
        try:
            got = [h for h in sample(c.thumbs[i], rect, (page.w, page.h)) if contrast(fg, h) >= 1.5]
        except Exception:
            got = []
        if got:
            return got
    if layers:
        out = []
        for base in ('000000', 'FFFFFF'):
            bg = base
            for colour, a in layers:
                bg = _blend(colour, bg, a)
            out.append(bg)
        return out
    return []


# ---------- D4 density ----------


def _mask_ratio(page: PagePlan, boxes: list[Box]) -> float:
    """Fraction of the page covered by the union of boxes (rasterised at 1/4 pt)."""
    from PIL import Image, ImageDraw
    k = 0.25
    w, h = max(1, int(page.w * k)), max(1, int(page.h * k))
    m = Image.new('L', (w, h), 0)
    d = ImageDraw.Draw(m)
    for b in boxes:
        x0, y0 = b.x * k, b.y * k
        d.rectangle((x0, y0, max(x0, (b.x + b.w) * k - 1), max(y0, (b.y + b.h) * k - 1)), fill=255)
    return m.histogram()[255] / (w * h)


def _d4(c: _Ctx) -> list[QaResult]:
    out = []
    slide = c.fmt == 'pptx'
    for i, p in enumerate(c.plan.pages):
        if slide:
            words, bullets = 0, False
            for b in p.boxes:
                if b.kind != 'text' or b.slot in _NOT_COUNTED:
                    continue
                txt = text_of(b)
                words += len(txt.split())
                bullets = bullets or '/items[' in (b.content or '') or any(_BULLET.match(l) for l in b.lines or ())
            limit = THRESHOLDS['words_bullets'] if bullets else THRESHOLDS['words_text']
            if words > limit:
                out.append(_fail('D4', f'{_unit(c.plan, i)} has {words} words; the limit is {limit} '
                                       f'{"with bullets" if bullets else "of prose"}.', page=i, value=words,
                                 threshold=limit))
                continue
        area = p.w * p.h
        if any(b.bleed and _is_photo(b) and b.w * b.h > area / 2 for b in p.boxes):
            continue   # a photo page: its space is the picture
        ink = [b for b in p.boxes if b.w > 0 and b.h > 0 and not b.bleed and
               (b.kind != 'shape' or b.style.fill)]
        if not ink:
            continue
        white = 1 - _mask_ratio(p, ink)
        low = THRESHOLDS['white_min_slide'] if slide else THRESHOLDS['white_min_page']
        ld = _layout(p.layout)
        airy = p.freeform or ld is None or ld.family in _AIRY
        if white < low - 1e-6:
            out.append(_fail('D4', f'{_unit(c.plan, i)} is crowded: {white:.0%} white space, below {low:.0%}.',
                             page=i, value=white, threshold=low))
        elif slide and not airy and white > THRESHOLDS['white_max'] + 1e-6:
            out.append(_fail('D4', f'{_unit(c.plan, i)} looks empty: {white:.0%} white space, above '
                                   f'{THRESHOLDS["white_max"]:.0%}.', page=i, value=white,
                             threshold=THRESHOLDS['white_max']))
    return out


# ---------- D5 balance ----------


def _weight(b: Box, c: _Ctx) -> float:
    area = b.w * b.h
    if b.kind in _FIGURE_KINDS:
        return area
    if b.kind == 'shape':
        return area * 0.5 if b.style.fill else 0.0
    if b.kind == 'text' and b.lines:
        size = _size(b, c.ds, c.fmt)
        tw = max(measure(line, b.font, size, bold=b.bold, fmt=c.fmt) for line in b.lines)
        th = len(b.lines) * size * (b.line_height or 1.2)
        return area * max(0.25, min(1.0, (tw * th) / max(1.0, area)))
    return area * 0.25 if b.kind == 'text' else 0.0


def _d5(c: _Ctx) -> list[QaResult]:
    out = []
    lo, hi = THRESHOLDS['balance_low'], THRESHOLDS['balance_high']
    for i, p in enumerate(c.plan.pages):
        ld = _layout(p.layout)
        if p.freeform or ld is None or ld.asymmetric or ld.family == 'cover':
            continue
        if c.fmt != 'pptx' and any(sl.flow for sl in ld.slots):
            continue   # running print text fills pages from the top; a section's last page is partly empty by nature
        items = [(b, _weight(b, c)) for b in p.boxes if not b.bleed and b.w > 0 and b.h > 0]
        total = sum(w for _, w in items)
        if total <= 0:
            continue
        cx = sum((b.x + b.w / 2) * w for b, w in items) / total / p.w
        cy = sum((b.y + b.h / 2) * w for b, w in items) / total / p.h
        if not (lo <= cx <= hi and lo <= cy <= hi):
            off = cx if not lo <= cx <= hi else cy
            out.append(_fail('D5', f'{_unit(c.plan, i)} is lopsided: its visual weight sits at '
                                   f'{cx:.0%} across and {cy:.0%} down.', page=i, value=off,
                             threshold=lo if off < lo else hi))
    return out


# ---------- D6 consistency ----------


def _column_starts(c: _Ctx, page: PagePlan) -> list[float]:
    ds = c.ds
    sp = getattr(ds, 'spacing', None) or Spacing()
    n = sp.columns or 12
    margin = sp.slide_margin if c.fmt == 'pptx' else sp.page_margin
    cw = (page.w - 2 * margin - (n - 1) * sp.gutter) / n
    starts = [margin + k * (cw + sp.gutter) for k in range(n)]
    starts += [k * page.w / n for k in range(n)]   # the bleed grid
    return starts


def _d6(c: _Ctx) -> list[QaResult]:
    out = []
    tol, snap = THRESHOLDS['scale_tolerance_pt'], THRESHOLDS['snap_tolerance_pt']
    steps = sorted({c.ds.size(s, c.fmt) for s in TYPE_STEPS}) if c.ds is not None else []
    for i, p in enumerate(c.plan.pages):
        starts = _column_starts(c, p)
        for b in p.boxes:
            if b.kind != 'text':
                continue
            size = _size(b, c.ds, c.fmt)
            if steps and min(abs(size - s) for s in steps) > tol:
                out.append(_fail('D6', f'{_unit(c.plan, i)}: {b.slot or b.id} is {size:g} pt, off the type scale.',
                                 page=i, box=b.id, value=size, threshold=tol))
                continue
            if p.freeform or b.slot is None:
                continue
            off = min(abs(b.x - s) for s in starts)
            if off > snap:
                out.append(_fail('D6', f'{_unit(c.plan, i)}: {b.slot} starts {off:.1f} pt off the grid.',
                                 page=i, box=b.id, value=off, threshold=snap))
    kinds = {}
    for i, p in enumerate(c.plan.pages):
        for b in p.boxes:
            if b.kind == 'image' and (b.content or '').startswith(('ws:images/', 'asset:')):
                t = 'rounded' if b.style.radius > 0.5 else 'framed' if b.style.stroke and b.style.stroke_w else 'plain'
                kinds.setdefault(t, i)
    if len(kinds) > 1:
        out.append(_fail('D6', f'Pictures use {len(kinds)} different treatments ({", ".join(sorted(kinds))}); one '
                               f'is expected per file.', page=max(kinds.values()), value=len(kinds), threshold=1))
    return out


# ---------- D7 variety ----------


def _d7(c: _Ctx) -> list[QaResult]:
    if c.fmt != 'pptx':
        return []
    out, pages = [], c.plan.pages
    run = 0
    for i, p in enumerate(pages):
        run = run + 1 if i and p.layout == pages[i - 1].layout else 1
        if run == THRESHOLDS['max_run'] + 1:
            out.append(_fail('D7', f'{_unit(c.plan, i)} is the {run}th {p.layout} slide in a row.', page=i,
                             value=run, threshold=THRESHOLDS['max_run']))
    used = {p.layout for p in pages}
    if len(pages) >= THRESHOLDS['variety_slides'] and len(used) < THRESHOLDS['variety_layouts']:
        out.append(_fail('D7', f'A deck of {len(pages)} slides uses only {len(used)} layouts; it needs '
                               f'{THRESHOLDS["variety_layouts"]} or more.', value=len(used),
                         threshold=THRESHOLDS['variety_layouts']))
    return out


# ---------- D8 images ----------


def _upscale(size_px, crop, b: Box, fmt: str) -> float:
    try:
        from .assets import upscale
        return float(upscale(size_px, crop, b.w, b.h, fmt))
    except Exception:
        pass
    dpi = THRESHOLDS['dpi_pptx'] if fmt == 'pptx' else THRESHOLDS['dpi_pdf']
    need_w, need_h = b.w / 72 * dpi, b.h / 72 * dpi
    have_w, have_h = max(1e-6, size_px[0] * crop[2]), max(1e-6, size_px[1] * crop[3])
    rx, ry = need_w / have_w, need_h / have_h
    return min(rx, ry) if b.fit == 'contain' else max(rx, ry)


def _d8(c: _Ctx) -> list[QaResult]:
    from .thumbs import load_image
    out = []
    for i, p in enumerate(c.plan.pages):
        for b in p.boxes:
            ref = b.content or ''
            if b.kind != 'image' or not ref.startswith(('ws:', 'asset:')):
                continue
            if b.focal and b.crop:
                x, y, w, h = b.crop
                fx, fy = b.focal
                if not (x - 1e-6 <= fx <= x + w + 1e-6 and y - 1e-6 <= fy <= y + h + 1e-6):
                    out.append(_fail('D8', f'{_unit(c.plan, i)}: the crop of {b.slot or b.id} cuts off its subject.',
                                     page=i, box=b.id))
                    continue
            im = load_image(ref, c.ws)
            if im is None:
                continue
            iw, ih = im.size
            same = b.h > 0 and ih > 0 and abs(iw / ih - b.w / b.h) <= 0.02 * (b.w / b.h)
            crop = (0.0, 0.0, 1.0, 1.0) if same or not b.crop else tuple(b.crop)
            up = _upscale((iw, ih), crop, b, c.fmt)
            if up > THRESHOLDS['max_upscale'] + 1e-6:
                out.append(_fail('D8', f'{_unit(c.plan, i)}: {b.slot or b.id} is enlarged {up:.1f} times; the most is '
                                       f'{THRESHOLDS["max_upscale"]:g}.', page=i, box=b.id, value=up,
                                 threshold=THRESHOLDS['max_upscale']))
    return out


_RUN = {'D1': _d1, 'D2': _d2, 'D3': _d3, 'D4': _d4, 'D5': _d5, 'D6': _d6, 'D7': _d7, 'D8': _d8}
_PASS_NOTE = {'D1': 'All text fits its boxes.', 'D2': 'No unintended overlaps.',
              'D3': 'Text sizes and contrast are readable.', 'D4': 'Text amount and white space are balanced.',
              'D5': 'Pages are visually balanced.', 'D6': 'Type sizes, edges and picture style are consistent.',
              'D7': 'Layouts vary enough.', 'D8': 'Pictures are sharp and well cropped.'}


def _run(cid: str, c: _Ctx) -> list[QaResult]:
    fails = _RUN[cid](c)
    return fails or [QaResult(cid, True, _PASS_NOTE[cid])]


def check(plan: DesignPlan, ws: Workspace | None = None, *, thumbs: list[bytes] | None = None) -> list[QaResult]:
    """Every D check over the plan; one QaResult per failure (page/box set) plus one ok=True result per passing check."""
    c = _Ctx(plan, ws, thumbs)
    return [r for cid in CHECK_IDS for r in _run(cid, c)]


def run_check(check_id: str, plan: DesignPlan, ws: Workspace | None = None, *,
              thumbs: list[bytes] | None = None) -> list[QaResult]:
    """One check's results."""
    if check_id not in _RUN:
        raise KeyError(check_id)
    return _run(check_id, _Ctx(plan, ws, thumbs))


def score(results: list[QaResult]) -> int:
    """100 minus the weight of every check with at least one unfixed failure; 0..100."""
    failed = {r.id for r in results if not r.ok and not r.fixed}
    return max(0, 100 - sum(WEIGHTS.get(i, 0) for i in failed))


def failing(results: list[QaResult]) -> list[QaResult]:
    """The unfixed failures."""
    return [r for r in results if not r.ok and not r.fixed]


def report(plan: DesignPlan, results: list[QaResult], *, critic: dict | None = None,
           fallbacks: list[dict] | None = None, notes: list[str] | None = None, thumbs: int = 0) -> dict:
    """The design report (plan.report_schema()): score, preset, fonts, rounds, stop, tokens, checks (all 8, in order),
    at most 50 failing results, layouts used {id: count}, fallbacks, critic summary, thumbs count, notes."""
    checks = []
    for cid in CHECK_IDS:
        fails = [r for r in results if r.id == cid and not r.ok and not r.fixed]
        oks = [r for r in results if r.id == cid and r.ok]
        note = fails[0].note if fails else (oks[0].note if oks else _PASS_NOTE[cid])
        checks.append({'id': cid, 'name': CHECKS[cid][0], 'ok': not fails, 'failures': len(fails), 'note': note})
    bad = failing(results) + [r for r in results if not r.ok and r.fixed]
    layouts: dict[str, int] = {}
    for p in plan.pages:
        layouts[p.layout] = layouts.get(p.layout, 0) + 1
    all_ok = all(ch['ok'] for ch in checks)
    stop = plan.stop if plan.stop in STOP_REASONS else ('pass' if all_ok else 'rounds')
    crit = {'ran': False, 'why': 'not requested', 'edits': [], 'rolled_back': []}
    crit.update(critic or {})
    return {
        'version': PLAN_VERSION, 'score': score(results), 'preset': plan.preset, 'format': plan.format,
        'fonts': [asdict(f) for f in plan.fonts], 'rounds': int(plan.rounds), 'stop': stop,
        'tokens': dict(plan.tokens or {}), 'checks': checks, 'results': [asdict(r) for r in bad[:50]],
        'layouts': layouts, 'fallbacks': list(fallbacks or []), 'critic': crit, 'thumbs': int(thumbs),
        'phases': [], 'notes': [*plan.notes, *(notes or [])],
    }


def contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio of two RRGGBB colours (shared helper; same maths as create/design.contrast)."""
    from ..create.design import contrast as _c
    return _c(fg, bg)
