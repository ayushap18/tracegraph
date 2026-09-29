"""Code-drawn decorative art (docs/PLAN-designer.md 3.4 and 9.11): seeded, deterministic, preset-consistent patterns for
pages without photos (covers, dividers, closing slides). Output is a list of primitives the painters draw natively.

Primitive dicts (points, top-left origin, colours as token names):
    {"type": "rect"|"ellipse", "x", "y", "w", "h", "fill", "stroke", "stroke_w", "opacity", "radius"}
    {"type": "line"|"path", "points": [[x, y], ...], "stroke", "stroke_w", "fill", "closed": bool, "opacity"}
    {"type": "gradient", "x", "y", "w", "h", "from": token, "to": token, "angle": deg}

Art is decoration, never content: it stays in the accent and chart colours at low opacity so text laid over it keeps
its contrast, it is seeded (the same seed always gives the same picture, so a restyle or a re-render never changes it),
and it never uses gradients in a print-first preset (mono). `paint_pil` draws any primitive list with Pillow, so
thumbnails and the PNG export match what the PPTX and PDF painters draw.

Owner: builder V.
"""
from __future__ import annotations

import io
import math
import random
import zlib

from .tokens import DesignSystem

ART_KINDS = ('dots', 'grid', 'gradient-mesh', 'blobs', 'orbit-rings', 'waves', 'stripes', 'corner-arcs')
MAX_PRIMITIVES = 200
REF_PT = 960.0            # png() draws on a canvas whose longer side is this many points

# preset -> the kinds that belong to its visual language, best first
PRESET_KINDS = {
    'bold-dark': ('orbit-rings', 'gradient-mesh', 'waves', 'corner-arcs'),
    'editorial': ('corner-arcs', 'stripes', 'grid'),
    'minimal': ('grid', 'dots', 'corner-arcs'),
    'vibrant': ('blobs', 'waves', 'dots', 'gradient-mesh'),
    'pastel': ('blobs', 'waves', 'dots'),
    'academic': ('grid', 'dots', 'corner-arcs'),
    'mono': ('stripes', 'grid', 'dots'),
    'high-legibility': ('corner-arcs', 'dots', 'grid'),
}
MOOD_KINDS = {
    'orbit-rings': ('space', 'cosmic', 'cinematic', 'dramatic', 'astronomy', 'planet', 'physics', 'science', 'tech'),
    'blobs': ('playful', 'fun', 'friendly', 'organic', 'creative', 'kids', 'biology', 'soft'),
    'waves': ('calm', 'ocean', 'water', 'flow', 'gentle', 'weather', 'climate', 'music'),
    'grid': ('data', 'maths', 'math', 'technical', 'engineering', 'structured', 'minimal', 'clean'),
    'dots': ('modern', 'pattern', 'texture', 'bright'),
    'gradient-mesh': ('bold', 'vibrant', 'energetic', 'sunset', 'glow', 'pitch'),
    'stripes': ('retro', 'print', 'poster', 'sporty'),
    'corner-arcs': ('elegant', 'editorial', 'classic', 'literary', 'history', 'formal'),
}
NO_GRADIENT = ('mono',)


def _rng(kind: str, seed: int, w: float, h: float) -> random.Random:
    key = f'{kind}|{int(seed)}|{round(w, 1)}|{round(h, 1)}'.encode()
    return random.Random(zlib.crc32(key))


def _accents(ds: DesignSystem) -> list[str]:
    """Colour tokens for decoration, in the preset's order: accent, accent2, then the chart colours."""
    return ['accent', 'accent2', 'chart1', 'chart2', 'chart3', 'chart4']


def _mono(ds: DesignSystem) -> bool:
    return ds.id in NO_GRADIENT or bool(ds.hatch)


def _r(v: float) -> float:
    return round(v, 2)


def _rect(x, y, w, h, fill=None, stroke=None, stroke_w=0.0, opacity=1.0, radius=0.0) -> dict:
    return {'type': 'rect', 'x': _r(x), 'y': _r(y), 'w': _r(w), 'h': _r(h), 'fill': fill, 'stroke': stroke,
            'stroke_w': _r(stroke_w), 'opacity': _r(opacity), 'radius': _r(radius)}


def _ellipse(x, y, w, h, fill=None, stroke=None, stroke_w=0.0, opacity=1.0) -> dict:
    return {'type': 'ellipse', 'x': _r(x), 'y': _r(y), 'w': _r(w), 'h': _r(h), 'fill': fill, 'stroke': stroke,
            'stroke_w': _r(stroke_w), 'opacity': _r(opacity), 'radius': 0.0}


def _path(points, *, stroke=None, stroke_w=0.0, fill=None, closed=False, opacity=1.0, kind='path') -> dict:
    return {'type': kind, 'points': [[_r(x), _r(y)] for x, y in points], 'stroke': stroke, 'stroke_w': _r(stroke_w),
            'fill': fill, 'closed': bool(closed), 'opacity': _r(opacity)}


def _dots(rng, w, h, ds) -> list[dict]:
    cols = _accents(ds)
    step = max(14.0, min(w, h) / 12)
    nx, ny = int(w // step) + 1, int(h // step) + 1
    while nx * ny > MAX_PRIMITIVES - 1:
        step *= 1.25
        nx, ny = int(w // step) + 1, int(h // step) + 1
    # a halftone that fades from one corner: bigger, stronger dots near the origin corner
    ox, oy = rng.choice([(0, 0), (w, 0), (0, h), (w, h)])
    diag = math.hypot(w, h) or 1
    color = cols[rng.randrange(2)]
    out = []
    for i in range(nx):
        for j in range(ny):
            cx, cy = i * step + step / 2, j * step + step / 2
            t = 1 - math.hypot(cx - ox, cy - oy) / diag
            r = step * (0.08 + 0.22 * t * t)
            if r < 1.0:
                continue
            out.append(_ellipse(cx - r, cy - r, 2 * r, 2 * r, fill=color, opacity=0.12 + 0.3 * t))
    return out


def _grid(rng, w, h, ds) -> list[dict]:
    cell = max(24.0, min(w, h) / rng.choice((6, 8, 10)))
    out = []
    x = 0.0
    while x <= w + 0.1 and len(out) < MAX_PRIMITIVES - 12:
        out.append(_path([(x, 0), (x, h)], stroke='border', stroke_w=0.75, opacity=0.6, kind='line'))
        x += cell
    y = 0.0
    while y <= h + 0.1 and len(out) < MAX_PRIMITIVES - 12:
        out.append(_path([(0, y), (w, y)], stroke='border', stroke_w=0.75, opacity=0.6, kind='line'))
        y += cell
    cols, rows = max(1, int(w // cell)), max(1, int(h // cell))
    picked = set()
    for k in range(min(8, cols * rows // 4 + 1)):
        c, r = rng.randrange(cols), rng.randrange(rows)
        if (c, r) in picked:
            continue
        picked.add((c, r))
        out.append(_rect(c * cell, r * cell, cell, cell, fill=_accents(ds)[k % 3], opacity=0.16 + 0.1 * (k % 3)))
    return out


def _gradient_mesh(rng, w, h, ds) -> list[dict]:
    if _mono(ds):
        return _dots(rng, w, h, ds)
    out = [{'type': 'gradient', 'x': 0.0, 'y': 0.0, 'w': _r(w), 'h': _r(h), 'from': 'accent', 'to': 'accent2',
            'angle': float(rng.choice((20, 35, 50, 135, 160)))}]
    for k in range(rng.randint(3, 5)):
        r = min(w, h) * rng.uniform(0.35, 0.7)
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        color, strength = f'chart{1 + (k + rng.randrange(6)) % 6}', rng.uniform(0.2, 0.34)
        for ring in range(4):   # concentric layers fake a soft edge natively (no blur in PPTX or PDF)
            rr = r * (1 - 0.18 * ring)
            out.append(_ellipse(cx - rr, cy - rr, 2 * rr, 2 * rr, fill=color, opacity=strength / 4))
    # a veil in the background colour keeps text over the mesh readable
    out.append(_rect(0, 0, w, h, fill='bg', opacity=0.25))
    return out


def _blob_points(cx, cy, r, rng, n=48) -> list[tuple[float, float]]:
    harm = [(k, rng.uniform(0.04, 0.14) / k ** 0.3, rng.uniform(0, 2 * math.pi)) for k in (2, 3, 5)]
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        rr = r * (1 + sum(amp * math.sin(k * a + ph) for k, amp, ph in harm))
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts + [pts[0]]


def _blobs(rng, w, h, ds) -> list[dict]:
    cols = _accents(ds)
    out = []
    n = rng.randint(3, 5)
    for k in range(n):
        r = min(w, h) * rng.uniform(0.18, 0.38)
        # blobs hug the edges so the middle stays free for text
        edge = rng.choice(('l', 'r', 't', 'b'))
        cx = {'l': rng.uniform(-0.2, 0.15) * w, 'r': rng.uniform(0.85, 1.2) * w}.get(edge, rng.uniform(0, w))
        cy = {'t': rng.uniform(-0.2, 0.15) * h, 'b': rng.uniform(0.85, 1.2) * h}.get(edge, rng.uniform(0, h))
        out.append(_path(_blob_points(cx, cy, r, rng), fill=cols[k % len(cols)], closed=True,
                         opacity=rng.uniform(0.22, 0.45)))
    return out


def _orbit_rings(rng, w, h, ds) -> list[dict]:
    cx = rng.choice((0.72, 0.8, 0.85)) * w
    cy = rng.uniform(0.3, 0.7) * h
    tilt = rng.uniform(0.35, 0.6)
    out = []
    base = min(w, h) * 0.16
    n = rng.randint(5, 7)
    for k in range(n):
        rx = base * (1 + 0.75 * k)
        ry = rx * tilt
        out.append(_ellipse(cx - rx, cy - ry, 2 * rx, 2 * ry, stroke='accent' if k % 2 == 0 else 'accent2',
                            stroke_w=1.2, opacity=0.55 - 0.06 * k))
        a = rng.uniform(0, 2 * math.pi)
        pr = max(2.5, base * 0.12 * (1 + (k % 3)))
        px, py = cx + rx * math.cos(a), cy + ry * math.sin(a)
        out.append(_ellipse(px - pr, py - pr, 2 * pr, 2 * pr, fill=f'chart{1 + k % 6}', opacity=0.9))
    core = base * 0.55
    out.append(_ellipse(cx - core, cy - core, 2 * core, 2 * core, fill='accent', opacity=0.85))
    return out


def _waves(rng, w, h, ds) -> list[dict]:
    cols = _accents(ds)
    out = []
    n = rng.randint(3, 4)
    for k in range(n):
        base = h * (0.62 + 0.1 * k)
        amp = h * rng.uniform(0.03, 0.07)
        freq = rng.uniform(1.0, 2.2)
        ph = rng.uniform(0, 2 * math.pi)
        pts = [(w * i / 40, base + amp * math.sin(freq * 2 * math.pi * i / 40 + ph)) for i in range(41)]
        pts += [(w, h), (0, h), pts[0]]
        out.append(_path(pts, fill=cols[k % len(cols)], closed=True, opacity=0.18 + 0.1 * k))
    return out


def _stripes(rng, w, h, ds) -> list[dict]:
    ang = math.radians(rng.choice((30, 45, 60, 120, 135)))
    band = max(10.0, min(w, h) / rng.choice((10, 14, 18)))
    dx, dy = math.cos(ang), math.sin(ang)
    nx, ny = -dy, dx
    span = w + h
    out = []
    k = 0
    offset = -span
    while offset < span and len(out) < 40:
        if k % 2 == 0:
            c = (w / 2 + nx * offset, h / 2 + ny * offset)
            a = (c[0] - dx * span, c[1] - dy * span)
            b = (c[0] + dx * span, c[1] + dy * span)
            out.append(_path([a, b, (b[0] + nx * band, b[1] + ny * band), (a[0] + nx * band, a[1] + ny * band), a],
                             fill='accent' if k % 4 == 0 else 'accent2', closed=True, opacity=0.1))
        offset += band
        k += 1
    return _clip_polys(out, w, h)


def _clip_polys(prims: list[dict], w: float, h: float) -> list[dict]:
    """Sutherland-Hodgman clip of closed fills to the art box, so nothing is drawn outside it."""
    def clip(pts, inside, cross):
        out = []
        for i in range(len(pts)):
            cur, prev = pts[i], pts[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        return out

    def at_x(x0):
        return lambda p, q: (x0, p[1] + (q[1] - p[1]) * (x0 - p[0]) / ((q[0] - p[0]) or 1e-9))

    def at_y(y0):
        return lambda p, q: (p[0] + (q[0] - p[0]) * (y0 - p[1]) / ((q[1] - p[1]) or 1e-9), y0)
    kept = []
    for p in prims:
        pts = [tuple(x) for x in p['points'][:-1]]
        for inside, cross in ((lambda q: q[0] >= 0, at_x(0)), (lambda q: q[0] <= w, at_x(w)),
                              (lambda q: q[1] >= 0, at_y(0)), (lambda q: q[1] <= h, at_y(h))):
            pts = clip(pts, inside, cross) if pts else pts
        if len(pts) >= 3:
            kept.append({**p, 'points': [[_r(x), _r(y)] for x, y in pts + [pts[0]]]})
    return kept


def _arc_pts(cx, cy, r, a0, a1, n=24) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]


def _corner_arcs(rng, w, h, ds) -> list[dict]:
    corners = {'tl': (0, 0, 0, math.pi / 2), 'tr': (w, 0, math.pi / 2, math.pi), 'br': (w, h, math.pi, 1.5 * math.pi),
               'bl': (0, h, 1.5 * math.pi, 2 * math.pi)}
    pick = rng.sample(sorted(corners), 2)
    out = []
    for n, key in enumerate(pick):
        cx, cy, a0, a1 = corners[key]
        big = min(w, h) * rng.uniform(0.28, 0.4)
        out.append(_path(_arc_pts(cx, cy, big * 0.55, a0, a1) + [(cx, cy)], fill='accent' if n == 0 else 'accent2',
                         closed=True, opacity=0.35))
        for k in range(1, 4):
            out.append(_path(_arc_pts(cx, cy, big * (0.55 + 0.2 * k), a0, a1), stroke='accent', stroke_w=1.2,
                             opacity=0.5 - 0.1 * k))
    return out


_DRAW = {'dots': _dots, 'grid': _grid, 'gradient-mesh': _gradient_mesh, 'blobs': _blobs, 'orbit-rings': _orbit_rings,
         'waves': _waves, 'stripes': _stripes, 'corner-arcs': _corner_arcs}


def draw(kind: str, seed: int, w: float, h: float, ds: DesignSystem) -> list[dict]:
    """Primitives filling (w, h) points; the same (kind, seed, size, ds) always gives the same list."""
    kind = kind if kind in _DRAW else ART_KINDS[0]
    try:
        w, h = float(w), float(h)
    except (TypeError, ValueError):
        return []
    if not (w > 0 and h > 0) or not math.isfinite(w + h):
        return []
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = 0
    out = _DRAW[kind](_rng(kind, seed, w, h), w, h, ds)
    return out[:MAX_PRIMITIVES]


def pick(ds: DesignSystem, mood: list[str], seed: int) -> str:
    """The art kind that suits a preset and mood words (deterministic)."""
    own = PRESET_KINDS.get(getattr(ds, 'id', ''), ('corner-arcs', 'dots', 'grid'))
    words = {str(m).lower().strip() for m in (mood or []) if m}
    for kind in own:
        if words & set(MOOD_KINDS.get(kind, ())):
            return kind
    if not _mono(ds):
        for kind, ws in MOOD_KINDS.items():
            if words & set(ws) and kind in own + ('dots', 'grid', 'corner-arcs'):
                return kind
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = 0
    return own[zlib.crc32(str(seed).encode()) % min(2, len(own))]


# ---------- raster ----------

def _hex(ds: DesignSystem, token: str | None) -> tuple[int, int, int] | None:
    if not token:
        return None
    try:
        h = ds.color(token)
    except Exception:
        h = (getattr(ds, 'colors', {}) or {}).get(token) or '888888'
    h = str(h).lstrip('#')
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (136, 136, 136)


def paint_pil(img, prims: list[dict], ds: DesignSystem, *, scale: float = 1.0, origin: tuple = (0.0, 0.0)) -> None:
    """Draws primitives (points) onto an RGBA Pillow image at `scale` pixels per point, offset by origin (pixels).
    Used by `png` and by thumbnails, so rasters match the native painters."""
    from PIL import Image, ImageDraw
    ox, oy = origin
    W, H = img.size

    def P(x, y):
        return (ox + x * scale, oy + y * scale)
    for p in prims:
        t = p.get('type')
        alpha = int(255 * max(0.0, min(1.0, float(p.get('opacity', 1.0) or 0.0))))
        if t == 'gradient':
            a, b = _hex(ds, p.get('from')), _hex(ds, p.get('to'))
            if a is None or b is None:
                continue
            x0, y0 = P(p['x'], p['y'])
            gw, gh = max(1, int(round(p['w'] * scale))), max(1, int(round(p['h'] * scale)))
            ang = math.radians(float(p.get('angle') or 0))
            side = int(math.hypot(gw, gh)) + 2
            mask = Image.linear_gradient('L').rotate(90).resize((side, side)).rotate(-math.degrees(ang), expand=False)
            mask = mask.crop(((side - gw) // 2, (side - gh) // 2, (side - gw) // 2 + gw, (side - gh) // 2 + gh))
            grad = Image.composite(Image.new('RGBA', (gw, gh), (*b, 255)), Image.new('RGBA', (gw, gh), (*a, 255)), mask)
            img.alpha_composite(grad, (int(round(x0)), int(round(y0))))
            continue
        fill, stroke = _hex(ds, p.get('fill')), _hex(ds, p.get('stroke'))
        sw = max(0, float(p.get('stroke_w') or 0) * scale)
        if t in ('rect', 'ellipse'):
            x0, y0 = P(p['x'], p['y'])
            x1, y1 = P(p['x'] + p['w'], p['y'] + p['h'])
            pad = int(sw) + 2
            bx0, by0 = max(0, int(min(x0, x1)) - pad), max(0, int(min(y0, y1)) - pad)
            bx1, by1 = min(W, int(max(x0, x1)) + pad + 1), min(H, int(max(y0, y1)) + pad + 1)
            if bx1 <= bx0 or by1 <= by0:
                continue
            layer = Image.new('RGBA', (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            box = [x0 - bx0, y0 - by0, x1 - bx0, y1 - by0]
            kw = {'fill': (*fill, alpha) if fill else None,
                  'outline': (*stroke, alpha) if stroke and sw > 0 else None, 'width': max(1, int(round(sw)))}
            if t == 'ellipse':
                d.ellipse(box, **kw)
            elif p.get('radius'):
                d.rounded_rectangle(box, radius=float(p['radius']) * scale, **kw)
            else:
                d.rectangle(box, **kw)
            img.alpha_composite(layer, (bx0, by0))
            continue
        if t in ('line', 'path'):
            pts = [P(x, y) for x, y in p.get('points') or []]
            if len(pts) < 2:
                continue
            xs, ys = [q[0] for q in pts], [q[1] for q in pts]
            pad = int(sw) + 2
            bx0, by0 = max(0, int(min(xs)) - pad), max(0, int(min(ys)) - pad)
            bx1, by1 = min(W, int(max(xs)) + pad + 1), min(H, int(max(ys)) + pad + 1)
            if bx1 <= bx0 or by1 <= by0:
                continue
            layer = Image.new('RGBA', (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            loc = [(x - bx0, y - by0) for x, y in pts]
            if fill and (p.get('closed') or t == 'path') and len(loc) >= 3:
                d.polygon(loc, fill=(*fill, alpha))
            if stroke and sw > 0:
                d.line(loc, fill=(*stroke, alpha), width=max(1, int(round(sw))), joint='curve')
            img.alpha_composite(layer, (bx0, by0))


def png(kind: str, seed: int, w_px: int, h_px: int, ds: DesignSystem) -> bytes:
    """The same art rasterised (thumbnails, DOCX)."""
    from PIL import Image
    w_px, h_px = max(1, min(int(w_px), 4096)), max(1, min(int(h_px), 4096))
    bg = _hex(ds, 'bg') or (255, 255, 255)
    img = Image.new('RGBA', (w_px, h_px), (*bg, 255))
    # drawn at a slide-sized canvas (the longer side 960 pt) and scaled, so a thumbnail shows the same picture as
    # the painted page of the same proportions
    k = REF_PT / max(w_px, h_px)
    w_pt, h_pt = round(w_px * k, 1), round(h_px * k, 1)
    paint_pil(img, draw(kind, seed, w_pt, h_pt, ds), ds, scale=w_px / w_pt)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'PNG')
    return buf.getvalue()
