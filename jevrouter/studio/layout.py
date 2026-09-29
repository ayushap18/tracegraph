"""The layout engine (docs/PLAN-designer.md 3.5 and 9.6): deterministic. For each page of the direction, fill the
layout's slots from the spec, measure text with studio/fonts, and fit: wrap -> shrink within the scale -> rebalance
(caption moves, bullets beyond the slot budget go to speaker notes, compact variant) -> split with "(continued)".

Everything a painter needs to draw text exactly as it was measured lives here too (box_text, wrap_runs, bullet_of,
table_geometry, theme_dict), so the PPTX and PDF painters and the thumbnails make no layout decisions of their own.

Owner: builder L.
"""
from __future__ import annotations

import io
import math
import re
import zlib
from dataclasses import dataclass, field, replace

from . import library
from .plan import (PAINTED, TYPE_STEPS, ArtDirection, AssetUse, Box, BoxStyle, DesignPlan, FontUse, PageDirection,
                   PagePlan)
from .tokens import DesignSystem
from .workspace import Workspace

# page sizes in points (w, h)
SLIDE = (960.0, 540.0)
PAPER = {'a4': (595.2756, 841.8898), 'letter': (612.0, 792.0), 'a3': (841.8898, 1190.5512),
         'a2': (1190.5512, 1683.7795)}
FIX_ACTIONS = ('shrink', 'rebalance', 'compact', 'split', 'recrop', 'swap_layout', 'snap', 'enlarge')

HEAD_STEPS = ('h3', 'h2', 'h1', 'display')
BULLET = '•'
CONTINUED = ' (continued)'
TABLE_LINE = 1.2                 # line height inside table cells
TABLE_STRETCH = 1.8              # a slide table's rows may grow to this many times their natural height to fill a slot
MAX_PAGES = 400                  # a runaway split never makes more pages than this
MIN_DPI = {'pptx': 96, 'pdf': 150, 'docx': 150}
MAX_UPSCALE = 1.5
TEXT_ROLES = ('para', 'bullet', 'quote', 'by', 'code')
SPLITTABLE = ('para', 'bullet', 'quote')
SMALL_REST_WORDS = 20            # a slide's leftover this short goes to the notes, never onto its own slide
CODE_CHUNK = 12                  # code is laid out in chunks of this many source lines (items[i:j] on a code block)
# slide layouts whose title sits in the top rows and gets a small accent bar under it
BAR_UNDER_TITLE = ('title-bullets', 'two-column', 'comparison', 'full-width-diagram', 'chart-focus', 'timeline-strip',
                   'stat-cards')
FLOW_SLOTS = {'chapter-opener': ('body',), 'text-side-figure': ('body',), 'two-column-text': ('col1', 'col2'),
              'pull-quote': ('body', 'body2'), 'key-points': ('body',), 'references': ('refs',)}


@dataclass
class FitResult:
    size: float                      # points chosen
    lines: list[str]
    height: float                    # points needed
    fits: bool
    overflow_words: int = 0          # words that did not fit (rebalance/split input)


def page_size(fmt: str, paper: str | None = None) -> tuple[float, float]:
    """(w, h) in points: pptx -> SLIDE, pdf/docx -> PAPER[paper or 'a4']."""
    return SLIDE if fmt == 'pptx' else PAPER.get((paper or 'a4').lower(), PAPER['a4'])


# ---------- the grid ----------


def _grid(ds: DesignSystem, fmt: str) -> tuple[int, int, float, float, float]:
    sp = ds.spacing
    rows = sp.rows_slide if fmt == 'pptx' else sp.rows_page
    margin = sp.slide_margin if fmt == 'pptx' else sp.page_margin
    return max(1, int(sp.columns or 12)), max(1, int(rows or 12)), margin, sp.gutter, sp.unit


def grid_rect(area: tuple[int, int, int, int], size: tuple[float, float], ds: DesignSystem, *, bleed: bool = False,
              fmt: str = 'pptx') -> tuple[float, float, float, float]:
    """(x, y, w, h) in points of a grid area: inside the margins with gutters, or over the whole page for bleed.
    Columns are separated by the gutter and rows by the spacing unit (the 8-point grid)."""
    col, row, cols, rows = area
    W, H = size
    ncols, nrows, margin, gutter, row_gap = _grid(ds, fmt)
    if bleed:
        cw, rh = W / ncols, H / nrows
        return (round(col * cw, 2), round(row * rh, 2), round(cols * cw, 2), round(rows * rh, 2))
    cw = (W - 2 * margin - (ncols - 1) * gutter) / ncols
    rh = (H - 2 * margin - (nrows - 1) * row_gap) / nrows
    x = margin + col * (cw + gutter)
    y = margin + row * (rh + row_gap)
    return (round(x, 2), round(y, 2), round(cols * cw + (cols - 1) * gutter, 2), round(rows * rh + (rows - 1) * row_gap, 2))


def column_edges(size: tuple[float, float], ds: DesignSystem, fmt: str) -> list[float]:
    """The x of every column's left edge (text left edges snap to these)."""
    return [grid_rect((c, 0, 1, 1), size, ds, fmt=fmt)[0] for c in range(_grid(ds, fmt)[0])]


def slot_areas(layout_id: str, variant: str = 'default') -> dict[str, tuple[tuple[int, int, int, int], str | None]]:
    """slot -> (area, step) for a layout's default or compact variant (library docstring: the first h1/h2 text slot
    loses a row, slots below it move up one row and grow by one, every text slot one step smaller)."""
    d = library.get(layout_id)
    out = {s.name: (tuple(s.area), s.step) for s in d.slots}
    if variant != 'compact':
        return out
    first = next((s for s in d.slots if s.kind == 'text' and s.step in ('h1', 'h2')), None)
    if first is not None:
        c, r, w, h = first.area
        if h > 1:
            out[first.name] = ((c, r, w, h - 1), first.step)
            for s in d.slots:
                if s is not first and s.area[1] >= r + h:
                    c2, r2, w2, h2 = s.area
                    out[s.name] = ((c2, r2 - 1, w2, h2 + 1), s.step)
    return {k: (a, step_down(st) if st else st) for k, (a, st) in out.items()}


def step_down(step: str) -> str:
    """One type step smaller; body and caption stay (the minimums)."""
    if step in ('body', 'caption'):
        return step
    return TYPE_STEPS[TYPE_STEPS.index(step) - 1]


def _steps_down(step: str) -> list[str]:
    i = TYPE_STEPS.index(step)
    floor = 0 if step == 'caption' else TYPE_STEPS.index('body')
    return [TYPE_STEPS[k] for k in range(i, floor - 1, -1)] if i >= floor else [step]


# ---------- fonts and measuring ----------

_STD = {'Helvetica': ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique'),
        'Times': ('Times-Roman', 'Times-Bold', 'Times-Italic', 'Times-BoldItalic'),
        'Courier': ('Courier', 'Courier-Bold', 'Courier-Oblique', 'Courier-BoldOblique')}
_SERIF = ('garamond', 'georgia', 'times', 'merriweather', 'lora', 'playfair', 'baskerville', 'caslon', 'crimson',
          'gelasio', 'tinos', 'caladea', 'fraunces', 'literata', 'spectral', 'cormorant', 'bitter', 'slab', 'cambria',
          'libre bodoni', 'bodoni', 'alegreya', 'vollkorn', 'newsreader', 'charter')
_CACHE: dict = {}


def _fonts():
    from . import fonts
    return fonts


def std_family(family: str | None) -> str:
    """The PDF standard family that stands in for a family no font file was found for."""
    f = str(family or '').lower()
    if any(k in f for k in ('mono', 'code', 'courier', 'consol', 'menlo')):
        return 'Courier'
    if ('serif' in f and 'sans' not in f) or any(k in f for k in _SERIF):
        return 'Times'
    return 'Helvetica'


def std_face(family: str | None, bold: bool = False, italic: bool = False) -> str:
    return _STD[std_family(family)][int(bool(bold)) + 2 * int(bool(italic))]


def std_width(text: str, family: str, size: float, *, bold: bool = False, italic: bool = False,
              fmt: str = 'pptx') -> float:
    """Width with the standard PDF font metrics; 8% wider for pptx/docx, whose viewers may substitute the family."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    try:
        w = stringWidth(text, std_face(family, bold, italic), size)
    except Exception:
        w = len(text) * size * 0.56
    return w * (1.08 if fmt in ('pptx', 'docx') else 1.0)


def measure(text: str, family: str, size: float, *, bold: bool = False, italic: bool = False,
            fmt: str = 'pptx') -> float:
    """Width in points through studio.fonts.measure (real metrics; for pptx the wider of the family and its office
    fallback). When the font manager can't measure, the standard PDF metrics stand in (and the PDF painter then
    draws with that same standard font)."""
    if fmt == 'pdf' and not pdf_face_ok(family):
        return std_width(text, family, size, bold=bold, italic=italic, fmt=fmt)
    fn = getattr(_fonts(), 'measure', None)
    key = (fn, text, family, round(float(size), 3), bool(bold), bool(italic), fmt)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        w = float(fn(text, family, size, bold=bold, italic=italic, fmt=fmt))
        if not (0 <= w < 1e7):
            raise ValueError('bad width')
    except Exception:
        w = std_width(text, family, size, bold=bold, italic=italic, fmt=fmt)
    if len(_CACHE) > 200_000:
        _CACHE.clear()
    _CACHE[key] = w
    return w


_PDF_OK: dict = {}


def pdf_face_ok(family: str) -> bool:
    """True when studio/fonts finds a TrueType file of the family that the PDF can subset-embed; otherwise the PDF
    draws (and the layout measures) with the standard font std_face() names."""
    fn = getattr(_fonts(), 'resolve_local', None)
    key = (fn, family)
    if key in _PDF_OK:
        return _PDF_OK[key]
    ok = False
    try:
        import os
        res = fn(family)
        faces = getattr(res, 'faces', None) or {}
        face = faces.get('regular') or next(iter(faces.values()), None)
        path = getattr(face, 'path', None)
        if path and os.path.exists(path):
            from reportlab.pdfbase.ttfonts import TTFont
            TTFont('TGProbe', path)          # raises for fonts reportlab can't embed (CFF outlines)
            ok = True
    except Exception:
        ok = False
    _PDF_OK[key] = ok
    return ok


def _forget_fonts() -> None:
    """studio.fonts.clear_caches() ran (a download, a prune): a family probed as missing may be there now."""
    _PDF_OK.clear()
    _CACHE.clear()


try:
    from . import fonts as _fonts_mod
    _fonts_mod.on_clear(_forget_fonts)
except Exception:   # pragma: no cover
    pass


def line_box(family: str, size: float, line_height: float) -> tuple[float, float, float]:
    """(ascent, descent, line advance) in points; the advance is always size x line_height, as the fit measured."""
    try:
        a, d, _ = _fonts().line_metrics(family, size, line_height)
        if not (0 < float(a) < size * 3):
            raise ValueError('bad metrics')
        return float(a), abs(float(d)), size * line_height
    except Exception:
        from reportlab.pdfbase.pdfmetrics import getAscentDescent
        try:
            a, d = getAscentDescent(std_face(family), size)
        except Exception:
            a, d = 0.75 * size, -0.22 * size
        return float(a), abs(float(d)), size * line_height


def family_for(ds: DesignSystem, step: str | None, role: str = '') -> str:
    f = ds.families
    if role == 'code':
        return f.mono
    if step == 'display':
        return f.display
    if step in ('h1', 'h2', 'h3'):
        return f.heading
    if step == 'caption':
        return f.caption
    return f.body


def line_height_for(ds: DesignSystem, step: str | None) -> float:
    if step in ('display', 'h1'):
        return float(ds.scale.display_line_height)
    if step in ('h2', 'h3'):
        return float(min(ds.scale.line_height, 1.15))
    return float(ds.scale.line_height)


# ---------- wrapping with emphasis runs ----------

Seg = tuple  # (text, bold, italic)


def _groups(text: str, bold: bool, italic: bool) -> list[list[Seg]]:
    """Whitespace-separated words, each a list of styled pieces (a word may mix **bold** and plain parts)."""
    from ..create.spec import runs
    out: list[list[Seg]] = []
    space = True
    for piece, b, it in runs(text) or ([(text, False, False)] if text else []):
        for part in re.split(r'(\s+)', piece):
            if not part:
                continue
            if part.isspace():
                space = True
                continue
            seg = (part, bool(b or bold), bool(it or italic))
            if space or not out:
                out.append([seg])
            else:
                out[-1].append(seg)
            space = False
    return out


def _gw(group, family, size, fmt) -> float:
    return sum(measure(t, family, size, bold=b, italic=i, fmt=fmt) for t, b, i in group)


def _break_group(group, family, size, width, fmt) -> list[list[Seg]]:
    """A word wider than the line, broken by characters."""
    chars = [(ch, b, i) for t, b, i in group for ch in t]
    out, cur, w = [], [], 0.0
    for ch, b, i in chars:
        cw = measure(ch, family, size, bold=b, italic=i, fmt=fmt)
        if cur and w + cw > width + 0.01:
            out.append(cur)
            cur, w = [], 0.0
        cur.append((ch, b, i))
        w += cw
    if cur:
        out.append(cur)
    return [_merge(g) for g in out] or [[]]


def _merge(segs) -> list[Seg]:
    out: list[Seg] = []
    for t, b, i in segs:
        if out and out[-1][1] == b and out[-1][2] == i:
            out[-1] = (out[-1][0] + t, b, i)
        else:
            out.append((t, b, i))
    return out


def _wrap(text: str, family: str, size: float, width: float, *, bold: bool = False, italic: bool = False,
          fmt: str = 'pptx') -> tuple[list[list[Seg]], list[int]]:
    """(lines of styled segments, words started up to and including each line)."""
    lines: list[list[Seg]] = []
    starts: list[int] = []
    count = 0
    for hard in str(text).split('\n'):
        cur: list[Seg] = []
        cur_w = 0.0
        for group in _groups(hard, bold, italic):
            gw = _gw(group, family, size, fmt)
            b0, i0 = group[0][1], group[0][2]
            sp = measure(' ', family, size, bold=b0, italic=i0, fmt=fmt) if cur else 0.0
            if cur and cur_w + sp + gw > width + 0.01:
                lines.append(_merge(cur))
                starts.append(count)
                cur, cur_w, sp = [], 0.0, 0.0
            count += 1
            if not cur and gw > width + 0.01:
                pieces = _break_group(group, family, size, width, fmt)
                for pc in pieces[:-1]:
                    lines.append(pc)
                    starts.append(count)
                group = pieces[-1]
                gw = _gw(group, family, size, fmt)
            if cur:
                cur.append((' ', b0, i0))
            cur.extend(group)
            cur_w += sp + gw
        lines.append(_merge(cur))
        starts.append(count)
    return lines, starts


def wrap_runs(text: str, family: str, size: float, width: float, *, bold: bool = False, italic: bool = False,
              fmt: str = 'pptx') -> list[list[Seg]]:
    """`text` (with **bold** / *italic*) greedily wrapped to `width` points with real metrics: a list of lines, each
    a list of (text, bold, italic) segments. Painters call this with the box's font, size and width to draw exactly
    the lines the layout measured."""
    return _wrap(text, family, size, width, bold=bold, italic=italic, fmt=fmt)[0]


def plain_line(line) -> str:
    return ''.join(t for t, _, _ in line)


def line_width(line, family: str, size: float, fmt: str) -> float:
    return sum(measure(t, family, size, bold=b, italic=i, fmt=fmt) for t, b, i in line)


def fit_text(text: str, family: str, step: str, ds: DesignSystem, w: float, h: float, fmt: str, *,
             bold: bool = False, line_height: float | None = None) -> FitResult:
    """Wrap and shrink `text` into (w, h), from the step's size down the scale, never below the format minimum."""
    return _fit(text, family, step, ds, w, h, fmt, bold=bold, line_height=line_height)[0]


def _fit(text, family, step, ds, w, h, fmt, *, bold=False, italic=False, line_height=None,
         shrink=True) -> tuple[FitResult, str]:
    res, used = None, step
    for st in (_steps_down(step) if shrink else [step]):
        size = ds.size(st, fmt)
        lh = line_height or line_height_for(ds, step)
        lines = wrap_runs(text, family, size, w, bold=bold, italic=italic, fmt=fmt)
        height = len(lines) * size * lh
        res, used = FitResult(size, [plain_line(ln) for ln in lines], round(height, 2), height <= h + 0.5), st
        if res.fits:
            return res, st
    lh = line_height or line_height_for(ds, step)
    keep = max(0, int((h + 0.5) // (res.size * lh)))
    res.overflow_words = sum(len(ln.split()) for ln in res.lines[keep:])
    return res, used


# ---------- content refs ----------

_SPEC_REF = re.compile(r'^spec:(?:(?P<top>title|subtitle)|(?P<s>\d+)/(?:(?P<part>heading|notes)|(?P<b>\d+)'
                       r'(?P<slices>(?:/(?:items|rows|words)\[\d+:\d+\])*)))(?:#(?P<field>[a-z]+))?$')
_SLICE = re.compile(r'/(items|rows|words)\[(\d+):(\d+)\]')


@dataclass
class Ref:
    s: int | None = None
    b: int | None = None
    top: str | None = None
    part: str | None = None
    items: tuple[int, int] | None = None
    rows: tuple[int, int] | None = None
    words: tuple[int, int] | None = None
    field: str | None = None


def parse_ref(ref: str) -> Ref | None:
    m = _SPEC_REF.match(ref or '')
    if not m:
        return None
    r = Ref(top=m['top'], part=m['part'], field=m['field'])
    if m['s'] is not None:
        r.s = int(m['s'])
    if m['b'] is not None:
        r.b = int(m['b'])
    for kind, i, j in _SLICE.findall(m['slices'] or ''):
        setattr(r, kind, (int(i), int(j)))
    return r


def block_of(ref: str, spec: dict) -> dict | None:
    r = parse_ref(ref)
    if r is None or r.b is None:
        return None
    try:
        return spec['sections'][r.s]['blocks'][r.b]
    except (IndexError, KeyError, TypeError):
        return None


def _balanced(text: str) -> str:
    """A word slice may cut through **bold** or *italic* markers: unbalanced markers are dropped."""
    if text.count('**') % 2:
        text = text.replace('**', '')
    if text.replace('**', '').count('*') % 2:
        text = re.sub(r'(?<!\*)\*(?!\*)', '', text)
    return text


def word_slice(text: str, i: int, j: int) -> str:
    return _balanced(' '.join(str(text).split()[i:j]))


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 3].rstrip() + '...'


def ref_text(ref: str, spec: dict) -> str:
    """The text a content ref resolves to ('' for non-text refs). Emphasis markers (**, *) are kept."""
    ref = ref or ''
    if ref.startswith('text:'):
        return ref[5:]
    r = parse_ref(ref)
    if r is None:
        return ''
    if r.top:
        t = str(spec.get(r.top) or '')
        return _short(t, 70) if r.field == 'footer' else t
    try:
        sec = spec['sections'][r.s]
    except (IndexError, KeyError, TypeError):
        return ''
    if r.part == 'heading':
        h = str(sec.get('heading') or '')
        return h + CONTINUED if r.field == 'continued' and h else h
    if r.part == 'notes':
        return str(sec.get('notes') or '')
    try:
        blk = sec['blocks'][r.b]
    except (IndexError, KeyError, TypeError):
        return ''
    t = blk.get('type')
    f = r.field
    if t == 'stat-cards':
        stats = blk.get('stats') or []
        if r.items:
            stats = stats[r.items[0]:r.items[1]]
        if f == 'title':
            return str(blk.get('title') or '')
        if f == 'stat':
            return ' '.join(str(x.get('value', '')) for x in stats)
        if f == 'rest':
            return ' '.join(str(x.get('label', '')) for x in stats)
        return '\n'.join(f"{x.get('value', '')} {x.get('label', '')}".strip() for x in stats)
    if f in ('caption', 'credit', 'by'):
        return str(blk.get(f) or '')
    if f == 'title':
        return str(blk.get('title') or blk.get('caption') or '')
    if f == 'summary':
        from ..create.spec import chart_summary
        return chart_summary(blk) if t == 'chart' else ''
    if t in ('paragraph', 'quote'):
        text = str(blk.get('text') or '')
    elif t == 'code':
        text = str(blk.get('text') or '')
        if r.items:
            text = '\n'.join(text.split('\n')[r.items[0]:r.items[1]])
    elif t == 'bullets':
        items = [str(x) for x in blk.get('items') or []]
        if r.items:
            items = items[r.items[0]:r.items[1]]
        text = '\n'.join(items)
    elif t == 'image':
        text = str(blk.get('caption') or '')
    else:
        text = str(blk.get('title') or '')
    if r.words:
        text = word_slice(text, *r.words)
    if f in ('stat', 'rest'):
        st = stat_of(text)
        if st is None:
            return text if f == 'rest' else ''
        return st[0] if f == 'stat' else st[1]
    return text


def box_text(box: Box, spec: dict) -> str:
    """The text a box's content ref resolves to ('' for non-text refs)."""
    return ref_text(box.content, spec)


def bullet_of(box: Box, spec: dict) -> str | None:
    """The bullet a text box starts with: '•', '3.' for an ordered list, None for everything else (and for the
    continuation of an item split across slides)."""
    r = parse_ref(box.content)
    if r is None or r.b is None or r.field or r.items is None or (r.words and r.words[0] > 0):
        return None
    blk = block_of(box.content, spec)
    if not blk or blk.get('type') != 'bullets':
        return None
    return f'{r.items[0] + 1}.' if blk.get('ordered') else BULLET


def bullet_indent(size: float, ordered: bool = False) -> float:
    return round(size * (1.6 if ordered else 1.1), 2)


# ---------- statistics in text ----------

_STAT_RE = re.compile(
    r'(?<![\w.,])(?P<cur>[$€£¥₹]\s?)?(?P<num>[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)'
    r'(?P<unit>\s?(?:%|percent\b|per cent\b|°\s?[CF]?\b|x\b|×|(?:k|bn|tn)\b|(?:thousand|million|billion|'
    r'trillion)\b|(?:km|kg|cm|mm|nm|mg|ml|kWh|kW|MW|GW|mph|km/h|m/s|ms|m|g|s)\b(?![\'\w])))?', re.I)


def stat_of(text: str) -> tuple[str, str] | None:
    """(value, label) for a short numeric fact ("72% of students ..." -> ("72%", "of students ...")); None when the
    text holds none or is too long to be a statistic. Bare years and small bare integers are not statistics."""
    from ..create.spec import strip_emphasis
    plain = strip_emphasis(str(text or '')).strip()
    words = plain.split()
    if not words or len(words) > 24:
        return None
    for m in _STAT_RE.finditer(plain):
        num, unit, cur = m['num'], (m['unit'] or '').strip(), m['cur']
        digits = num.replace(',', '').lstrip('+-−')
        if not unit and not cur and ',' not in num:
            try:
                v = float(digits)
            except ValueError:
                continue
            if '.' not in digits and (v < 100 or (1000 <= v <= 2100 and len(digits) == 4)):
                continue
        value = ' '.join(m.group(0).split())
        before = plain[:m.start()].split()
        after = plain[m.end():].strip(' :;,.-–—')
        rest = after if len(before) <= 2 and after else plain
        return value, rest
    return None


# ---------- theme dicts for create/diagram and the chart helpers ----------


def theme_dict(ds: DesignSystem) -> dict:
    """The create/themes-style dict the shared diagram and chart drawers read, from the design tokens."""
    c = ds.color
    return {'bg': c('bg'), 'text': c('text'), 'muted': c('muted'), 'heading': c('heading'), 'accent': c('accent'),
            'header_bg': c('header_bg'), 'header_text': c('header_text'), 'stripe': c('stripe'),
            'code_bg': c('code_bg'), 'border': c('border'), 'surface': c('surface'),
            'palette': list(ds.chart_palette or [c('accent')]), 'hatch': bool(ds.hatch),
            'grey_images': bool(ds.grey_images), 'radius': ds.shape.radius, 'font': ds.families.body,
            'heading_font': ds.families.heading, 'mono': ds.families.mono}


def contrast(fg: str, bg: str) -> float:
    from ..create import themes
    return themes.contrast(fg, bg)


def best_on(ds: DesignSystem, bg_token: str, candidates=('text', 'bg', 'on_accent', 'header_text', 'heading')) -> str:
    """The token colour that reads best on another token colour."""
    bg = ds.color(bg_token)
    return max(candidates, key=lambda t: (contrast(ds.color(t), bg), -candidates.index(t)))


# ---------- tables ----------


def table_cells(block: dict) -> tuple[list[str], list[list[str]]]:
    """(header, rows) as display text: a table block, or any diagram drawn as a table."""
    from ..create import render as _render_pkg  # noqa: F401  (the module, not create.render the function)
    import importlib
    render = importlib.import_module('jevrouter.create.render')
    if block.get('type') == 'table':
        formats = render.formats_of(block)
        cols = [render.show(c) for c in block.get('columns') or []]
        rows = [[render.show(v, formats[j] if j < len(formats) else None) for j, v in enumerate(r)]
                for r in block.get('rows') or []]
        return cols, rows
    if block.get('type') == 'chart':
        cols, rows = render.chart_rows(block)
        return [render.show(c) for c in cols], [[render.show(v) for v in r] for r in rows]
    from ..create import diagram
    try:
        cols, rows = diagram.table_of(block)
    except Exception:
        cols, rows = ['Item'], [[x] for x in _diagram_labels(block)]
    return [str(c) for c in cols], [[str(v) for v in r] for r in rows]


def _diagram_labels(block: dict) -> list[str]:
    out = []
    for v in block.values():
        if isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict):
                    out += [str(x[k]) for k in ('label', 'value', 'date') if x.get(k)]
    return out or [str(block.get('title') or block.get('type'))]


@dataclass
class TableGeometry:
    cols: list[float]                # column widths
    heights: list[float]             # header, then each row
    cells: list[list[list[str]]]     # [row][col] -> lines (row 0 is the header)
    size: float
    pad: float
    family: str
    vpad: float = 0.0                # top and bottom cell padding (pad, more when the rows are stretched to a height)


def table_geometry(block: dict, rows: tuple[int, int] | None, w: float, size: float, ds: DesignSystem,
                   fmt: str, height: float | None = None) -> TableGeometry:
    """Column widths, row heights and wrapped cell lines of a table (header + rows[i:j]) drawn `w` wide at `size`.
    With `height` taller than the natural table, every row grows by the same amount (text centred in its row), so a
    short table fills the box the layout gave it; the painters pass the box height so they draw what was laid out."""
    cols, all_rows = table_cells(block)
    body = all_rows[rows[0]:rows[1]] if rows else all_rows
    n = max(1, len(cols))
    lengths = [max([len(cols[j]) if j < len(cols) else 0] + [len(r[j]) if j < len(r) else 0 for r in all_rows])
               for j in range(n)]
    weights = [min(max(x, 4), 30) for x in lengths]
    widths = [w * x / sum(weights) for x in weights]
    pad = round(max(2.5, size * (0.25 if fmt == 'pptx' else 0.35)), 2)
    fam = family_for(ds, 'body')
    cells, heights = [], []
    for ri, row in enumerate([cols] + body):
        lines_row = []
        for j in range(n):
            text = row[j] if j < len(row) else ''
            lines = [plain_line(ln) for ln in wrap_runs(text, fam, size, max(4.0, widths[j] - 2 * pad),
                                                          bold=ri == 0, fmt=fmt)]
            lines_row.append(lines)
        cells.append(lines_row)
        heights.append(round(max(len(x) for x in lines_row) * size * TABLE_LINE + 2 * pad, 2))
    vpad = pad
    if height is not None and heights and height > sum(heights) + 0.5:
        extra = (height - sum(heights)) / len(heights)
        heights = [round(h + extra, 2) for h in heights]
        vpad = round(pad + extra / 2, 2)
    return TableGeometry([round(x, 2) for x in widths], heights, cells, size, pad, fam, vpad)


# ---------- stat cards ----------


@dataclass
class Item:
    """One piece of content headed for a text slot."""
    ref: str
    text: str
    role: str                        # para bullet quote by code heading title caption credit summary label number break
    s: int | None = None
    b: int | None = None
    ordered: bool = False
    w0: int = 0                      # word offset (a split item)
    base: str = ''                   # the ref without its word slice

    def __post_init__(self):
        if not self.base:
            self.base = self.ref

    def split(self, k: int) -> tuple['Item', 'Item']:
        words = self.text.split()
        a = replace(self, ref=f'{self.base}/words[{self.w0}:{self.w0 + k}]', text=_balanced(' '.join(words[:k])))
        b = replace(self, ref=f'{self.base}/words[{self.w0 + k}:{self.w0 + len(words)}]',
                    text=_balanced(' '.join(words[k:])), w0=self.w0 + k)
        return a, b

    @property
    def words(self) -> int:
        return len(self.text.split())


def section_items(spec: dict, s: int) -> tuple[list[Item], list[tuple[int, dict]]]:
    """(text items in order, visual blocks (index, block)) of one section."""
    sec = spec['sections'][s]
    texts, visuals = [], []
    for b, blk in enumerate(sec.get('blocks') or []):
        t = blk.get('type')
        if t == 'paragraph':
            if str(blk.get('text') or '').strip():
                texts.append(Item(f'spec:{s}/{b}', str(blk['text']), 'para', s, b))
        elif t == 'bullets':
            for i, it in enumerate(blk.get('items') or []):
                texts.append(Item(f'spec:{s}/{b}/items[{i}:{i + 1}]', str(it), 'bullet', s, b,
                                  ordered=bool(blk.get('ordered'))))
        elif t == 'quote':
            texts.append(Item(f'spec:{s}/{b}', str(blk.get('text') or ''), 'quote', s, b))
            if blk.get('by'):
                texts.append(Item(f'spec:{s}/{b}#by', str(blk['by']), 'by', s, b))
        elif t == 'code':
            lines = str(blk.get('text') or '').split('\n')
            for i in range(0, len(lines), CODE_CHUNK):
                texts.append(Item(f'spec:{s}/{b}/items[{i}:{i + CODE_CHUNK}]', '\n'.join(lines[i:i + CODE_CHUNK]),
                                  'code', s, b))
        elif t == 'page_break':
            texts.append(Item('none', '', 'break', s, b))
        elif t in ('figure',):
            continue
        else:
            visuals.append((b, blk))
    return texts, visuals


def stat_items(spec: dict, s: int, focus: int | None = None) -> list[Item]:
    """Statistic-bearing items of a section (bullet items and short paragraphs), the focus block's first."""
    texts, visuals = section_items(spec, s)
    out = [i for i in texts if i.role in ('para', 'bullet') and stat_of(i.text)]
    for b, blk in visuals:
        if blk.get('type') == 'stat-cards':
            for i, x in enumerate(blk.get('stats') or []):
                out.append(Item(f'spec:{s}/{b}/items[{i}:{i + 1}]', f"{x.get('value', '')} {x.get('label', '')}",
                                'stat', s, b))
    if focus is not None:
        out.sort(key=lambda i: i.b != focus)
    return out


# ---------- the designer ----------


def _seed(*parts) -> int:
    return zlib.crc32('|'.join(str(p) for p in parts).encode()) & 0x7FFFFFFF


def _asset_path(sha: str):
    from ..create import assets
    return assets.CACHE / f'{sha}.png'


def image_px(sha: str) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(_asset_path(sha)) as im:
            return im.size
    except Exception:
        return None


def crop_fractions(size_px, focal, box_w, box_h, fit='cover') -> tuple[float, float, float, float]:
    """(x, y, w, h) fractions of the source shown in a box of that aspect, centred on the focal point."""
    if fit != 'cover' or not size_px or box_w <= 0 or box_h <= 0:
        return (0.0, 0.0, 1.0, 1.0)
    pw, ph = size_px
    box_a, img_a = box_w / box_h, pw / max(ph, 1)
    fx, fy = focal or (0.5, 0.42)
    if img_a > box_a:
        wf = box_a / img_a
        x = min(max(fx - wf / 2, 0.0), 1.0 - wf)
        return (round(x, 4), 0.0, round(wf, 4), 1.0)
    hf = img_a / box_a
    y = min(max(fy - hf / 2, 0.0), 1.0 - hf)
    return (0.0, round(y, 4), 1.0, round(hf, 4))


def _licence(credit: str) -> str:
    m = re.search(r'(CC[\s-]?BY(?:[\s-]SA)?(?:\s?\d\.\d)?|CC0(?:\s?1\.0)?|public domain)', str(credit or ''), re.I)
    return m.group(1) if m else 'see credit'


class _PB:
    """One page being built."""

    def __init__(self, d: '_Designer', layout: str, section: int | None, di: int | None, variant: str = 'default',
                 continued: bool = False):
        self.d = d
        self.layout = layout
        self.defn = library.get(layout)
        self.page = PagePlan(index=0, layout=layout, w=d.size[0], h=d.size[1], section=section, variant=variant,
                             continued=continued, direction=di)
        self.areas = slot_areas(layout, variant)
        self.notes: list[str] = []
        self.on_photo = False            # text slots marked overlay_ok sit on a photo or art

    # geometry
    def rect(self, slot: str) -> tuple[float, float, float, float]:
        area, _ = self.areas[slot]
        s = self.defn.slot(slot)
        return grid_rect(area, self.d.size, self.d.ds, bleed=bool(s and s.bleed), fmt=self.d.fmt)

    def step(self, slot: str) -> str | None:
        return self.areas[slot][1]

    def add(self, box: Box) -> Box:
        self.page.boxes.append(box)
        return box

    # colour of text in a slot
    def color_for(self, role: str, slot_def) -> str:
        if slot_def is not None and slot_def.overlay_ok and self.on_photo:
            return self.d.on_overlay
        if role in ('number', 'stat'):
            return 'accent'
        if role in ('heading', 'title'):
            return 'heading'
        if role in ('caption', 'credit', 'by', 'summary', 'label', 'kicker'):
            return 'muted'
        return 'text'

    # text slots
    def _measure_items(self, items: list[Item], step: str, w: float, *, bold=None, italic=None):
        d = self.d
        size = d.ds.size(step, d.fmt)
        lh = line_height_for(d.ds, step)
        out = []
        for it in items:
            fam = family_for(d.ds, step, it.role)
            b = (it.role in ('heading', 'title', 'number', 'stat') or step == 'display') if bold is None else bold
            i = (it.role in ('quote', 'by')) if italic is None else italic
            indent = bullet_indent(size, it.ordered) if it.role == 'bullet' else 0.0
            lines, starts = _wrap(it.text, fam, size, max(8.0, w - indent), bold=b, italic=i, fmt=d.fmt)
            out.append({'item': it, 'lines': lines, 'starts': starts, 'fam': fam, 'bold': b, 'italic': i,
                        'h': len(lines) * size * lh, 'indent': indent})
        return size, lh, out

    @staticmethod
    def _gap(prev: Item | None, cur: Item, size: float) -> float:
        if prev is None:
            return 0.0
        if prev.role == 'bullet' and cur.role == 'bullet':
            return round(0.3 * size, 2)
        if prev.role == 'code' and cur.role == 'code' and prev.b == cur.b:
            return 0.0
        if cur.role == 'by':
            return round(0.2 * size, 2)
        return round(0.6 * size, 2)

    def text(self, slot: str, items: list[Item], *, rect=None, step: str | None = None, color: str | None = None,
             bold=None, italic=None, align: str | None = None, valign: str = 'top', shrink: bool = True,
             budget: bool = True, force: bool = False, fill=None) -> list[Item]:
        """Stack `items` in a text slot: fit (shrink within the scale), rebalance over-budget items to the notes,
        then place what fits and return the rest (the caller splits it onto a continued page)."""
        items = [i for i in items if i.role != 'break' and (i.text.strip() or i.role == 'code')]
        if not items:
            return []
        d = self.d
        sd = self.defn.slot(slot)
        rect = rect or self.rect(slot)
        step = step or self.step(slot) or 'body'
        x, y, w, h = rect
        over: list[Item] = []
        if budget and d.fmt == 'pptx' and sd is not None and sd.max_words:
            items, moved = _budget(items, sd.max_words)
            if moved:
                self.notes += [_plain_text(i.text) for i in moved]
                d.flag('notes')
        chosen = None
        steps = _steps_down(step) if shrink else [step]
        for st in steps:
            size, lh, ms = self._measure_items(items, st, w, bold=bold, italic=italic)
            total = sum(m['h'] for m in ms) + sum(self._gap(ms[k - 1]['item'] if k else None, ms[k]['item'], size)
                                                  for k in range(len(ms)))
            chosen = (st, size, lh, ms, total)
            if total <= h + 0.5:
                break
        st, size, lh, ms, total = chosen
        if total > h + 0.5:
            ms, rest = self._take(ms, size, lh, h, force=force)
            over = rest + over
            total = sum(m['h'] for m in ms) + sum(self._gap(ms[k - 1]['item'] if k else None, ms[k]['item'], size)
                                                  for k in range(len(ms)))
        if not ms:
            return over
        align = align or (sd.align if sd is not None else 'left')
        single = len(ms) == 1
        top = y if single or valign == 'top' else (y + h - total if valign == 'bottom' else y + (h - total) / 2)
        cy = top
        prev = None
        for m in ms:
            it = m['item']
            cy += self._gap(prev, it, size)
            role_color = color or self.color_for(it.role, sd)
            bx = Box(id='', kind='text', x=x, y=round(cy, 2), w=w, h=round(m['h'], 2), slot=slot, content=it.ref,
                     style=BoxStyle(text_color=role_color, fill=fill if fill else ('code_bg' if it.role == 'code' else None)),
                     font=m['fam'], step=st, size=size, bold=m['bold'], italic=m['italic'], line_height=lh,
                     align=align, valign=valign if single else 'top', lines=[plain_line(ln) for ln in m['lines']],
                     overlay_ok=bool(sd.overlay_ok) if sd is not None else False)
            if single:
                bx.y, bx.h = y, max(h, round(m['h'], 2))
            self.add(bx)
            cy += m['h']
            prev = it
        return over

    def _take(self, ms, size, lh, h, *, force=False):
        """The measured items that fit in height h; the first one that doesn't is split by lines (at least 2 lines
        each side) when it can be. Returns (placed, rest as items)."""
        placed, cum, prev = [], 0.0, None
        for k, m in enumerate(ms):
            it = m['item']
            gap = self._gap(prev, it, size)
            if cum + gap + m['h'] <= h + 0.5:
                placed.append(m)
                cum += gap + m['h']
                prev = it
                continue
            room = h - cum - gap
            n = int((room + 0.5) // (size * lh))
            total = len(m['lines'])
            if it.role in SPLITTABLE and n >= 1 and total - n >= 1 and (n >= 2 or total <= 2 or not placed):
                words = m['starts'][n - 1]
                if 0 < words < it.words:
                    head, tail = it.split(words)
                    lines = m['lines'][:n]
                    placed.append({**m, 'item': head, 'lines': lines, 'starts': m['starts'][:n],
                                   'h': n * size * lh})
                    return placed, [tail] + [x['item'] for x in ms[k + 1:]]
            if not placed and force:
                placed.append(m)      # never drop words: the box grows past its slot and QA says so
                self.d.flag('overflow')
                return placed, [x['item'] for x in ms[k + 1:]]
            return placed, [x['item'] for x in ms[k:]]
        return placed, []

    def shape(self, slot: str, *, content: str = 'none', fill: str | None = None, opacity: float = 1.0,
              rect=None, radius: float = 0.0, alt: str | None = None, overlay_ok: bool = False) -> Box:
        sd = self.defn.slot(slot)
        x, y, w, h = rect or self.rect(slot)
        return self.add(Box(id='', kind='shape', x=x, y=y, w=w, h=h, slot=slot, content=content,
                            style=BoxStyle(fill=fill, opacity=opacity, radius=radius), alt=alt,
                            overlay_ok=overlay_ok or bool(sd and sd.overlay_ok), bleed=bool(sd and sd.bleed)))


def _plain_text(text: str) -> str:
    from ..create.spec import strip_emphasis
    return strip_emphasis(text)


def _budget(items: list[Item], max_words: int) -> tuple[list[Item], list[Item]]:
    """Items kept within a slot's word budget (bullets: max_words; prose: 1.5x); the first item always stays. The
    rest go to the speaker notes (rebalance)."""
    limit = max_words if any(i.role == 'bullet' for i in items) else int(max_words * 1.5)
    kept, moved, n = [], [], 0
    for i in items:
        if kept and (moved or n + i.words > limit) and i.role in ('bullet', 'para'):
            moved.append(i)
            continue
        kept.append(i)
        n += i.words
    return kept, moved


class _Designer:
    def __init__(self, spec: dict, fmt: str, ds: DesignSystem, direction: ArtDirection, ws, size, file_id: str = ''):
        from . import direct
        self.spec, self.fmt, self.ds, self.ws, self.size = spec, fmt, ds, ws, size
        self.file_id = file_id
        self.outline = direct.outline_of(spec, fmt)
        last = self.outline.pages[-1] if self.outline.pages else None
        if (last is not None and last.section is None and last.index > 0 and
                len(direction.pages) == len(self.outline.pages) - 1 and
                all(p.layout != 'closing' for p in direction.pages)):
            self.outline.pages.pop()   # a deck held to its slide count has no closing slide (agent._hold_count)
        if len(direction.pages) != len(self.outline.pages):
            fixed, _ = direct.validate_direction(direction.to_dict(), self.outline, fmt)
            fixed.preset, fixed.source, fixed.mood, fixed.dark = direction.preset, direction.source, direction.mood, \
                direction.dark
            fixed.notes = list(direction.notes) + ['The layout plan was matched to the content again.']
            direction = fixed
        self.direction = direction
        self.slide = fmt == 'pptx'
        self.assets: dict[str, AssetUse] = {}
        self.flags: set[str] = set()
        self.plan_notes: list[str] = []
        self.images = [(s, b) for s, sec in enumerate(spec.get('sections') or [])
                       for b, blk in enumerate(sec.get('blocks') or []) if blk.get('type') == 'image']
        self.on_overlay = 'text'      # the overlay is a tint of the background, so the text role reads on it
        level1 = [s for s, sec in enumerate(spec.get('sections') or []) if sec.get('level', 1) == 1]
        self.number = {s: k + 1 for k, s in enumerate(level1)}
        self.flow_pb: _PB | None = None
        self.flow_slots: list[list] = []        # [slot, rect, used height]
        self.flow_section: int | None = None

    def flag(self, what: str):
        self.flags.add(what)

    # ----- assets -----
    def use_asset(self, ref: str, kind: str, source: str, licence: str, credit: str | None = None):
        if ref not in self.assets:
            self.assets[ref] = AssetUse(ref, kind, source, licence, credit)

    def art_ref(self, key) -> str:
        seed = _seed(self.file_id, key)
        try:
            from . import art
            kind = art.pick(self.ds, list(self.direction.mood or []), seed)
            if kind not in art.ART_KINDS:
                raise ValueError(kind)
        except Exception:
            kinds = ('corner-arcs', 'orbit-rings', 'dots', 'waves', 'grid', 'blobs', 'stripes', 'gradient-mesh')
            kind = kinds[_seed(self.ds.id) % len(kinds)]
        ref = f'art:{kind}:{seed}'
        self.use_asset(ref, 'art', 'studio', 'generated')
        return ref

    def image_box(self, pb: _PB, slot: str, s: int, b: int, rect, *, fit: str, bleed: bool) -> Box | None:
        blk = self.spec['sections'][s]['blocks'][b]
        sha = str(blk.get('asset') or '')
        px = image_px(sha)
        if px is None:
            self.plan_notes.append('An image was missing from the image cache, so its slot was left out.')
            return None
        x, y, w, h = rect
        if fit == 'contain':
            dpi = MIN_DPI.get(self.fmt, 96)
            scale = min(w / px[0], h / px[1], MAX_UPSCALE * 72.0 / dpi)
            iw, ih = px[0] * scale, px[1] * scale
            x, w, h = x + (w - iw) / 2, iw, ih
        content, crop, focal = f'asset:{sha}', None, (0.5, 0.42)
        try:
            from . import assets as vis
            duo = (self.ds.color('overlay'), self.ds.color('accent')) if self.ds.image == 'duotone' else None
            content, crop, focal = vis.prepare(sha, w, h, self.ws, fmt=self.fmt, fit=fit, mono=bool(self.ds.grey_images),
                                               duotone=duo)
            crop, focal = tuple(crop), tuple(focal)
        except Exception:
            content = f'asset:{sha}'
            crop = crop_fractions(px, focal, w, h, fit)
        credit = str(blk.get('credit') or '')
        self.use_asset(f'asset:{sha}', 'image', 'commons', _licence(credit), credit or None)
        alt = str(blk.get('caption') or '') or credit or 'Image'
        sd = pb.defn.slot(slot)
        return pb.add(Box(id='', kind='image', x=round(x, 2), y=round(y, 2), w=round(w, 2), h=round(h, 2), slot=slot,
                          content=content, fit=fit, crop=crop, focal=focal, alt=alt[:300], bleed=bleed,
                          style=BoxStyle(radius=self.ds.shape.radius if self.ds.image == 'rounded' and not bleed else 0,
                                         stroke='border' if self.ds.image == 'framed' and not bleed else None,
                                         stroke_w=self.ds.shape.stroke if self.ds.image == 'framed' and not bleed else 0),
                          overlay_ok=bool(sd and sd.overlay_ok)))

    # ----- figures (image, chart, table, diagram) placed in a rect -----
    def figure(self, pb: _PB, slot: str, s: int, b: int, blk: dict, rect, *, summary: bool = True,
               caption: bool = True) -> tuple | None:
        """Place one visual in rect with its labels (diagram/chart title, chart summary, image caption and credit).
        Returns a leftover (s, b, blk, row_start) when a table's rows did not all fit."""
        t = blk.get('type')
        x, y, w, h = rect
        cap_step = 'caption'
        if t == 'image':
            below = []
            if caption and blk.get('caption'):
                below.append(Item(f'spec:{s}/{b}#caption', str(blk['caption']), 'caption', s, b))
            if caption and blk.get('credit'):
                below.append(Item(f'spec:{s}/{b}#credit', str(blk['credit']), 'credit', s, b))
            hb = self._stack_height(below, cap_step, w)
            if below and h - hb < h * 0.5:
                moved = [i for i in below if i.role == 'caption']
                pb.notes += [_plain_text(i.text) for i in moved]
                below = [i for i in below if i.role != 'caption']
                hb = self._stack_height(below, cap_step, w)
            gap = self.ds.spacing.unit if below else 0
            img = self.image_box(pb, slot, s, b, (x, y, w, max(20.0, h - hb - gap)), fit='contain', bleed=False)
            if img is not None and below:
                top = img.y + img.h + gap
                left = pb.text(slot, below, rect=(x, top, w, y + h - top), step=cap_step, budget=False, force=True)
                pb.notes += [_plain_text(i.text) for i in left]
            return None
        if t in ('table',) or (t not in ('chart', 'stat-cards') and _diagram_aspect(blk) is None):
            head = []
            if blk.get('title') and not self.repeats_heading(s, blk['title']):
                head.append(Item(f'spec:{s}/{b}#title', str(blk['title']), 'caption', s, b))
            ht = self._stack_height(head, cap_step, w, bold=True)
            if head:
                pb.text(slot, head, rect=(x, y, w, ht), step=cap_step, bold=True, budget=False, force=True)
                y, h = y + ht + 4, h - ht - 4
            return self.table_box(pb, slot, s, b, blk, (x, y, w, h), 0)
        if t == 'stat-cards':
            self.cards(pb, slot, [i for i in stat_items(self.spec, s) if i.b == b and i.role == 'stat'], (x, y, w, h))
            return None
        head = []
        if t != 'chart' or not self.slide:     # PPTX charts carry their title natively
            title = blk.get('title')
            if title:
                head.append(Item(f'spec:{s}/{b}#title', str(title), 'caption', s, b))
        tail = []
        if t == 'chart' and summary:
            tail.append(Item(f'spec:{s}/{b}#summary', ref_text(f'spec:{s}/{b}#summary', self.spec), 'summary', s, b))
        ht = self._stack_height(head, cap_step, w, bold=True)
        hs = self._stack_height(tail, cap_step, w)
        if head:
            pb.text(slot, head, rect=(x, y, w, ht), step=cap_step, bold=True, budget=False, force=True)
            y, h = y + ht + 4, h - ht - 4
        body_h = h - (hs + 4 if tail else 0)
        if t == 'chart':
            fw, fx = w, x
            fh = max(60.0, min(body_h, w * (0.72 if self.slide else 0.62)))
            body_h = fh
            alt = f"Chart: {blk.get('title') or ''}. {ref_text(f'spec:{s}/{b}#summary', self.spec)}"
            pb.add(Box(id='', kind='chart', x=round(fx, 2), y=round(y, 2), w=round(fw, 2), h=round(fh, 2), slot=slot,
                       content=f'spec:{s}/{b}', fit='contain', alt=alt[:500],
                       style=BoxStyle(text_color='text')))
        else:
            aspect = _diagram_aspect(blk) or 0.6
            fw = w
            fh = fw * aspect
            if fh > body_h:
                fh = max(40.0, body_h)
                fw = fh / aspect
            fx = x + (w - fw) / 2
            from ..create import diagram
            try:
                alt = diagram.alt_text(blk)
            except Exception:
                alt = f"Diagram: {blk.get('title') or t}"
            pb.add(Box(id='', kind='diagram', x=round(fx, 2), y=round(y, 2), w=round(fw, 2), h=round(fh, 2), slot=slot,
                       content=f'spec:{s}/{b}', fit='contain', alt=alt[:500], style=BoxStyle(text_color='text')))
            body_h = fh
        if tail:
            top = y + fh + 4
            pb.text(slot, tail, rect=(x, top, w, hs), step=cap_step, budget=False, force=True)
        return None

    def repeats_heading(self, s: int, title) -> bool:
        """A table titled like its section ("Orders" under the heading "Orders"): the title would only say the heading
        again above it, so it is left out (the table's alt text still names it)."""
        return _bare_title(title) == _bare_title((self.spec['sections'][s] or {}).get('heading'))

    def _stack_height(self, items: list[Item], step: str, w: float, bold=None) -> float:
        if not items:
            return 0.0
        pb = _PB.__new__(_PB)
        pb.d = self
        size, lh, ms = _PB._measure_items(pb, items, step, w, bold=bold)
        return round(sum(m['h'] for m in ms) + sum(_PB._gap(ms[k - 1]['item'] if k else None, ms[k]['item'], size)
                                                   for k in range(len(ms))), 2)

    def table_box(self, pb: _PB, slot: str, s: int, b: int, blk: dict, rect, start: int) -> tuple | None:
        x, y, w, h = rect
        _, rows = table_cells(blk)
        n = len(rows)
        steps = ['body', 'caption'] if self.slide else ['body', 'caption']
        if self.slide and len(table_cells(blk)[0]) > 5:
            steps = ['caption']
        chosen = None
        for st in steps:
            size = self.ds.size(st, self.fmt)
            g = table_geometry(blk, (start, n), w, size, self.ds, self.fmt)
            chosen = (st, size, g)
            if sum(g.heights) <= h + 0.5:
                break
        st, size, g = chosen
        end, total = start, g.heights[0]
        for k, rh in enumerate(g.heights[1:]):
            if total + rh > h + 0.5 and end > start:
                break
            total += rh
            end = start + k + 1
        if end < n:
            fit = max(1, end - start)
            parts = math.ceil((n - start) / fit)
            end = min(end, start + math.ceil((n - start) / parts))    # even parts: no near-empty continued page
            g = table_geometry(blk, (start, end), w, size, self.ds, self.fmt)
            total = sum(g.heights)
        box_h = total
        if self.slide and total < h - 0.5:
            # a short table on a slide grows its rows toward the slot (never past TABLE_STRETCH times its natural
            # height) instead of leaving most of the slide empty under it
            box_h = min(h, total * TABLE_STRETCH)
        content = f'spec:{s}/{b}' + (f'/rows[{start}:{end}]' if (start, end) != (0, n) else '')
        title = blk.get('title') or (blk.get('type') if blk.get('type') != 'table' else 'Table')
        pb.add(Box(id='', kind='table', x=round(x, 2), y=round(y, 2), w=round(w, 2), h=round(box_h, 2),
                   slot=slot, content=content, font=g.family, step=st, size=size, line_height=TABLE_LINE,
                   alt=f'Table: {title}'[:300], style=BoxStyle(text_color='text', stroke='border',
                                                                 stroke_w=self.ds.shape.stroke)))
        return (s, b, blk, end) if end < n else None

    def cards(self, pb: _PB, slot: str, stats: list[Item], rect):
        """2-4 statistic cards: a surface card each, the value large, the label under it."""
        stats = stats[:4]
        x, y, w, h = rect
        n = max(1, len(stats))
        gap = self.ds.spacing.gutter
        cw = (w - (n - 1) * gap) / n
        pad = max(12.0, self.ds.spacing.unit * 2)
        for k, it in enumerate(stats):
            cx = x + k * (cw + gap)
            pb.add(Box(id='', kind='shape', x=round(cx, 2), y=round(y, 2), w=round(cw, 2), h=round(h, 2), slot=slot,
                       content='none', style=BoxStyle(fill='surface', stroke='border', stroke_w=self.ds.shape.stroke,
                                                      radius=self.ds.shape.radius, shadow=self.ds.shape.shadow)))
            inner = (cx + pad, y + pad, cw - 2 * pad, h - 2 * pad)
            value = Item(it.base + '#stat', ref_text(it.base + '#stat', self.spec) or it.text, 'stat', it.s, it.b)
            label = Item(it.base + '#rest', ref_text(it.base + '#rest', self.spec), 'label', it.s, it.b)
            vh = inner[3] * 0.5
            pb.text(slot, [value], rect=(inner[0], inner[1], inner[2], vh), step='h1', color='accent', bold=True,
                    budget=False, force=True, valign='bottom')
            if label.text:
                left = pb.text(slot, [label], rect=(inner[0], inner[1] + vh + 4, inner[2], inner[3] - vh - 4),
                               step='body' if self.slide else 'caption', color='text', budget=False, force=True)
                pb.notes += [_plain_text(i.text) for i in left]
        for bx in pb.page.boxes:
            if bx.slot == slot and bx.kind == 'text':
                bx.overlay_ok = True       # text on its card by design

    # ----- pages -----
    def pb(self, layout, section, di, variant='default', continued=False) -> _PB:
        return _PB(self, layout, section, di, variant, continued)

    def heading(self, s: int, continued: bool = False) -> Item:
        h = str(self.spec['sections'][s].get('heading') or '')
        return Item(f'spec:{s}/heading' + ('#continued' if continued else ''), h + (CONTINUED if continued and h else ''),
                    'heading', s)

    def title_bar(self, pb: _PB):
        if not self.slide or pb.layout not in BAR_UNDER_TITLE or self.ds.shape.accent_shape not in ('bar', 'underline'):
            return
        tb = next((bx for bx in pb.page.boxes if bx.slot == 'title' and bx.kind == 'text'), None)
        if tb is None:
            return
        x, y, w, h = pb.rect('title')
        widest = max((measure(ln, tb.font, tb.size, bold=tb.bold, fmt=self.fmt) for ln in tb.lines or ['']),
                     default=56.0)
        bar_w = 56.0 if self.ds.shape.accent_shape == 'bar' else min(w, max(56.0, widest))
        pb.add(Box(id='', kind='shape', x=x, y=round(y + h + 1.5, 2), w=round(bar_w, 2), h=3.5 if bar_w > 56 else 4.0,
                   slot=None, content='none', style=BoxStyle(fill='accent', radius=0)))

    def notes_for(self, s: int | None) -> str:
        if s is None:
            return ''
        return str(self.spec['sections'][s].get('notes') or '')

    def finish_page(self, pb: _PB, first_of_section: bool = True) -> PagePlan:
        notes = [self.notes_for(pb.page.section)] if first_of_section and not pb.page.continued else []
        pb.page.notes = '\n\n'.join(x for x in notes + pb.notes if x)
        return pb.page

    # ----- direction entries -----
    def direction_pages(self, di: int) -> list[PagePlan]:
        pd = self.direction.pages[di]
        op = self.outline.pages[di]
        if op.section is None:
            if di == 0:
                return self.cover(di, pd)
            return self.closing(di, pd, None)
        if self.slide:
            return self.slide_section(di, pd, op.section)
        return self.pdf_section(di, pd, op.section)

    # ----- slides -----
    def cover(self, di: int, pd: PageDirection) -> list[PagePlan]:
        self.close_flow()
        spec = self.spec
        title = Item('spec:title', str(spec.get('title') or ''), 'title')
        subtitle = Item('spec:subtitle', str(spec.get('subtitle') or ''), 'label')
        if not self.slide:
            pb = self.pb('cover', None, di, pd.variant)
            img = self._image_index(pd.image, None)
            if img is not None:
                box = self.image_box(pb, 'image', *img, pb.rect('image'), fit='cover', bleed=True)
                if box is None:
                    pb.shape('image', content=self.art_ref('cover'))
            else:
                pb.shape('image', content=self.art_ref('cover'))
            pb.text('title', [title], color='heading', valign='bottom', force=True)
            pb.text('subtitle', [subtitle], color='muted', force=True)
            if img is not None:
                s, b = img
                credit = self.spec['sections'][s]['blocks'][b].get('credit')
                if credit:
                    left = pb.text('meta', [Item(f'spec:{s}/{b}#credit', str(credit), 'credit', s, b)], force=True)
                    pb.notes += [_plain_text(i.text) for i in left]
            self.flow_open(pb, None, ())
            return [self.finish_page(pb)]
        layout = pd.layout if pd.layout in ('cover-hero', 'cover-type') else 'cover-type'
        img = self._image_index(pd.image, None) if layout == 'cover-hero' else None
        if layout == 'cover-hero' and img is None:
            layout = 'cover-type'
        pb = self.pb(layout, None, di, pd.variant)
        if layout == 'cover-hero':
            box = self.image_box(pb, 'image', *img, pb.rect('image'), fit='cover', bleed=True)
            if box is None:
                return self.cover(di, replace(pd, layout='cover-type'))
            pb.on_photo = True
            pb.shape('overlay', fill='overlay', opacity=self.ds.overlay_alpha, alt=None)
            pb.text('title', [title], valign='bottom', force=True)
            pb.text('subtitle', [subtitle], force=True)
            s, b = img
            credit = self.spec['sections'][s]['blocks'][b].get('credit')
            if credit:
                left = pb.text('credit', [Item(f'spec:{s}/{b}#credit', str(credit), 'credit', s, b)], force=False)
                if left:
                    pb.notes += [_plain_text(i.text) for i in left]
        else:
            pb.shape('art', content=self.art_ref('cover'), alt=None)
            pb.text('title', [title], color='heading', valign='bottom', force=True)
            pb.text('subtitle', [subtitle], color='muted', force=True)
        return [self.finish_page(pb)]

    def closing(self, di: int, pd: PageDirection, s: int | None) -> list[PagePlan]:
        pb = self.pb('closing', s, di, pd.variant)
        pb.shape('art', content=self.art_ref('closing'), opacity=0.35)
        if s is not None:
            texts, _ = section_items(self.spec, s)
            pb.text('title', [self.heading(s)], color='heading', force=True)
            rest = pb.text('subtitle', [i for i in texts if i.role in TEXT_ROLES][:2], color='muted')
            pb.notes += [_plain_text(i.text) for i in rest]
        else:
            pb.text('title', [Item('text:Thank you', 'Thank you', 'title')], color='heading', force=True)
            pb.text('subtitle', [Item('spec:title', str(self.spec.get('title') or ''), 'label')], color='muted',
                    force=True)
        credits = [Item(f'spec:{a}/{b}#credit', str(self.spec['sections'][a]['blocks'][b].get('credit') or ''),
                        'credit', a, b) for a, b in self.images]
        credits = [c for c in credits if c.text]
        if credits:
            left = pb.text('credits', credits, color='muted', budget=False)
            if left:
                pb.page.boxes = [bx for bx in pb.page.boxes if bx.slot != 'credits']
                more = Item('text:More image credits are in the speaker notes.',
                            'More image credits are in the speaker notes.', 'credit')
                keep = credits[:max(0, len(credits) - len(left) - 1)]
                moved = credits[len(keep):]
                pb.text('credits', keep + [more], color='muted', budget=False, force=True)
                pb.notes += ['Image credits:'] + [_plain_text(i.text) for i in moved]
        return [self.finish_page(pb)]

    def _image_index(self, idx: int | None, s: int | None) -> tuple[int, int] | None:
        if idx is not None and 0 <= idx < len(self.images):
            cand = self.images[idx]
            if s is None or cand[0] == s:
                return cand
        if s is None:
            return self.images[0] if self.images else None
        return next((x for x in self.images if x[0] == s), None)

    def slide_section(self, di: int, pd: PageDirection, s: int) -> list[PagePlan]:
        from . import direct
        layout = pd.layout if pd.layout in library.SLIDE_LAYOUTS else 'title-bullets'
        op = self.outline.pages[di]
        pages: list[PagePlan] = []
        if layout == 'section-divider':
            pb = self.pb('section-divider', s, di, pd.variant)
            num = self.number.get(s)
            if num is not None:
                pb.text('number', [Item(f'text:{num:02d}', f'{num:02d}', 'number')], color='accent', bold=True,
                        force=True)
            pb.text('title', [self.heading(s)], force=True)
            self.accent(pb, 'accent')
            texts, visuals = section_items(self.spec, s)
            if not texts and not visuals:
                return [self.finish_page(pb)]
            pages.append(self.finish_page(pb))
            layout = direct.content_layout(op, self.outline, 'pptx', avoid=('section-divider',))
            pd = replace(pd, layout=layout)
        if layout in ('cover-hero', 'cover-type'):
            layout = direct.content_layout(op, self.outline, 'pptx')
        if layout == 'closing':
            return pages + self.closing(di, pd, s)
        if not direct.fits_layout(layout, op, self.outline):
            layout = direct.content_layout(op, self.outline, 'pptx')
        texts, visuals = section_items(self.spec, s)
        texts = [i for i in texts if i.role != 'break']
        first = not pages
        pb, left_texts, left_vis = self.fit_slide(layout, di, pd, s, texts, visuals)
        self.title_bar(pb)
        pages.append(self.finish_page(pb, first))
        pages += self.more_slides(di, s, left_texts, left_vis)
        return pages

    def fit_slide(self, layout: str, di: int, pd: PageDirection, s: int, texts: list[Item], visuals: list):
        """Fill a slide, then when text is left over: the compact variant first; a small remainder (a line or two)
        goes to the speaker notes instead of a near-empty continued slide; a real remainder is split evenly so the
        continued slide carries a fair share."""
        pb = self.pb(layout, s, di, pd.variant)
        left, vis = self.fill_slide(pb, pd, s, texts, visuals)
        if left and pd.variant != 'compact':
            pb2 = self.pb(layout, s, di, 'compact')
            left2, vis2 = self.fill_slide(pb2, replace(pd, variant='compact'), s, texts, visuals)
            if _words(left2) < _words(left):
                pb, left, vis = pb2, left2, vis2
        if not left:
            return pb, left, vis
        if _words(left) <= SMALL_REST_WORDS:
            pb.notes += [_plain_text(i.text) for i in left]
            self.flag('notes')
            return pb, [], vis
        placed = [i for i in texts if i.role != 'break' and i.ref not in {x.ref for x in left}
                  and not any(x.base == i.ref for x in left)]
        if len(placed) >= 2 and _words(left) * 2 < _words(texts):
            total = _words(texts)
            k, acc = 0, 0
            for k, it in enumerate(texts):
                if acc + it.words > total / 2 and k:
                    break
                acc += it.words
            k = max(1, min(k, len(placed)))
            pb3 = self.pb(pb.layout, s, di, pb.page.variant)
            left3, vis3 = self.fill_slide(pb3, replace(pd, variant=pb.page.variant), s, texts[:k], visuals)
            if not left3:
                return pb3, texts[k:], vis3
        return pb, left, vis

    def fill_slide(self, pb: _PB, pd: PageDirection, s: int, texts: list[Item], visuals: list):
        """Fill one slide's slots from a section; returns (text items left, visuals left)."""
        L = pb.layout
        focus = _focus(pd)
        head = self.heading(s, pb.page.continued)
        vis = list(visuals)
        left: list[Item] = []

        def take_visual(kinds, prefer=None):
            for k, v in enumerate(vis):
                if prefer is not None and v[0] != prefer:
                    continue
                if v[1].get('type') in kinds:
                    return vis.pop(k)
            if prefer is not None:
                return take_visual(kinds)
            return None

        from ..create.spec import DIAGRAMS
        diagram_kinds = tuple(DIAGRAMS) + tuple(k for k in _new_kinds() if k not in DIAGRAMS)
        if L == 'title-bullets':
            pb.text('title', [head], force=True, valign='bottom')
            left = pb.text('body', texts)
        elif L in ('image-left-text', 'image-right-text'):
            img = self._image_index(pd.image, s)
            if img is not None:
                vis = [v for v in vis if v[0] != img[1]]
                box = self.image_box(pb, 'image', img[0], img[1], pb.rect('image'), fit='cover', bleed=True)
                blk = self.spec['sections'][img[0]]['blocks'][img[1]]
            else:
                box, blk = None, {}
            pb.text('title', [head], force=True, valign='bottom')
            left = pb.text('body', texts)
            below = []
            if blk.get('caption'):
                below.append(Item(f'spec:{img[0]}/{img[1]}#caption', str(blk['caption']), 'caption', *img))
            if blk.get('credit'):
                below.append(Item(f'spec:{img[0]}/{img[1]}#credit', str(blk['credit']), 'credit', *img))
            rest = pb.text('caption', below, budget=False)
            pb.notes += [_plain_text(i.text) for i in rest]
            if box is None and img is not None:
                pass
        elif L == 'full-bleed-image-caption':
            img = self._image_index(pd.image, s)
            if img is not None:
                vis = [v for v in vis if v[0] != img[1]]
                self.image_box(pb, 'image', img[0], img[1], pb.rect('image'), fit='cover', bleed=True)
                pb.on_photo = True
                blk = self.spec['sections'][img[0]]['blocks'][img[1]]
                self.overlay_band(pb)
                pb.text('caption', [head], bold=True, valign='bottom', force=True)
                if blk.get('credit'):
                    rest = pb.text('credit', [Item(f'spec:{img[0]}/{img[1]}#credit', str(blk['credit']), 'credit',
                                                   *img)], budget=False)
                    pb.notes += [_plain_text(i.text) for i in rest]
                if blk.get('caption'):
                    pb.notes.append(_plain_text(str(blk['caption'])))
            else:
                pb.text('caption', [head], force=True)
            left = texts
        elif L == 'big-number':
            stats = stat_items(self.spec, s, focus)
            pb.text('title', [head], force=True)
            if stats:
                it = stats[0]
                pb.text('number', [Item(it.base + '#stat', ref_text(it.base + '#stat', self.spec), 'number', it.s,
                                        it.b)], color='accent', bold=True, force=True, valign='bottom')
                lab = ref_text(it.base + '#rest', self.spec)
                if lab:
                    rest = pb.text('label', [Item(it.base + '#rest', lab, 'label', it.s, it.b)], color='text')
                    pb.notes += [_plain_text(i.text) for i in rest]
                others = [i for i in texts if i.ref != it.ref]
                vis = [v for v in vis if v[1].get('type') != 'stat-cards' or v[0] != it.b]
            else:
                others = texts
            left = pb.text('context', others)
        elif L == 'stat-cards':
            stats = stat_items(self.spec, s, focus)[:4]
            pb.text('title', [head], force=True, valign='bottom')
            if len(stats) >= 1:
                self.cards(pb, 'cards', stats, pb.rect('cards'))
                used = {i.ref for i in stats}
                used_blocks = {i.b for i in stats if i.role == 'stat'}
                vis = [v for v in vis if v[0] not in used_blocks]
                others = [i for i in texts if i.ref not in used]
            else:
                others = texts
            left = pb.text('note', others)
        elif L == 'quote':
            q = next((i for i in texts if i.role == 'quote' and (focus is None or i.b == focus)), None) or \
                next((i for i in texts if i.role == 'quote'), None)
            x, y, w, h = grid_rect((1, 0, 10, 1), self.size, self.ds, fmt=self.fmt)
            pb.text('title', [head], rect=(x, y, w, h), step='h3', color='muted', force=True)
            for bx in pb.page.boxes:
                if bx.content.startswith(f'spec:{s}/heading'):
                    bx.slot = None
            self.icon(pb, 'mark', 'quote', 'Quotation mark')
            if q is not None:
                pb.text('quote', [q], italic=True, bold=False, color='heading', force=True)
                by = next((i for i in texts if i.role == 'by' and i.b == q.b), None)
                if by is not None:
                    pb.text('attribution', [by], color='muted', force=True)
                left = [i for i in texts if i.b != q.b]
            else:
                left = texts
        elif L == 'two-column':
            pb.text('title', [head], force=True, valign='bottom')
            v = vis.pop(0) if vis else None
            if v is not None and texts:
                trial = self.pb(pb.layout, s, pb.page.direction, pb.page.variant)
                if self.figure(trial, 'right', s, v[0], v[1], trial.rect('right')):
                    vis.insert(0, v)       # a table too long for half the slide gets a full-width slide of its own
                    v = None
            if v is not None:
                left = pb.text('left', texts)
                extra = self.figure(pb, 'right', s, v[0], v[1], pb.rect('right'))
                if extra:
                    vis.insert(0, (v[0], v[1], extra[3]))
            else:
                a, b = _halves(texts)
                left = pb.text('left', a) + pb.text('right', b)
        elif L == 'comparison':
            par = parallel_lists(self.spec, s)
            pb.text('title', [head], force=True, valign='bottom')
            if par:
                (ha, la), (hb, lb) = par
                pb.text('left_head', [ha], color='heading', bold=True, force=True)
                pb.text('right_head', [hb], color='heading', bold=True, force=True)
                used = {i.ref for i in [ha, hb] + la + lb}
                left = pb.text('left', la) + pb.text('right', lb) + [i for i in texts if i.ref not in used]
            else:
                a, b = _halves(texts)
                left = pb.text('left', a) + pb.text('right', b)
        elif L in ('full-width-diagram', 'chart-focus', 'timeline-strip'):
            pb.text('title', [head], force=True, valign='bottom')
            if L == 'chart-focus':
                v = take_visual(('chart',), focus) or take_visual(('table',), focus) or \
                    (vis.pop(0) if vis else None)
                slot = 'chart'
            elif L == 'timeline-strip':
                v = take_visual(('timeline', 'process', 'cycle', 'flow'), focus) or take_visual(diagram_kinds, focus) \
                    or (vis.pop(0) if vis else None)
                slot = 'timeline'
            else:
                v = (take_visual(diagram_kinds + ('table', 'chart'), focus)
                     or (vis.pop(0) if vis else None))
                slot = 'figure'
            summary_items = []
            if v is not None:
                b, blk = v[0], v[1]
                start = v[2] if len(v) > 2 else 0
                if blk.get('type') == 'table' and start:
                    extra = self.table_box(pb, slot, s, b, blk, pb.rect(slot), start)
                else:
                    on_side = L == 'chart-focus' and blk.get('type') == 'chart'
                    extra = self.figure(pb, slot, s, b, blk, pb.rect(slot), summary=not on_side)
                    if on_side:
                        summary_items.append(Item(f'spec:{s}/{b}#summary', ref_text(f'spec:{s}/{b}#summary',
                                                                                     self.spec), 'summary', s, b))
                if extra:
                    vis.insert(0, (b, blk, extra[3]))
            other = 'takeaway' if L == 'chart-focus' else ('body' if L == 'timeline-strip' else 'caption')
            left = pb.text(other, summary_items + texts, color='text' if L == 'chart-focus' else None)
        else:   # cover/closing layouts handled elsewhere; anything unknown reads as title and bullets
            pb.text('title', [head], force=True)
            left = pb.text('body', texts)
        return left, vis

    def overlay_band(self, pb: _PB):
        """The overlay behind caption text on a photo: the slot's band, grown to cover every overlay text slot."""
        x, y, w, h = pb.rect('overlay')
        tops = [pb.rect(n)[1] for n in ('caption', 'credit') if n in pb.areas]
        if tops:
            top = max(0.0, min(tops) - self.ds.spacing.unit * 2)
            if top < y:
                h, y = h + (y - top), top
        pb.shape('overlay', fill='overlay', opacity=self.ds.overlay_alpha, rect=(x, y, w, h))

    def accent(self, pb: _PB, slot: str):
        shape = self.ds.shape.accent_shape
        if shape == 'none' or slot not in pb.areas:
            return
        x, y, w, h = pb.rect(slot)
        if shape == 'circle':
            d = min(w, h)
            pb.shape(slot, fill='accent', rect=(x, y, d, d), radius=d / 2)
        elif shape == 'underline':
            pb.shape(slot, fill='accent', rect=(x, y, w, 4.0))
        else:
            pb.shape(slot, fill='accent', rect=(x, y, w, 8.0), radius=4.0 if shape == 'blob' else 0.0)

    def icon(self, pb: _PB, slot: str, name: str, alt: str):
        try:
            from . import icons
            if icons.get(name) is None:
                raise LookupError(name)
            self.use_asset(f'icon:{name}', 'icon', 'lucide', 'ISC')
        except Exception:
            pass
        x, y, w, h = pb.rect(slot)
        d = min(w, h)
        pb.add(Box(id='', kind='icon', x=x, y=y, w=round(d, 2), h=round(d, 2), slot=slot, content=f'icon:{name}',
                   alt=alt, style=BoxStyle(stroke='accent', stroke_w=self.ds.shape.stroke * 2)))

    def more_slides(self, di: int, s: int, texts: list[Item], visuals: list) -> list[PagePlan]:
        """Continued slides: leftover text on title-and-bullets slides, then one slide per leftover visual."""
        pages = []
        guard = 0
        while texts and guard < MAX_PAGES:
            guard += 1
            pb = self.pb('title-bullets', s, di, continued=True)
            pb.text('title', [self.heading(s, True)], force=True, valign='bottom')
            texts = pb.text('body', texts, budget=False, force=True)
            self.title_bar(pb)
            self.flag('split')
            pages.append(self.finish_page(pb, False))
        for v in visuals:
            b, blk = v[0], v[1]
            start = v[2] if len(v) > 2 else 0
            t = blk.get('type')
            while True and guard < MAX_PAGES:
                guard += 1
                if t == 'image':
                    pb = self.pb('full-bleed-image-caption', s, di, continued=True)
                    pd = PageDirection(layout='full-bleed-image-caption', image=self.images.index((s, b))
                                       if (s, b) in self.images else None)
                    self.fill_slide(pb, pd, s, [], [])
                    pages.append(self.finish_page(pb, False))
                    break
                layout = 'chart-focus' if t == 'chart' else 'timeline-strip' if t in ('timeline', 'process') \
                    else 'full-width-diagram'
                pb = self.pb(layout, s, di, continued=True)
                _, extra = self.fill_slide(pb, PageDirection(layout=layout), s, [], [(b, blk, start)] if start
                                           else [(b, blk)])
                self.title_bar(pb)
                pages.append(self.finish_page(pb, False))
                self.flag('split')
                if not extra:
                    break
                start = extra[0][2] if len(extra[0]) > 2 else start
                if start <= 0:
                    break
        return pages

    # ----- print pages -----
    def close_flow(self):
        self.flow_pb, self.flow_slots, self.flow_section = None, [], None

    def flow_open(self, pb: _PB | None, s: int | None, slots):
        self.flow_pb = pb
        self.flow_section = s
        self.flow_slots = [[n, pb.rect(n), 0.0, None] for n in slots] if pb is not None else []

    def flow_room(self) -> float:
        return sum(r[3] - used for _, r, used, _ in self.flow_slots) if self.flow_slots else 0.0

    def flow_new_page(self, di: int, s: int | None) -> _PB:
        pb = self.pb('two-column-text', s, di, continued=True)
        if s is not None:
            pb.text('heading', [self.heading(s, True)], force=True, valign='bottom')
        self.pdf_pages.append(pb)
        self.flow_open(pb, s, ('col1', 'col2'))
        self.flag('split')
        return pb

    def flow_place(self, di: int, s: int | None, items: list[Item]):
        """Pour text items into the open flow slots in order, opening continuation pages as needed."""
        guard = 0
        queue = list(items)
        size = self.ds.size('body', self.fmt)
        lh = line_height_for(self.ds, 'body')
        while queue and guard < 20 * MAX_PAGES:
            guard += 1
            it = queue[0]
            if it.role == 'break':
                queue.pop(0)
                self.flow_slots = []
                if queue:
                    self.flow_new_page(di, s)
                continue
            if not self.flow_slots:
                self.flow_new_page(di, s)
            slot, rect, used, prev = self.flow_slots[0]
            pb = self.flow_pb
            x, y, w, h = rect
            step = 'caption' if slot == 'refs' else ('h2' if it.role == 'heading' and (
                self.spec['sections'][it.s].get('level', 1) == 1) else 'h3' if it.role == 'heading' else 'body')
            if it.role == 'code':
                step = 'caption'
            bsize, blh, ms = pb._measure_items([it], step, w)
            m = ms[0]
            gap = pb._gap(prev, it, bsize) if used else 0.0
            if it.role == 'heading':
                gap = (bsize * 0.9) if used else 0.0
            need = m['h'] + (2 * size * lh if it.role == 'heading' else 0)
            room = h - used - gap
            if need <= room + 0.5:
                pb.text(slot, [it], rect=(x, y + used + gap, w, m['h']), step=step, budget=False, shrink=False,
                        force=True, color='heading' if it.role == 'heading' else None,
                        bold=True if it.role == 'heading' else None)
                self.flow_slots[0][2] = used + gap + m['h']
                self.flow_slots[0][3] = it
                queue.pop(0)
                continue
            n = int((room + 0.5) // (bsize * blh))
            total = len(m['lines'])
            done = False
            if it.role in SPLITTABLE and n >= 1 and total - n >= 1 and (not used or (n >= 2 and total - n >= 2)):
                k = m['starts'][n - 1]
                if 0 < k < it.words:
                    head, tail = it.split(k)
                    pb.text(slot, [head], rect=(x, y + used + gap, w, n * bsize * blh), step=step, budget=False,
                            shrink=False, force=True)
                    queue[0] = tail
                    done = True
            if not done and not used:
                # it can't be split and doesn't fit an empty slot: place it whole rather than loop
                pb.text(slot, [it], rect=(x, y, w, h), step=step, budget=False, shrink=False, force=True)
                queue.pop(0)
            self.flow_slots.pop(0)

    def pdf_section(self, di: int, pd: PageDirection, s: int) -> list[PagePlan]:
        from . import direct
        op = self.outline.pages[di]
        layout = pd.layout if pd.layout in library.PAGE_TEMPLATES else 'two-column-text'
        if layout == 'cover' or not direct.fits_layout(layout, op, self.outline):
            layout = direct.content_layout(op, self.outline, 'pdf', avoid=('cover',))
        texts, visuals = section_items(self.spec, s)
        focus = _focus(pd)
        head = self.heading(s)
        start = len(self.pdf_pages)
        vis = list(visuals)
        if layout == 'two-column-text' and self.flow_slots and self.flow_room() > 6 * self.ds.size('body', 'pdf') * 1.3:
            self.flow_section = s
            self.flow_place(di, s, [head] + texts)
        else:
            self.close_flow()
            pb = self.pb(layout, s, di, pd.variant)
            self.pdf_pages.append(pb)
            flow_items = texts
            if layout == 'chapter-opener':
                num = self.number.get(s)
                if num is not None:
                    pb.text('number', [Item(f'text:{num:02d}', f'{num:02d}', 'number')], color='accent', bold=True,
                            force=True)
                pb.text('title', [head], force=True, valign='bottom')
                if texts and texts[0].role == 'para' and texts[0].words <= 60:
                    rest = pb.text('intro', [texts[0]], color='text')
                    flow_items = rest + texts[1:]
                self.flow_open(pb, s, ('body',))
            elif layout in ('text-side-figure', 'full-figure'):
                pb.text('heading', [head], force=True, valign='bottom')
                v = None
                for k, (b, blk) in enumerate(vis):
                    if focus is None or b == focus:
                        v = vis.pop(k)
                        break
                if v is None and vis:
                    v = vis.pop(0)
                if v is not None:
                    b, blk = v
                    cap = []
                    fig_rect = pb.rect('figure')
                    if layout == 'text-side-figure':
                        cr = pb.rect('caption')
                        fig_rect = (fig_rect[0], fig_rect[1], fig_rect[2], cr[1] + cr[3] - fig_rect[1])
                    else:
                        cr = pb.rect('caption')
                        fig_rect = (fig_rect[0], fig_rect[1], fig_rect[2], cr[1] + cr[3] - fig_rect[1])
                    extra = self.figure(pb, 'figure', s, b, blk, fig_rect)
                    if extra:
                        vis.insert(0, (b, blk, extra[3]))
                    elif layout == 'full-figure':
                        vis = self.stack_figures(pb, s, vis, fig_rect)
                if layout == 'text-side-figure':
                    self.flow_open(pb, s, ('body',))
                else:
                    self.flow_slots = []
            elif layout == 'two-column-text':
                pb.text('heading', [head], force=True, valign='bottom')
                self.flow_open(pb, s, ('col1', 'col2'))
            elif layout == 'pull-quote':
                q = next((i for i in texts if i.role == 'quote'), None)
                if q is not None:
                    pb.text('quote', [q], italic=True, bold=False, color='heading', force=True)
                    flow_items = [i for i in texts if i.b != q.b or i.role not in ('quote', 'by')]
                    by = next((i for i in texts if i.role == 'by' and i.b == q.b), None)
                    if by is not None:
                        flow_items = [i for i in flow_items if i.ref != by.ref]
                flow_items = [head] + flow_items
                self.flow_open(pb, s, ('body', 'body2'))
            elif layout == 'key-points':
                pb.text('heading', [head], force=True, valign='bottom')
                pts = [i for i in texts if i.role == 'bullet']
                first_b = pts[0].b if pts else None
                pts = [i for i in pts if i.b == first_b]
                x, y, w, h = pb.rect('box')
                pb.shape('box', fill='surface', radius=self.ds.shape.radius)
                pad = self.ds.spacing.unit * 2
                rest = pb.text('points', pts, rect=(x + pad, y + pad, w - 2 * pad, h - 2 * pad), color='text')
                used = {i.ref for i in pts}
                flow_items = rest + [i for i in texts if i.ref not in used]
                self.flow_open(pb, s, ('body',))
            elif layout == 'references':
                pb.text('heading', [head], force=True, valign='bottom')
                self.flow_open(pb, s, ('refs',))
            self.flow_place(di, s, flow_items)
        for v in vis:
            b, blk = v[0], v[1]
            row = v[2] if len(v) > 2 else 0
            guard = 0
            while guard < MAX_PAGES:
                guard += 1
                self.close_flow()
                pb = self.pb('full-figure', s, di, continued=True)
                self.pdf_pages.append(pb)
                pb.text('heading', [self.heading(s, True)], force=True, valign='bottom')
                fr, cr = pb.rect('figure'), pb.rect('caption')
                rect = (fr[0], fr[1], fr[2], cr[1] + cr[3] - fr[1])
                if blk.get('type') == 'table' and row:
                    extra = self.table_box(pb, 'figure', s, b, blk, rect, row)
                else:
                    extra = self.figure(pb, 'figure', s, b, blk, rect)
                if not extra:
                    break
                row = extra[3]
        return [p.page for p in self.pdf_pages[start:]]

    def stack_figures(self, pb: _PB, s: int, vis: list, rect) -> list:
        """Print: after a page's figure, the section's next tables, charts and diagrams go under it on the same page
        while each fits whole at its natural size, so a short table is not a page with a strip of content at the top
        and the chart after it is not a page of its own. Returns the visuals left for pages of their own."""
        x, y, w, h = rect
        bottom = y + h
        gap = self.ds.spacing.unit * 3
        vis = list(vis)
        while vis:
            v = vis[0]
            b, blk = v[0], v[1]
            if len(v) > 2 or blk.get('type') in ('image', 'stat-cards'):
                break
            used = [bx.y + bx.h for bx in pb.page.boxes if bx.slot == 'figure']
            top = (max(used) if used else y) + gap
            if top >= bottom:
                break
            trial = self.pb(pb.layout, s, pb.page.direction, pb.page.variant, pb.page.continued)
            if self.figure(trial, 'figure', s, b, blk, (x, top, w, self.size[1] * 4)):
                break                                     # a table longer than any page: it gets pages of its own
            need = max((bx.y + bx.h for bx in trial.page.boxes), default=top) - top
            if need > bottom - top + 0.5:
                break
            self.figure(pb, 'figure', s, b, blk, (x, top, w, bottom - top))
            vis.pop(0)
        return vis

    # ----- the whole file -----
    def run(self) -> list[PagePlan]:
        pages: list[PagePlan] = []
        self.pdf_pages: list[_PB] = []
        for di in range(len(self.direction.pages)):
            if self.slide:
                pages += self.direction_pages(di)
            else:
                if di == 0:
                    cover = self.direction_pages(0)
                    pages += cover
                    self.close_flow()
                    continue
                self.direction_pages(di)
            if len(pages) + len(self.pdf_pages) > MAX_PAGES:
                self.plan_notes.append('The document was too long to lay out in full; the rest was cut.')
                break
        if not self.slide:
            pages += [self.finish_page(pb, pb.page.section is not None and not pb.page.continued)
                      for pb in self.pdf_pages]
        return self.finish(pages)

    def finish(self, pages: list[PagePlan]) -> list[PagePlan]:
        for i, p in enumerate(pages):
            p.index = i
            if not self.slide:
                self.footer(p, i)
            seen: dict[str, int] = {}
            for bx in p.boxes:
                bx.z = paint_order(bx)
                name = bx.slot or bx.kind
                n = seen.get(name, 0)
                seen[name] = n + 1
                bx.id = f'p{i}.{name}' + (f'.{n}' if n else '')
        return pages

    def footer(self, p: PagePlan, i: int):
        size = self.ds.size('caption', self.fmt)
        lh = line_height_for(self.ds, 'caption')
        _, _, margin, _, _ = _grid(self.ds, self.fmt)
        y = round(p.h - margin / 2 - size * lh / 2, 2)
        left = grid_rect((0, 0, 8, 1), self.size, self.ds, fmt=self.fmt)
        right = grid_rect((8, 0, 4, 1), self.size, self.ds, fmt=self.fmt)
        fam = family_for(self.ds, 'caption')
        label = f'Page {i + 1}'
        color = 'muted'
        for ref, text, (x, _, w, _), align in (('spec:title#footer', ref_text('spec:title#footer', self.spec), left,
                                                'left'), (f'text:{label}', label, right, 'right')):
            if not text:
                continue
            lines = [plain_line(ln) for ln in wrap_runs(text, fam, size, w, fmt=self.fmt)][:1]
            p.boxes.append(Box(id='', kind='text', x=x, y=y, w=w, h=round(size * lh, 2), slot=None, content=ref,
                               style=BoxStyle(text_color=color), font=fam, step='caption', size=size,
                               line_height=lh, align=align, lines=lines))

    def font_uses(self, pages: list[PagePlan]) -> list[FontUse]:
        roles = {}
        for p in pages:
            for bx in p.boxes:
                if bx.kind in ('text', 'table') and bx.font:
                    role = {'display': 'display', 'h1': 'heading', 'h2': 'heading', 'h3': 'heading',
                            'caption': 'caption'}.get(bx.step or '', 'body')
                    if bx.font == self.ds.families.mono:
                        role = 'mono'
                    roles.setdefault((bx.font, role), None)
        out, noted = [], set()
        for family, role in roles:
            out.append(self._font_use(family, role, noted))
        return out

    def _font_use(self, family: str, role: str, noted: set) -> FontUse:
        res = None
        try:
            res = _fonts().resolve_local(family)
        except Exception:
            res = None
        if res is not None and (self.fmt != 'pdf' or pdf_face_ok(family)):
            note = res.note if res.note and family not in noted else None
            noted.add(family)
            return FontUse(res.family or family, role, res.source, res.licence,
                           bool(self.fmt == 'pdf' and res.embeddable), fallback=res.office_fallback,
                           requested=res.requested if res.requested and res.requested != res.family else None,
                           note=note)
        if self.fmt == 'pdf':
            used = std_face(family)
            note = None
            if family not in noted and std_family(family).lower() not in family.lower():
                note = f"{family} isn't available here, so the PDF uses {std_family(family)}."
                noted.add(family)
            return FontUse(used, role, 'builtin', 'builtin', False, requested=family, note=note)
        office = {'Times': 'Georgia', 'Courier': 'Consolas'}.get(std_family(family), 'Calibri')
        return FontUse(family, role, 'builtin', 'builtin', False, fallback=office)

    def notes_sentences(self) -> list[str]:
        out = list(self.plan_notes)
        if 'notes' in self.flags:
            out.append('Some text was moved to the speaker notes to keep slides readable.')
        if 'split' in self.flags:
            out.append('Some content continues on the next ' + ('slide.' if self.slide else 'page.'))
        if 'overflow' in self.flags:
            out.append('Some text is larger than its space even at the smallest size.')
        return list(dict.fromkeys(out))


def paint_order(bx: Box) -> int:
    """z by role: full-bleed photos and art at the back, then overlays, shapes, figures, and text on top (boxes keep
    their reading order within a level)."""
    if bx.kind == 'image' and bx.bleed or bx.kind == 'shape' and bx.content.startswith('art:'):
        return 0
    if bx.kind == 'shape' and bx.slot == 'overlay':
        return 1
    if bx.kind == 'shape':
        return 2
    if bx.kind in ('image', 'chart', 'table', 'diagram', 'icon'):
        return 3
    return 4


def _words(items: list[Item]) -> int:
    return sum(i.words for i in items)


def _new_kinds() -> tuple[str, ...]:
    from .plan import DIAGRAM_KINDS_NEW
    return tuple(k for k in DIAGRAM_KINDS_NEW if k != 'stat-cards')


def _diagram_aspect(blk: dict) -> float | None:
    """height / width of a diagram as create/diagram lays it out; None when it can't be drawn as a diagram."""
    try:
        from ..create import diagram
        lay = diagram.layout(blk, 480.0)
        if lay.width <= 0 or lay.height <= 0:
            return None
        return lay.height / lay.width
    except Exception:
        return None


_CONT_SUFFIX = re.compile(r'\s*\((?:cont\.?|continued)\)\s*$', re.I)


def _bare_title(text) -> str:
    """A heading or title compared loosely: no "(cont.)" suffix, spacing, case or trailing punctuation."""
    t = _CONT_SUFFIX.sub('', _plain_text(str(text or '')))
    return re.sub(r'\s+', ' ', t).strip().rstrip('.:').casefold()


def _focus(pd: PageDirection) -> int | None:
    m = re.match(r'^block:(\d+)$', str(pd.focus or ''))
    return int(m.group(1)) if m else None


def _halves(texts: list[Item]) -> tuple[list[Item], list[Item]]:
    """Text items split into two columns at a block boundary as close to half the words as possible."""
    if len(texts) < 2:
        return texts, []
    total = sum(i.words for i in texts)
    best, best_d = 1, None
    for k in range(1, len(texts)):
        if texts[k].role == 'by':
            continue
        d = abs(sum(i.words for i in texts[:k]) * 2 - total)
        if best_d is None or d < best_d:
            best, best_d = k, d
    return texts[:best], texts[best:]


def parallel_lists(spec: dict, s: int):
    """((head A, items A), (head B, items B)) when a section holds two bullet lists that each have a heading: a short
    paragraph right before the list, or a first item ending with ':'. None otherwise."""
    blocks = spec['sections'][s].get('blocks') or []
    lists = [b for b, blk in enumerate(blocks) if blk.get('type') == 'bullets' and blk.get('items')]
    if len(lists) != 2:
        return None
    out = []
    for b in lists:
        blk = blocks[b]
        items = [Item(f'spec:{s}/{b}/items[{i}:{i + 1}]', str(x), 'bullet', s, b, ordered=bool(blk.get('ordered')))
                 for i, x in enumerate(blk['items'])]
        prev = blocks[b - 1] if b > 0 else None
        if prev is not None and prev.get('type') == 'paragraph' and 0 < len(str(prev.get('text', '')).split()) <= 8:
            out.append((Item(f'spec:{s}/{b - 1}', str(prev['text']), 'heading', s, b - 1), items))
        elif len(items) >= 2 and _plain_text(items[0].text).rstrip().endswith(':') and items[0].words <= 6:
            out.append((replace(items[0], role='heading'), items[1:]))
        else:
            return None
    return tuple(out)


# ---------- public entry points ----------


def lay_out(spec: dict, fmt: str, ds: DesignSystem, direction: ArtDirection, ws: Workspace, *,
            file_id: str, paper: str | None = None) -> DesignPlan:
    """The DesignPlan for a normalized spec: every page's boxes positioned, sized, measured and fitted; figures cropped
    via studio/assets into the workspace; decorative art via studio/art. fonts/assets lists filled. No I/O but `ws`."""
    from .presets import PRESETS
    preset = ds.id if ds.id in PRESETS else 'custom'
    if fmt not in PAINTED:
        return DesignPlan(file_id=file_id, format=fmt, preset=preset, system=ds.to_dict(),
                          direction=direction.to_dict(), pages=[], flow=flow_styles(spec, fmt, ds))
    size = page_size(fmt, paper or spec.get('paper'))
    d = _Designer(spec, fmt, ds, direction, ws, size, file_id)
    pages = d.run()
    plan = DesignPlan(file_id=file_id, format=fmt, preset=preset, system=ds.to_dict(),
                      direction=d.direction.to_dict(), pages=pages, fonts=d.font_uses(pages),
                      assets=list(d.assets.values()), notes=d.notes_sentences())
    _log(ws, {'phase': 'layout', 'pages': len(pages), 'layouts': [p.layout for p in pages]})
    return plan


def _log(ws, event: dict):
    try:
        ws.log(event)
    except Exception:
        pass


def lay_out_page(direction_index: int, spec: dict, fmt: str, ds: DesignSystem, direction: ArtDirection,
                 ws: Workspace, size: tuple[float, float]) -> list[PagePlan]:
    """One direction entry -> one page, or several when split."""
    d = _Designer(spec, fmt, ds, direction, ws, size)
    d.pdf_pages = []
    pages = d.direction_pages(direction_index)
    if not d.slide and direction_index != 0:
        pages = [d.finish_page(pb, pb.page.section is not None and not pb.page.continued) for pb in d.pdf_pages]
    return d.finish(pages)


def _ds_of(plan: DesignPlan, ds: DesignSystem | None) -> DesignSystem:
    return ds if ds is not None else DesignSystem.from_dict(plan.system)


def refit(plan: DesignPlan, spec: dict, page: int, action: str, ds: DesignSystem, ws: Workspace, *,
          box: str | None = None, arg=None) -> bool:
    """Apply one FIX_ACTIONS code fix to a page in place; True when the plan changed. swap_layout uses
    library.next_best; split inserts pages and renumbers indexes."""
    if action not in FIX_ACTIONS or not (0 <= page < len(plan.pages)):
        return False
    ds = _ds_of(plan, ds)
    p = plan.pages[page]
    fmt = plan.format
    if action == 'shrink':
        return _shrink(p, spec, ds, fmt, box)
    if action == 'snap':
        return _snap(p, ds, fmt)
    if action == 'recrop':
        return _recrop(p, ds, fmt, ws, box, arg)
    if action == 'rebalance':
        return _rebalance(p, spec, ds, fmt, box)
    if action == 'split':
        return _split(plan, page, spec, ds, fmt, box)
    if action == 'enlarge':
        return _enlarge(p, spec, ds, fmt, box)
    direction = ArtDirection.from_dict(plan.direction)
    di = p.direction
    if di is None or not (0 <= di < len(direction.pages)):
        return False
    pd = direction.pages[di]
    if action == 'compact':
        if pd.variant == 'compact':
            return False
        direction.pages[di] = replace(pd, variant='compact')
    else:   # swap_layout
        from . import direct
        outline = direct.outline_of(spec, fmt)
        op = outline.pages[di] if di < len(outline.pages) else None
        target = arg if isinstance(arg, str) and arg in library.for_format(fmt) else None
        if target is None:
            avoid = {pd.layout}
            target = library.next_best(pd.layout, avoid) if pd.layout in library.for_format(fmt) else None
            while target is not None and op is not None and not direct.fits_layout(target, op, outline):
                avoid.add(target)
                target = library.next_best(pd.layout, avoid)
        if target is None or target == pd.layout:
            return False
        direction.pages[di] = replace(pd, layout=target)
    _replace_direction(plan, spec, ds, ws, direction, di)
    return True


def _replace_direction(plan: DesignPlan, spec: dict, ds: DesignSystem, ws, direction: ArtDirection, di: int):
    if plan.format == 'pdf':   # print pages share flowing text between sections: lay the whole file out again
        new = lay_out(spec, plan.format, ds, direction, ws, file_id=plan.file_id)
        plan.pages, plan.direction = new.pages, new.direction
        return
    size = (plan.pages[0].w, plan.pages[0].h) if plan.pages else page_size(plan.format)
    new_pages = lay_out_page(di, spec, plan.format, ds, direction, ws, size)
    idx = [i for i, p in enumerate(plan.pages) if p.direction == di]
    at = idx[0] if idx else len(plan.pages)
    rest = [p for p in plan.pages if p.direction != di]
    plan.pages = rest[:at] + new_pages + rest[at:]
    plan.direction = direction.to_dict()
    _renumber(plan)


def _renumber(plan: DesignPlan):
    for i, p in enumerate(plan.pages):
        old = p.index
        p.index = i
        seen: dict[str, int] = {}
        for bx in p.boxes:
            if not bx.id:
                name = bx.slot or bx.kind
                n = seen.get(name, 0)
                seen[name] = n + 1
                bx.id = f'p{i}.{name}.n{n}'
                continue
            if bx.id.startswith(f'p{old}.'):
                bx.id = f'p{i}.' + bx.id[len(f'p{old}.'):]
            if bx.content.startswith('text:Page ') and plan.format == 'pdf':
                bx.content = f'text:Page {i + 1}'
                bx.lines = [f'Page {i + 1}']


def _text_boxes(p: PagePlan, box: str | None):
    return [b for b in p.boxes if b.kind == 'text' and (box is None or b.id == box)]


def _rewrap(bx: Box, spec: dict, ds: DesignSystem, fmt: str):
    text = box_text(bx, spec)
    indent = bullet_indent(bx.size, bool(bullet_of(bx, spec) and bullet_of(bx, spec) != BULLET)) \
        if bullet_of(bx, spec) else 0.0
    bx.lines = [plain_line(ln) for ln in wrap_runs(text, bx.font or family_for(ds, bx.step), bx.size,
                                                    max(8.0, bx.w - indent), bold=bx.bold, italic=bx.italic, fmt=fmt)]


def _shrink(p: PagePlan, spec: dict, ds: DesignSystem, fmt: str, box: str | None) -> bool:
    changed = False
    for bx in _text_boxes(p, box):
        if not bx.step or bx.content.startswith('text:Page'):
            continue
        need = len(bx.lines or []) * (bx.size or 0) * bx.line_height
        if box is None and need <= bx.h + 0.5:
            continue
        smaller = step_down(bx.step)
        size = ds.size(smaller, fmt)
        if smaller == bx.step or size >= (bx.size or 0):
            continue
        bx.step, bx.size = smaller, size
        _rewrap(bx, spec, ds, fmt)
        changed = True
    return changed


def _snap(p: PagePlan, ds: DesignSystem, fmt: str) -> bool:
    size = (p.w, p.h)
    edges = column_edges(size, ds, fmt)
    changed = False
    for bx in p.boxes:
        if bx.bleed or bx.kind != 'text':
            continue
        near = min(edges, key=lambda e: abs(e - bx.x))
        if 0.01 < abs(near - bx.x) <= 24:
            bx.w = round(bx.w - (near - bx.x), 2)
            bx.x = round(near, 2)
            changed = True
        ry, rh = round(bx.y * 2) / 2, round(bx.h * 2) / 2
        if (ry, rh) != (bx.y, bx.h):
            bx.y, bx.h = ry, max(rh, bx.h)
            changed = True
    return changed


def _recrop(p: PagePlan, ds: DesignSystem, fmt: str, ws, box: str | None, arg) -> bool:
    changed = False
    for bx in p.boxes:
        if bx.kind != 'image' or (box is not None and bx.id != box) or bx.fit != 'cover':
            continue
        sha = bx.content[6:] if bx.content.startswith('asset:') else None
        focal = tuple(arg) if isinstance(arg, (list, tuple)) and len(arg) == 2 else (bx.focal or (0.5, 0.42))
        if sha:
            px = image_px(sha)
            crop = crop_fractions(px, focal, bx.w, bx.h, 'cover')
        else:
            try:
                from . import assets as vis
                crop = tuple(vis.crop_for(image_px_of(ws, bx.content), focal, bx.w, bx.h, 'cover'))
            except Exception:
                continue
        if crop != bx.crop or focal != bx.focal:
            bx.crop, bx.focal = crop, focal
            changed = True
    return changed


def image_px_of(ws, ref: str) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(ws.get(ref))) as im:
            return im.size
    except Exception:
        return None


def _rebalance(p: PagePlan, spec: dict, ds: DesignSystem, fmt: str, box: str | None) -> bool:
    """Move the last text box of an overflowing (or the named) slot into the speaker notes and close the gap."""
    slots = {}
    for bx in _text_boxes(p, None):
        if bx.slot and bx.content.startswith('spec:') and '/heading' not in bx.content and bx.slot not in (
                'title', 'number'):
            slots.setdefault(bx.slot, []).append(bx)
    target = None
    if box is not None:
        tb = next((b for b in p.boxes if b.id == box), None)
        target = tb.slot if tb is not None else None
    for name, boxes in slots.items():
        if target is not None and name != target:
            continue
        if len(boxes) < 2 and target is None:
            continue
        last = boxes[-1]
        p.boxes.remove(last)
        p.notes = '\n\n'.join(x for x in (p.notes, _plain_text(box_text(last, spec))) if x)
        return True
    return False


def _split(plan: DesignPlan, page: int, spec: dict, ds: DesignSystem, fmt: str, box: str | None) -> bool:
    """Move the second half of the page's largest text slot onto a new continued page after it."""
    p = plan.pages[page]
    slots: dict[str, list[Box]] = {}
    for bx in _text_boxes(p, None):
        if bx.slot and '/heading' not in bx.content and bx.content.startswith('spec:') and bx.slot not in ('title',):
            slots.setdefault(bx.slot, []).append(bx)
    if box is not None:
        tb = next((b for b in p.boxes if b.id == box), None)
        slots = {k: v for k, v in slots.items() if tb is not None and k == tb.slot}
    cand = [(k, v) for k, v in slots.items() if len(v) >= 2]
    if not cand:
        return False
    name, boxes = max(cand, key=lambda kv: sum(len(b.lines or []) for b in kv[1]))
    moved = boxes[len(boxes) // 2:]
    for bx in moved:
        p.boxes.remove(bx)
    layout = 'title-bullets' if fmt == 'pptx' else 'two-column-text'
    new = PagePlan(index=-1, layout=layout, w=p.w, h=p.h, section=p.section, continued=True,
                   direction=p.direction)
    d = _Designer.__new__(_Designer)
    d.spec, d.fmt, d.ds, d.size, d.slide = spec, fmt, ds, (p.w, p.h), fmt == 'pptx'
    d.flags, d.plan_notes, d.assets = set(), [], {}
    pb = _PB(d, layout, p.section, p.direction, continued=True)
    pb.page = new
    if p.section is not None:
        pb.text('title' if fmt == 'pptx' else 'heading', [d.heading(p.section, True)], force=True, valign='bottom')
    items = [Item(b.content, box_text(b, spec), _role_of(b, spec), p.section) for b in moved]
    left = pb.text('body' if fmt == 'pptx' else 'col1', items, budget=False, force=True)
    if left and fmt != 'pptx':
        pb.text('col2', left, budget=False, force=True)
    plan.pages.insert(page + 1, new)
    _renumber(plan)
    return True


ENLARGE_MAX = 2.5     # the enlarge fix grows a figure to at most this many times its height


def _enlarge(p: PagePlan, spec: dict, ds: DesignSystem, fmt: str, box: str | None) -> bool:
    """A page that looks empty because its figure is small: grow each table, chart or diagram toward the bottom of
    its slot (the boxes of that slot below it move down with it). Tables get taller rows, charts a taller plot,
    diagrams scale up with their aspect kept. Adds nothing: the visual gets the room, never filler."""
    if p.freeform or p.layout not in library.for_format(fmt):
        return False
    try:
        areas = slot_areas(p.layout, p.variant or 'default')
    except Exception:
        return False
    changed = False
    for bx in p.boxes:
        if bx.kind not in ('table', 'chart', 'diagram') or bx.bleed or not bx.slot or bx.slot not in areas:
            continue
        if box is not None and bx.id != box:
            continue
        sx, sy, sw, sh = grid_rect(areas[bx.slot][0], (p.w, p.h), ds, fmt=fmt)
        below = [o for o in p.boxes if o is not bx and o.slot == bx.slot and o.y >= bx.y + bx.h - 0.5]
        tail = (max(o.y + o.h for o in below) - (bx.y + bx.h)) if below else 0.0
        room = sy + sh - tail - bx.y
        # anything else under the figure (another slot's text, a footer) caps the growth too
        others = [o.y for o in p.boxes if o is not bx and o not in below and not o.bleed and o.y >= bx.y + bx.h - 0.5
                  and o.x < bx.x + bx.w and o.x + o.w > bx.x]
        if others:
            room = min(room, min(others) - bx.y - tail)
        new_h = min(room, bx.h * ENLARGE_MAX)
        if new_h <= bx.h + 1.0:
            continue
        if bx.kind == 'diagram':
            blk = block_of(bx.content, spec)
            aspect = (_diagram_aspect(blk) if blk else None) or (bx.h / bx.w if bx.w else None)
            if not aspect:
                continue
            new_w = min(sw, new_h / aspect)
            new_h = new_w * aspect
            if new_h <= bx.h + 1.0:
                continue
            bx.x, bx.w = round(sx + (sw - new_w) / 2, 2), round(new_w, 2)
        delta = new_h - bx.h
        bx.h = round(new_h, 2)
        for o in below:
            o.y = round(o.y + delta, 2)
        changed = True
    return changed


def _role_of(bx: Box, spec: dict) -> str:
    if bullet_of(bx, spec):
        return 'bullet'
    blk = block_of(bx.content, spec)
    t = (blk or {}).get('type')
    return {'quote': 'quote', 'code': 'code'}.get(t, 'para')


def relayout(plan: DesignPlan, spec: dict, ds: DesignSystem, ws: Workspace, *,
             layouts: dict[int, str] | None = None) -> DesignPlan:
    """A restyle: the same direction (with per-page layout overrides) laid out again with another DesignSystem."""
    direction = ArtDirection.from_dict(plan.direction)
    allowed = library.for_format(plan.format)
    for page0, layout_id in (layouts or {}).items():
        if layout_id not in allowed:
            raise ValueError(f'unknown layout {layout_id!r}')
        di = None
        if 0 <= int(page0) < len(plan.pages):
            di = plan.pages[int(page0)].direction
        if di is None:
            di = int(page0)
        if 0 <= di < len(direction.pages):
            direction.pages[di] = replace(direction.pages[di], layout=layout_id)
    direction.source = 'restyle'
    paper = None
    if plan.format == 'pdf' and plan.pages:
        paper = next((k for k, v in PAPER.items() if abs(v[0] - plan.pages[0].w) < 1 and abs(v[1] - plan.pages[0].h) < 1),
                     None)
    return lay_out(spec, plan.format, ds, direction, ws, file_id=plan.file_id, paper=paper)


def flow_styles(spec: dict, fmt: str, ds: DesignSystem) -> dict:
    """docx/md/xlsx: the plan.FLOW_SCHEMA dict (paragraph styles per type step, figure placement, page breaks,
    chart palette). Phase 5."""
    styles = {}
    for st in TYPE_STEPS:
        size = ds.size(st, fmt)
        head = st in HEAD_STEPS
        styles[st] = {'family': family_for(ds, st), 'size': size,
                      'color': ds.color('heading' if head else 'muted' if st == 'caption' else 'text'),
                      'bold': head, 'italic': False, 'space_before': round(size * 0.8, 1) if head else 0.0,
                      'space_after': round(size * 0.5, 1), 'line_height': line_height_for(ds, st)}
    figures, breaks = [], []
    sections = spec.get('sections') or []
    hierarchical = any(sec.get('level', 1) > 1 for sec in sections)
    for s, sec in enumerate(sections):
        if s and hierarchical and sec.get('level', 1) == 1:
            breaks.append(s)
        for b, blk in enumerate(sec.get('blocks') or []):
            t = blk.get('type')
            if t in ('paragraph', 'bullets', 'quote', 'code', 'page_break', 'figure'):
                continue
            if t in ('table', 'chart'):
                placement = 'full'
            elif t == 'image':
                px = image_px(str(blk.get('asset') or ''))
                placement = 'side' if px and px[1] > px[0] * 1.2 else 'inline'
            else:
                placement = 'full'
            figures.append({'ref': f'spec:{s}/{b}', 'placement': placement})
    return {'styles': styles, 'figures': figures, 'breaks': breaks,
            'palette': [c.upper() for c in (ds.chart_palette or [ds.color('accent')])]}
