"""The freeform hatch (docs/PLAN-designer.md 3.8 step 3 and 9.9): at most STUDIO_FREEFORM_PAGES pages per file (a cover,
a poster panel, an infographic) drawn from a model-written shape program (plan.freeform_schema()): primitives in grid
units with token colour names. Validated (bounds, overlap, contrast, minimum sizes, only workspace/asset-cache refs and
bundled icons), text fitted with real metrics, painted natively. Fails QA twice -> the closest library layout.

Owner: builder Q.
"""
from __future__ import annotations

import io
import time

from .plan import TOKEN_COLORS, TYPE_STEPS, Box, BoxStyle, ShapeProgram, freeform_schema
from .plan import validate as schema_validate
from .tokens import DesignSystem
from .workspace import Workspace

SYSTEM = ('You design one page as a JSON shape program matching the schema: primitives on a {cols} x {rows} grid, '
          'colours as token names only, text short. Use only the listed image refs and icon names. No prose.')


MAX_LITERAL = 120      # a literal text shape becomes a 'text:' content ref, which holds at most 120 characters
_ROLE = {'display': 'display', 'h1': 'heading', 'h2': 'heading', 'h3': 'heading', 'lead': 'body', 'body': 'body',
         'caption': 'caption'}


def grid_of(fmt: str) -> tuple[int, int]:
    """(cols, rows) of the freeform grid: 12 x 12 on slides, 12 x 16 on print pages."""
    return (12, 12) if fmt == 'pptx' else (12, 16)


def _frame(size: tuple[float, float], ds, fmt: str, cols: int, rows: int) -> tuple[float, float, float]:
    """(margin, points per column, points per row)."""
    m = ds.spacing.slide_margin if fmt == 'pptx' else ds.spacing.page_margin
    return m, (size[0] - 2 * m) / cols, (size[1] - 2 * m) / rows


def _page_size(fmt: str) -> tuple[float, float]:
    from .layout import page_size
    return page_size(fmt)


async def compose(page: int, section: dict, ds: DesignSystem, engine, ws: Workspace, *, fmt: str,
                  refs: list[str] = (), budget_tokens: int = 2000,
                  feedback: list[str] = ()) -> tuple[ShapeProgram | None, dict]:
    """(program or None, usage {calls, llm_in, llm_out, ms}). None when the engine fails or the reply is invalid.
    feedback: problems with an earlier attempt, shown to the model on the retry. usage also carries 'problems' (why
    the reply was rejected) for the agent's record."""
    cols, rows = grid_of(fmt)
    usage = {'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': 0, 'problems': []}
    if engine is None:
        usage['problems'] = ['no engine']
        return None, usage
    t0 = time.monotonic()
    try:
        from ..engines.base import parse_json
        usage['calls'] = 1
        reply = await engine.stream(system=SYSTEM.format(cols=cols, rows=rows),
                                    prompt=_prompt(section, ds, fmt, refs, feedback, cols, rows), effort='low',
                                    max_tokens=int(budget_tokens), schema=freeform_schema(cols, rows))
        usage['llm_in'], usage['llm_out'] = int(reply.input_tokens or 0), int(reply.output_tokens or 0)
        program, problems = validate(parse_json(reply.text), ds, ws, page=page, fmt=fmt, refs=refs)
        usage['problems'] = problems
    except Exception as e:
        program = None
        usage['problems'] = [f'the engine failed ({getattr(e, "why", None) or type(e).__name__})']
    usage['ms'] = int((time.monotonic() - t0) * 1000)
    return program, usage


def _prompt(section: dict, ds, fmt: str, refs, feedback, cols: int, rows: int) -> str:
    from .thumbs import _block_text
    body = []
    for b in (section or {}).get('blocks') or []:
        t = _block_text(b).strip()
        if t:
            body.append(t)
    content = '\n'.join(body)[:1500]
    lines = [f'Page heading: {(section or {}).get("heading") or "(cover)"}', 'Content:', content or '(none)', '',
             f'Grid: {cols} columns x {rows} rows; x, y, w, h in grid units, multiples of 0.5.',
             'Colour tokens: ' + ', '.join(TOKEN_COLORS), 'Type steps: ' + ', '.join(TYPE_STEPS),
             f'Text shapes: literal text of at most {MAX_LITERAL} characters, colour in "fill", no text on top of '
             'other text, and text over a picture needs a filled shape between them.',
             'Image or diagram refs you may use: ' + (', '.join(refs) if refs else 'none')]
    try:
        from . import icons
        names = icons.names()
        if names:
            lines.append('Icons (ref "icon:<name>"): ' + ', '.join(names[:120]))
    except Exception:
        pass
    if feedback:
        lines += ['', 'Your last attempt was rejected: ' + '; '.join(list(feedback)[:8])]
    return '\n'.join(lines)


def _inside(s: dict, cols: int, rows: int) -> bool:
    return s['x'] + s['w'] <= cols + 1e-9 and s['y'] + s['h'] <= rows + 1e-9


def _overlap(a: dict, b: dict) -> float:
    w = min(a['x'] + a['w'], b['x'] + b['w']) - max(a['x'], b['x'])
    h = min(a['y'] + a['h'], b['y'] + b['h']) - max(a['y'], b['y'])
    return w * h if w > 0 and h > 0 else 0.0


def _blend(fg: str, bg: str, a: float) -> str:
    from .qa import _blend as b
    return b(fg, bg, a)


def _fit(text: str, step: str, bold: bool, w_pt: float, h_pt: float, ds, fmt: str):
    """(step, size, lines) of the largest step from `step` down to body (caption stays caption) whose wrap fits."""
    from .thumbs import wrap
    family = getattr(ds.families, _ROLE.get(step, 'body'))
    k = TYPE_STEPS.index(step)
    floor = 0 if step == 'caption' else TYPE_STEPS.index('body')
    lh = ds.scale.display_line_height if step == 'display' else ds.scale.line_height
    for j in range(k, floor - 1, -1):
        st = TYPE_STEPS[j]
        size = ds.size(st, fmt)
        lines = wrap(text, family, size, w_pt, bold=bold, fmt=fmt)
        if len(lines) * size * lh <= h_pt + 0.5:
            return st, size, lines, family, lh
    return None


def validate(raw: dict, ds: DesignSystem, ws: Workspace, *, page: int, fmt: str,
             refs: list[str] = ()) -> tuple[ShapeProgram | None, list[str]]:
    """(program, problems). Schema, then: shapes inside the grid, text contrast against what is under it, text sizes
    >= the format minimum, refs only from `refs`/ws/icon names, at most 40 shapes."""
    from .qa import THRESHOLDS, contrast
    from .thumbs import hex_of
    cols, rows = grid_of(fmt)
    problems = schema_validate(raw, freeform_schema(cols, rows))
    if problems:
        return None, problems[:10]
    shapes = [dict(s) for s in raw['shapes']]
    size = _page_size(fmt)
    m, cw, ch = _frame(size, ds, fmt, cols, rows)
    icon_names = None
    offered = set(refs or ())
    for n, s in enumerate(shapes):
        t, where = s['type'], f'shape {n + 1} ({s["type"]})'
        s.setdefault('z', 0)
        if not _inside(s, cols, rows):
            problems.append(f'{where} goes outside the grid')
            continue
        if t in ('line', 'path'):
            pts = s.get('points') or []
            if len(pts) < 2:
                problems.append(f'{where} needs at least 2 points')
            elif any(not (0 <= x <= cols and 0 <= y <= rows) for x, y in pts):
                problems.append(f'{where} has a point outside the grid')
            continue
        if s['w'] <= 0 or s['h'] <= 0:
            problems.append(f'{where} has no size')
            continue
        if t == 'text':
            text, ref = s.get('text') or '', s.get('ref') or ''
            if not text.strip() and not ref.startswith('spec:'):
                problems.append(f'{where} has no text')
            elif len(text) > MAX_LITERAL:
                problems.append(f'{where} has more than {MAX_LITERAL} characters')
            elif text.strip():
                step = s.get('step') or 'body'
                if _fit(' '.join(text.split()), step, s.get('weight') == 'bold', s['w'] * cw, s['h'] * ch, ds,
                        fmt) is None:
                    problems.append(f'{where} does not fit its box at the minimum text size')
        elif t in ('image', 'diagram'):
            ref = s.get('ref') or ''
            if ref not in offered:
                problems.append(f'{where} uses {ref[:40]!r}, which this page was not offered')
            elif ref.startswith('ws:') and ws is not None:
                if ws.get(ref) is None:
                    problems.append(f'{where} uses {ref[:40]!r}, which is not in the workspace')
        elif t == 'icon':
            ref = s.get('ref') or ''
            if icon_names is None:
                try:
                    from . import icons
                    icon_names = set(icons.names())
                except Exception:
                    icon_names = set()
            if not ref.startswith('icon:') or ref[5:] not in icon_names:
                problems.append(f'{where} names {ref[:40]!r}, which is not a bundled icon')
    texts = [(n, s) for n, s in enumerate(shapes) if s['type'] == 'text']
    for a in range(len(texts)):
        for b in range(a + 1, len(texts)):
            if _overlap(texts[a][1], texts[b][1]) > 0:
                problems.append(f'shapes {texts[a][0] + 1} and {texts[b][0] + 1} put text on top of text')
    order = sorted(range(len(shapes)), key=lambda k: (shapes[k].get('z', 0), k))
    for n, s in texts:
        cx, cy = s['x'] + s['w'] / 2, s['y'] + s['h'] / 2
        layers, base = [], hex_of('bg', ds)
        for k in reversed(order[:order.index(n)]):
            u = shapes[k]
            if u['type'] in ('text', 'line', 'path', 'icon') or not (u['x'] <= cx <= u['x'] + u['w'] and
                                                                      u['y'] <= cy <= u['y'] + u['h']):
                continue
            if u['type'] in ('image', 'diagram'):
                if not any(a >= 0.5 for _, a in layers):
                    problems.append(f'shape {n + 1} puts text on a picture without a filled shape between them')
                base = None
                break
            if u.get('fill'):
                a = float(u.get('opacity', 1))
                if a >= 0.999:
                    base = hex_of(u['fill'], ds)
                    break
                layers.append((hex_of(u['fill'], ds), a))
        if base is None:
            continue
        bg = base
        for colour, a in reversed(layers):
            bg = _blend(colour, bg, a)
        step = s.get('step') or 'body'
        pt = ds.size(step, fmt)
        large = pt >= THRESHOLDS['large_pt'] or (s.get('weight') == 'bold' and pt >= THRESHOLDS['large_bold_pt'])
        need = THRESHOLDS['contrast_large'] if large else THRESHOLDS['contrast_text']
        ratio = contrast(hex_of(s.get('fill') or 'text', ds), bg)
        if ratio < need:
            problems.append(f'shape {n + 1} has text contrast {ratio:.1f}:1, below {need:g}:1')
    if problems:
        return None, problems
    return ShapeProgram(page, cols, rows, shapes), []


def _line_png(s: dict, x0: float, y0: float, w: float, h: float, cw: float, ch: float, ds, pad: float) -> bytes:
    from PIL import Image, ImageDraw
    from .thumbs import hex_of
    k = 2.0   # pixels per point
    im = Image.new('RGBA', (max(1, int((w + 2 * pad) * k)), max(1, int((h + 2 * pad) * k))), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pts = [((x * cw - x0 + pad) * k, (y * ch - y0 + pad) * k) for x, y in s.get('points') or []]
    colour = hex_of(s.get('stroke') or s.get('fill') or 'accent', ds)
    rgba = (*(int(colour[i:i + 2], 16) for i in (0, 2, 4)), int(255 * float(s.get('opacity', 1))))
    width = max(1, int(max(0.5, float(s.get('stroke_w') or 1)) * ds.shape.stroke * k))
    if s['type'] == 'path' and s.get('fill') and len(pts) >= 3:
        fill = hex_of(s['fill'], ds)
        d.polygon(pts, fill=(*(int(fill[i:i + 2], 16) for i in (0, 2, 4)), rgba[3]))
    d.line(pts, fill=rgba, width=width, joint='curve')
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    return buf.getvalue()


def to_boxes(program: ShapeProgram, size: tuple[float, float], ds: DesignSystem, spec: dict, *, fmt: str,
             ws: Workspace) -> list[Box]:
    """Grid units -> points (margins applied), text fitted with studio/fonts; boxes ready for a PagePlan."""
    from .thumbs import _resolve
    m, cw, ch = _frame(size, ds, fmt, program.cols, program.rows)
    out: list[Box] = []
    for n, s in enumerate(program.shapes):
        t = s['type']
        x, y, w, h = m + s['x'] * cw, m + s['y'] * ch, s['w'] * cw, s['h'] * ch
        bid, z = f'p{program.page}.ff{n}', int(s.get('z', 0))
        opacity = float(s.get('opacity', 1))
        if t in ('rect', 'ellipse'):
            full = s['x'] == 0 and s['y'] == 0 and s['w'] == program.cols and s['h'] == program.rows
            if full and t == 'rect':
                x, y, w, h = 0.0, 0.0, size[0], size[1]
            radius = min(w, h) / 2 if t == 'ellipse' else float(s.get('radius', 0)) * min(w, h)
            out.append(Box(bid, 'shape', x, y, w, h, z=z, overlay_ok=True, bleed=full and t == 'rect',
                           style=BoxStyle(fill=s.get('fill'), stroke=s.get('stroke'),
                                          stroke_w=float(s.get('stroke_w') or 0) * ds.shape.stroke, radius=radius,
                                          opacity=opacity)))
        elif t in ('line', 'path'):
            pts = s.get('points') or []
            xs, ys = [p[0] * cw for p in pts], [p[1] * ch for p in pts]
            pad = max(1.0, float(s.get('stroke_w') or 1) * ds.shape.stroke)
            x0, y0 = min(xs), min(ys)
            bw, bh = max(xs) - x0, max(ys) - y0
            ref = ws.put('art', f'freeform-p{program.page}-{n}.png', _line_png(s, x0, y0, bw, bh, cw, ch, ds, pad))
            out.append(Box(bid, 'image', m + x0 - pad, m + y0 - pad, bw + 2 * pad, bh + 2 * pad, z=z, content=ref,
                           fit='contain', alt=s.get('alt') or 'decorative line', overlay_ok=True))
        elif t == 'text':
            ref = s.get('ref') or ''
            text = ' '.join((s.get('text') or _resolve(ref, spec)).split())
            bold = s.get('weight') == 'bold'
            fitted = _fit(text, s.get('step') or 'body', bold, w, h, ds, fmt)
            if fitted is None:
                step = s.get('step') or 'body'
                from .thumbs import wrap
                family = getattr(ds.families, _ROLE.get(step, 'body'))
                lh = ds.scale.line_height
                fitted = (step, ds.size(step, fmt), wrap(text, family, ds.size(step, fmt), w, bold=bold, fmt=fmt),
                          family, lh)
            step, pt, lines, family, lh = fitted
            content = f'text:{text[:MAX_LITERAL]}' if s.get('text') else (ref or 'none')
            out.append(Box(bid, 'text', x, y, w, h, z=z, content=content, font=family, step=step, size=pt, bold=bold,
                           line_height=lh, align=s.get('align') or 'left', lines=lines, overlay_ok=True,
                           style=BoxStyle(text_color=s.get('fill') or 'text', opacity=opacity)))
        elif t == 'image':
            out.append(Box(bid, 'image', x, y, w, h, z=z, content=s.get('ref') or 'none', fit='cover',
                           alt=s.get('alt') or 'picture', overlay_ok=True,
                           style=BoxStyle(radius=float(s.get('radius', 0)) * min(w, h), opacity=opacity)))
        elif t == 'icon':
            out.append(Box(bid, 'icon', x, y, w, h, z=z, content=s.get('ref') or 'none', overlay_ok=True,
                           alt=s.get('alt') or (s.get('ref') or 'icon')[5:].replace('-', ' '),
                           style=BoxStyle(text_color=s.get('stroke') or s.get('fill') or 'accent')))
        elif t == 'diagram':
            out.append(Box(bid, 'diagram', x, y, w, h, z=z, content=s.get('ref') or 'none', fit='contain',
                           alt=s.get('alt') or 'diagram', overlay_ok=True))
    return out
