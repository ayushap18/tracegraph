"""DocSpec -> file bytes. Deterministic renderers apply one theme, the size limits and the safety rules:
F1-F6 (structure per format), X1-X3 (no active content, no formula injection, no external references) and A1-A4
(real headings, marked table headers, titled and summarised charts, readable contrast). Layout lives here only, so
converting a file to another format never costs tokens."""
import io
import os
import re
from functools import lru_cache

from . import themes
from .rules import SpecError
from .spec import FORMATS, chart_summary, clean_chars, normalize, runs, show_number, strip_emphasis

MAX_BYTES = 15 * 1024 * 1024
AUTHOR = 'TraceGraph'
BULLET = '•'
SAFE_FUNCTIONS = ('SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'ROUND')
INJECTION_START = ('=', '+', '-', '@', '\t', '\r')


def theme_name(spec: dict, theme: str | None) -> str:
    """An explicit non-default theme wins; otherwise the spec's own theme; unknown names fall back to clean (A4)."""
    name = theme if theme and theme != 'clean' else spec.get('theme') or 'clean'
    return name if name in themes.THEMES else 'clean'


def render(spec: dict, fmt: str, theme: str = 'clean') -> bytes:
    """The file for one format. The spec is normalized first (idempotent), so a stored raw spec renders directly."""
    if fmt not in FORMATS:
        raise SpecError('X1', f'Files are made as {", ".join(FORMATS)} only, not {str(fmt)[:12]!r}.')
    spec, _ = normalize(spec, fmt)
    name = theme_name(spec, theme)
    try:
        data = {'pdf': _pdf, 'docx': _docx, 'pptx': _pptx, 'xlsx': _xlsx, 'md': _md}[fmt](spec, name)
    except SpecError:
        raise
    except Exception as e:  # a layout or library failure is an honest "no file" (V1), never a crash or a 500
        raise SpecError('V1', f'The {fmt.upper()} file could not be laid out ({type(e).__name__}: {str(e)[:160]}).')
    if len(data) > MAX_BYTES:
        raise SpecError('L4', f'The file would be {len(data) / 1e6:.1f} MB; the limit is {MAX_BYTES // (1024 * 1024)} MB.')
    return data


# ---------- shared helpers ----------


def show(v, fmt: str | None = None) -> str:
    """A table cell as display text (formulas as written; numbers without thousands separators, so years stay years;
    a number in a money or percent column as that column's format writes it)."""
    if v is None:
        return ''
    if fmt and (shown := show_number(v, fmt)) is not None:
        return shown
    if isinstance(v, dict):
        return str(v.get('formula', ''))
    if isinstance(v, bool):
        return 'Yes' if v else 'No'
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else f'{v:.10g}'
    return str(v)


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def formats_of(b: dict) -> list:
    """A table block's per-column number formats (None for plain columns)."""
    f = b.get('formats') or []
    return [f[j] if j < len(f) else None for j in range(len(b['columns']))]


# Table layout (PDF and DOCX): every column is at least as wide as its widest number or word (words capped, since long
# ones may wrap) plus the cell padding, so "2023" or "-100" never breaks across lines; the rest of the width goes to
# columns by how much text they hold. When the minimums don't fit the page, the columns are split into several tables
# that repeat the first (label) column.
TABLE_LAYOUT = {'pdf': {'pad': 12.0, 'scale': 1.0}, 'docx': {'pad': 10.8 + 2, 'scale': 1.08}}
WORD_CAP = 0.25  # of the page width: a longer word may wrap


def table_font_size(fmt: str, n: int) -> float:
    if fmt == 'pdf':
        return 9.5 if n <= 5 else 8.5 if n <= 8 else 7.5
    return 10 if n <= 6 else 9


def page_width(fmt: str, paper: str | None) -> float:
    """The text width in points: the page less 2 cm margins on each side."""
    w = 612.0 if paper == 'letter' else 595.2756
    return w - 2 * 56.6929


def table_groups(cols: list, rows: list, fmt: str, paper: str | None = None, formats: list | None = None,
                 font: str = 'Helvetica') -> list[tuple[list[int], list[float]]]:
    """[(column indices, widths in points)] for one table: one group when it fits, else several."""
    from reportlab.pdfbase import pdfmetrics
    n = len(cols)
    if not n:
        return [([], [])]
    formats = formats or [None] * n
    size, layout, total = table_font_size(fmt, n), TABLE_LAYOUT[fmt], page_width(fmt, paper)
    bold = 'Helvetica-Bold' if font.startswith('Helvetica') else font

    def width(text, f):
        try:
            return pdfmetrics.stringWidth(text, f, size) * layout['scale']
        except Exception:
            return len(text) * size * 0.6 * layout['scale']
    cap = total * WORD_CAP
    mins, weights = [], []
    for j in range(n):
        need = max((width(w, bold) for w in show(cols[j]).split()), default=0)
        longest = len(show(cols[j]))
        for r in rows[:300]:
            v = r[j] if j < len(r) else None
            text = show(v, formats[j])
            longest = max(longest, len(text))
            if is_num(v):
                need = max(need, width(text, font))
            else:
                need = max(need, max((width(w, font) for w in text.split()), default=0))
        mins.append(min(need, cap) + layout['pad'] + 1)
        weights.append(min(max(longest, 4), 40))
    if sum(mins) <= total:
        groups = [list(range(n))]
    else:
        key = [0] if n > 1 and not all(is_num(r[0]) for r in rows[:300] if r and r[0] is not None) else []
        groups, cur = [], list(key)
        for j in range(len(key), n):
            if len(cur) > len(key) and sum(mins[k] for k in cur) + mins[j] > total:
                groups.append(cur)
                cur = list(key)
            cur.append(j)
        groups.append(cur)
    out = []
    for g in groups:
        base = [mins[j] for j in g]
        spare = total - sum(base)
        if spare > 0:
            wsum = sum(weights[j] for j in g)
            base = [b + spare * weights[j] / wsum for b, j in zip(base, g)]
        else:  # a single column wider than the page: squeeze it to fit
            base = [b * total / sum(base) for b in base]
        out.append((g, base))
    return out


def continued_note(cols: list, group: list[int], first: int) -> str:
    """The caption above a table that continues an earlier one with more columns."""
    names = [show(cols[j]) or f'column {j + 1}' for j in group[first:]]
    return f'Table continued: {", ".join(names[:6])}{" and more" if len(names) > 6 else ""}.'


def chart_rows(block: dict) -> tuple[list[str], list[list]]:
    """A chart as a table: the label column then one column per series (DOCX, MD and spreadsheet data)."""
    cols = [''] + [s['name'] for s in block['series']]
    return cols, [[lab] + [s['values'][i] for s in block['series']] for i, lab in enumerate(block['labels'])]


def safe_formula(text: str) -> str | None:
    """X2: a formula the spec marked as one, kept only if it is built from cell references, numbers, operators and
    SUM/AVERAGE/MIN/MAX/COUNT/ROUND. No sheet or file references, no strings, no other functions."""
    s = str(text or '').strip()
    if not s.startswith('=') or len(s) > 200:
        return None
    body, pos, depth = s[1:].upper(), 0, 0
    token = re.compile(r'\s*(?:(?P<fn>[A-Z]+)\(|(?P<ref>\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(?::\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?)'
                       r'(?![A-Z0-9(])|(?P<num>\d+(?:\.\d+)?)|(?P<op>[-+*/^,%()]))')
    while pos < len(body):
        m = token.match(body, pos)
        if not m or m.end() == pos:
            if body[pos:].strip() == '':
                break
            return None
        if m.group('fn'):
            if m.group('fn') not in SAFE_FUNCTIONS:
                return None
            depth += 1
        elif m.group('op') == '(':
            depth += 1
        elif m.group('op') == ')':
            depth -= 1
            if depth < 0:
                return None
        pos = m.end()
    return '=' + body.strip() if depth == 0 and body.strip() else None


def safe_cell(v):
    """X2: text that a spreadsheet could run as a formula (=, +, -, @, tab, CR first, also after spaces) is stored
    as text with a leading apostrophe. Numbers stay numbers; allowed formulas stay formulas."""
    if v is None or is_num(v):
        return v
    if isinstance(v, dict):
        f = safe_formula(v.get('formula', ''))
        return f if f else safe_cell(str(v.get('formula', '')))
    s = clean_chars(v)
    if s[:1] in INJECTION_START or s.lstrip(' \u00a0')[:1] in INJECTION_START:
        return "'" + s
    return s


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 3].rstrip() + '...'


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


# ---------- PDF (reportlab platypus) ----------

UNICODE_FONTS = [  # (regular, bold); the first one found is embedded (subset) for text Helvetica cannot draw
    ('/System/Library/Fonts/Supplemental/Arial Unicode.ttf', None),
    ('/Library/Fonts/Arial Unicode.ttf', None),
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/dejavu/DejaVuSans.ttf', '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/TTF/DejaVuSans.ttf', '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf'),
    ('C:\\Windows\\Fonts\\arialuni.ttf', None),
    ('C:\\Windows\\Fonts\\seguisym.ttf', None),
]


@lru_cache(maxsize=1)
def unicode_font() -> tuple[str, str] | None:
    """(regular, bold) names of a registered Unicode TTF, or None when the system has none."""
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    extra = os.environ.get('TRACEGRAPH_PDF_FONT')
    for regular, bold in ([(extra, None)] if extra else []) + UNICODE_FONTS:
        if not regular or not os.path.exists(regular):
            continue
        try:
            pdfmetrics.registerFont(TTFont('TGUni', regular))
            bold_name = 'TGUni'
            if bold and os.path.exists(bold):
                pdfmetrics.registerFont(TTFont('TGUniBold', bold))
                bold_name = 'TGUniBold'
            for b in (0, 1):
                for i in (0, 1):
                    addMapping('TGUni', b, i, bold_name if b else 'TGUni')
            return 'TGUni', bold_name
        except Exception:
            continue
    return None


@lru_cache(maxsize=1)
def unicode_glyphs() -> frozenset:
    """Code points the registered Unicode font can draw."""
    from reportlab.pdfbase import pdfmetrics
    try:
        return frozenset(pdfmetrics.getFont(unicode_font()[0]).face.charToGlyph)
    except Exception:
        return frozenset()


def latin(text: str) -> bool:
    """True when the built-in PDF fonts (WinAnsi) can draw every character."""
    try:
        str(text).encode('cp1252')
        return True
    except UnicodeEncodeError:
        return False


def pdf_safe(text: str) -> str:
    """Characters no available font can draw replaced with '?', so a PDF never fails on them."""
    return str(text).encode('cp1252', 'replace').decode('cp1252')


def _xml(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _markup(text: str) -> str:
    """S4-clean text with **bold** / *italic* -> reportlab paragraph markup."""
    out = []
    for piece, bold, italic in runs(text):
        s = _xml(piece).replace('\n', '<br/>')
        if italic:
            s = f'<i>{s}</i>'
        if bold:
            s = f'<b>{s}</b>'
        out.append(s)
    return ''.join(out)


class _Fonts:
    """Picks the theme's built-in font for Latin text and the Unicode TTF for everything else."""

    def __init__(self, t: dict):
        self.regular, self.bold, self.italic, self.bold_italic = t['pdf_font']
        self.uni = unicode_font()

    def fit(self, text: str) -> tuple[str, bool]:
        """(text to draw, use the Unicode font). Characters no available font has (emoji, say) become '?'."""
        if latin(text):
            return text, False
        if self.uni:
            glyphs = unicode_glyphs()
            return ''.join(ch if ord(ch) in glyphs or ch in '\n\t' else '?' for ch in text), True
        return pdf_safe(text), False

    def name(self, text: str, bold: bool = False) -> str:
        _, uni = self.fit(text)
        if uni:
            return self.uni[1] if bold else self.uni[0]
        return self.bold if bold else self.regular


def _pdf(spec: dict, name: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph, Preformatted, Spacer,
                                    Table, TableStyle)

    t = themes.get(name)
    fonts = _Fonts(t)
    c = {k: colors.HexColor('#' + t[k]) for k in ('bg', 'text', 'muted', 'heading', 'accent', 'header_bg',
                                                     'header_text', 'stripe', 'code_bg', 'border')}
    page = letter if spec.get('paper') == 'letter' else A4
    margin = 2 * cm
    width = page[0] - 2 * margin
    title = spec['title']

    base = ParagraphStyle('body', fontName=fonts.regular, fontSize=10.5, leading=14.5, textColor=c['text'],
                          spaceAfter=6)
    styles = {
        'body': base,
        'title': ParagraphStyle('title', parent=base, fontName=fonts.bold, fontSize=24, leading=29,
                                textColor=c['heading'], spaceAfter=6),
        'subtitle': ParagraphStyle('subtitle', parent=base, fontSize=13, leading=17, textColor=c['muted'],
                                   spaceAfter=14),
        1: ParagraphStyle('h1', parent=base, fontName=fonts.bold, fontSize=17, leading=21, textColor=c['heading'],
                          spaceBefore=14, spaceAfter=6),
        2: ParagraphStyle('h2', parent=base, fontName=fonts.bold, fontSize=14, leading=18, textColor=c['heading'],
                          spaceBefore=10, spaceAfter=4),
        3: ParagraphStyle('h3', parent=base, fontName=fonts.bold, fontSize=12, leading=15, textColor=c['heading'],
                          spaceBefore=8, spaceAfter=3),
        'bullet': ParagraphStyle('bullet', parent=base, leftIndent=16, bulletIndent=4, spaceAfter=3),
        'quote': ParagraphStyle('quote', parent=base, fontName=fonts.italic, fontSize=11, leading=15, leftIndent=14,
                                rightIndent=8, backColor=c['stripe'], borderPadding=(6, 8, 6, 8), spaceBefore=6,
                                spaceAfter=8),
        'by': ParagraphStyle('by', parent=base, textColor=c['muted'], alignment=TA_RIGHT, spaceAfter=0),
        'caption': ParagraphStyle('caption', parent=base, fontName=fonts.bold, fontSize=11, spaceAfter=2,
                                  keepWithNext=True),
        'summary': ParagraphStyle('summary', parent=base, textColor=c['muted'], fontSize=10, spaceAfter=10),
        'code': ParagraphStyle('code', parent=base, fontName='Courier', fontSize=9.5, leading=12.5,
                               backColor=c['code_bg'], borderPadding=6, spaceBefore=4, spaceAfter=10),
        'cell': ParagraphStyle('cell', parent=base, fontSize=9.5, leading=12, spaceAfter=0),
        'head': ParagraphStyle('head', parent=base, fontName=fonts.bold, fontSize=9.5, leading=12, spaceAfter=0,
                               textColor=c['header_text']),
    }

    def para(text: str, style, **kw):
        """A Paragraph in the right font: the theme font for Latin text, the Unicode TTF (or '?') otherwise."""
        text, uni = fonts.fit(text)
        if uni:
            bold = style.fontName in (fonts.bold, fonts.bold_italic)
            style = ParagraphStyle(style.name + '-u', parent=style, fontName=fonts.uni[1] if bold else fonts.uni[0],
                                   bulletFontName=fonts.uni[0])
        return Paragraph(_markup(text), style, **kw)

    class Heading(Paragraph):
        """A heading paragraph that also becomes a PDF outline entry (F1, A1)."""

        def __init__(self, text, style, level):
            super().__init__(_markup(fonts.fit(text)[0]), style if not fonts.fit(text)[1] else ParagraphStyle(
                style.name + '-u', parent=style, fontName=fonts.uni[1]))
            self.outline = (strip_emphasis(text), level)

    class Doc(BaseDocTemplate):
        def afterFlowable(self, flowable):
            if isinstance(flowable, Heading):
                text, level = flowable.outline
                key = f'h{id(flowable)}'
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(text, key, level=level, closed=False)

    def decorate(canvas, doc):
        canvas.saveState()
        if doc.page == 1:
            canvas.showOutline()
        if t['bg'] != 'FFFFFF':
            canvas.setFillColor(c['bg'])
            canvas.rect(0, 0, page[0], page[1], fill=1, stroke=0)
        canvas.setFillColor(c['muted'])
        canvas.setFont(fonts.regular, 9)
        canvas.drawRightString(page[0] - margin, margin / 2, f'Page {doc.page}')
        short = fonts.fit(_short(title, 70))
        canvas.setFont(fonts.uni[0] if short[1] else fonts.regular, 9)
        canvas.drawString(margin, margin / 2, short[0])
        canvas.restoreState()

    def table(cols: list, rows: list, formats: list | None = None) -> list:
        """The table as one or more Tables (columns split when they can't fit), rows split across pages even when
        one row is taller than a page (splitInRow), the header repeated on every page (A2)."""
        n = max(len(cols), 1)
        formats = formats or [None] * len(cols)
        size = table_font_size('pdf', n)
        cell = ParagraphStyle('c', parent=styles['cell'], fontSize=size, leading=size + 2.5)
        num = ParagraphStyle('n', parent=cell, alignment=TA_RIGHT)
        head = ParagraphStyle('h', parent=styles['head'], fontSize=size, leading=size + 2.5)
        groups = table_groups(cols, rows, 'pdf', spec.get('paper'), formats, fonts.regular)
        out = []
        for gi, (g, widths) in enumerate(groups):
            if gi:
                out.append(para(continued_note(cols, g, 1 if g[0] == 0 else 0), styles['summary']))
            data = [[para(show(cols[j]), head) for j in g]]
            data += [[para(show(r[j], formats[j]), num if is_num(r[j]) else cell) for j in g] for r in rows]
            tbl = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT', splitInRow=1)
            tbl.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), c['header_bg']),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [c['bg'], c['stripe']]),
                ('LINEBELOW', (0, 0), (-1, -1), 0.4, c['border']),
                ('BOX', (0, 0), (-1, -1), 0.6, c['border']),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]))
            out += [tbl, Spacer(1, 10)]
        return out

    story = [Heading(title, styles['title'], 0)]
    if spec.get('subtitle'):
        story.append(para(spec['subtitle'], styles['subtitle']))
    else:
        story.append(Spacer(1, 8))
    for sec in spec['sections']:
        heading = [Heading(sec['heading'], styles[sec['level']], sec['level'])] if sec['heading'] else []
        if not sec['blocks']:
            story += heading
        for b in sec['blocks']:
            kind, parts = b['type'], []
            if kind == 'paragraph':
                parts.append(para(b['text'], styles['body']))
            elif kind == 'bullets':
                for i, item in enumerate(b['items'], 1):
                    parts.append(para(item, styles['bullet'], bulletText=f'{i}.' if b.get('ordered') else BULLET))
                parts.append(Spacer(1, 4))
            elif kind == 'quote':
                # paragraphs, which split across pages however long the quote is, with an accent rule on the left
                parts.append(para(b['text'], styles['quote']))
                if b.get('by'):
                    parts.append(para(b['by'], styles['by']))
                parts.append(Spacer(1, 8))
            elif kind == 'code':
                chars = int(width / (9.5 * 0.6)) - 2
                lines = []
                for ln in b['text'].split('\n'):
                    lines += [ln[i:i + chars] for i in range(0, len(ln), chars)] or ['']
                text, uni = fonts.fit('\n'.join(lines))
                style = styles['code'] if not uni else ParagraphStyle('code-u', parent=styles['code'],
                                                                      fontName=fonts.uni[0])
                parts.append(Preformatted(text, style))
            elif kind == 'table':
                parts += table(b['columns'], b['rows'], formats_of(b))
            elif kind == 'chart':
                parts.append(KeepTogether([para(b['title'], styles['caption']), _pdf_chart(b, t, fonts, width),
                                           para(chart_summary(b), styles['summary'])]))
            if heading:  # a heading never ends a page alone: it travels with the start of its first block
                big = kind == 'table' and len(b['rows']) > 15 or kind == 'code' and b['text'].count('\n') > 30
                story.append(KeepTogether(heading + parts[:1]) if not big else heading[0])
                story += parts[1:] if not big else parts
                heading = []
            else:
                story += parts
    buf = io.BytesIO()
    doc = Doc(buf, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin,
              title=title, author=AUTHOR, subject=spec.get('subtitle') or '', creator=AUTHOR)
    frame = Frame(margin, margin, width, page[1] - 2 * margin, id='body', leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='page', frames=[frame], onPage=decorate)])
    doc.build(story)
    return buf.getvalue()


def _pdf_chart(b: dict, t: dict, fonts: _Fonts, width: float):
    """A native vector chart (reportlab graphics) with value labels or marker shapes, so colour is not the only cue."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.widgets.markers import makeMarker
    from reportlab.lib import colors

    text_c, palette = colors.HexColor('#' + t['text']), [colors.HexColor('#' + p) for p in t['palette']]
    labels = [fonts.fit(_short(x, 18))[0] for x in b['labels']]
    all_text = ' '.join(labels + [s['name'] for s in b['series']])
    font = fonts.name(all_text)
    series = b['series'] if b['kind'] != 'pie' else b['series'][:1]
    h = 230
    d = Drawing(width, h)
    many = len(labels) > 6
    legend_needed = len(series) > 1 or b['kind'] == 'pie'
    if b['kind'] == 'pie':
        vals, labs = list(series[0]['values']), list(labels)
        if len(vals) > 10:  # the smallest slices are grouped so labels stay readable
            order = sorted(range(len(vals)), key=lambda i: -vals[i])
            keep = sorted(order[:9])
            other = sum(vals[i] for i in order[9:])
            vals, labs = [vals[i] for i in keep] + [other], [labs[i] for i in keep] + ['Other']
        total = sum(vals) or 1
        p = Pie()
        p.x, p.y, p.width, p.height = 30, 20, h - 50, h - 50
        p.data = vals
        p.labels = [f'{v / total:.0%}' for v in vals]
        p.simpleLabels = 1
        for i in range(len(vals)):
            p.slices[i].fillColor = palette[i % len(palette)]
            p.slices[i].strokeColor = colors.white
            p.slices[i].fontName, p.slices[i].fontSize, p.slices[i].fontColor = font, 8, text_c
        d.add(p)
        lg = Legend()
        lg.x, lg.y, lg.alignment = h + 10, h - 30, 'right'
        lg.fontName, lg.fontSize, lg.fillColor = font, 8.5, text_c
        lg.colorNamePairs = [(palette[i % len(palette)], f'{labs[i]} ({vals[i] / total:.0%})') for i in range(len(vals))]
        d.add(lg)
        return d
    if b['kind'] == 'line':
        ch = HorizontalLineChart()
        markers = ['FilledCircle', 'FilledSquare', 'FilledDiamond', 'FilledTriangle', 'Circle', 'Square']
        ch.data = [tuple(s['values']) for s in series]
        for i in range(len(series)):
            ch.lines[i].strokeColor = palette[i % len(palette)]
            ch.lines[i].strokeWidth = 1.8
            ch.lines[i].symbol = makeMarker(markers[i % len(markers)])
            ch.lines[i].symbol.fillColor = palette[i % len(palette)]
    else:
        ch = VerticalBarChart()
        ch.data = [tuple(v if v is not None else 0 for v in s['values']) for s in series]
        for i in range(len(series)):
            ch.bars[i].fillColor = palette[i % len(palette)]
            ch.bars[i].strokeColor = None
        if len(labels) * len(series) <= 24:
            ch.barLabelFormat = lambda v: _num_label(v)
            ch.barLabels.nudge = 7
            ch.barLabels.fontName, ch.barLabels.fontSize, ch.barLabels.fillColor = font, 7.5, text_c
    values = [v for s in series for v in s['values'] if v is not None]
    ch.x, ch.y = 45, 55 if many else 30
    ch.width = width - (170 if legend_needed else 60)
    ch.height = h - ch.y - 20
    ch.categoryAxis.categoryNames = labels
    ch.valueAxis.valueMin = min(0, min(values))
    ch.valueAxis.valueMax = max(values) * 1.12 if max(values) > 0 else 0
    if ch.valueAxis.valueMax <= ch.valueAxis.valueMin:
        ch.valueAxis.valueMax = ch.valueAxis.valueMin + 1
    for ax in (ch.categoryAxis, ch.valueAxis):
        ax.labels.fontName, ax.labels.fontSize, ax.labels.fillColor = font, 8, text_c
        ax.strokeColor = text_c
    ch.valueAxis.labelTextFormat = _num_label
    if many:
        ch.categoryAxis.labels.angle, ch.categoryAxis.labels.boxAnchor = 35, 'ne'
        ch.categoryAxis.labels.dy = -2
    d.add(ch)
    if legend_needed:
        lg = Legend()
        lg.x, lg.y, lg.alignment = width - 115, h - 20, 'right'
        lg.fontName, lg.fontSize, lg.fillColor = font, 8.5, text_c
        lg.colorNamePairs = [(palette[i % len(palette)], fonts.fit(_short(s['name'], 20))[0])
                             for i, s in enumerate(series)]
        d.add(lg)
    return d


def _num_label(v) -> str:
    if v is None:
        return ''
    if abs(v) >= 1e6:
        return f'{v / 1e6:.3g}M'
    if abs(v) >= 1e4:
        return f'{v / 1e3:.3g}k'
    return f'{v:g}'


# ---------- DOCX (python-docx) ----------


def _docx(spec: dict, name: str) -> bytes:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    t = themes.get(name, paper=True)
    doc = Document()
    rgb = {k: RGBColor(*_rgb(t[k])) for k in ('text', 'muted', 'heading', 'accent', 'header_text')}
    cp = doc.core_properties
    cp.title, cp.subject, cp.author, cp.last_modified_by = spec['title'], spec.get('subtitle') or '', AUTHOR, AUTHOR
    cp.comments = 'Created by TraceGraph'
    for sec in doc.sections:
        if spec.get('paper') != 'letter':
            sec.page_width, sec.page_height = Cm(21), Cm(29.7)
        sec.left_margin = sec.right_margin = sec.top_margin = sec.bottom_margin = Cm(2)

    def font(style, family, size=None, color=None, bold=None):
        style.font.name = family
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find(qn('w:rFonts'))
        if fonts is None:
            fonts = OxmlElement('w:rFonts')
            rpr.append(fonts)
        for attr in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'):
            fonts.set(qn(attr), family)
        for attr in ('w:asciiTheme', 'w:hAnsiTheme', 'w:eastAsiaTheme', 'w:cstheme'):
            fonts.attrib.pop(qn(attr), None)
        if size:
            style.font.size = Pt(size)
        if color is not None:
            style.font.color.rgb = color
        if bold is not None:
            style.font.bold = bold
    font(doc.styles['Normal'], t['font'], 11, rgb['text'])  # body text 11 pt (F6)
    font(doc.styles['Title'], t['heading_font'], 26, rgb['heading'])
    font(doc.styles['Subtitle'], t['font'], 14, rgb['muted'])
    for lvl, size in ((1, 16), (2, 13.5), (3, 12)):
        font(doc.styles[f'Heading {lvl}'], t['heading_font'], size, rgb['heading'], True)
    for st in ('Quote', 'Caption', 'List Bullet', 'List Number'):
        if st in [s.name for s in doc.styles]:
            font(doc.styles[st], t['font'], 11 if st != 'Caption' else 10, rgb['muted'] if st == 'Caption' else
                 rgb['text'])

    def add_runs(p, text, size=None, color=None, mono=False, italic=False):
        for piece, bold, it in runs(text):
            r = p.add_run(piece)
            r.bold, r.italic = bold or None, (it or italic) or None
            if size:
                r.font.size = Pt(size)
            if color is not None:
                r.font.color.rgb = color
            if mono:
                r.font.name = t['mono']
        return p

    def shade(el, fill):
        pr = el.get_or_add_tcPr() if el.tag == qn('w:tc') else el.get_or_add_pPr()
        shd = OxmlElement('w:shd')
        shd.set(qn('w:val'), 'clear')
        shd.set(qn('w:color'), 'auto')
        shd.set(qn('w:fill'), fill)
        pr.append(shd)

    def table(cols, rows, formats=None):
        """Real Word tables with a marked header row; columns split into several tables when they can't fit."""
        formats = formats or [None] * len(cols)
        groups = table_groups(cols, rows, 'docx', spec.get('paper'), formats,
                              'Times-Roman' if t['font'] == 'Georgia' else 'Helvetica')
        for gi, (g, widths) in enumerate(groups):
            if gi:
                doc.add_paragraph(continued_note(cols, g, 1 if g[0] == 0 else 0), style='Caption')
            one_table([cols[j] for j in g], [[r[j] for j in g] for r in rows], [formats[j] for j in g],
                      [Pt(w) for w in widths])

    def one_table(cols, rows, formats, widths):
        tbl = doc.add_table(rows=len(rows) + 1, cols=len(cols))
        tbl.style = doc.styles['Table Grid']
        tbl.alignment = WD_TABLE_ALIGNMENT.LEFT
        size = table_font_size('docx', len(formats))
        trs = tbl.rows
        head = trs[0]
        tr_pr = head._tr.get_or_add_trPr()
        mark = OxmlElement('w:tblHeader')  # a real header row that repeats on each page (A2)
        mark.set(qn('w:val'), 'true')
        tr_pr.append(mark)
        tbl.autofit = False
        for col, w in zip(tbl.columns, widths):
            col.width = w
        tbl_w = tbl._tbl.tblPr.find(qn('w:tblW'))
        if tbl_w is not None:
            tbl_w.set(qn('w:type'), 'dxa')
            tbl_w.set(qn('w:w'), str(sum(int(w.twips) for w in widths)))
        for row in trs:
            for cell, w in zip(row.cells, widths):
                cell.width = w
                cell.paragraphs[0].paragraph_format.space_after = Pt(0)
        for cell, text in zip(head.cells, cols):
            p = cell.paragraphs[0]
            r = p.add_run(show(text))
            r.bold, r.font.size, r.font.color.rgb = True, Pt(size), rgb['header_text']
            shade(cell._tc, t['header_bg'])
        for i, (row, data) in enumerate(zip(trs[1:], rows)):
            for cell, v, f in zip(row.cells, data, formats):
                p = cell.paragraphs[0]
                r = p.add_run(show(v, f))
                r.font.size = Pt(size)
                if is_num(v):
                    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                if i % 2:
                    shade(cell._tc, t['stripe'])
        doc.add_paragraph()
        return tbl

    number_style = doc.styles['List Number']

    def restart_numbering():
        """A fresh numbering instance so every ordered list starts at 1."""
        try:
            numbering = doc.part.numbering_part.numbering_definitions._numbering
            num_id = number_style.element.pPr.numPr.numId.val
            abstract = numbering.num_having_numId(num_id).abstractNumId.val
            num = numbering.add_num(abstract)
            num.add_lvlOverride(ilvl=0).add_startOverride(1)
            return num.numId
        except Exception:
            return None

    doc.add_heading(spec['title'], 0)
    if spec.get('subtitle'):
        doc.add_paragraph(spec['subtitle'], style='Subtitle')
    for sec in spec['sections']:
        if sec['heading']:
            doc.add_heading(sec['heading'], sec['level'])  # Heading 1-3 styles (F2, A1)
        for b in sec['blocks']:
            kind = b['type']
            if kind == 'paragraph':
                add_runs(doc.add_paragraph(), b['text'])
            elif kind == 'bullets':
                num_id = restart_numbering() if b.get('ordered') else None
                for item in b['items']:
                    p = doc.add_paragraph(style='List Number' if b.get('ordered') else 'List Bullet')
                    if num_id is not None:
                        num_pr = p._p.get_or_add_pPr().get_or_add_numPr()
                        num_pr.get_or_add_ilvl().val = 0
                        num_pr.get_or_add_numId().val = num_id
                    add_runs(p, item)
            elif kind == 'quote':
                add_runs(doc.add_paragraph(style='Quote'), b['text'], italic=True)
                if b.get('by'):
                    p = add_runs(doc.add_paragraph(), b['by'], color=rgb['muted'], italic=True)
                    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif kind == 'code':
                p = doc.add_paragraph()
                shade(p._p, t['code_bg'])
                for i, ln in enumerate(b['text'].split('\n')):
                    r = p.add_run(ln)
                    r.font.name, r.font.size = t['mono'], Pt(10)
                    r._r.get_or_add_rPr().get_or_add_rFonts().set(qn('w:hAnsi'), t['mono'])
                    if i < b['text'].count('\n'):
                        r.add_break()
            elif kind == 'table':
                if b.get('title') and b['title'] != sec['heading']:
                    doc.add_paragraph(b['title'], style='Caption')
                table(b['columns'], b['rows'], formats_of(b))
            elif kind == 'chart':
                # Word has no chart builder here: the chart becomes its data table plus a one-line summary (V4, A3)
                doc.add_paragraph(f'Chart: {b["title"]}', style='Caption')
                cols, rows = chart_rows(b)
                cols[0] = 'Label'
                table(cols, rows)
                add_runs(doc.add_paragraph(), f'{chart_summary(b)} ({b["kind"]} chart shown as a table)',
                         color=rgb['muted'], italic=True)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------- PPTX (python-pptx) ----------

SLIDE_W, SLIDE_H = 13.333, 7.5
BODY_PT, TITLE_PT, TABLE_PT = 20, 32, 16


def _pptx(spec: dict, name: str) -> bytes:
    from lxml import etree
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_MARKER_STYLE
    from pptx.enum.text import PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Inches, Pt

    t = themes.get(name)
    rgb = {k: RGBColor(*_rgb(t[k])) for k in ('bg', 'text', 'muted', 'heading', 'accent', 'header_bg', 'header_text',
                                             'stripe', 'code_bg')}
    palette = [RGBColor(*_rgb(p)) for p in t['palette']]
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SLIDE_W), Inches(SLIDE_H)  # 16:9 (F3)
    prs.core_properties.title, prs.core_properties.author = spec['title'], AUTHOR
    prs.core_properties.subject = spec.get('subtitle') or ''
    margin, top, body_h = 0.6, 1.55, SLIDE_H - 1.55 - 0.45
    body_w = SLIDE_W - 2 * margin

    def style_runs(p, text, size, color, bold=False, italic=False, family=None):
        for piece, b, it in runs(text):
            r = p.add_run()
            r.text = piece
            r.font.size, r.font.color.rgb = Pt(size), color
            r.font.bold, r.font.italic = (bold or b) or None, (italic or it) or None
            r.font.name = family or t['font']

    def background(slide):
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = rgb['bg']

    def set_title(shape, text, size, color, box):
        shape.left, shape.top, shape.width, shape.height = (Inches(x) for x in box)
        tf = shape.text_frame
        tf.clear()
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        style_runs(p, text, size, color, bold=True, family=t['heading_font'])

    def bullet(p, char):
        ppr = p._p.get_or_add_pPr()
        ppr.set('marL', str(Inches(0.32)))
        ppr.set('indent', str(-Inches(0.32)))
        for tag in ('a:buNone', 'a:buChar', 'a:buAutoNum'):
            for el in ppr.findall(qn(tag)):
                ppr.remove(el)
        if char == 'num':
            el = etree.SubElement(ppr, qn('a:buAutoNum'))
            el.set('type', 'arabicPeriod')
        else:
            el = etree.SubElement(ppr, qn('a:buChar'))
            el.set('char', char)

    def text_box(slide, blocks, box):
        shape = slide.shapes.add_textbox(*(Inches(x) for x in box))
        shape.name = 'Body'
        tf = shape.text_frame
        tf.word_wrap = True
        first = True
        for b in blocks:
            items = b['items'] if b['type'] == 'bullets' else [b['text']]
            for item in items:
                p = tf.paragraphs[0] if first else tf.add_paragraph()
                first = False
                p.space_after = Pt(10)
                if b['type'] == 'bullets':
                    bullet(p, 'num' if b.get('ordered') else BULLET)
                style_runs(p, item, BODY_PT, rgb['text'], italic=b['type'] == 'quote')
            if b['type'] == 'quote' and b.get('by'):
                p = tf.add_paragraph()
                p.alignment = PP_ALIGN.RIGHT
                style_runs(p, b['by'], BODY_PT - 2, rgb['muted'], italic=True)

    def table_shape(slide, b, box):
        cols, rows, formats = b['columns'], b['rows'], formats_of(b)
        size = TABLE_PT if len(cols) <= 5 else 14
        row_h = (size + 12) / 72
        height = min(box[3], row_h * (len(rows) + 1))
        shape = slide.shapes.add_table(len(rows) + 1, len(cols), Inches(box[0]), Inches(box[1]), Inches(box[2]),
                                       Inches(height))
        tbl = shape.table
        tbl.first_row = True  # header row marked as a header (A2)
        tbl.horz_banding = False
        lengths = [max([len(show(cols[j]))] + [len(show(r[j], formats[j])) for r in rows]) for j in range(len(cols))]
        weights = [min(max(x, 4), 30) for x in lengths]
        for j, w in enumerate(weights):
            tbl.columns[j].width = Inches(box[2] * w / sum(weights))
        for i, data in enumerate([cols] + rows):
            for j, v in enumerate(data):
                cell = tbl.cell(i, j)
                cell.fill.solid()
                cell.fill.fore_color.rgb = rgb['header_bg'] if i == 0 else rgb['stripe'] if i % 2 == 0 else rgb['bg']
                cell.margin_top = cell.margin_bottom = Inches(0.04)
                p = cell.text_frame.paragraphs[0]
                if i and is_num(v):
                    p.alignment = PP_ALIGN.RIGHT
                style_runs(p, show(v, formats[j] if i else None), size, rgb['header_text'] if i == 0 else rgb['text'],
                           bold=i == 0)
        return shape

    def chart_shape(slide, b, box):
        kind = {'bar': XL_CHART_TYPE.COLUMN_CLUSTERED, 'line': XL_CHART_TYPE.LINE_MARKERS,
                'pie': XL_CHART_TYPE.PIE}[b['kind']]
        data = CategoryChartData()
        # X2: the chart's data lives in an embedded workbook whose writer stores any "=..." text as a live formula
        data.categories = [safe_cell(lab) for lab in b['labels']]
        series = b['series'][:1] if b['kind'] == 'pie' else b['series']
        for s in series:
            data.add_series(safe_cell(s['name']), s['values'])
        x, y, w, h = box
        frame = slide.shapes.add_chart(kind, Inches(x), Inches(y), Inches(w), Inches(h - 0.75), data)
        chart = frame.chart
        chart.has_title = True
        chart.chart_title.text_frame.text = b['title']
        tp = chart.chart_title.text_frame.paragraphs[0]
        for r in tp.runs:
            r.font.size, r.font.bold, r.font.color.rgb = Pt(18), True, rgb['text']
        chart.font.size, chart.font.color.rgb = Pt(14), rgb['text']
        chart.has_legend = len(series) > 1 or b['kind'] == 'pie'
        if chart.has_legend:
            chart.legend.position, chart.legend.include_in_layout = XL_LEGEND_POSITION.BOTTOM, False
        plot = chart.plots[0]
        if b['kind'] == 'pie':
            plot.has_data_labels = True
            plot.data_labels.show_percentage, plot.data_labels.show_value = True, False
            plot.data_labels.number_format, plot.data_labels.number_format_is_linked = '0%', False
            for i, point in enumerate(plot.series[0].points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = palette[i % len(palette)]
        else:
            markers = [XL_MARKER_STYLE.CIRCLE, XL_MARKER_STYLE.SQUARE, XL_MARKER_STYLE.DIAMOND,
                       XL_MARKER_STYLE.TRIANGLE, XL_MARKER_STYLE.X, XL_MARKER_STYLE.STAR]
            for i, s in enumerate(plot.series):
                if b['kind'] == 'line':
                    s.format.line.color.rgb = palette[i % len(palette)]
                    s.marker.style = markers[i % len(markers)]
                    s.marker.format.fill.solid()
                    s.marker.format.fill.fore_color.rgb = palette[i % len(palette)]
                else:
                    s.format.fill.solid()
                    s.format.fill.fore_color.rgb = palette[i % len(palette)]
            if b['kind'] == 'bar' and len(b['labels']) * len(series) <= 24:
                plot.has_data_labels = True
                plot.data_labels.font.size, plot.data_labels.font.color.rgb = Pt(12), rgb['text']
        summary = slide.shapes.add_textbox(Inches(x), Inches(y + h - 0.7), Inches(w), Inches(0.7))
        summary.name = 'Chart summary'
        summary.text_frame.word_wrap = True
        style_runs(summary.text_frame.paragraphs[0], chart_summary(b), 18, rgb['muted'])  # A3
        return frame

    def code_box(slide, b, box):
        shape = slide.shapes.add_textbox(*(Inches(v) for v in box))
        shape.name = 'Code'
        shape.fill.solid()
        shape.fill.fore_color.rgb = rgb['code_bg']
        tf = shape.text_frame
        tf.word_wrap = True
        for i, ln in enumerate(b['text'].split('\n')):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            r = p.add_run()
            r.text = ln or ' '
            r.font.name, r.font.size, r.font.color.rgb = t['mono'], Pt(14), rgb['text']
        return shape

    slide = prs.slides.add_slide(prs.slide_layouts[0])  # the title slide (F3)
    background(slide)
    set_title(slide.shapes.title, spec['title'], 40, rgb['heading'], (margin + 0.2, 2.3, body_w - 0.4, 1.6))
    sub = slide.placeholders[1]
    if spec.get('subtitle'):
        set_title(sub, spec['subtitle'], 22, rgb['muted'], (margin + 0.2, 4.0, body_w - 0.4, 1.0))
        for r in sub.text_frame.paragraphs[0].runs:
            r.font.bold = None
    else:
        sub._element.getparent().remove(sub._element)
    for sec in spec['sections']:
        slide = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only: a real title placeholder (A1)
        background(slide)
        text = [b for b in sec['blocks'] if b['type'] in ('paragraph', 'bullets', 'quote')]
        visuals = [b for b in sec['blocks'] if b['type'] in ('table', 'chart', 'code')]
        only_title = not text and not visuals
        set_title(slide.shapes.title, sec['heading'], TITLE_PT if not only_title else 40, rgb['heading'],
                  (margin, 0.35 if not only_title else 2.8, body_w, 1.05 if not only_title else 1.6))
        if text and visuals:
            text_w = 4.5
            text_box(slide, text, (margin, top, text_w, body_h))
            vbox = (margin + text_w + 0.35, top, body_w - text_w - 0.35, body_h)
        elif text:
            text_box(slide, text, (margin, top, body_w, body_h))
            vbox = None
        else:
            vbox = (margin, top, body_w, body_h)
        for b in visuals[:1]:
            {'table': table_shape, 'chart': chart_shape, 'code': code_box}[b['type']](slide, b, vbox)
        if sec.get('notes'):
            slide.notes_slide.notes_text_frame.text = sec['notes']  # overflow text lives in the notes (L3, F3)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ---------- XLSX (openpyxl) ----------

_SHEET_BAD = re.compile(r'[\[\]:*?/\\]')


def sheet_name(name: str, used: set) -> str:
    """F4: at most 31 characters, none of []:*?/\\, not blank, not starting or ending with an apostrophe, unique."""
    s = _SHEET_BAD.sub(' ', clean_chars(name)).replace('\n', ' ')
    s = re.sub(r'\s+', ' ', s).strip().strip("'").strip() or 'Sheet'
    if s.lower() == 'history':
        s = 'History data'
    s = s[:31].strip()
    base, n = s, 2
    while s.lower() in used:
        tail = f' ({n})'
        s = base[:31 - len(tail)].rstrip() + tail
        n += 1
    used.add(s.lower())
    return s


def _chart_matches_table(chart: dict, table: dict) -> tuple[int, list[int]] | None:
    """(label column, series columns) when a chart plots columns of the table as they are, so it can point at them."""
    cols, rows = table['columns'], table['rows']
    if len(rows) != len(chart['labels']) or not rows:
        return None
    label_col = next((j for j in range(len(cols)) if [show(r[j]) for r in rows] == chart['labels']), None)
    if label_col is None:
        return None
    series_cols = []
    for s in chart['series']:
        j = next((j for j, c in enumerate(cols) if c == s['name']), None)
        if j is None or [r[j] if is_num(r[j]) else None for r in rows] != s['values']:
            return None
        series_cols.append(j)
    return label_col, series_cols


def xlsx_plan(spec: dict) -> list[dict]:
    """The workbook's sheets in order: a Notes sheet when there is text, one sheet per table (with the charts of its
    section that plot it or sit after it), and a sheet for each chart with no table before it."""
    used, plan = set(), []
    has_text = any(b['type'] in ('paragraph', 'bullets', 'quote', 'code') for s in spec['sections'] for b in s['blocks'])
    if has_text:
        plan.append({'name': sheet_name('Notes', used), 'kind': 'notes'})
    n_table = n_chart = 0
    for s in spec['sections']:
        last_table, named = None, False
        for b in s['blocks']:
            if b['type'] == 'table':
                n_table += 1
                multi = sum(1 for x in s['blocks'] if x['type'] == 'table') > 1
                label = b.get('title') or s['heading'] or f'Table {n_table}'
                if multi and not b.get('title'):
                    label = f'{s["heading"] or "Table"} {n_table}'
                last_table = {'name': sheet_name(label, used), 'kind': 'table', 'table': b, 'charts': [],
                              'heading': s['heading']}
                named = True
                plan.append(last_table)
            elif b['type'] == 'chart':
                n_chart += 1
                if last_table is not None:
                    last_table['charts'].append(b)
                else:
                    # the section's first chart sheet carries its heading, like table sheets do
                    label = s['heading'] if s['heading'] and not named else b['title'] or f'Chart {n_chart}'
                    named = True
                    plan.append({'name': sheet_name(label, used), 'kind': 'chart', 'charts': [b],
                                 'heading': s['heading']})
    if not plan:
        plan.append({'name': sheet_name('Notes', used), 'kind': 'notes'})
    return plan


def _xlsx(spec: dict, name: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.chart.marker import Marker
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    t = themes.get(name, paper=True)
    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.title, wb.properties.creator = spec['title'], AUTHOR
    wb.properties.subject = spec.get('subtitle') or ''
    head_font = Font(bold=True, color=t['header_text'], name=t['font'])
    head_fill = PatternFill('solid', fgColor=t['header_bg'])
    body_font = Font(name=t['font'], color=t['text'], size=11)
    markers = ['circle', 'square', 'diamond', 'triangle', 'x', 'star']

    def put(ws, row, col, v, font=None):
        cell = ws.cell(row=row, column=col, value=safe_cell(v))
        cell.font = font or body_font
        return cell

    def write_table(ws, cols, rows, r0=1, c0=1, formats=None):
        formats = formats or [None] * len(cols)
        for j, c in enumerate(cols):
            cell = put(ws, r0, c0 + j, c, head_font)
            cell.fill = head_fill
            cell.alignment = Alignment(vertical='center')
        for i, row in enumerate(rows, 1):
            for j, v in enumerate(row):
                cell = put(ws, r0 + i, c0 + j, v)
                if formats[j] and is_num(v):  # money and percentages stay numbers, shown with their symbol (F4)
                    cell.number_format = formats[j]
        for j in range(len(cols)):
            longest = max([len(show(cols[j]))] + [len(show(r[j], formats[j])) for r in rows[:500]])
            letter = get_column_letter(c0 + j)
            ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, min(max(longest + 2, 9), 50))

    def add_chart(ws, b, anchor, cats, data_ref):
        kind = b['kind']
        ch = {'bar': BarChart, 'line': LineChart, 'pie': PieChart}[kind]()
        if kind == 'bar':
            ch.type = 'col'
        ch.title = _short(b['title'], 120)
        ch.add_data(data_ref, titles_from_data=True)
        ch.set_categories(cats)
        ch.width, ch.height = 18, 9
        if kind != 'pie':
            ch.x_axis.delete = ch.y_axis.delete = False
            for i, s in enumerate(ch.series):
                color = t['palette'][i % len(t['palette'])]
                if kind == 'line':
                    s.graphicalProperties.line.solidFill = color
                    s.marker = Marker(symbol=markers[i % len(markers)], size=7)
                    s.marker.graphicalProperties.solidFill = color
                else:
                    s.graphicalProperties.solidFill = color
            if len(ch.series) < 2:
                ch.legend = None
        ws.add_chart(ch, anchor)

    plan = xlsx_plan(spec)
    index = {id(p['table']): p['name'] for p in plan if p['kind'] == 'table'}
    for sh in plan:
        ws = wb.create_sheet(sh['name'])
        if sh['kind'] == 'notes':
            ws.column_dimensions['A'].width = 100
            wrap = Alignment(wrap_text=True, vertical='top')
            r = 1
            put(ws, r, 1, spec['title'], Font(bold=True, size=16, color=t['heading'], name=t['font']))
            r += 1
            if spec.get('subtitle'):
                put(ws, r, 1, spec['subtitle'], Font(italic=True, color=t['muted'], name=t['font']))
                r += 1
            for s in spec['sections']:
                r += 1
                if s['heading']:
                    put(ws, r, 1, s['heading'], Font(bold=True, size=13, color=t['heading'], name=t['font']))
                    r += 1
                for b in s['blocks']:
                    if b['type'] == 'paragraph':
                        put(ws, r, 1, strip_emphasis(b['text'])).alignment = wrap
                        r += 1
                    elif b['type'] == 'bullets':
                        for i, item in enumerate(b['items'], 1):
                            mark = f'{i}. ' if b.get('ordered') else BULLET + ' '
                            put(ws, r, 1, mark + strip_emphasis(item)).alignment = wrap
                            r += 1
                    elif b['type'] == 'quote':
                        put(ws, r, 1, f'"{strip_emphasis(b["text"])}"' + (f' ({b["by"]})' if b.get('by') else ''),
                            Font(italic=True, name=t['font'], color=t['text'])).alignment = wrap
                        r += 1
                    elif b['type'] == 'code':
                        for ln in b['text'].split('\n'):
                            put(ws, r, 1, ln, Font(name=t['mono'], color=t['text']))
                            r += 1
                    elif b['type'] == 'table' and id(b) in index:
                        put(ws, r, 1, f'Table: see the sheet "{index[id(b)]}".',
                            Font(italic=True, color=t['muted'], name=t['font']))
                        r += 1
                    elif b['type'] == 'chart':
                        put(ws, r, 1, f'Chart: {b["title"]}. {chart_summary(b)}',
                            Font(italic=True, color=t['muted'], name=t['font'])).alignment = wrap
                        r += 1
            continue
        if sh['kind'] == 'table':
            b = sh['table']
            cols, rows = b['columns'], b['rows']
            write_table(ws, cols, rows, formats=formats_of(b))
            ws.freeze_panes = 'A2'  # bold frozen header row (F4, A2)
            if rows:
                ws.auto_filter.ref = f'A1:{get_column_letter(len(cols))}{len(rows) + 1}'
            free_col = len(cols) + 2
        else:
            cols, rows, free_col = [], [], 1
        top = 1
        summary_font = Font(italic=True, color=t['muted'], name=t['font'])
        for b in sh['charts']:
            # every chart sits beside its data with a one-line summary (F4, A3)
            match = _chart_matches_table(b, sh['table']) if sh['kind'] == 'table' else None
            if match and match[1] == list(range(match[1][0], match[1][0] + len(match[1]))):
                label_col, series_cols = match
                put(ws, top, free_col, f'{b["title"]}: {chart_summary(b)}', summary_font)
                cats = Reference(ws, min_col=label_col + 1, min_row=2, max_row=len(rows) + 1)
                data = Reference(ws, min_col=series_cols[0] + 1, max_col=series_cols[-1] + 1, min_row=1,
                                 max_row=len(rows) + 1)
                add_chart(ws, b, f'{get_column_letter(free_col)}{top + 1}', cats, data)
                top += 21
                continue
            ccols, crows = chart_rows(b)
            ccols[0] = 'Label'
            write_table(ws, ccols, crows, r0=top, c0=free_col)
            if sh['kind'] == 'chart' and top == 1:
                ws.freeze_panes = 'A2'
            put(ws, top + len(crows) + 1, free_col, f'{b["title"]}: {chart_summary(b)}', summary_font)
            cats = Reference(ws, min_col=free_col, min_row=top + 1, max_row=top + len(crows))
            data = Reference(ws, min_col=free_col + 1, max_col=free_col + len(ccols) - 1, min_row=top,
                             max_row=top + len(crows))
            add_chart(ws, b, f'{get_column_letter(free_col + len(ccols) + 1)}{top}', cats, data)
            top += max(len(crows) + 3, 20)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- Markdown ----------


def _md_html(text: str) -> str:
    """No raw HTML or entities in a Markdown file: & and < are written as entities (X1, X3)."""
    return text.replace('&', '&amp;').replace('<', '&lt;')


def _md_escape(text: str) -> str:
    """Keep plain text from turning into Markdown structure or HTML: leading #, >, list markers, a leading [ (a link
    reference definition could load a remote image), ![ (an image), & and <."""
    lines = []
    for ln in _md_html(text).split('\n'):
        ln = re.sub(r'^(\s*)([#>+\[]|[-*](?=\s))', r'\1\\\2', ln)
        ln = re.sub(r'^(\s*\d+)([.)])(?=\s)', r'\1\\\2', ln)
        ln = ln.replace('![', '!\\[')
        lines.append(ln)
    return '\n'.join(lines)


def _md_line(text: str) -> str:
    """A title, heading or byline: escaped like text, on one line."""
    return _md_escape(str(text).replace('\n', ' '))


def _md_cell(v, fmt: str | None = None) -> str:
    return _md_html(show(v, fmt)).replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ').replace('![', '!\\[')


def _md_table(cols, rows, formats=None) -> str:
    formats = formats or [None] * len(cols)
    out = ['| ' + ' | '.join(_md_cell(c) or ' ' for c in cols) + ' |',
           '| ' + ' | '.join('---:' if rows and all(is_num(r[j]) or r[j] is None for r in rows) else '---'
                             for j in range(len(cols))) + ' |']
    out += ['| ' + ' | '.join(_md_cell(v, formats[j]) for j, v in enumerate(r)) + ' |' for r in rows]
    return '\n'.join(out)


def _md(spec: dict, name: str) -> bytes:
    """F5: CommonMark with GFM tables, one # title, headings that never skip a level (## for level 1)."""
    out = [f'# {_md_line(spec["title"])}']
    if spec.get('subtitle'):
        out.append(f'*{_md_line(spec["subtitle"])}*')
    for sec in spec['sections']:
        if sec['heading']:
            out.append('#' * (sec['level'] + 1) + ' ' + _md_line(sec['heading']))
        for b in sec['blocks']:
            kind = b['type']
            if kind == 'paragraph':
                out.append(_md_escape(b['text']))
            elif kind == 'bullets':
                out.append('\n'.join((f'{i}. ' if b.get('ordered') else '- ') + _md_escape(x).replace('\n', ' ')
                                     for i, x in enumerate(b['items'], 1)))
            elif kind == 'quote':
                q = '\n'.join('> ' + ln for ln in _md_escape(b['text']).split('\n'))
                out.append(q + (f'\n>\n> *{_md_line(b["by"])}*' if b.get('by') else ''))
            elif kind == 'code':
                fence = '```'
                while fence in b['text']:
                    fence += '`'
                out.append(f'{fence}{b.get("lang") or ""}\n{b["text"]}\n{fence}')
            elif kind == 'table':
                out.append(_md_table(b['columns'], b['rows'], formats_of(b)))
            elif kind == 'chart':
                cols, rows = chart_rows(b)
                cols[0] = 'Label'
                out.append(f'**Chart: {_md_line(b["title"])}** ({b["kind"]} chart)')
                out.append(_md_table(cols, rows))
                out.append(f'*{_md_line(chart_summary(b))}*')
    return ('\n\n'.join(out) + '\n').encode('utf-8')
