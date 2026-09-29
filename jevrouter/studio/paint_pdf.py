"""The PDF painter (docs/PLAN-designer.md 2, 3.3 and 9.6): draws a positioned DesignPlan with the reportlab canvas,
embedding subset TrueType fonts from studio/fonts, vector shapes/icons/diagrams, and the workspace image crops. Makes no
layout decisions. Rules F1/A1-A4 and X1-X3 hold exactly as in create/render._pdf (tagged headings as outline entries).

Owner: builder L.
"""
from __future__ import annotations

import importlib
import io
import os

from .plan import DesignPlan
from .workspace import Workspace

AUTHOR = 'TraceGraph'
STYLE = {(False, False): 'regular', (True, False): 'bold', (False, True): 'italic', (True, True): 'bolditalic'}
_REGISTERED: dict[str, str] = {}     # font file path -> reportlab font name


def _render():
    return importlib.import_module('jevrouter.create.render')


def ttf_faces(family: str) -> dict[str, str]:
    """style -> TrueType file of a family studio/fonts can find locally (cache or system); {} when none. Only files
    reportlab can embed (TrueType outlines) are returned."""
    try:
        from . import fonts
        res = fonts.resolve_local(family)
    except Exception:
        res = None
    if res is None or not getattr(res, 'faces', None):
        return {}
    out = {}
    for style, face in res.faces.items():
        path = getattr(face, 'path', None)
        if path and os.path.exists(path) and str(path).lower().endswith(('.ttf', '.otf')):
            out[style] = path
    return out


class Fonts:
    """Registered faces per (family, bold, italic): the family's TrueType files (subset and embedded), or the PDF
    standard font that stood in for it when the layout measured; plus the Unicode fallback for characters the face
    lacks."""

    def __init__(self):
        self.names: dict[tuple, str] = {}
        self.glyphs: dict[str, frozenset | None] = {}
        self.uni = None
        try:
            self.uni = _render().unicode_font()
        except Exception:
            self.uni = None

    def face(self, family: str, bold: bool = False, italic: bool = False) -> str:
        key = (family, bool(bold), bool(italic))
        if key in self.names:
            return self.names[key]
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from .layout import pdf_face_ok, std_face
        name = None
        faces = ttf_faces(family) if pdf_face_ok(family) else {}
        style = STYLE[(bool(bold), bool(italic))]
        path = faces.get(style) or (faces.get('bold') if bold else None) or faces.get('regular')
        if path:
            if path in _REGISTERED:
                name = _REGISTERED[path]
            else:
                cand = 'TGS' + str(len(_REGISTERED) + 1)
                try:
                    pdfmetrics.registerFont(TTFont(cand, path))
                    _REGISTERED[path] = cand
                    name = cand
                except Exception:
                    name = None
        if name is None:
            name = std_face(family, bold, italic)
            self.glyphs.setdefault(name, None)
        else:
            try:
                self.glyphs.setdefault(name, frozenset(pdfmetrics.getFont(name).face.charToGlyph))
            except Exception:
                self.glyphs.setdefault(name, frozenset())
        self.names[key] = name
        return name

    def can(self, name: str, ch: str) -> bool:
        g = self.glyphs.get(name)
        if g is None:
            try:
                ch.encode('cp1252')
                return True
            except UnicodeEncodeError:
                return False
        return ord(ch) in g

    def pieces(self, text: str, name: str, bold: bool) -> list[tuple[str, str]]:
        """(text, font name) runs: the face where it has the glyphs, the Unicode font (or '?') where it doesn't."""
        out: list[tuple[str, str]] = []
        uni = (self.uni[1] if bold else self.uni[0]) if self.uni else None
        uni_glyphs = None
        if uni:
            try:
                uni_glyphs = _render().unicode_glyphs()
            except Exception:
                uni_glyphs = frozenset()
        for ch in text:
            if self.can(name, ch):
                f, c = name, ch
            elif uni and uni_glyphs and ord(ch) in uni_glyphs:
                f, c = uni, ch
            else:
                f, c = name, '?'
            if out and out[-1][1] == f:
                out[-1] = (out[-1][0] + c, f)
            else:
                out.append((c, f))
        return out


class _ChartFonts:
    """The two methods render._pdf_chart needs, on top of the Studio fonts."""

    def __init__(self, fonts: Fonts, family: str):
        self.fonts, self.family = fonts, family

    def fit(self, text: str):
        name = self.fonts.face(self.family)
        if all(self.fonts.can(name, ch) for ch in text):
            return text, False
        if self.fonts.uni:
            return text, True
        return _render().pdf_safe(text), False

    def name(self, text: str, bold: bool = False) -> str:
        _, uni = self.fit(text)
        if uni:
            return self.fonts.uni[1] if bold else self.fonts.uni[0]
        return self.fonts.face(self.family, bold)


class _Painter:
    def __init__(self, plan: DesignPlan, spec: dict, ws: Workspace):
        from .tokens import DesignSystem
        self.plan, self.spec, self.ws = plan, spec, ws
        self.ds = DesignSystem.from_dict(plan.system)
        self.fonts = Fonts()
        self.outline_level = 0
        self.keys = 0

    def color(self, token: str | None, default: str = 'text'):
        from reportlab.lib import colors
        return colors.HexColor('#' + self.ds.color(token or default))

    # coordinates: the plan is top-left, the PDF bottom-left
    def Y(self, y: float, h: float = 0.0) -> float:
        return self.page_h - y - h

    # ----- text -----
    def text(self, c, bx):
        from .layout import box_text, bullet_indent, bullet_of, line_box, plain_line, wrap_runs
        text = box_text(bx, self.spec)
        if not text and not bx.lines:
            return
        size = float(bx.size or self.ds.size(bx.step or 'body', 'pdf'))
        family = bx.font or self.ds.families.body
        marker = bullet_of(bx, self.spec)
        cont = self.continues_bullet(bx)
        indent = bullet_indent(size, bool(marker and marker != '•')) if (marker or cont) else 0.0
        lines = wrap_runs(text, family, size, max(8.0, bx.w - indent), bold=bx.bold, italic=bx.italic, fmt='pdf')
        if bx.lines is not None and [plain_line(ln) for ln in lines] != list(bx.lines):
            lines = [[(ln, bool(bx.bold), bool(bx.italic))] for ln in bx.lines]   # draw what the layout measured
        asc, desc, adv = line_box(family, size, bx.line_height)
        total = len(lines) * adv
        top = bx.y if bx.valign == 'top' else (bx.y + (bx.h - total) / 2 if bx.valign == 'middle' else
                                              bx.y + bx.h - total)
        if bx.style.fill:
            c.saveState()
            c.setFillColor(self.color(bx.style.fill))
            pad = max(2.0, size * 0.3)
            c.rect(bx.x - pad, self.Y(bx.y - pad, bx.h + 2 * pad), bx.w + 2 * pad, bx.h + 2 * pad, stroke=0, fill=1)
            c.restoreState()
        c.setFillColor(self.color(bx.style.text_color))
        from reportlab.pdfbase.pdfmetrics import stringWidth
        tracking = self.ds.scale.display_tracking * size if bx.step in ('display', 'h1') else 0.0
        for i, line in enumerate(lines):
            base = top + i * adv + (adv - (asc + desc)) / 2 + asc
            runs = []
            for t, b, it in line:
                name = self.fonts.face(family, b, it)
                runs += [(piece, f) for piece, f in self.fonts.pieces(t, name, b)]
            width = sum(stringWidth(t, f, size) for t, f in runs) + tracking * max(0, sum(len(t) for t, _ in runs) - 1)
            x = bx.x + indent
            avail = bx.w - indent
            if bx.align == 'center':
                x += (avail - width) / 2
            elif bx.align == 'right':
                x += avail - width
            if i == 0 and marker:
                name = self.fonts.face(family, bx.bold, False)
                c.setFont(name, size)
                c.drawString(bx.x, self.Y(base), marker)
            for t, f in runs:
                c.setFont(f, size)
                if tracking:
                    obj = c.beginText(x, self.Y(base))
                    obj.setFont(f, size)
                    obj.setCharSpace(tracking)
                    obj.textOut(t)
                    c.drawText(obj)
                    x += stringWidth(t, f, size) + tracking * len(t)
                else:
                    c.drawString(x, self.Y(base), t)
                    x += stringWidth(t, f, size)

    def continues_bullet(self, bx) -> bool:
        from .layout import block_of, parse_ref
        r = parse_ref(bx.content)
        blk = block_of(bx.content, self.spec)
        return bool(r and r.items and r.words and blk and blk.get('type') == 'bullets')

    def outline(self, c, bx):
        """Outline entries (F1, A1): the title once, every section heading once (not its continued repeats)."""
        from ..create.spec import strip_emphasis
        from .layout import parse_ref
        r = parse_ref(bx.content)
        if r is None or r.field is not None:
            return
        if r.top == 'title':
            level, text = 0, str(self.spec.get('title') or '')
        elif r.part == 'heading':
            sec = self.spec['sections'][r.s]
            level, text = int(sec.get('level') or 1), str(sec.get('heading') or '')
        else:
            return
        if not text:
            return
        level = max(0, min(level, self.outline_level + 1))
        self.keys += 1
        key = f'h{self.keys}'
        c.bookmarkHorizontal(key, 0, self.Y(bx.y) + 4)
        c.addOutlineEntry(_render()._short(strip_emphasis(text), _render().MAX_PDF_HEADING), key, level=level,
                          closed=False)
        self.outline_level = level

    # ----- shapes and art -----
    def rect(self, c, x, y, w, h, *, fill=None, stroke=None, stroke_w=0.0, opacity=1.0, radius=0.0, ellipse=False):
        c.saveState()
        if fill:
            c.setFillColor(self.color(fill))
            c.setFillAlpha(max(0.0, min(1.0, float(opacity))))
        if stroke and stroke_w:
            c.setStrokeColor(self.color(stroke))
            c.setStrokeAlpha(max(0.0, min(1.0, float(opacity))))
            c.setLineWidth(stroke_w)
        f, s = (1 if fill else 0), (1 if stroke and stroke_w else 0)
        if f or s:
            if ellipse:
                c.ellipse(x, self.Y(y, h), x + w, self.Y(y, h) + h, stroke=s, fill=f)
            elif radius > 0:
                c.roundRect(x, self.Y(y, h), w, h, min(radius, w / 2, h / 2), stroke=s, fill=f)
            else:
                c.rect(x, self.Y(y, h), w, h, stroke=s, fill=f)
        c.restoreState()

    def poly(self, c, points, *, stroke=None, stroke_w=0.0, fill=None, closed=False, opacity=1.0):
        pts = [(float(a), float(b)) for a, b in points]
        if len(pts) < 2:
            return
        c.saveState()
        p = c.beginPath()
        p.moveTo(pts[0][0], self.Y(pts[0][1]))
        for a, b in pts[1:]:
            p.lineTo(a, self.Y(b))
        if closed:
            p.close()
        f = 1 if fill and closed else 0
        s = 1 if stroke and stroke_w else 0
        if f:
            c.setFillColor(self.color(fill))
            c.setFillAlpha(float(opacity))
        if s:
            c.setStrokeColor(self.color(stroke))
            c.setStrokeAlpha(float(opacity))
            c.setLineWidth(stroke_w)
            c.setLineCap(1)
            c.setLineJoin(1)
        if f or s:
            c.drawPath(p, stroke=s, fill=f)
        c.restoreState()

    def gradient(self, c, prim, ox, oy, opacity):
        import math
        x, y, w, h = (float(prim.get(k, 0)) for k in ('x', 'y', 'w', 'h'))
        x, y = ox + x, oy + y
        ang = math.radians(float(prim.get('angle', 0)))
        cx, cy = x + w / 2, self.Y(y, h) + h / 2
        dx, dy = math.cos(ang) * w / 2, math.sin(ang) * h / 2
        c.saveState()
        p = c.beginPath()
        p.rect(x, self.Y(y, h), w, h)
        c.clipPath(p, stroke=0, fill=0)
        c.setFillAlpha(float(prim.get('opacity', 1.0)) * opacity)
        c.linearGradient(cx - dx, cy - dy, cx + dx, cy + dy,
                         (self.color(prim.get('from') or 'accent'), self.color(prim.get('to') or 'accent2')),
                         extend=True)
        c.restoreState()

    def art(self, c, bx):
        _, kind, seed = (bx.content.split(':') + ['', ''])[:3]
        try:
            from . import art
            prims = art.draw(kind, int(seed or 0), bx.w, bx.h, self.ds)
        except Exception:
            prims = [{'type': 'ellipse', 'x': bx.w * 0.35, 'y': bx.h * 0.15, 'w': bx.w * 0.6, 'h': bx.w * 0.6,
                      'fill': 'accent', 'opacity': 0.25}]
        op = float(bx.style.opacity if bx.style.opacity is not None else 1.0)
        c.saveState()
        p = c.beginPath()
        p.rect(bx.x, self.Y(bx.y, bx.h), bx.w, bx.h)
        c.clipPath(p, stroke=0, fill=0)
        for pr in prims:
            t = pr.get('type')
            o = float(pr.get('opacity', 1.0)) * op
            if t in ('rect', 'ellipse'):
                self.rect(c, bx.x + float(pr.get('x', 0)), bx.y + float(pr.get('y', 0)), float(pr.get('w', 0)),
                          float(pr.get('h', 0)), fill=pr.get('fill'), stroke=pr.get('stroke'),
                          stroke_w=float(pr.get('stroke_w') or 0), opacity=o, radius=float(pr.get('radius') or 0),
                          ellipse=t == 'ellipse')
            elif t in ('line', 'path'):
                self.poly(c, [(bx.x + float(a), bx.y + float(b)) for a, b in pr.get('points') or []],
                          stroke=pr.get('stroke'), stroke_w=float(pr.get('stroke_w') or 0), fill=pr.get('fill'),
                          closed=bool(pr.get('closed')), opacity=o)
            elif t == 'gradient':
                self.gradient(c, pr, bx.x, bx.y, op)
        c.restoreState()

    def icon(self, c, bx):
        name = bx.content[5:]
        color = bx.style.stroke or 'accent'
        try:
            from . import icons
            ic = icons.get(name)
            if ic is None:
                raise LookupError(name)
            size = min(bx.w, bx.h)
            width = max(0.75, float(getattr(icons, 'STROKE', 2.0)) * size / float(getattr(icons, 'VIEWBOX', 24)))
            for pl in icons.polylines(ic, size):
                pts = [(bx.x + a, bx.y + b) for a, b in pl]
                closed = len(pts) > 2 and pts[0] == pts[-1]
                self.poly(c, pts[:-1] if closed else pts, stroke=color, stroke_w=width, closed=closed)
            return
        except Exception:
            pass
        if name == 'quote':
            c.saveState()
            c.setFillColor(self.color(color))
            size = max(24.0, bx.h * 1.6)
            c.setFont(self.fonts.face(self.ds.families.display, True), size)
            c.drawString(bx.x, self.Y(bx.y) - size * 0.75, '“' if self.fonts.can(
                self.fonts.face(self.ds.families.display, True), '“') else '"')
            c.restoreState()

    # ----- figures -----
    def picture(self, c, bx):
        from reportlab.lib.utils import ImageReader
        from .paint_pptx import image_bytes
        data = image_bytes(bx, self.ws, mono=bool(self.ds.grey_images))
        img = ImageReader(io.BytesIO(data))
        c.saveState()
        if bx.style.radius and bx.style.radius > 0:
            p = c.beginPath()
            p.roundRect(bx.x, self.Y(bx.y, bx.h), bx.w, bx.h, min(bx.style.radius, bx.w / 2, bx.h / 2))
            c.clipPath(p, stroke=0, fill=0)
        c.drawImage(img, bx.x, self.Y(bx.y, bx.h), bx.w, bx.h, mask='auto')
        c.restoreState()
        if bx.style.stroke and bx.style.stroke_w:
            self.rect(c, bx.x, bx.y, bx.w, bx.h, stroke=bx.style.stroke, stroke_w=bx.style.stroke_w)

    def chart(self, c, bx):
        from reportlab.graphics import renderPDF
        from .layout import block_of, theme_dict
        b = block_of(bx.content, self.spec)
        if not b or b.get('type') != 'chart':
            raise ValueError(f'{bx.content} is not a chart')
        s = min(bx.h / 230.0, bx.w / 320.0, 1.6)
        w0 = bx.w / s
        d = _render()._pdf_chart(b, theme_dict(self.ds), _ChartFonts(self.fonts, self.ds.families.body), w0)
        c.saveState()
        c.translate(bx.x, self.Y(bx.y, d.height * s))
        c.scale(s, s)
        renderPDF.draw(d, c, 0, 0)
        c.restoreState()

    def diagram(self, c, bx):
        from reportlab.graphics import renderPDF
        from ..create import diagram
        from .layout import block_of, theme_dict
        b = block_of(bx.content, self.spec)
        if not b:
            raise ValueError(f'{bx.content} is not a block')
        body = self.ds.families.body
        fa = _ChartFonts(self.fonts, body)

        def fit(text, bold):
            drawn, uni = fa.fit(text)
            return drawn, fa.name(text, bold)
        d = diagram.pdf_drawing(b, theme_dict(self.ds), bx.w, max_height=bx.h, fit=fit,
                                font=self.fonts.face(body), bold=self.fonts.face(body, True))
        renderPDF.draw(d, c, bx.x, self.Y(bx.y, d.height))

    def table(self, c, bx):
        from .layout import block_of, line_box, parse_ref, table_cells, table_geometry
        b = block_of(bx.content, self.spec)
        if not b:
            raise ValueError(f'{bx.content} is not a block')
        r = parse_ref(bx.content)
        cols, rows = table_cells(b)
        rng = r.rows or (0, len(rows))
        size = float(bx.size or self.ds.size('body', 'pdf'))
        g = table_geometry(b, rng, bx.w, size, self.ds, 'pdf', height=bx.h)
        raw = [None] + ([(b.get('rows') or [])[rng[0] + k] for k in range(rng[1] - rng[0])]
                        if b.get('type') == 'table' else [None] * (rng[1] - rng[0]))
        is_num = _render().is_num
        from reportlab.pdfbase.pdfmetrics import stringWidth
        asc, desc, adv = line_box(g.family, size, 1.2)
        y = bx.y
        for i, (h, cells) in enumerate(zip(g.heights, g.cells)):
            fill = 'header_bg' if i == 0 else ('stripe' if i % 2 == 0 else 'bg')
            self.rect(c, bx.x, y, sum(g.cols), h, fill=fill)
            x = bx.x
            for j, lines in enumerate(cells):
                src = raw[i][j] if i and raw[i] is not None and j < len(raw[i]) else None
                right = bool(i and is_num(src))
                c.setFillColor(self.color('header_text' if i == 0 else 'text'))
                for k, ln in enumerate(lines):
                    base = y + (g.vpad or g.pad) + k * adv + (adv - (asc + desc)) / 2 + asc
                    name = self.fonts.face(g.family, i == 0)
                    pieces = self.fonts.pieces(ln, name, i == 0)
                    width = sum(stringWidth(t, f, size) for t, f in pieces)
                    tx = x + g.cols[j] - g.pad - width if right else x + g.pad
                    for t, f in pieces:
                        c.setFont(f, size)
                        c.drawString(tx, self.Y(base), t)
                        tx += stringWidth(t, f, size)
                x += g.cols[j]
            y += h
            c.saveState()
            c.setStrokeColor(self.color('border'))
            c.setLineWidth(0.4)
            c.line(bx.x, self.Y(y), bx.x + sum(g.cols), self.Y(y))
            c.restoreState()
        self.rect(c, bx.x, bx.y, sum(g.cols), y - bx.y, stroke='border', stroke_w=0.6)

    # ----- the file -----
    def paint(self) -> bytes:
        from reportlab.pdfgen import canvas
        plan, spec = self.plan, self.spec
        if plan.format != 'pdf' or not plan.pages:
            raise ValueError('a pdf plan with pages is needed')
        buf = io.BytesIO()
        first = plan.pages[0]
        c = canvas.Canvas(buf, pagesize=(first.w, first.h), pageCompression=1)
        c.setTitle(spec.get('title') or '')
        c.setAuthor(AUTHOR)
        c.setCreator(AUTHOR)
        c.setSubject(_render().subject(spec.get('subtitle')))
        for n, page in enumerate(plan.pages):
            self.page_h = page.h
            c.setPageSize((page.w, page.h))
            if n == 0:
                c.showOutline()
            bg = page.background if page.background in self.ds.colors else 'bg'
            if self.ds.color(bg).upper() != 'FFFFFF':
                c.setFillColor(self.color(bg))
                c.rect(0, 0, page.w, page.h, stroke=0, fill=1)
            order = sorted(range(len(page.boxes)), key=lambda k: (page.boxes[k].z, k))
            for k in order:
                bx = page.boxes[k]
                if bx.kind == 'text':
                    self.outline(c, bx)
                    self.text(c, bx)
                elif bx.kind == 'shape':
                    if bx.content.startswith('art:'):
                        self.art(c, bx)
                    else:
                        round_ = bool(bx.style.radius and abs(bx.w - bx.h) < 0.5 and
                                      bx.style.radius >= min(bx.w, bx.h) / 2 - 0.01)
                        self.rect(c, bx.x, bx.y, bx.w, bx.h, fill=bx.style.fill, stroke=bx.style.stroke,
                                  stroke_w=bx.style.stroke_w, opacity=bx.style.opacity, radius=bx.style.radius,
                                  ellipse=round_)
                elif bx.kind == 'image':
                    self.picture(c, bx)
                elif bx.kind == 'chart':
                    self.chart(c, bx)
                elif bx.kind == 'table':
                    self.table(c, bx)
                elif bx.kind == 'diagram':
                    self.diagram(c, bx)
                elif bx.kind == 'icon':
                    self.icon(c, bx)
            c.showPage()
        c.save()
        return buf.getvalue()


def paint(plan: DesignPlan, spec: dict, ws: Workspace) -> bytes:
    """The .pdf bytes for a plan of format 'pdf'. Raises on a library failure; the caller falls back."""
    return _Painter(plan, spec, ws).paint()
