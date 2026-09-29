"""Image preparation (docs/PLAN-designer.md 3.4 and 9.11): smart crop around a focal point (Pillow edge/entropy map),
minimum resolution per slot, crops stored in the workspace. Photos still come from create/assets.py (Commons, licence
allow-list, credits, rule X6); nothing here downloads.

The focal point is where the picture has the most detail: an edge map (Pillow FIND_EDGES, softened) and a local
entropy map (grey-level histograms per cell) on a 64 px copy, with a gentle bias to the centre. It is not face
detection, but subjects (a person, a building, an animal) are where edges and texture gather, so a full-bleed crop
keeps them. Crops keep the focal point inside, on a third line when it is off centre.

Owner: builder V.
"""
from __future__ import annotations

import hashlib
import io
import math
import re

from .workspace import Workspace

MIN_DPI = {'pptx': 96, 'pdf': 150, 'docx': 150}
MAX_UPSCALE = 1.5
ANALYSE_PX = 64           # the focal point is found on a copy this many pixels on its longer side
CELL = 8                  # entropy cells, pixels of the analysis copy
TOP_SHARE = 0.15          # the focal point is the centre of the most salient 15 % of the picture
FLAT_STD = 4.0            # grey-level spread under which a picture counts as flat
KEEP_DPI = 1.5            # stored crops are at most this multiple of MIN_DPI (sharp without being huge)


def _open(data: bytes):
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _grey_small(img):
    from PIL import Image
    g = img.convert('L')
    w, h = g.size
    k = ANALYSE_PX / max(w, h)
    if k < 1:
        g = g.resize((max(1, round(w * k)), max(1, round(h * k))), Image.BILINEAR)
    return g


def pixels(img) -> list:
    """Every pixel of an image (Pillow 12 renamed getdata to get_flattened_data)."""
    get = getattr(img, 'get_flattened_data', None) or img.getdata
    return list(get())


def _entropy(vals: list[int]) -> float:
    if not vals:
        return 0.0
    hist = [0] * 16
    for v in vals:
        hist[v >> 4] += 1
    n = len(vals)
    return -sum(c / n * math.log2(c / n) for c in hist if c)


def saliency(data: bytes) -> tuple[list[list[float]], float]:
    """(saliency rows on the analysis copy, grey-level standard deviation) for tests and thumbnails."""
    from PIL import ImageFilter
    g = _grey_small(_open(data))
    w, h = g.size
    px = pixels(g)
    mean = sum(px) / len(px)
    std = math.sqrt(sum((v - mean) ** 2 for v in px) / len(px))
    edges = g.filter(ImageFilter.FIND_EDGES)
    ev = pixels(edges)
    for x in range(w):              # FIND_EDGES marks the frame itself; it is not detail
        ev[x] = ev[(h - 1) * w + x] = 0
    for y in range(h):
        ev[y * w] = ev[y * w + w - 1] = 0
    edges.putdata(ev)
    edges = edges.filter(ImageFilter.GaussianBlur(radius=max(1.0, max(w, h) / 32)))
    ev = pixels(edges)
    emax = max(ev) or 1
    cells: dict[tuple[int, int], float] = {}
    for cy in range(0, h, CELL):
        for cx in range(0, w, CELL):
            vals = [px[y * w + x] for y in range(cy, min(h, cy + CELL)) for x in range(cx, min(w, cx + CELL))]
            cells[(cx // CELL, cy // CELL)] = _entropy(vals)
    entmax = max(cells.values()) or 1
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            dx, dy = (x + 0.5) / w - 0.5, (y + 0.5) / h - 0.5
            prior = 1.0 - 0.35 * min(1.0, math.hypot(dx, dy) / 0.7071)
            s = 0.65 * ev[y * w + x] / emax + 0.35 * cells[(x // CELL, y // CELL)] / entmax
            row.append(s * prior)
        rows.append(row)
    return rows, std


def focal_point(data: bytes) -> tuple[float, float]:
    """(x, y) fractions of the most salient point (edge density + entropy, faces not detected); (0.5, 0.5) for flat
    images."""
    try:
        rows, std = saliency(data)
    except Exception:
        return (0.5, 0.5)
    h, w = len(rows), len(rows[0]) if rows else 0
    if not w or std < FLAT_STD:
        return (0.5, 0.5)
    flat = sorted((v for r in rows for v in r), reverse=True)
    if flat[0] - flat[-1] < 1e-3:
        return (0.5, 0.5)
    cut = flat[max(0, int(len(flat) * TOP_SHARE) - 1)]
    sx = sy = tot = 0.0
    for y, r in enumerate(rows):
        for x, v in enumerate(r):
            if v >= cut:
                sx += v * (x + 0.5)
                sy += v * (y + 0.5)
                tot += v
    if tot <= 0:
        return (0.5, 0.5)
    return (round(min(1.0, max(0.0, sx / tot / w)), 4), round(min(1.0, max(0.0, sy / tot / h)), 4))


def _third(f: float) -> float:
    """Where the focal point sits inside the crop: on a third line when it is off centre, else the middle."""
    return 1 / 3 if f < 0.4 else 2 / 3 if f > 0.6 else 0.5


def crop_for(size_px: tuple[int, int], focal: tuple[float, float], box_w: float, box_h: float,
             fit: str = 'cover') -> tuple[float, float, float, float]:
    """(x, y, w, h) fractions of the source to show in a box of that aspect: cover crops around the focal point
    (kept inside, rule-of-thirds bias); contain returns (0, 0, 1, 1)."""
    try:
        W, H = (float(v) for v in size_px)
        bw, bh = float(box_w), float(box_h)
        fx, fy = (min(1.0, max(0.0, float(v))) for v in focal)
    except (TypeError, ValueError):
        return (0.0, 0.0, 1.0, 1.0)
    if fit != 'cover' or not (W > 0 and H > 0 and bw > 0 and bh > 0) or not all(map(math.isfinite, (W, H, bw, bh))):
        return (0.0, 0.0, 1.0, 1.0)
    src, box = W / H, bw / bh
    if abs(src - box) / box < 1e-3:
        return (0.0, 0.0, 1.0, 1.0)
    if src > box:                      # wider than the box: cut the sides
        cw, ch = box / src, 1.0
    else:                              # taller: cut top and bottom
        cw, ch = 1.0, src / box
    x0 = min(max(fx - cw * _third(fx), 0.0), 1.0 - cw)
    y0 = min(max(fy - ch * _third(fy), 0.0), 1.0 - ch)
    return (round(x0, 4), round(y0, 4), round(cw, 4), round(ch, 4))


def upscale(size_px: tuple[int, int], crop: tuple[float, float, float, float], box_w: float, box_h: float,
            fmt: str) -> float:
    """How much the cropped source is enlarged at MIN_DPI[fmt] (> 1 means upscaled; D8 fails above MAX_UPSCALE)."""
    dpi = MIN_DPI.get(fmt, 150)
    try:
        W, H = (float(v) for v in size_px)
        cw, ch = float(crop[2]), float(crop[3])
        need_w, need_h = float(box_w) / 72 * dpi, float(box_h) / 72 * dpi
    except (TypeError, ValueError, IndexError):
        return float('inf')
    have_w, have_h = W * cw, H * ch
    if have_w <= 0 or have_h <= 0:
        return float('inf')
    return round(max(need_w / have_w, need_h / have_h), 4)


def focal_in(crop: tuple, focal: tuple) -> bool:
    """D8's second half: the focal point lies inside the crop."""
    x, y, w, h = crop
    return x - 1e-6 <= focal[0] <= x + w + 1e-6 and y - 1e-6 <= focal[1] <= y + h + 1e-6


def _hex(c) -> tuple[int, int, int] | None:
    s = str(c or '').lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', s):
        return None
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _source(asset: str):
    from ..create import assets as cache
    sha = str(asset or '').strip().lower()
    sha = sha.split(':', 1)[1] if ':' in sha else sha
    if not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise FileNotFoundError(f'not an asset id: {str(asset)[:80]!r}')
    path = cache.CACHE / f'{sha}.png'
    if not path.is_file():
        raise FileNotFoundError(f'asset {sha[:12]} is not in the asset cache')
    return sha, path.read_bytes()


def prepare(asset: str, box_w: float, box_h: float, ws: Workspace, *, fmt: str, fit: str = 'cover',
            mono: bool = False, duotone: tuple[str, str] | None = None) -> tuple[str, tuple, tuple]:
    """(ws ref of the cropped PNG, crop, focal) for an asset-cache image (create/assets.CACHE/<sha>.png) in a box.
    `mono` greys the picture; `duotone` (dark, light) as RRGGBB maps its greys between two colours. The stored PNG is
    never enlarged: when the crop has fewer pixels than the slot needs, the painter scales it and D8 reports it."""
    from PIL import Image, ImageOps
    sha, data = _source(asset)
    img = _open(data)
    focal = focal_point(data)
    crop = crop_for(img.size, focal, box_w, box_h, fit)
    W, H = img.size
    dpi = MIN_DPI.get(fmt, 150) * KEEP_DPI
    left, top = round(crop[0] * W), round(crop[1] * H)
    right, bottom = max(left + 1, round((crop[0] + crop[2]) * W)), max(top + 1, round((crop[1] + crop[3]) * H))
    want_w = max(1, round(float(box_w) / 72 * dpi)) if box_w and box_w > 0 else right - left
    tones = (_hex(duotone[0]), _hex(duotone[1])) if duotone else None
    mode = 'mono' if mono else ('duo-' + ''.join(duotone)) if tones and all(tones) else 'color'
    tag = hashlib.sha256(f'{sha}|{crop}|{want_w}|{mode}'.encode()).hexdigest()[:10]
    name = f'{sha[:16]}-{tag}.png'
    ref = f'ws:images/{name}'
    try:
        if ws.get(ref):
            return ref, crop, focal
    except Exception:
        pass
    out = img.convert('RGB').crop((left, top, right, bottom))
    if out.width > want_w:
        out = out.resize((want_w, max(1, round(out.height * want_w / out.width))), Image.LANCZOS)
    if tones and all(tones) and not mono:
        out = ImageOps.colorize(out.convert('L'), black=tones[0], white=tones[1])
    elif mono or (duotone and not (tones and all(tones))):
        out = out.convert('L').convert('RGB')
    buf = io.BytesIO()
    out.save(buf, 'PNG')
    ref = ws.put('images', name, buf.getvalue())
    return ref, crop, focal
