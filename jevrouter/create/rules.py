"""The ruleset for created files (docs/RULES-files.md): the rule list served by GET /api/rules, the RuleResult each
check produces, and verify(), which reopens a rendered file with its own library and checks it (V1-V4, L4, plus the
per-format F, X and A rules that can be read back from the file)."""
import io
import re
import zipfile
from dataclasses import asdict, dataclass

MAX_BYTES = 15 * 1024 * 1024


@dataclass
class RuleResult:
    id: str
    severity: str  # block | fix | warn
    ok: bool
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


class SpecError(ValueError):
    """A block rule failed: the file is not produced. rule_id says which rule."""

    def __init__(self, rule_id: str, message: str):
        super().__init__(f'{rule_id}: {message}')
        self.rule_id, self.message = rule_id, message


_GROUPS = {'S': 'Content spec', 'L': 'Size limits', 'F': 'Structure and style', 'X': 'Safety',
           'V': 'Verification', 'A': 'Accessibility'}


def _rule(rid: str, severity: str | None, text: str) -> dict:
    return {'id': rid, 'group': _GROUPS[rid[0]], 'text': text, 'severity': severity, 'enforced': True}


RULES: list[dict] = [
    _rule('S1', 'block', 'The spec validates against the DocSpec schema: known block types only, required fields present.'),
    _rule('S2', 'block', 'At least one section with at least one non-empty block.'),
    _rule('S3', 'fix', 'A title of 1 to 120 characters. Missing: taken from the first heading or the request.'),
    _rule('S4', 'fix', 'Text is plain: no HTML, no raw Markdown syntax inside paragraphs (converted to plain text, with '
                       'bold and italic kept as runs where the format supports them).'),
    _rule('S5', 'fix', 'Numbers in tables and charts are numbers, not strings ("1,200" becomes 1200; "$1,200.50" and "12%" '
                       'become numbers that keep their money or percent format). Non-numeric chart '
                       'values drop that point.'),
    _rule('S6', 'fix', 'Every table row has as many cells as there are columns (short rows padded, long rows trimmed).'),
    _rule('S7', 'block', 'Facts in the file come from the conversation, attached files or the model\'s answer in this '
                         'run; the spec never asks the renderer to fetch anything.'),
    _rule('L1', 'block', 'Spec at most 60 KB of JSON (table rows are limited by L2 instead); at most 40 sections and 30 '
                         'blocks per section.'),
    _rule('L2', 'fix', 'Tables: at most 2,000 rows and 30 columns in XLSX; 200 rows in PDF, DOCX and MD (the rest noted); '
                       '12 rows per slide (split across slides).'),
    _rule('L3', 'fix', 'Slides: at most 6 bullets per slide and 18 words per bullet; longer content moves to the slide '
                       'notes.'),
    _rule('L4', 'block', 'Output file at most 15 MB.'),
    _rule('L5', None, 'The model is asked for the spec once per request; conversions to another format reuse the stored '
                      'spec (0 LLM tokens).'),
    _rule('F1', 'fix', 'PDF: A4 (Letter when asked for), 2 cm margins, page numbers, the title in the document '
                       'properties, headings as PDF outline entries.'),
    _rule('F2', 'fix', 'DOCX: built-in Heading 1 to 3 styles (so a table of contents works), real Word tables with a '
                       'header row, core properties set.'),
    _rule('F3', 'fix', 'PPTX: 16:9; a title slide, then one slide per section; native charts (not pictures); speaker '
                       'notes for overflow text.'),
    _rule('F4', 'fix', 'XLSX: one sheet per table (sheet names at most 31 characters, unique, none of []:*?/\\), a bold '
                       'frozen header row, column widths fitted, numbers stored as numbers, native charts next to '
                       'their data.'),
    _rule('F5', 'fix', 'Markdown: CommonMark with GFM tables; one # title; headings never skip a level.'),
    _rule('F6', 'fix', 'One theme per file (font, colours); body text at least 10 pt in PDF and DOCX and 18 pt on '
                       'slides.'),
    _rule('F7', 'fix', 'File names: lowercase words joined by hyphens from the title, the right extension, at most 80 '
                       'characters, no path separators.'),
    _rule('X1', 'block', 'No macros or active content: never .docm, .xlsm or .pptm, no embedded scripts, no OLE objects.'),
    _rule('X2', 'fix', 'Spreadsheet formula injection: a cell whose text starts with =, +, -, @, tab or CR is stored as '
                       'text with a leading apostrophe, unless it is a formula the spec marked as one, built only from '
                       'cell references and SUM, AVERAGE, MIN, MAX, COUNT or ROUND.'),
    _rule('X3', 'fix', 'No external references: no remote images, no external links in formulas, no linked objects. '
                       'Web addresses appear only as text.'),
    _rule('X4', 'block', 'Content Jev would block is not written to a file: the request goes through the same safety '
                         'check as any question, and so does the spec text.'),
    _rule('X5', None, 'Created files are stored like uploads (by id, never by user path). Sandbox files stay in memory '
                      'and disappear with the sandbox.'),
    _rule('V1', 'block', 'The file reopens with its own library (pypdf, python-docx, python-pptx, openpyxl) or parses as '
                         'Markdown.'),
    _rule('V2', 'warn', 'The title and every section heading appear in the reopened file\'s text.'),
    _rule('V3', 'warn', 'Page, slide or sheet counts match the spec (after the L2 and L3 splits).'),
    _rule('V4', 'warn', 'Every chart in the spec exists in PPTX or XLSX, is drawn in PDF, or appears as a table in DOCX '
                        'and MD.'),
    _rule('A1', 'fix', 'Headings are real headings (styles, outline entries, slide titles), never bold paragraphs.'),
    _rule('A2', 'fix', 'Tables have a header row marked as a header.'),
    _rule('A3', 'warn', 'Charts carry a text title, and a one-line summary of what they show appears next to them.'),
    _rule('A4', 'fix', 'Colour is never the only way information is shown; theme colours meet 4.5:1 contrast for text.'),
]


# ---------- verify ----------


def _norm(text: str) -> str:
    s = re.sub(r'[\u200b-\u200d\ufeff]', '', str(text)).replace('**', '').replace('*', '')
    return re.sub(r'\s+', ' ', s).strip().lower()


def _found(needle: str, corpus: str) -> bool:
    n = _norm(needle)
    if not n or n in corpus:
        return True
    squashed = corpus.replace(' ', '')
    return n.replace(' ', '') in squashed  # PDF text extraction can drop or add spaces at line breaks


def _zip_checks(data: bytes) -> tuple[list[str], list[str]]:
    """(active content found, external references found) inside an OOXML package (X1, X3)."""
    active, external = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            low = n.lower()
            if 'vbaproject' in low or 'activex' in low or 'oleobject' in low or ('embeddings/' in low and low.endswith('.bin')):
                active.append(n)
            if low.endswith('.rels'):
                rels = z.read(n).decode('utf-8', 'replace')
                if 'TargetMode="External"' in rels:
                    external.append(n)
        types = z.read('[Content_Types].xml').decode('utf-8', 'replace') if '[Content_Types].xml' in z.namelist() else ''
        if 'macroEnabled' in types or 'vbaProject' in types:
            active.append('[Content_Types].xml')
    return active, external


def _count_charts(data: bytes, folder: str) -> tuple[int, list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if re.match(rf'{folder}/charts/chart\d+\.xml$', n)]
        titles = [z.read(n).decode('utf-8', 'replace') for n in names]
    return len(names), titles


def _pdf_info(data: bytes, spec: dict) -> dict:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = len(reader.pages)
    text = '\n'.join((p.extract_text() or '') for p in reader.pages)

    def walk(items, depth=0):
        for it in items:
            if isinstance(it, list):
                yield from walk(it, depth + 1)
            else:
                yield depth, str(it.title)
    outline = list(walk(reader.outline))
    meta = reader.metadata or {}
    box = reader.pages[0].mediabox
    raw = data
    return {'text': text + '\n' + '\n'.join(t for _, t in outline), 'pages': pages, 'outline': outline,
            'title': str(meta.get('/Title') or ''), 'size': (float(box.width), float(box.height)),
            'first_page': reader.pages[0].extract_text() or '',
            'active': re.findall(rb'/(JavaScript|JS|Launch|EmbeddedFiles?|RichMedia|XFA)\b', raw),
            'external': re.findall(rb'/(URI|GoToR|SubmitForm|ImportData)\b', raw)}


def _docx_info(data: bytes) -> dict:
    from docx import Document
    from docx.oxml.ns import qn
    doc = Document(io.BytesIO(data))
    paras = [(p.style.name if p.style is not None else '', p.text) for p in doc.paragraphs]
    cells = []
    header_marked = []
    for t in doc.tables:
        for row in t.rows:
            cells.extend(c.text for c in row.cells)
        tr_pr = t.rows[0]._tr.trPr if len(t.rows) else None
        header_marked.append(tr_pr is not None and tr_pr.find(qn('w:tblHeader')) is not None)
    normal = doc.styles['Normal'].font.size
    return {'text': '\n'.join([t for _, t in paras] + cells), 'paras': paras, 'tables': len(doc.tables),
            'header_marked': header_marked, 'title': doc.core_properties.title or '',
            'normal_pt': normal.pt if normal is not None else None}


def _pptx_info(data: bytes) -> dict:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    slides, texts, body_sizes, tables_first_row, notes = [], [], [], [], []
    for slide in prs.slides:
        title = slide.shapes.title.text_frame.text if slide.shapes.title is not None else ''
        slides.append(title)
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
                if shape.name == 'Body':
                    body_sizes += [r.font.size.pt for p in shape.text_frame.paragraphs for r in p.runs
                                   if r.font.size is not None]
            if getattr(shape, 'has_table', False) and shape.has_table:
                tables_first_row.append(shape.table.first_row)
                texts += [c.text for row in shape.table.rows for c in row.cells]
            if getattr(shape, 'has_chart', False) and shape.has_chart and shape.chart.has_title:
                texts.append(shape.chart.chart_title.text_frame.text)
        notes.append(slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else '')
    return {'text': '\n'.join(slides + texts), 'slides': slides, 'size': (prs.slide_width, prs.slide_height),
            'body_sizes': body_sizes, 'tables_first_row': tables_first_row, 'notes': notes,
            'title': prs.core_properties.title or ''}


def _xlsx_info(data: bytes) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    sheets, texts, cells = [], [wb.properties.title or ''], []
    for ws in wb.worksheets:
        sheets.append({'name': ws.title, 'freeze': ws.freeze_panes, 'a1_bold': bool(ws['A1'].font and ws['A1'].font.b),
                       'ws': ws})
        texts.append(ws.title)
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    cells.append(c)
                    if isinstance(c.value, str):
                        texts.append(c.value)
    return {'text': '\n'.join(texts), 'sheets': sheets, 'cells': cells, 'title': wb.properties.title or '', 'wb': wb}


def _md_info(data: bytes) -> dict:
    import html
    text = data.decode('utf-8')
    heads, fence, prose = [], None, []
    for ln in text.split('\n'):
        m = re.match(r'^(`{3,}|~{3,})', ln)
        if m and (fence is None or ln.startswith(fence)):
            fence = None if fence else m.group(1)
            continue
        if fence:
            continue
        prose.append(ln)
        h = re.match(r'^(#{1,6}) (.*)$', ln)
        if h:
            heads.append((len(h.group(1)), h.group(2)))
    # the text as a reader sees it (escapes and entities resolved), for finding titles and headings
    shown = html.unescape(re.sub(r'\\([\\`*_{}\[\]()#+\-.!|>])', r'\1', text))
    return {'text': shown, 'heads': heads, 'prose': '\n'.join(prose)}


# Markdown outside code blocks that a viewer would load or run: raw HTML tags, images (inline or by reference) and
# reference definitions pointing at a web address.
_MD_REMOTE = re.compile(r'!\[[^\]]*\]\(\s*<?(?:https?:)?//|!\[[^\]]*\]\[[^\]]*\]|^\s{0,3}\[[^\]]+\]:\s*<?(?:https?:)?//|'
                        r'<\s*(?:img|image|iframe|object|embed|link|svg|video|audio|source|picture|input|form)\b', re.I | re.M)
_MD_ACTIVE = re.compile(r'<\s*(?:script|style|iframe|object|embed)\b|<[A-Za-z][^<>]*\son[a-z]+\s*=|'
                        r'<[A-Za-z][^<>]*(?:javascript|vbscript):', re.I)


def _embedded_formulas(data: bytes) -> list[str]:
    """Formulas inside the workbooks a PPTX embeds for its charts (X2): python-pptx writes each chart's data there, and
    any "=..." label written as-is would become a live formula when someone picks Edit Data."""
    found = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if re.match(r'ppt/embeddings/.*\.xlsx$', n, re.I):
                with zipfile.ZipFile(io.BytesIO(z.read(n))) as inner:
                    for m in inner.namelist():
                        if m.startswith('xl/worksheets/') and m.endswith('.xml'):
                            found += re.findall(r'<f(?:\s[^>]*)?>([^<]*)</f>', inner.read(m).decode('utf-8', 'replace'))
    return found


def _lost_chars(spec: dict, fonts) -> tuple[int, int, str]:
    """(characters the PDF fonts could not draw, all non-space characters, a few examples) over the spec's text."""
    texts = [spec['title'], spec.get('subtitle') or '']
    for s in spec['sections']:
        texts.append(s['heading'])
        for b in s['blocks']:
            texts += [str(b.get(k) or '') for k in ('text', 'title', 'by')] + [str(x) for x in b.get('items') or []]
            texts += [str(x) for x in (b.get('columns') or []) + (b.get('labels') or [])]
            texts += [str(v) for r in b.get('rows') or [] for v in r if isinstance(v, str)]
            texts += [str(x.get('name') or '') for x in b.get('series') or []]
    lost, total, examples = 0, 0, []
    for t in texts:
        drawn, _ = fonts.fit(t)
        for a, d in zip(t, drawn):
            if a.isspace():
                continue
            total += 1
            if a != d and d == '?':
                lost += 1
                if a not in examples and len(examples) < 5:
                    examples.append(a)
    return lost, total, ''.join(examples)


def verify(spec: dict, fmt: str, data: bytes) -> list[RuleResult]:
    """Reopen the rendered file and check it. Raises SpecError for V1 (does not reopen), L4 (too big) and X1 (active
    content); everything else comes back as one RuleResult per rule."""
    from . import themes
    from .render import _Fonts, chart_summary, safe_formula, sheet_name, theme_name, xlsx_plan
    from .spec import FORMATS, normalize

    if fmt not in FORMATS:
        raise SpecError('X1', f'Files are made as {", ".join(FORMATS)} only, not {str(fmt)[:12]!r}.')
    spec, _ = normalize(spec, fmt)
    size = len(data)
    if size > MAX_BYTES:
        raise SpecError('L4', f'The file is {size / 1e6:.1f} MB; the limit is {MAX_BYTES // (1024 * 1024)} MB.')
    try:
        info = {'pdf': lambda: _pdf_info(data, spec), 'docx': lambda: _docx_info(data), 'pptx': lambda: _pptx_info(data),
                'xlsx': lambda: _xlsx_info(data), 'md': lambda: _md_info(data)}[fmt]()
    except Exception as e:
        raise SpecError('V1', f'The {fmt.upper()} file does not reopen: {str(e)[:160]}')
    out = [RuleResult('V1', 'block', True, f'reopened with {_LIB[fmt]}'), RuleResult('L4', 'block', True, f'{size:,} bytes')]
    sections = spec['sections']
    headed = [s for s in sections if s['heading']]
    charts = [b for s in sections for b in s['blocks'] if b['type'] == 'chart']
    tables = [b for s in sections for b in s['blocks'] if b['type'] == 'table']
    corpus = _norm(info['text'])
    pdf_fonts = _Fonts(themes.get('clean')) if fmt == 'pdf' else None
    fix = (lambda s: pdf_fonts.fit(s)[0]) if pdf_fonts else (lambda s: s)  # what the PDF could draw

    # V2: title and headings in the text, as written (a PDF that drew them as "????" has lost them)
    missing = [x for x in [spec['title']] + [s['heading'] for s in headed] if not _found(x, corpus)]
    lost_note = ''
    if pdf_fonts is not None:
        lost, total, examples = _lost_chars(spec, pdf_fonts)
        if lost and lost * 2 >= total:
            raise SpecError('V1', f'The PDF fonts on this server cannot draw {lost:,} of the {total:,} characters '
                                  f'(for example {examples}), so most of the text would print as "?". Make it as Word, '
                                  f'PowerPoint, Excel or Markdown, or set TRACEGRAPH_PDF_FONT to a font that has them.')
        if lost:
            lost_note = f'{lost:,} characters could not be drawn in the PDF font and show as "?" (for example {examples})'
            missing = missing or ['']
    if fmt == 'xlsx':
        names = {sh['name'].lower() for sh in info['sheets']}
        missing = [x for x in missing if sheet_name(x, set()).lower() not in names]
    note = 'not found in the file: ' + '; '.join(m[:60] for m in missing[:5] if m) if any(missing) else ''
    out.append(RuleResult('V2', 'warn', not missing, 'title and headings found' if not missing else
                          '; '.join(x for x in (note, lost_note) if x)))

    # V3: counts; V4: charts; format rules
    if fmt == 'pdf':
        want = 1 + len(headed)
        got = len(info['outline'])
        out.append(RuleResult('V3', 'warn', info['pages'] >= 1 and got == want,
                              f'{info["pages"]} pages, {got} outline entries for {want} headings'))
        lost = [b['title'] for b in charts if not _found(fix(b['title']), corpus)]
        out.append(RuleResult('V4', 'warn', not lost, f'{len(charts)} charts drawn' if not lost else
                              'charts not found: ' + '; '.join(lost[:3])))
        w, h = info['size']
        paper = (612, 792) if spec.get('paper') == 'letter' else (595.27, 841.89)
        problems = []
        if abs(w - paper[0]) > 2 or abs(h - paper[1]) > 2:
            problems.append(f'page size {w:.0f}x{h:.0f}')
        if _norm(info['title']) != _norm(fix(spec['title'])):
            problems.append('title missing from the document properties')
        if not info['outline']:
            problems.append('no outline')
        if 'page 1' not in _norm(info['first_page']):
            problems.append('no page number')
        out.append(RuleResult('F1', 'fix', not problems, '; '.join(problems) or
                              f'{"Letter" if spec.get("paper") == "letter" else "A4"}, page numbers, outline, title set'))
        out.append(RuleResult('F6', 'fix', True, 'one theme; body text 10.5 pt'))
        if info['active']:
            raise SpecError('X1', 'The PDF holds active content: ' + ', '.join(sorted({a.decode() for a in info['active']})))
        out.append(RuleResult('X1', 'block', True, 'no scripts or embedded files'))
        out.append(RuleResult('X3', 'fix', not info['external'], 'no links or remote actions' if not info['external']
                              else 'external actions found'))
        out.append(RuleResult('A1', 'fix', got == want, 'headings are outline entries'))
        out.append(RuleResult('A2', 'fix', True, 'table header rows repeat on every page'))
    elif fmt == 'docx':
        heads = [(int(st.split()[-1]), t) for st, t in info['paras'] if re.match(r'Heading [1-9]$', st)]
        from .render import chart_rows, formats_of, table_groups
        font = 'Times-Roman' if themes.get(theme_name(spec, None), paper=True)['font'] == 'Georgia' else 'Helvetica'
        want_tables = sum(len(table_groups(b['columns'], b['rows'], 'docx', spec.get('paper'), formats_of(b), font))
                          for b in tables)
        want_tables += sum(len(table_groups(*chart_rows(b), 'docx', spec.get('paper'), None, font)) for b in charts)
        out.append(RuleResult('V3', 'warn', len(heads) == len(headed) and info['tables'] == want_tables,
                              f'{len(heads)} headings for {len(headed)} sections, {info["tables"]} tables for '
                              f'{want_tables}'))
        lost = [b['title'] for b in charts if not _found(b['title'], corpus)]
        out.append(RuleResult('V4', 'warn', not lost and info['tables'] >= want_tables,
                              f'{len(charts)} charts as tables' if not lost else 'charts not found: ' + '; '.join(lost)))
        has_title = any(st == 'Title' for st, _ in info['paras'])
        ok = has_title and len(heads) == len(headed) and bool(info['title'])
        out.append(RuleResult('F2', 'fix', ok, 'Title and Heading styles, core properties set' if ok else
                              'heading styles or core properties missing'))
        pt = info['normal_pt'] or 0
        out.append(RuleResult('F6', 'fix', pt >= 10, f'body text {pt:g} pt'))
        active, external = _zip_checks(data)
        if active:
            raise SpecError('X1', 'The file holds active content: ' + ', '.join(active[:3]))
        out.append(RuleResult('X1', 'block', True, 'no macros, scripts or OLE objects'))
        out.append(RuleResult('X3', 'fix', not external, 'no external references' if not external else
                              'external references in ' + ', '.join(external[:3])))
        out.append(RuleResult('A1', 'fix', len(heads) == len(headed), 'headings use Heading styles'))
        out.append(RuleResult('A2', 'fix', all(info['header_marked']), 'every table has a marked header row'
                              if all(info['header_marked']) else 'a table has no marked header row'))
    elif fmt == 'pptx':
        want = 1 + len(sections)
        out.append(RuleResult('V3', 'warn', len(info['slides']) == want, f'{len(info["slides"])} slides for {want}'))
        n, xml = _count_charts(data, 'ppt')
        out.append(RuleResult('V4', 'warn', n == len(charts), f'{n} native charts for {len(charts)}'))
        w, h = info['size']
        notes_ok = all(not s.get('notes') or info['notes'][i + 1].strip() for i, s in enumerate(sections)
                       if i + 1 < len(info['notes']))
        ok = abs(w / h - 16 / 9) < 0.01 and _norm(info['slides'][0]) == _norm(spec['title']) and notes_ok
        out.append(RuleResult('F3', 'fix', ok, '16:9, title slide, one slide per section, notes kept' if ok else
                              'slide size, title slide or notes are off'))
        small = [s for s in info['body_sizes'] if s < 18]
        out.append(RuleResult('F6', 'fix', not small, 'body text at least 18 pt' if not small else
                              f'{len(small)} body runs under 18 pt'))
        active, external = _zip_checks(data)
        if active:
            raise SpecError('X1', 'The file holds active content: ' + ', '.join(active[:3]))
        out.append(RuleResult('X1', 'block', True, 'no macros, scripts or OLE objects'))
        formulas = _embedded_formulas(data)
        out.append(RuleResult('X2', 'fix', not formulas, 'no formulas in the chart data' if not formulas else
                              'formulas in the chart data: ' + ', '.join(f[:40] for f in formulas[:3])))
        out.append(RuleResult('X3', 'fix', not external, 'no external references' if not external else
                              'external references in ' + ', '.join(external[:3])))
        titled = all(_norm(t) == _norm(s['heading']) for t, s in zip(info['slides'][1:], sections))
        out.append(RuleResult('A1', 'fix', titled, 'every slide has a real title' if titled else
                              'a slide title does not match its section'))
        out.append(RuleResult('A2', 'fix', all(info['tables_first_row']), 'table header rows marked'))
    elif fmt == 'xlsx':
        plan = xlsx_plan(spec)
        got = [sh['name'] for sh in info['sheets']]
        out.append(RuleResult('V3', 'warn', got == [p['name'] for p in plan], f'{len(got)} sheets for {len(plan)}'))
        n, xml = _count_charts(data, 'xl')
        out.append(RuleResult('V4', 'warn', n == len(charts), f'{n} native charts for {len(charts)}'))
        problems = []
        for p, sh in zip(plan, info['sheets']):
            if p['kind'] == 'table' and (sh['freeze'] != 'A2' or not sh['a1_bold']):
                problems.append(f'{sh["name"]}: header not bold and frozen')
            if p['kind'] == 'table':
                ws, b = sh['ws'], p['table']
                for i, row in enumerate(b['rows'][:50], 2):
                    for j, v in enumerate(row, 1):
                        if isinstance(v, (int, float)) and not isinstance(v, bool) and \
                                not isinstance(ws.cell(row=i, column=j).value, (int, float)):
                            problems.append(f'{sh["name"]}: a number stored as text')
                            break
        bad_names = [g for g in got if len(g) > 31 or re.search(r'[\[\]:*?/\\]', g)]
        problems += [f'bad sheet name {g}' for g in bad_names]
        out.append(RuleResult('F4', 'fix', not problems, '; '.join(problems[:3]) or
                              'bold frozen headers, numbers as numbers, valid sheet names'))
        active, external = _zip_checks(data)
        if active:
            raise SpecError('X1', 'The file holds active content: ' + ', '.join(active[:3]))
        out.append(RuleResult('X1', 'block', True, 'no macros, scripts or OLE objects'))
        risky = []
        for c in info['cells']:
            v = c.value
            if c.data_type == 'f' or (isinstance(v, str) and v.startswith('=')):
                if not safe_formula(str(v)):
                    risky.append(c.coordinate)
            elif isinstance(v, str) and v.lstrip(' \u00a0')[:1] in ('=', '+', '-', '@', '\t', '\r'):
                risky.append(c.coordinate)
        out.append(RuleResult('X2', 'fix', not risky, 'no cell can run as a formula' if not risky else
                              'cells that could run as formulas: ' + ', '.join(risky[:5])))
        out.append(RuleResult('X3', 'fix', not external, 'no external references' if not external else
                              'external references in ' + ', '.join(external[:3])))
        heads = all(sh['a1_bold'] for p, sh in zip(plan, info['sheets']) if p['kind'] == 'table')
        out.append(RuleResult('A2', 'fix', heads, 'header rows bold and frozen'))
    else:  # md
        heads = info['heads']
        titles = [h for h in heads if h[0] == 1]
        out.append(RuleResult('V3', 'warn', len(heads) == 1 + len(headed),
                              f'{len(heads)} headings for {1 + len(headed)}'))
        lost = [b['title'] for b in charts if f'chart: {_norm(b["title"])}' not in corpus]
        out.append(RuleResult('V4', 'warn', not lost, f'{len(charts)} charts as tables' if not lost else
                              'charts not found: ' + '; '.join(lost)))
        skips = [t for (a, _), (b2, t) in zip(heads, heads[1:]) if b2 > a + 1]
        ok = len(titles) == 1 and heads and heads[0][0] == 1 and not skips
        out.append(RuleResult('F5', 'fix', bool(ok), 'one # title, no skipped heading levels' if ok else
                              'title or heading levels are off'))
        if _MD_ACTIVE.search(info['prose']):
            raise SpecError('X1', 'The Markdown file holds raw HTML that could run in a viewer.')
        out.append(RuleResult('X1', 'block', True, 'no raw HTML'))
        remote = _MD_REMOTE.findall(info['prose'])
        out.append(RuleResult('X3', 'fix', not remote, 'no remote images or raw HTML' if not remote else
                              'remote images or raw HTML: ' + ', '.join(r[:30] for r in remote[:3])))
        out.append(RuleResult('A1', 'fix', len(heads) == 1 + len(headed), 'headings are Markdown headings'))

    # A3: chart titles and summaries next to them; A4: theme contrast
    if charts:
        if fmt == 'xlsx':
            _, xml = _count_charts(data, 'xl')
            titled = sum('<c:title>' in x or '<title>' in x for x in xml) >= len(charts)
        elif fmt == 'pptx':
            _, xml = _count_charts(data, 'ppt')
            titled = sum('<c:title>' in x for x in xml) >= len(charts)
        else:
            titled = all(_found(fix(b['title']), corpus) for b in charts)
        summaries = all(_found(fix(chart_summary(b)), corpus) for b in charts)
        out.append(RuleResult('A3', 'warn', titled and summaries, 'every chart has a title and a summary line'
                              if titled and summaries else 'a chart is missing its title or summary line'))
    ratio, pair = themes.worst_contrast(theme_name(spec, None))
    out.append(RuleResult('A4', 'fix', ratio >= 4.5, f'lowest text contrast {ratio:.1f}:1 ({pair})'))
    return out


_LIB = {'pdf': 'pypdf', 'docx': 'python-docx', 'pptx': 'python-pptx', 'xlsx': 'openpyxl', 'md': 'a UTF-8 Markdown read'}
