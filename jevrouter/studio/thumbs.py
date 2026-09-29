"""Thumbnails (docs/PLAN-designer.md 3.7 and 9.8): Studio's own Pillow rendering of each page from the DesignPlan,
with the same fonts (studio/fonts), geometry, images and art the painters use. Not PowerPoint's rendering; the PDF path
is exact.

Also the small shared helpers the QA, critic and freeform modules use to measure and resolve text the same way the
thumbnails draw it (`measure`, `wrap`, `text_of`, `hex_of`), so a check never disagrees with the picture.

Owner: builder Q.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from functools import lru_cache

from .plan import Box, DesignPlan, PagePlan
from .workspace import Workspace

THUMB_PX = 480             # width of a stored thumbnail
SHEET_PX = 1200            # width of a critic contact sheet
SHEET_COLS, SHEET_ROWS = 3, 2   # 6 pages per contact sheet
SAMPLE_GRID = 12           # sample() reads a 12 x 12 grid of pixels under a box

# colours used when a plan has no usable system (a hand-made or damaged plan): never crash a thumbnail
_FALLBACK = {'bg': 'FFFFFF', 'surface': 'F3F4F6', 'text': '1F2328', 'heading': '111827', 'muted': '6B7280',
             'accent': '2563EB', 'accent2': 'F59E0B', 'border': 'D1D5DB', 'header_bg': '111827',
             'header_text': 'FFFFFF', 'stripe': 'F9FAFB', 'code_bg': 'F3F4F6', 'overlay': '000000',
             'on_accent': 'FFFFFF'}
_PALETTE = ['2563EB', 'F59E0B', '059669', 'DC2626', '7C3AED', '0891B2']
_HEX = re.compile(r'^[0-9A-Fa-f]{6}$')


# ---------- shared helpers: colours, fonts, text ----------


def system_of(plan: DesignPlan):
    """The plan's DesignSystem, or None when plan.system can't be read."""
    from .tokens import DesignSystem
    try:
        return DesignSystem.from_dict(plan.system)
    except Exception:
        return None


def hex_of(name: str | None, ds, default: str = 'text') -> str:
    """A token colour name as RRGGBB (upper case). Unknown names fall back to `default`'s colour."""
    for n in (name, default):
        if not n:
            continue
        if ds is not None:
            try:
                return ds.color(n).upper().lstrip('#')
            except Exception:
                pass
        if n in _FALLBACK:
            return _FALLBACK[n]
        if n.startswith('chart') and n[5:].isdigit():
            return _PALETTE[(int(n[5:]) - 1) % 6]
        if _HEX.match(n):
            return n.upper()
    return '000000'


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


@lru_cache(maxsize=256)
def font_path(family: str | None, bold: bool = False, italic: bool = False) -> str | None:
    """The TrueType file the painters use for a family and style: the Studio font manager first (cache, then
    installed), then the legacy installed-font index; None when neither has it."""
    if not family:
        return None
    style = ('bold' if bold else '') + ('italic' if italic else '') or 'regular'
    try:
        from . import fonts
        res = fonts.resolve_local(family)
        if res is not None and res.faces:
            face = res.faces.get(style) or res.faces.get('bold' if bold else 'regular') or res.faces.get('regular') \
                or next(iter(res.faces.values()))
            return face.path
    except Exception:
        pass
    try:
        from ..create import fonts as legacy
        faces = legacy.system_index().get(legacy.key(family)) or {}
        path = faces.get(style) or faces.get('regular') or (next(iter(faces.values())) if faces else None)
        if path:
            return path
    except Exception:
        pass
    return None


def _forget_fonts() -> None:
    font_path.cache_clear()
    pil_font.cache_clear()


@lru_cache(maxsize=512)
def pil_font(family: str | None, px: int, bold: bool = False, italic: bool = False):
    """A Pillow font at a pixel size: the family's real file when there is one, else Pillow's bundled font."""
    from PIL import ImageFont
    px = max(1, int(round(px)))
    path = font_path(family, bold, italic)
    if path:
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=px)
    except Exception:
        return ImageFont.load_default()


def measure(text: str, family: str | None, size_pt: float, *, bold: bool = False, italic: bool = False,
            fmt: str = 'pptx') -> float:
    """Width in points: studio/fonts.measure (the layout engine's measure), else the thumbnail font's advance."""
    if not text:
        return 0.0
    try:
        from . import fonts
        return float(fonts.measure(text, family or 'Inter', size_pt, bold=bold, italic=italic, fmt=fmt))
    except Exception:
        pass
    f = pil_font(family, 100, bold, italic)
    try:
        return f.getlength(text) * size_pt / 100.0
    except Exception:
        return len(text) * size_pt * 0.55


def wrap(text: str, family: str | None, size_pt: float, width_pt: float, *, bold: bool = False,
         italic: bool = False, fmt: str = 'pptx') -> list[str]:
    """Greedy wrap with measure(); studio/fonts.wrap when it is built. Paragraph breaks are kept."""
    try:
        from . import fonts
        return list(fonts.wrap(text, family or 'Inter', size_pt, width_pt, bold=bold, italic=italic, fmt=fmt))
    except Exception:
        pass
    out: list[str] = []
    for para in (text or '').split('\n'):
        line = ''
        for word in para.split():
            cand = f'{line} {word}'.strip()
            if not line or measure(cand, family, size_pt, bold=bold, italic=italic, fmt=fmt) <= width_pt:
                line = cand
                continue
            out.append(line)
            line = word
        while line and measure(line, family, size_pt, bold=bold, italic=italic, fmt=fmt) > width_pt and len(line) > 1:
            cut = len(line) - 1   # a word wider than the line is broken by characters
            while cut > 1 and measure(line[:cut], family, size_pt, bold=bold, italic=italic, fmt=fmt) > width_pt:
                cut -= 1
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return out


_ITEMS = re.compile(r'^spec:(\d+)/(\d+)/items\[(\d*):(\d*)\]$')


def _block_text(b: dict) -> str:
    t = b.get('type')
    if t == 'bullets':
        return '\n'.join(i if isinstance(i, str) else str((i or {}).get('text', '')) for i in b.get('items') or [])
    if t in ('paragraph', 'quote', 'code'):
        return str(b.get('text') or '')
    if t in ('image', 'figure'):
        return str(b.get('caption') or '')
    return str(b.get('title') or '')


def _resolve(ref: str, spec: dict | None) -> str:
    """The contract's content refs resolved locally (used until/unless layout.box_text is available)."""
    if ref.startswith('text:'):
        return ref[5:]
    if not ref.startswith('spec:') or not spec:
        return ''
    body = ref[5:]
    if body in ('title', 'subtitle'):
        return str(spec.get(body) or '')
    secs = spec.get('sections') or []
    m = _ITEMS.match(ref)
    try:
        if m:
            b = secs[int(m[1])]['blocks'][int(m[2])]
            items = b.get('items') or []
            i = int(m[3]) if m[3] else 0
            j = int(m[4]) if m[4] else len(items)
            return '\n'.join(x if isinstance(x, str) else str((x or {}).get('text', '')) for x in items[i:j])
        parts = body.split('/')
        s = secs[int(parts[0])]
        if parts[1] == 'heading':
            return str(s.get('heading') or '')
        if parts[1] == 'notes':
            return str(s.get('notes') or '')
        return _block_text(s['blocks'][int(parts[1])])
    except (IndexError, KeyError, ValueError, TypeError):
        return ''


def text_of(box: Box, spec: dict | None = None) -> str:
    """What a text box says: its measured lines, else layout.box_text, else the ref resolved here."""
    if box.lines:
        return '\n'.join(box.lines)
    if spec is not None:
        try:
            from .layout import box_text
            return box_text(box, spec)
        except Exception:
            pass
    return _resolve(box.content or 'none', spec)


# ---------- images ----------


def load_image(ref: str | None, ws: Workspace | None, *, ds=None, size_px: tuple[int, int] = (256, 256)):
    """A PIL image for a content ref (ws:, asset:, art:), or None when it can't be read."""
    from PIL import Image
    if not ref:
        return None
    data = None
    try:
        if ref.startswith('ws:') and ws is not None:
            data = ws.get(ref)
        elif ref.startswith('asset:'):
            from ..create.assets import CACHE
            sha = ref[6:]
            if re.fullmatch(r'[0-9a-f]{64}', sha):
                p = CACHE / f'{sha}.png'
                data = p.read_bytes() if p.is_file() else None
        elif ref.startswith('art:') and ds is not None:
            _, kind, seed = (ref.split(':') + ['0'])[:3]
            from . import art
            data = art.png(kind, int(seed or 0), max(1, size_px[0]), max(1, size_px[1]), ds)
    except Exception:
        data = None
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im.convert('RGBA')
    except Exception:
        return None


def _fit_image(im, box: Box, w: int, h: int):
    """The image as painted into a w x h pixel box: its crop (unless the stored file is already cropped to the box's
    aspect), then cover around the focal point or contain."""
    from PIL import Image
    w, h = max(1, w), max(1, h)
    box_aspect = w / h
    iw, ih = im.size
    if box.crop and abs(iw / ih - box_aspect) > 0.02 * box_aspect:
        x, y, cw, ch = box.crop
        l, t = int(x * iw), int(y * ih)
        r, b = max(l + 1, int((x + cw) * iw)), max(t + 1, int((y + ch) * ih))
        im = im.crop((l, t, min(r, iw), min(b, ih)))
        iw, ih = im.size
    if (box.fit or 'cover') == 'contain':
        k = min(w / iw, h / ih)
        nw, nh = max(1, int(iw * k)), max(1, int(ih * k))
        canvas = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        canvas.paste(im.resize((nw, nh), Image.LANCZOS), ((w - nw) // 2, (h - nh) // 2))
        return canvas
    k = max(w / iw, h / ih)
    nw, nh = max(w, int(round(iw * k))), max(h, int(round(ih * k)))
    im = im.resize((nw, nh), Image.LANCZOS)
    fx, fy = box.focal or (0.5, 0.5)
    left = min(max(0, int(fx * nw - w / 2)), nw - w)
    top = min(max(0, int(fy * nh - h / 2)), nh - h)
    return im.crop((left, top, left + w, top + h))


def _mask(w: int, h: int, radius: float):
    from PIL import Image, ImageDraw
    m = Image.new('L', (max(1, w), max(1, h)), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=max(0, int(radius)), fill=255)
    return m


# ---------- rendering ----------


def _paste(canvas, im, x: int, y: int, opacity: float = 1.0, radius: float = 0.0):
    from PIL import Image
    if im.mode != 'RGBA':
        im = im.convert('RGBA')
    if opacity < 1 or radius > 0:
        alpha = im.getchannel('A')
        if radius > 0:
            from PIL import ImageChops
            alpha = ImageChops.multiply(alpha, _mask(im.width, im.height, radius))
        if opacity < 1:
            alpha = alpha.point(lambda a: int(a * max(0.0, opacity)))
        im = im.copy()
        im.putalpha(alpha)
    layer = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
    layer.paste(im, (x, y))
    canvas.alpha_composite(layer)


def _shape(canvas, box: Box, s: float, ds, *, fill: str | None, stroke: str | None):
    from PIL import Image, ImageDraw
    x, y, w, h = int(round(box.x * s)), int(round(box.y * s)), int(round(box.w * s)), int(round(box.h * s))
    if w < 1 or h < 1:
        return
    layer = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    a = int(255 * max(0.0, min(1.0, box.style.opacity)))
    r = max(0, int(box.style.radius * s))
    sw = max(1, int(round(box.style.stroke_w * s))) if stroke and box.style.stroke_w else 0
    d.rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=min(r, w // 2, h // 2),
                        fill=(*_rgb(hex_of(fill, ds)), a) if fill else None,
                        outline=(*_rgb(hex_of(stroke, ds)), a) if sw else None, width=sw)
    canvas.alpha_composite(layer)


def _placeholder(canvas, box: Box, s: float, ds, kind: str):
    """Diagrams, charts and tables without a rendered picture: a card with a simple glyph of the kind, so balance and
    density read right on the thumbnail."""
    from PIL import ImageDraw
    x, y, w, h = box.x * s, box.y * s, box.w * s, box.h * s
    if w < 2 or h < 2:
        return
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=int(min(w, h) * 0.04),
                        fill=_rgb(hex_of('surface', ds)), outline=_rgb(hex_of('border', ds)))
    ink = _rgb(hex_of('accent', ds))
    pad = min(w, h) * 0.12
    if kind == 'chart':
        n = 5
        bw = (w - 2 * pad) / (n * 1.6)
        for i in range(n):
            bh = (h - 2 * pad) * (0.35 + 0.13 * ((i * 7) % 5))
            bx = x + pad + i * bw * 1.6
            d.rectangle((bx, y + h - pad - bh, bx + bw, y + h - pad), fill=_rgb(hex_of(f'chart{i % 6 + 1}', ds)))
    elif kind == 'table':
        rows, cols = 4, 3
        for r_ in range(rows + 1):
            yy = y + pad + r_ * (h - 2 * pad) / rows
            d.line((x + pad, yy, x + w - pad, yy), fill=ink, width=1)
        for c in range(cols + 1):
            xx = x + pad + c * (w - 2 * pad) / cols
            d.line((xx, y + pad, xx, y + h - pad), fill=ink, width=1)
    else:
        n = 3
        r_ = min((w - 2 * pad) / (n * 3), (h - 2 * pad) / 3)
        cy = y + h / 2
        for i in range(n):
            cx = x + pad + (i * 2 + 1) * (w - 2 * pad) / (n * 2)
            d.ellipse((cx - r_, cy - r_, cx + r_, cy + r_), outline=ink, width=max(1, int(r_ / 6)))
            if i:
                d.line((cx - (w - 2 * pad) / n + r_, cy, cx - r_, cy), fill=ink, width=max(1, int(r_ / 8)))


def _icon(canvas, box: Box, s: float, ds):
    from PIL import Image, ImageDraw
    name = (box.content or '')[5:] if (box.content or '').startswith('icon:') else ''
    colour = hex_of(box.style.text_color or box.style.stroke or 'accent', ds, 'accent')
    x, y = int(round(box.x * s)), int(round(box.y * s))
    px = max(1, int(round(min(box.w, box.h) * s)))
    try:
        from . import icons
        data = icons.png(name, colour, px)
        im = Image.open(io.BytesIO(data)).convert('RGBA')
        _paste(canvas, im, x + (int(box.w * s) - im.width) // 2, y + (int(box.h * s) - im.height) // 2)
        return
    except Exception:
        pass
    d = ImageDraw.Draw(canvas)
    d.ellipse((x, y, x + px - 1, y + px - 1), outline=_rgb(colour), width=max(1, px // 10))


def _text(canvas, box: Box, s: float, ds, spec, fmt: str):
    from PIL import ImageDraw
    size = box.size or (ds.size(box.step or 'body', fmt) if ds is not None and box.step else 18.0)
    text = text_of(box, spec)
    if not text.strip():
        return
    lines = box.lines or wrap(text, box.font, size, box.w, bold=box.bold, italic=box.italic, fmt=fmt)
    f = pil_font(box.font, max(1, size * s), box.bold, box.italic)
    colour = _rgb(hex_of(box.style.text_color or 'text', ds))
    adv = size * (box.line_height or 1.2) * s
    total = adv * len(lines)
    top = box.y * s + {'top': 0, 'middle': (box.h * s - total) / 2, 'bottom': box.h * s - total}.get(box.valign, 0)
    try:
        asc, desc = f.getmetrics()
    except Exception:
        asc, desc = size * s * 0.8, size * s * 0.2
    d = ImageDraw.Draw(canvas)
    for i, line in enumerate(lines):
        base = top + i * adv + (adv - (asc + desc)) / 2 + asc
        if box.align == 'center':
            xy, anchor = (box.x * s + box.w * s / 2, base), 'ms'
        elif box.align == 'right':
            xy, anchor = (box.x * s + box.w * s, base), 'rs'
        else:
            xy, anchor = (box.x * s, base), 'ls'
        try:
            d.text(xy, line, font=f, fill=colour, anchor=anchor)
        except Exception:
            d.text((xy[0], base - asc), line, font=f, fill=colour)


def _order(page: PagePlan) -> list[Box]:
    return [b for _, b in sorted(enumerate(page.boxes), key=lambda t: (t[1].z, t[0]))]


def _render(plan: DesignPlan, page: int, ws: Workspace | None, *, width_px: int = THUMB_PX, spec: dict | None = None,
            text: bool = True):
    """The page as a PIL RGB image. text=False leaves text out: what is *under* the text, for contrast sampling."""
    from PIL import Image
    p = plan.pages[page]
    ds = system_of(plan)
    s = width_px / max(1.0, p.w)
    size = (max(1, int(round(p.w * s))), max(1, int(round(p.h * s))))
    bg = p.background or 'bg'
    canvas = Image.new('RGBA', size, (*_rgb(hex_of(bg if bg in _FALLBACK or bg.startswith('chart') else 'bg', ds, 'bg')),
                                      255))
    if ':' in bg:
        im = load_image(bg, ws, ds=ds, size_px=size)
        if im is not None:
            _paste(canvas, _fit_image(im, Box('bg', 'image', 0, 0, p.w, p.h), *size), 0, 0)
    for b in _order(p):
        k = b.kind
        if k == 'text':
            if text:
                _text(canvas, b, s, ds, spec, plan.format)
            continue
        w, h = int(round(b.w * s)), int(round(b.h * s))
        if w < 1 or h < 1:
            continue
        ref = b.content or 'none'
        if k == 'icon' or ref.startswith('icon:'):
            _icon(canvas, b, s, ds)
            continue
        if k == 'shape':
            if ref.startswith(('art:', 'ws:', 'asset:')):
                im = load_image(ref, ws, ds=ds, size_px=(w, h))
                if im is not None:
                    _paste(canvas, _fit_image(im, b, w, h), int(round(b.x * s)), int(round(b.y * s)),
                           b.style.opacity, b.style.radius * s)
            _shape(canvas, b, s, ds, fill=b.style.fill, stroke=b.style.stroke)
            continue
        im = load_image(ref, ws, ds=ds, size_px=(w, h)) if ref.startswith(('ws:', 'asset:', 'art:')) else None
        if im is not None:
            _paste(canvas, _fit_image(im, b, w, h), int(round(b.x * s)), int(round(b.y * s)), b.style.opacity,
                   b.style.radius * s)
            if b.style.stroke and b.style.stroke_w:
                _shape(canvas, b, s, ds, fill=None, stroke=b.style.stroke)
        elif k == 'image':
            _shape(canvas, Box(b.id, 'shape', b.x, b.y, b.w, b.h, style=b.style), s, ds, fill='muted', stroke=None)
        else:
            _placeholder(canvas, b, s, ds, k)
    return canvas.convert('RGB')


def _png(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=False)
    return buf.getvalue()


def render_page(plan: DesignPlan, page: int, ws: Workspace, *, width_px: int = THUMB_PX, spec: dict | None = None) -> bytes:
    """A PNG of one 0-based page."""
    return _png(_render(plan, page, ws, width_px=width_px, spec=spec))


def render_background(plan: DesignPlan, page: int, ws: Workspace | None, *, width_px: int = 240) -> bytes:
    """A PNG of one page without its text: what D3 samples under text that sits on a photo or art."""
    return _png(_render(plan, page, ws, width_px=width_px, text=False))


def render_all(plan: DesignPlan, ws: Workspace, *, width_px: int = THUMB_PX, spec: dict | None = None) -> list[str]:
    """Every page rendered and stored with ws.save_thumb; returns the refs in page order."""
    return [ws.save_thumb(i, render_page(plan, i, ws, width_px=width_px, spec=spec)) for i in range(len(plan.pages))]


def contact_sheet(pngs: list[bytes], *, cols: int = SHEET_COLS, rows: int = SHEET_ROWS, width_px: int = SHEET_PX,
                  first_page: int = 1) -> bytes:
    """Up to cols*rows thumbnails on one PNG, each labelled with its 1-based page number (the critic's input)."""
    from PIL import Image, ImageDraw
    ims = []
    for data in list(pngs)[:cols * rows]:
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
            ims.append(im.convert('RGB'))
        except Exception:
            ims.append(Image.new('RGB', (16, 9), (200, 200, 200)))
    pad = max(8, width_px // 100)
    cell_w = max(1, (width_px - (cols + 1) * pad) // cols)
    thumbs = [im.resize((cell_w, max(1, round(im.height * cell_w / im.width))), Image.LANCZOS) for im in ims]
    cell_h = max((t.height for t in thumbs), default=max(1, cell_w * 9 // 16))
    used_rows = max(1, -(-len(thumbs) // cols))
    sheet = Image.new('RGB', (width_px, used_rows * (cell_h + pad) + pad), (229, 231, 235))
    d = ImageDraw.Draw(sheet)
    label = pil_font(None, max(10, cell_w // 14), True)
    for i, t in enumerate(thumbs):
        x = pad + (i % cols) * (cell_w + pad)
        y = pad + (i // cols) * (cell_h + pad)
        sheet.paste(t, (x, y + (cell_h - t.height) // 2))
        n = str(first_page + i)
        tw = d.textlength(n, font=label)
        bh = max(12, cell_w // 10)
        d.rectangle((x, y, x + tw + bh * 0.6, y + bh), fill=(17, 24, 39))
        d.text((x + bh * 0.3, y + bh / 2), n, font=label, fill=(255, 255, 255), anchor='lm')
    return _png(sheet)


def sample(png: bytes, box: tuple[float, float, float, float], page_size: tuple[float, float]) -> list[str]:
    """RRGGBB colours sampled under a box (points) on a page thumbnail: D3 contrast over photos."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert('RGB')
    sx, sy = im.width / max(1.0, page_size[0]), im.height / max(1.0, page_size[1])
    x, y, w, h = box
    l, t = max(0, int(x * sx)), max(0, int(y * sy))
    r, b = min(im.width, int(round((x + w) * sx))), min(im.height, int(round((y + h) * sy)))
    if r <= l or b <= t:
        return []
    out, seen = [], set()
    n = SAMPLE_GRID
    for j in range(n):
        for i in range(n):
            px = min(r - 1, l + int((i + 0.5) * (r - l) / n))
            py = min(b - 1, t + int((j + 0.5) * (b - t) / n))
            c = '%02X%02X%02X' % im.getpixel((px, py))[:3]
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def preset_thumb(preset_id: str, *, width_px: int = 320) -> bytes:
    """A sample cover slide in a preset (GET /api/design/presets/{id}/thumb), cached in data/cache/design/presets/."""
    from . import presets
    from .plan import BoxStyle
    from .workspace import DESIGN_DIR
    if preset_id not in presets.PRESETS:
        raise KeyError(preset_id)
    ds = presets.get(preset_id)
    digest = hashlib.sha256(json.dumps(ds.to_dict(), sort_keys=True, default=str).encode()).hexdigest()[:12]
    cache = DESIGN_DIR / 'presets' / f'{preset_id}-{int(width_px)}-{digest}.png'
    try:
        if cache.is_file():
            return cache.read_bytes()
    except OSError:
        pass
    W, H, m = 960.0, 540.0, ds.spacing.slide_margin
    fams = ds.families
    try:
        from . import art
        art_ref = f'art:{art.pick(ds, [], 1)}:1'
    except Exception:
        art_ref = 'none'
    boxes = [Box('p0.art', 'shape', W * 7 / 12, 0, W * 5 / 12, H, z=0, slot='art', content=art_ref, bleed=True,
                 style=BoxStyle(fill=None if art_ref != 'none' else 'accent', opacity=1.0))]
    w = W * 7 / 12 - 2 * m
    parts = []
    for slot, txt, step, fam, bold in (('kicker', 'YEAR 9 BIOLOGY', 'caption', fams.caption, True),
                                       ('title', 'Photosynthesis', 'display', fams.display, True),
                                       ('subtitle', 'How plants turn light into food', 'lead', fams.body, False)):
        steps = ('display', 'h1', 'h2') if step == 'display' else (step,)
        for st in steps:   # the largest step whose words all fit the line (no word broken in two)
            size = ds.size(st, 'pptx')
            if all(measure(word, fam, size, bold=bold) <= w for word in txt.split()) or st == steps[-1]:
                break
        lh = ds.scale.display_line_height if st in ('display', 'h1') else ds.scale.line_height
        lines = wrap(txt, fam, size, w, bold=bold)
        parts.append((slot, txt, st, fam, bold, size, lines, lh, len(lines) * size * lh))
    gap = ds.spacing.unit * 2
    y = max(m, (H - sum(p[-1] for p in parts) - gap * (len(parts) - 1)) / 2)
    for slot, txt, st, fam, bold, size, lines, lh, h in parts:
        boxes.append(Box(f'p0.{slot}', 'text', m, y, w, h, z=2, slot=slot, content=f'text:{txt}', font=fam, step=st,
                         size=size, bold=bold, lines=lines, line_height=lh,
                         style=BoxStyle(text_color='heading' if slot == 'title' else 'muted' if slot == 'kicker'
                                        else 'text')))
        y += h + gap
    plan = DesignPlan('000000000000', 'pptx', preset_id, ds.to_dict(), {'preset': preset_id, 'pages': []},
                      pages=[PagePlan(0, 'cover-type', W, H, boxes=boxes)])
    png = render_page(plan, 0, None, width_px=width_px)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix('.tmp')
        tmp.write_bytes(png)
        tmp.replace(cache)
    except OSError:
        pass
    return png


try:
    from . import fonts as _fonts_mod
    _fonts_mod.on_clear(_forget_fonts)
except Exception:   # pragma: no cover
    pass
