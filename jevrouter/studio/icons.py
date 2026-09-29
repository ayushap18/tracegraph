"""Icons (docs/PLAN-designer.md 3.4 and 9.11): a curated Lucide subset (~150 icons: science, history, maths, geography,
tech, arrows, UI) bundled as JSON under jevrouter/studio/data/, with Lucide's ISC LICENSE beside it. Drawn as native
vector shapes in PPTX/PDF and as PNG in thumbnails; picked by keyword.

data/lucide.json (fixed by contract):
    {"source": "lucide", "version": "<lucide release>", "license": "ISC", "viewbox": 24,
     "icons": {"<name>": {"tags": ["..."], "nodes": [["path", {"d": "..."}], ["circle", {"cx": 12, "cy": 12, "r": 10}],
                                                    ["rect", {...}], ["line", {...}], ["polyline", {"points": "..."}],
                                                    ["polygon", {...}], ["ellipse", {...}]]}}}
Strokes only (Lucide is a 2 px stroke set on a 24 grid, round caps and joins); fill none.

The data was taken once from lucide-static (the release named in the JSON, jsDelivr) and is committed; nothing here
touches the network. Every node becomes polylines (`polylines`), which python-pptx freeforms, reportlab paths and
Pillow lines all draw the same way.

Owner: builder V.
"""
from __future__ import annotations

import functools
import io
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
ICONS_JSON = DATA / 'lucide.json'
LICENSE = DATA / 'LICENSE-lucide'
VIEWBOX = 24
STROKE = 2.0               # in viewbox units
NODE_TYPES = ('path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse')
ATTRIBUTION = 'Icons: Lucide (lucide.dev), ISC licence'

# Words students use that the Lucide tags miss, and subject words that should name one clear icon. Checked before tags.
SYNONYMS = {
    'science': 'atom', 'physics': 'atom', 'chemistry': 'flask-conical', 'biology': 'dna', 'genetics': 'dna',
    'gene': 'dna', 'genes': 'dna', 'cell': 'microscope', 'cells': 'microscope', 'lab': 'flask-conical',
    'experiment': 'test-tube', 'history': 'landmark', 'ancient': 'pyramid', 'war': 'sword', 'battle': 'sword',
    'king': 'crown', 'queen': 'crown', 'empire': 'crown', 'maths': 'sigma', 'math': 'sigma', 'mathematics': 'sigma',
    'algebra': 'variable', 'geometry': 'shapes', 'statistics': 'chart-bar', 'data': 'database',
    'geography': 'globe', 'world': 'globe', 'planet': 'orbit', 'space': 'rocket', 'climate': 'thermometer',
    'weather': 'cloud', 'rain': 'cloud-rain', 'water': 'droplet', 'ocean': 'waves-horizontal', 'sea': 'waves-horizontal',
    'technology': 'cpu', 'tech': 'cpu', 'computer': 'laptop', 'internet': 'wifi', 'ai': 'bot', 'robot': 'bot',
    'software': 'code', 'programming': 'code', 'energy': 'zap', 'electricity': 'zap', 'power': 'zap',
    'health': 'heart-pulse', 'medicine': 'pill', 'doctor': 'stethoscope', 'idea': 'lightbulb', 'ideas': 'lightbulb',
    'goal': 'target', 'goals': 'target', 'time': 'clock', 'growth': 'trending-up', 'increase': 'trending-up',
    'decrease': 'trending-down', 'decline': 'trending-down', 'money': 'dollar-sign', 'cost': 'dollar-sign',
    'economy': 'landmark', 'people': 'users', 'population': 'users', 'team': 'users', 'person': 'user',
    'education': 'graduation-cap', 'learning': 'book-open', 'reading': 'book-open', 'law': 'scale',
    'justice': 'scale', 'government': 'landmark', 'election': 'megaphone', 'safety': 'shield-check',
    'security': 'lock', 'plant': 'sprout', 'plants': 'sprout', 'nature': 'leaf', 'environment': 'leaf',
    'farming': 'wheat', 'food': 'wheat', 'transport': 'train-front', 'travel': 'plane', 'city': 'building',
    'industry': 'factory', 'question': 'circle-question-mark', 'warning': 'triangle-alert', 'result': 'circle-check',
    'results': 'chart-bar', 'summary': 'clipboard-list', 'conclusion': 'flag', 'cycle': 'refresh-cw',
    'process': 'arrow-right', 'step': 'chevron-right', 'steps': 'chevrons-right', 'award': 'trophy',
    'win': 'trophy', 'success': 'trophy', 'communication': 'message-square', 'speech': 'mic', 'music': 'headphones',
    'art': 'pen-tool', 'design': 'pen-tool', 'writing': 'feather', 'research': 'search', 'discovery': 'telescope',
    'astronomy': 'telescope', 'star': 'star', 'stars': 'sparkles', 'brain': 'brain', 'mind': 'brain',
    'heat': 'flame', 'photosynthesis': 'leaf', 'machine': 'cpu', 'ecosystem': 'tree-pine',
    'evolution': 'dna', 'vote': 'megaphone', 'trade': 'ship', 'exploration': 'compass', 'religion': 'church', 'fire': 'flame', 'temperature': 'thermometer', 'mountains': 'mountain', 'map': 'map',
}
_CATEGORY_TAGS = {'science', 'history', 'maths', 'geography', 'tech', 'arrows', 'ui'}
_STOP = {'the', 'a', 'an', 'of', 'and', 'or', 'in', 'on', 'for', 'to', 'with', 'by', 'at', 'from', 'is', 'are', 'how',
         'what', 'why', 'its', 'their', 'our', 'your'}


@dataclass
class Icon:
    name: str
    tags: list[str] = field(default_factory=list)
    nodes: list[tuple[str, dict]] = field(default_factory=list)


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    try:
        raw = json.loads(ICONS_JSON.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    icons = raw.get('icons') if isinstance(raw, dict) else None
    return icons if isinstance(icons, dict) else {}


def names() -> list[str]:
    """Every bundled icon name, sorted ([] when the data file is missing)."""
    return sorted(_data())


def get(name: str) -> Icon | None:
    d = _data().get(str(name or '').strip().lower())
    if not isinstance(d, dict):
        return None
    nodes = [(str(n[0]), dict(n[1])) for n in d.get('nodes') or [] if isinstance(n, (list, tuple)) and len(n) == 2
             and n[0] in NODE_TYPES and isinstance(n[1], dict)]
    return Icon(name=str(name).strip().lower(), tags=list(d.get('tags') or []), nodes=nodes)


def _words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", str(text or '').lower()):
        if w in _STOP or len(w) < 2:
            continue
        out.append(w)
    return out


def _stem(w: str) -> str:
    for suf in ('ies', 'es', 's'):
        if len(w) > 4 and w.endswith(suf):
            return w[:-len(suf)] + ('y' if suf == 'ies' else '')
    return w


def pick(text: str, *, used: set[str] | None = None) -> str | None:
    """The best icon for a phrase by name and tags ("battery", "planet", "dna"), skipping `used`; None when nothing
    matches well enough. Deterministic."""
    used = used or set()
    data = _data()
    if not data:
        return None
    raw = str(text or '').strip().lower()
    if raw in data and raw not in used:
        return raw
    words = _words(raw)
    if not words:
        return None
    stems = [_stem(w) for w in words]
    scores: dict[str, float] = {}
    for i, (w, s) in enumerate(zip(words, stems)):
        weight = 1.0 + 0.5 / (i + 1)       # earlier words weigh a little more ("battery life" is about batteries)
        syn = SYNONYMS.get(w) or SYNONYMS.get(s)
        if syn and syn in data:
            scores[syn] = scores.get(syn, 0) + 3.2 * weight
        for name, d in data.items():
            parts = name.split('-')
            got = 0.0
            if w in parts or s in parts:
                got = 3.0
            else:
                tags = d.get('tags') or ()
                if w in tags or s in tags:
                    got = 1.0 if (w in _CATEGORY_TAGS or s in _CATEGORY_TAGS) else 2.0
                elif len(s) >= 5 and any(t.startswith(s) or (len(t) >= 5 and s.startswith(t)) for t in tags
                                         if t not in _CATEGORY_TAGS):
                    got = 1.0
            if got:
                scores[name] = scores.get(name, 0) + got * weight
    best = sorted(((-sc, len(n), n) for n, sc in scores.items() if n not in used and sc >= 2.0))
    return best[0][2] if best else None


# ---------- geometry: every node flattened to polylines ----------

def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _arc_steps(r: float, sweep: float, tol: float) -> int:
    """Segments so the chord never strays more than tol from an arc of radius r (same units)."""
    if r <= tol or r <= 0:
        return max(2, int(math.ceil(abs(sweep) / (math.pi / 2))))
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - tol / r)))
    return max(2, min(256, int(math.ceil(abs(sweep) / max(step, 1e-3)))))


def _ellipse(cx, cy, rx, ry, tol) -> list[tuple[float, float]]:
    n = max(8, _arc_steps(max(rx, ry), 2 * math.pi, tol))
    pts = [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return pts + [pts[0]]


def _cubic(p0, p1, p2, p3, tol) -> list[tuple[float, float]]:
    dd = max(math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
             math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]))
    n = max(1, min(64, int(math.ceil(math.sqrt(0.75 * dd / max(tol, 1e-4))))))
    out = []
    for i in range(1, n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def _quad(p0, p1, p2, tol) -> list[tuple[float, float]]:
    dd = math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1])
    n = max(1, min(64, int(math.ceil(math.sqrt(dd / (4 * max(tol, 1e-4)))))))
    out = []
    for i in range(1, n + 1):
        t = i / n
        a, b, c = (1 - t) ** 2, 2 * t * (1 - t), t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]))
    return out


def _arc(p0, rx, ry, phi_deg, large, sweep, p1, tol) -> list[tuple[float, float]]:
    """An SVG elliptical arc (endpoint form) as points after p0 (SVG 1.1 appendix F.6)."""
    x1, y1 = p0
    x2, y2 = p1
    if (x1, y1) == (x2, y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [p1]
    phi = math.radians(phi_deg % 360)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx, cy = cp * cxp - sp * cyp + (x1 + x2) / 2, sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = _arc_steps(max(rx, ry), dt, tol)
    out = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        out.append((cp * ex - sp * ey + cx, sp * ex + cp * ey + cy))
    out[-1] = (x2, y2)
    return out


_TOKEN = re.compile(r'[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')


def _path_tokens(d: str) -> list:
    return _TOKEN.findall(d or '')


def path_polylines(d: str, tol: float) -> list[list[tuple[float, float]]]:
    """An SVG path's subpaths as point lists (viewbox units); closed subpaths repeat their first point."""
    toks = _path_tokens(d)
    # Packed arc flags: an arc's large-arc and sweep flags are one character each and may touch the next number, so
    # "a10 10 0 0010-10" is flags 0, 0 and x 10 (the regex reads "0010"), and "a5 5 0 012.9-4.5" is 0, 1, 2.9. At a
    # flag position the first character is the flag and the rest of the token is read again at the next position.
    out_toks, cmd, argn = [], None, 0
    for t in toks:
        if t.isalpha():
            cmd, argn = t, 0
            out_toks.append(t)
            continue
        while cmd in ('a', 'A') and argn % 7 in (3, 4) and len(t) > 1 and t[0] in '01':
            out_toks.append(t[0])
            argn += 1
            t = t[1:]
        out_toks.append(t)
        argn += 1
    toks = out_toks
    subpaths: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = 0.0
    sx = sy = 0.0
    last_c = last_q = None
    cmd = None
    i = 0

    def nums(k):
        nonlocal i
        vals = []
        while len(vals) < k:
            if i >= len(toks) or toks[i].isalpha():
                raise ValueError('short path')
            vals.append(float(toks[i]))
            i += 1
        return vals
    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in 'Zz':
                if cur:
                    if cur[-1] != (sx, sy):
                        cur.append((sx, sy))
                    subpaths.append(cur)
                cur = []
                x, y = sx, sy
                last_c = last_q = None
                continue
        elif cmd is None:
            break
        rel = cmd.islower()
        c = cmd.upper()
        try:
            if c == 'M':
                nx, ny = nums(2)
                if rel:
                    nx, ny = x + nx, y + ny
                if len(cur) > 1:
                    subpaths.append(cur)
                x, y, sx, sy = nx, ny, nx, ny
                cur = [(x, y)]
                cmd = 'l' if rel else 'L'   # further pairs are line-tos
                last_c = last_q = None
            elif c == 'L':
                nx, ny = nums(2)
                x, y = (x + nx, y + ny) if rel else (nx, ny)
                cur.append((x, y))
                last_c = last_q = None
            elif c == 'H':
                (nx,) = nums(1)
                x = x + nx if rel else nx
                cur.append((x, y))
                last_c = last_q = None
            elif c == 'V':
                (ny,) = nums(1)
                y = y + ny if rel else ny
                cur.append((x, y))
                last_c = last_q = None
            elif c == 'C':
                a = nums(6)
                if rel:
                    a = [a[0] + x, a[1] + y, a[2] + x, a[3] + y, a[4] + x, a[5] + y]
                cur += _cubic((x, y), (a[0], a[1]), (a[2], a[3]), (a[4], a[5]), tol)
                last_c, last_q = (a[2], a[3]), None
                x, y = a[4], a[5]
            elif c == 'S':
                a = nums(4)
                if rel:
                    a = [a[0] + x, a[1] + y, a[2] + x, a[3] + y]
                c1 = (2 * x - last_c[0], 2 * y - last_c[1]) if last_c else (x, y)
                cur += _cubic((x, y), c1, (a[0], a[1]), (a[2], a[3]), tol)
                last_c, last_q = (a[0], a[1]), None
                x, y = a[2], a[3]
            elif c == 'Q':
                a = nums(4)
                if rel:
                    a = [a[0] + x, a[1] + y, a[2] + x, a[3] + y]
                cur += _quad((x, y), (a[0], a[1]), (a[2], a[3]), tol)
                last_q, last_c = (a[0], a[1]), None
                x, y = a[2], a[3]
            elif c == 'T':
                a = nums(2)
                if rel:
                    a = [a[0] + x, a[1] + y]
                q1 = (2 * x - last_q[0], 2 * y - last_q[1]) if last_q else (x, y)
                cur += _quad((x, y), q1, (a[0], a[1]), tol)
                last_q, last_c = q1, None
                x, y = a[0], a[1]
            elif c == 'A':
                a = nums(7)
                end = (a[5] + x, a[6] + y) if rel else (a[5], a[6])
                cur += _arc((x, y), a[0], a[1], a[2], bool(a[3]), bool(a[4]), end, tol)
                x, y = end
                last_c = last_q = None
            else:
                i += 1
        except ValueError:
            break
        if not cur:
            cur = [(x, y)]
    if len(cur) > 1:
        subpaths.append(cur)
    return subpaths


def _rounded_rect(x, y, w, h, rx, ry, tol) -> list[tuple[float, float]]:
    rx, ry = min(rx, w / 2), min(ry, h / 2)
    if rx <= 0 or ry <= 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    pts = []
    corners = [(x + w - rx, y + ry, -math.pi / 2), (x + w - rx, y + h - ry, 0.0), (x + rx, y + h - ry, math.pi / 2),
               (x + rx, y + ry, math.pi)]
    n = max(2, _arc_steps(max(rx, ry), math.pi / 2, tol))
    for cx, cy, a0 in corners:
        for k in range(n + 1):
            a = a0 + (math.pi / 2) * k / n
            pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts + [pts[0]]


def _points_attr(s: str) -> list[tuple[float, float]]:
    vals = [float(v) for v in re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', s or '')]
    return list(zip(vals[0::2], vals[1::2]))


def node_polylines(kind: str, a: dict, tol: float) -> list[list[tuple[float, float]]]:
    """One Lucide node as polylines in viewbox units."""
    if kind == 'path':
        return path_polylines(str(a.get('d') or ''), tol)
    if kind == 'circle':
        r = _num(a.get('r'))
        return [_ellipse(_num(a.get('cx')), _num(a.get('cy')), r, r, tol)] if r > 0 else []
    if kind == 'ellipse':
        rx, ry = _num(a.get('rx')), _num(a.get('ry'))
        return [_ellipse(_num(a.get('cx')), _num(a.get('cy')), rx, ry, tol)] if rx > 0 and ry > 0 else []
    if kind == 'rect':
        w, h = _num(a.get('width')), _num(a.get('height'))
        if w <= 0 or h <= 0:
            return []
        rx = a.get('rx', a.get('ry'))
        ry = a.get('ry', a.get('rx'))
        return [_rounded_rect(_num(a.get('x')), _num(a.get('y')), w, h, _num(rx), _num(ry), tol)]
    if kind == 'line':
        return [[(_num(a.get('x1')), _num(a.get('y1'))), (_num(a.get('x2')), _num(a.get('y2')))]]
    if kind in ('polyline', 'polygon'):
        pts = _points_attr(str(a.get('points') or ''))
        if len(pts) < 2:
            return []
        return [pts + [pts[0]] if kind == 'polygon' else pts]
    return []


def polylines(icon: Icon, size_pt: float, *, tolerance: float = 0.25) -> list[list[tuple[float, float]]]:
    """Every node flattened to polylines in points within a size_pt square (arcs and curves approximated), for the
    PPTX freeform builder and the PDF canvas; closed shapes repeat their first point."""
    if icon is None or size_pt <= 0:
        return []
    s = size_pt / VIEWBOX
    tol = max(tolerance, 1e-3) / s          # tolerance in viewbox units
    out = []
    for kind, attrs in icon.nodes:
        try:
            lines = node_polylines(kind, attrs, tol)
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
        for ln in lines:
            pts = [(round(px * s, 3), round(py * s, 3)) for px, py in ln]
            if len(pts) >= 2:
                out.append(pts)
    return out


def _rgb(color: str) -> tuple[int, int, int]:
    h = str(color or '000000').lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', h):
        h = '000000'
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def draw_pil(draw, icon: Icon, x: float, y: float, size: float, color: tuple, width: float) -> None:
    """Strokes an icon on a Pillow ImageDraw at (x, y), size pixels square, with round caps and joins."""
    r = width / 2
    for ln in polylines(icon, size, tolerance=max(0.2, size / 200)):
        pts = [(x + px, y + py) for px, py in ln]
        draw.line(pts, fill=color, width=max(1, int(round(width))), joint='curve')
        if r >= 1:
            for px, py in (pts[0], pts[-1]):
                draw.ellipse([px - r, py - r, px + r, py + r], fill=color)


def png(name: str, color: str, px: int) -> bytes:
    """The icon as a transparent PNG stroked in RRGGBB (thumbnails, DOCX)."""
    from PIL import Image, ImageDraw
    px = max(8, min(int(px or 24), 2048))
    icon = get(name)
    ss = 4
    big = Image.new('RGBA', (px * ss, px * ss), (0, 0, 0, 0))
    if icon is not None:
        d = ImageDraw.Draw(big)
        draw_pil(d, icon, 0, 0, px * ss, (*_rgb(color), 255), STROKE * px * ss / VIEWBOX)
    img = big.resize((px, px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()
