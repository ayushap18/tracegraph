"""The DocSpec: the one compact JSON a model writes for a file (no layout), its schema, and the code that cleans it up.

normalize() applies the content and size rules (S1-S7, L1-L3) for one format and returns the fixed spec with one
RuleResult per check. from_markdown() and from_table() build a spec from an answer or an attached table with no LLM.
"""
import copy
import html
import json
import math
import re
import unicodedata

from .rules import RuleResult, SpecError

FORMATS = ('pdf', 'docx', 'pptx', 'xlsx', 'md')
EXTENSIONS = {f: '.' + f for f in FORMATS}
BLOCKS = ('paragraph', 'bullets', 'table', 'chart', 'quote', 'code')
CHART_KINDS = ('bar', 'line', 'pie')
CHART_ALIASES = {'column': 'bar', 'columns': 'bar', 'bars': 'bar', 'histogram': 'bar', 'area': 'line', 'lines': 'line',
                 'scatter': 'line', 'donut': 'pie', 'doughnut': 'pie'}
THEMES = ('clean', 'dark', 'warm')

MAX_SPEC_BYTES = 60_000
MAX_SECTIONS = 40
MAX_BLOCKS = 30
MAX_TITLE = 120
TABLE_ROWS = {'pdf': 200, 'docx': 200, 'md': 200, 'xlsx': 2000, 'pptx': 12}
XLSX_COLS = 30
SLIDE_COLS = 8
SLIDE_BULLETS = 6
SLIDE_BULLETS_BESIDE = 3  # text next to a table or chart gets half the room
BULLET_WORDS = 18
SLIDE_PARA_WORDS = 40
SLIDE_CODE_LINES = 14

# ---------- the schema handed to the engine (strict: every property required, no $refs) ----------


def _obj(props: dict) -> dict:
    return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}


_STR = {'type': 'string'}
_STRS = {'type': 'array', 'items': _STR}
_CELL = {'anyOf': [_STR, {'type': 'number'}, {'type': 'null'}, _obj({'formula': _STR})]}
DOCSPEC_SCHEMA = _obj({
    'title': _STR, 'subtitle': _STR,
    'sections': {'type': 'array', 'items': _obj({
        'heading': _STR, 'level': {'type': 'integer', 'minimum': 1, 'maximum': 3}, 'notes': _STR,
        'blocks': {'type': 'array', 'items': {'anyOf': [
            _obj({'type': {'type': 'string', 'enum': ['paragraph']}, 'text': _STR}),
            _obj({'type': {'type': 'string', 'enum': ['bullets']}, 'items': _STRS, 'ordered': {'type': 'boolean'}}),
            _obj({'type': {'type': 'string', 'enum': ['table']}, 'columns': _STRS,
                  'rows': {'type': 'array', 'items': {'type': 'array', 'items': _CELL}}}),
            _obj({'type': {'type': 'string', 'enum': ['chart']}, 'kind': {'type': 'string', 'enum': list(CHART_KINDS)},
                  'title': _STR, 'labels': _STRS,
                  'series': {'type': 'array', 'items': _obj({'name': _STR, 'values': {
                      'type': 'array', 'items': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}}})}}),
            _obj({'type': {'type': 'string', 'enum': ['quote']}, 'text': _STR, 'by': _STR}),
            _obj({'type': {'type': 'string', 'enum': ['code']}, 'lang': _STR, 'text': _STR}),
        ]}},
    })},
})

# Keys each object may carry after normalize (anything else is dropped).
KEYS = {'spec': ('title', 'subtitle', 'format', 'theme', 'paper', 'sections'),
        'section': ('heading', 'level', 'blocks', 'notes'),
        'paragraph': ('type', 'text'), 'bullets': ('type', 'items', 'ordered'),
        'table': ('type', 'title', 'columns', 'rows', 'formats'), 'chart': ('type', 'kind', 'title', 'labels', 'series'),
        'quote': ('type', 'text', 'by'), 'code': ('type', 'lang', 'text')}
REQUIRED = {'paragraph': ('text',), 'bullets': ('items',), 'table': ('columns', 'rows'),
            'chart': ('labels', 'series'), 'quote': ('text',), 'code': ('text',)}
# S7: a spec asks for nothing outside itself; keys like these mean "go and fetch".
FETCH_KEYS = {'url', 'urls', 'src', 'href', 'image', 'images', 'img', 'fetch', 'include', 'import', 'link', 'links',
              'remote', 'file', 'path'}

# ---------- text: S4 plain text with **bold** / *italic* kept as the only inline markup ----------

_BAD_CHARS = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]')
_SCRIPT = re.compile(r'<(script|style|template|noscript|iframe|object|svg|math)\b[^>]*>.*?</\1\s*>', re.I | re.S)
# S4: any tag at all (a letter right after "<" or "</"), and comments. A list of tag names would let unknown ones
# (<details ontoggle=...>, <image src=...>) through; "a < b" is not a tag and stays.
_TAG = re.compile(r'<!--.*?(?:-->|$)|<![A-Za-z][^<>]*>|</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>', re.S)
_BOLD_TAG = re.compile(r'<(b|strong)\b[^>]*>(.*?)</\1\s*>', re.I | re.S)
_ITAL_TAG = re.compile(r'<(i|em)\b[^>]*>(.*?)</\1\s*>', re.I | re.S)
_BR = re.compile(r'<br\s*/?>', re.I)
_ENTITY = re.compile(r'&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]*);?', re.I)
_CODE_SPAN = re.compile(r'(`+)([^`]+?)\1')
_IMAGE = re.compile(r'!\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])')  # inline and reference images
_LINK = re.compile(r'\[([^\]]+)\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)')
_AUTOLINK = re.compile(r'<(https?://[^>\s]+)>')
_HEADING_MARK = re.compile(r'^\s{0,3}#{1,6}\s+', re.M)
_QUOTE_MARK = re.compile(r'^\s{0,3}>\s?', re.M)
_INLINE_CODE = re.compile(r'`+([^`]+?)`+')
_STRIKE = re.compile(r'~~(.+?)~~')
_UBOLD = re.compile(r'(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)')
_UITAL = re.compile(r'(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)')
_BOLD = re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*')
_ITAL = re.compile(r'(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])')
_LIST_LINE = re.compile(r'^\s*(?:[-*+\u2022]|\d{1,3}[.)])\s+(.*)$')


def clean_chars(text) -> str:
    """Characters XML documents cannot hold (control codes, lone surrogates) removed; tab, CR and newline kept."""
    return _BAD_CHARS.sub('', str(text))


def _strip_html(s: str) -> str:
    """Entities decoded first, then every tag removed, until nothing changes: "&lt;img src=x onerror=...&gt;" and
    "&amp;lt;script&amp;gt;" never come out as live tags."""
    for _ in range(5):
        before = s
        s = _SCRIPT.sub('', s)
        s = _BR.sub('\n', s)
        s = _BOLD_TAG.sub(r'**\2**', s)
        s = _ITAL_TAG.sub(r'*\2*', s)
        s = _TAG.sub('', s)
        if _ENTITY.search(s):
            s = clean_chars(html.unescape(s))
        if s == before:
            break
    return _TAG.sub('', s)


def plain(text, emphasis: bool = True) -> str:
    """S4: HTML and Markdown syntax turned into plain text. With emphasis, **bold** and *italic* stay as the only
    markup (renderers turn them into runs); without it they are removed too (headings, cells, labels). Inline code
    keeps its text as written (`Vec<String>`); renderers escape it for their format."""
    s = clean_chars('' if text is None else text)
    if '<' in s or '&' in s:
        spans = []

        def hold(m):
            spans.append(m.group(0))
            return f'\ue000{len(spans) - 1}\ue001'
        s = _CODE_SPAN.sub(hold, s.replace('\ue000', '').replace('\ue001', ''))
        s = _strip_html(s)
        s = re.sub('\ue000(\\d+)\ue001', lambda m: spans[int(m.group(1))] if int(m.group(1)) < len(spans) else '', s)
    s = _IMAGE.sub(lambda m: m.group(1), s)  # X3: no remote images, the alt text stays
    s = _LINK.sub(lambda m: f'{m.group(1)} ({m.group(2)})' if re.match(r'https?://', m.group(2), re.I)
                  and m.group(2) != m.group(1) else m.group(1), s)
    s = _AUTOLINK.sub(r'\1', s)
    s = _HEADING_MARK.sub('', s)
    s = _QUOTE_MARK.sub('', s)
    s = _INLINE_CODE.sub(r'\1', s)
    s = _STRIKE.sub(r'\1', s)
    s = _UBOLD.sub(r'**\1**', s)
    s = _UITAL.sub(r'*\1*', s)
    s = s.replace('***', '**')
    if s.count('**') % 2:
        s = s.replace('**', '')
    if not emphasis:
        s = _BOLD.sub(r'\1', s)
        s = _ITAL.sub(r'\1', s)
    lines = [re.sub(r'[ \t\u00a0]+', ' ', ln).strip() for ln in s.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def runs(text: str) -> list[tuple[str, bool, bool]]:
    """(text, bold, italic) pieces of one S4-clean string, for formats that keep emphasis as runs."""
    out, pos = [], 0
    pattern = re.compile(_BOLD.pattern + '|' + _ITAL.pattern)
    for m in pattern.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False, False))
        if m.group(1) is not None:
            inner = m.group(1)
            parts = [(p, True, it) for p, _, it in runs(inner)] if '*' in inner else [(inner, True, False)]
            out.extend(parts)
        else:
            out.append((m.group(2), False, True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False, False))
    return [r for r in out if r[0]]


def strip_emphasis(text: str) -> str:
    return ''.join(t for t, _, _ in runs(text))


def words(text: str) -> list[str]:
    return str(text).split()


# ---------- numbers: S5 ----------

_NUM = re.compile(r'^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$')
_CURRENCY = '$€£¥₹₩₽'


def finite(v) -> bool:
    """A number a float can hold: not NaN or infinite, and no int too big for a float (math.isfinite would raise)."""
    if isinstance(v, int):
        return abs(v) < 10 ** 308
    return math.isfinite(v)


def number(v, loose: bool = False):
    """A number from a cell or chart value, or None. Strict (tables): plain digits with optional thousands commas, so
    ids with leading zeros, dates and codes stay text. Loose (charts): currency signs, %, spaces and (1,200) too."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v if finite(v) else None
    s = str(v).strip().replace('\u2212', '-')
    if loose:
        neg = s.startswith('(') and s.endswith(')')
        s = re.sub(r'[\s\u00a0]', '', s.strip('()')).rstrip('%')
        sign = s[0] if s[:1] in '+-' else ''
        s = sign + s[len(sign):].lstrip(_CURRENCY)
        if neg and not sign:
            s = '-' + s
    if not _NUM.match(s):
        return None
    digits = re.sub(r'\D', '', s.split('.')[0])
    if (len(digits) > 1 and digits[0] == '0') or len(digits) > 15:
        return None
    f = float(s.replace(',', ''))
    if not math.isfinite(f):
        return None
    return int(f) if '.' not in s and f.is_integer() else f


_UNIT = re.compile(r'^(?P<neg>[-+\u2212]|\()?\s*(?P<cur>[' + _CURRENCY + r'])?\s*(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)'
                   r'(?:\.(?P<dec>\d+))?)\s*(?P<pct>%)?\)?$')
# XLSX number formats a table column may carry (normalize writes only these): currency with 0 or 2 decimals, percent
# with 0 to 2 decimals. Values are stored as numbers (a percent as a fraction, 12% -> 0.12).
NUMBER_FORMATS = {f'"{c}"#,##0{d}' for c in _CURRENCY for d in ('', '.00')} | {'0%', '0.0%', '0.00%'}


def unit_number(v) -> tuple[float | int, str] | None:
    """(value, kind) for a table cell like "$1,200.50" (kind "$") or "12.5%" (kind "%"), else None."""
    if not isinstance(v, str):
        return None
    m = _UNIT.match(v.strip())
    if not m or bool(m.group('cur')) == bool(m.group('pct')) or (v.strip().startswith('(') != v.strip().endswith(')')):
        return None
    n = number(m.group('num'))
    if n is None:
        return None
    if m.group('neg') in ('-', '\u2212', '('):
        n = -n
    if m.group('pct'):
        return round(n / 100, 12), '%' + str(min(len(m.group('dec') or ''), 2))
    return n, m.group('cur') + ('2' if m.group('dec') else '0')


def unit_format(kind: str) -> str:
    """The XLSX number format for a unit_number kind: "$2" -> '"$"#,##0.00', "%1" -> '0.0%'."""
    if kind[0] == '%':
        return '0' + ('.' + '0' * int(kind[1]) if kind[1] != '0' else '') + '%'
    return f'"{kind[0]}"#,##0' + ('.00' if kind[1] == '2' else '')


def show_number(v, fmt: str | None) -> str | None:
    """A number shown the way its column's format writes it ("$1,200.50", "12%"), or None with no format."""
    if not fmt or fmt not in NUMBER_FORMATS or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if fmt.endswith('%'):
        dec = len(fmt.split('.')[1]) - 1 if '.' in fmt else 0
        return f'{v * 100:.{dec}f}%'
    sym, dec = fmt[1], 2 if fmt.endswith('.00') else 0
    return f'{"-" if v < 0 else ""}{sym}{abs(v):,.{dec}f}'


def fmt_number(v) -> str:
    if v is None:
        return ''
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) < 1e15):
        return f'{int(v):,}'
    return f'{v:,.2f}' if abs(v) >= 100 else f'{v:.4g}'


def chart_summary(block: dict) -> str:
    """A3: one plain line saying what a chart shows, placed next to it in every format."""
    labels, series = block.get('labels') or [], block.get('series') or []
    parts = []
    for s in series[:3]:
        pts = [(labels[i] if i < len(labels) else str(i + 1), v) for i, v in enumerate(s.get('values') or [])
               if isinstance(v, (int, float))]
        if not pts:
            continue
        name = s.get('name') or 'Value'
        hi, lo = max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])
        if block.get('kind') == 'pie':
            total = sum(v for _, v in pts if v > 0) or 1
            parts.append(f'Largest share: {hi[0]} ({hi[1] / total:.0%}); smallest: {lo[0]} ({lo[1] / total:.0%}).')
            break
        if block.get('kind') == 'line' and len(pts) > 1:
            parts.append(f'{name} goes from {fmt_number(pts[0][1])} ({pts[0][0]}) to {fmt_number(pts[-1][1])} '
                         f'({pts[-1][0]}), highest at {hi[0]} ({fmt_number(hi[1])}).')
        else:
            prefix = f'{name}: highest' if len(series) > 1 else 'Highest'
            parts.append(f'{prefix} {hi[0]} ({fmt_number(hi[1])}), lowest {lo[0]} ({fmt_number(lo[1])}).')
    return ' '.join(parts) or 'No values to show.'


# ---------- normalize ----------


def _results() -> dict:
    return {}


def _note(res: dict, rid: str, severity: str, note: str):
    """Record one fix or warning under a rule (several notes for one rule are joined)."""
    r = res.get(rid)
    if r is None or r.ok:
        res[rid] = RuleResult(rid, severity, False, note)
    elif note not in r.note:
        r.note = (r.note + '; ' + note)[:400]


def _size_without_rows(spec) -> int:
    """L1 budget in bytes of JSON. Table rows are left out: L2 limits them per format, so an attached 2,000 row CSV
    turned into a spreadsheet is not blocked by a limit meant for what a model writes."""
    def strip(x):
        if isinstance(x, dict):
            return {k: ([] if k == 'rows' and x.get('type') == 'table' else strip(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return len(json.dumps(strip(spec), ensure_ascii=False, default=str).encode())


def _find_fetch(x, where='spec'):
    if isinstance(x, dict):
        for k, v in x.items():
            if str(k).lower() in FETCH_KEYS:
                raise SpecError('S7', f'The spec asks for "{k}" in {where}; files are built only from the content given, '
                                      'nothing is fetched.')
            if isinstance(v, (dict, list)) and k not in ('rows',):
                _find_fetch(v, where)
    elif isinstance(x, list):
        for v in x:
            _find_fetch(v, where)


def _cell(v, res):
    if isinstance(v, dict) and 'formula' in v:
        return {'formula': clean_chars(v['formula']).strip()}
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    if v is None or isinstance(v, (int, float)) and not isinstance(v, bool):
        if v is None or finite(v):
            return v
        _note(res, 'S5', 'fix', 'numbers too large to store kept as text')
        try:
            return str(v) if isinstance(v, int) else None
        except ValueError:  # more digits than Python will print
            return 'a number too large to show'
    if isinstance(v, bool):
        return 'Yes' if v else 'No'
    n = number(v)
    if n is not None:
        _note(res, 'S5', 'fix', 'numbers written as text in tables stored as numbers')
        return n
    return plain(v, emphasis=False)


def _unit_columns(rows: list[list], width: int, given, res: dict) -> list[str | None]:
    """S5 for money and percentages: a column whose cells with a unit all share one ("$1,200.50", "12%") stores them
    as numbers (in place, in rows) and gets an XLSX number format, so sums and sorting work and the symbol still shows.
    A column mixing units keeps them as text. `given` is the formats of an already normalized table (kept as is)."""
    given = given if isinstance(given, list) else []
    out = []
    for j in range(width):
        kinds = {}
        for r in rows:
            u = unit_number(r[j])
            if u is not None:
                kinds.setdefault(u[1][0], set()).add(u[1][1])
        if len(kinds) == 1:
            k = next(iter(kinds))
            kind = k + max(kinds[k])
            for r in rows:
                u = unit_number(r[j])
                if u is not None:
                    r[j] = u[0]
                elif k == '%' and (n := number(r[j])) is not None:  # "8" among "12%" cells is 8% too
                    r[j] = round(n / 100, 12)
            _note(res, 'S5', 'fix', 'money and percentages in tables stored as numbers with a number format')
            out.append(unit_format(kind))
        else:
            g = given[j] if j < len(given) else None
            out.append(g if g in NUMBER_FORMATS else None)
    return out


def _check_block(b, where) -> str:
    if not isinstance(b, dict):
        raise SpecError('S1', f'{where} is not an object.')
    t = b.get('type')
    if t not in BLOCKS:
        raise SpecError('S1', f'{where} has an unknown type {str(t)[:40]!r}; use one of {", ".join(BLOCKS)}.')
    missing = [k for k in REQUIRED[t] if b.get(k) is None]
    if missing:
        raise SpecError('S1', f'{where} ({t}) is missing {", ".join(missing)}.')
    lists = {'bullets': ('items',), 'table': ('columns', 'rows'), 'chart': ('labels', 'series')}.get(t, ())
    for k in lists:
        if not isinstance(b[k], list):
            raise SpecError('S1', f'{where} ({t}): {k} must be a list.')
    return t


def _block(b: dict, t: str, res: dict) -> dict | None:
    """One block cleaned (S4-S6); None when it ends up empty."""
    if t == 'paragraph':
        text = plain(b['text'])
        if text != str(b['text']).strip():
            _note(res, 'S4', 'fix', 'HTML or Markdown syntax in text converted to plain text')
        return {'type': t, 'text': text} if text else None
    if t == 'bullets':
        items = []
        for it in b['items']:
            s = plain(it)
            if s != str(it).strip():
                _note(res, 'S4', 'fix', 'HTML or Markdown syntax in text converted to plain text')
            m = _LIST_LINE.match(s)
            items.append(m.group(1) if m else s)
        items = [i for i in items if i]
        return {'type': t, 'items': items, 'ordered': bool(b.get('ordered'))} if items else None
    if t == 'quote':
        text = plain(b['text'])
        return {'type': t, 'text': text, 'by': plain(b.get('by') or '', emphasis=False)} if text else None
    if t == 'code':
        text = clean_chars(b['text']).replace('\r\n', '\n').expandtabs(4).strip('\n')
        lang = re.sub(r'[^\w+#.-]', '', str(b.get('lang') or ''))[:20]
        return {'type': t, 'lang': lang, 'text': text} if text.strip() else None
    if t == 'table':
        cols = [plain(c, emphasis=False) for c in b['columns']]
        rows = [r if isinstance(r, list) else [r] for r in b['rows']]
        width = len(cols) or max((len(r) for r in rows), default=0)
        if not cols and width:
            cols = [f'Column {i + 1}' for i in range(width)]
            _note(res, 'S6', 'fix', 'a table without column names got numbered columns')
        cols = [c or f'Column {i + 1}' for i, c in enumerate(cols)]
        fixed = []
        for r in rows:
            if len(r) != width:
                _note(res, 'S6', 'fix', 'table rows padded or trimmed to the number of columns')
                r = (r + [None] * width)[:width]
            fixed.append(list(r))
        formats = _unit_columns(fixed, width, b.get('formats'), res)
        fixed = [[_cell(v, res) for v in r] for r in fixed]
        out = {'type': t, 'columns': cols, 'rows': fixed}
        if any(formats):
            out['formats'] = formats
        if b.get('title'):
            out['title'] = plain(b['title'], emphasis=False)[:MAX_TITLE]
        return out if cols else None
    # chart
    kind = str(b.get('kind') or 'bar').lower().strip()
    kind = CHART_ALIASES.get(kind, kind)
    if kind not in CHART_KINDS:
        raise SpecError('S1', f'Chart kind {kind[:20]!r} is not one of {", ".join(CHART_KINDS)}.')
    labels = [plain(x, emphasis=False) if x is not None else '' for x in b['labels']]
    series = []
    for i, s in enumerate(b['series']):
        if not isinstance(s, dict) or not isinstance(s.get('values'), list):
            raise SpecError('S1', f'Chart series {i + 1} needs a name and a list of values.')
        vals = []
        for v in s['values'][:len(labels)] if labels else s['values']:
            n = number(v, loose=True)
            if n is None and v is not None:
                _note(res, 'S5', 'fix', 'chart values that are not numbers were dropped')
            elif n is not None and not isinstance(v, (int, float)):
                _note(res, 'S5', 'fix', 'numbers written as text in charts stored as numbers')
            vals.append(n)
        if not labels:
            labels = [str(j + 1) for j in range(len(vals))]
        vals = (vals + [None] * len(labels))[:len(labels)]
        if any(v is not None for v in vals):
            series.append({'name': plain(s.get('name') or '', emphasis=False) or f'Series {len(series) + 1}',
                           'values': vals})
    if kind == 'pie' and series:
        s = series[0]
        keep = [(lab, v) for lab, v in zip(labels, s['values']) if v is not None and v > 0]
        if len(keep) < len(labels):
            _note(res, 'S5', 'fix', 'pie slices without a positive value were dropped')
        labels, series = [k[0] for k in keep], [{'name': s['name'], 'values': [k[1] for k in keep]}] if keep else []
    if not series or not labels:
        _note(res, 'S5', 'fix', 'a chart without any numeric values was dropped')
        return None
    title = plain(b.get('title') or '', emphasis=False)[:MAX_TITLE]
    if not title:
        title = ' and '.join(s['name'] for s in series[:2])
        _note(res, 'A3', 'warn', 'a chart without a title got one from its series names')
    return {'type': 'chart', 'kind': kind, 'title': title, 'labels': labels, 'series': series}


def _fix_levels(sections: list, res: dict, fmt: str):
    """Heading levels 1-3 that never skip a level (F5, and PDF outlines need it too)."""
    prev = 0
    for s in sections:
        try:
            lvl = int(s.get('level') or 1)
        except (TypeError, ValueError):
            lvl = 1
        lvl = min(max(lvl, 1), 3, prev + 1)
        if s.get('heading'):
            if s.get('level') not in (None, lvl) and fmt == 'md':
                _note(res, 'F5', 'fix', 'heading levels adjusted so they never skip a level')
            prev = lvl
        s['level'] = lvl


def _limit_tables(sections: list, fmt: str, res: dict):
    """L2 for PDF, DOCX, MD and XLSX: long tables cut with a note saying how many rows were left out."""
    cap = TABLE_ROWS[fmt]
    for s in sections:
        out = []
        for b in s['blocks']:
            out.append(b)
            if b['type'] != 'table':
                continue
            if fmt == 'xlsx' and len(b['columns']) > XLSX_COLS:
                _note(res, 'L2', 'fix', f'tables cut to {XLSX_COLS} columns')
                b['columns'] = b['columns'][:XLSX_COLS]
                b['rows'] = [r[:XLSX_COLS] for r in b['rows']]
                if b.get('formats'):
                    b['formats'] = b['formats'][:XLSX_COLS]
            n = len(b['rows'])
            if n > cap:
                b['rows'] = b['rows'][:cap]
                _note(res, 'L2', 'fix', f'tables cut to {cap:,} rows')
                out.append({'type': 'paragraph', 'text': f'Showing the first {cap:,} of {n:,} rows; '
                                                         f'{n - cap:,} more rows are left out.'})
        s['blocks'] = out


def _slide_text(b: dict, res: dict) -> tuple[list[dict], list[str], int]:
    """A text block fitted for a slide: (blocks to show, text moved to the notes, room it takes in items)."""
    notes = []

    def short(text, limit):
        w = words(text)
        if len(w) <= limit:
            return text
        notes.append(text)
        _note(res, 'L3', 'fix', f'bullets over {BULLET_WORDS} words or long paragraphs shortened on the slide, '
                                'full text in the notes')
        first = re.split(r'(?<=[.!?])\s+', text)[0]
        return first if len(words(first)) <= limit else ' '.join(w[:limit]) + '...'
    if b['type'] == 'bullets':
        return [{**b, 'items': [short(i, BULLET_WORDS) for i in b['items']]}], notes, len(b['items'])
    if b['type'] == 'quote':
        return [{**b, 'text': short(b['text'], SLIDE_PARA_WORDS)}], notes, 1
    text = short(b['text'], SLIDE_PARA_WORDS)
    return [{**b, 'text': text}], notes, 1 if len(words(text)) <= BULLET_WORDS else 2


def _trim_items(blocks: list[dict], room: int, res: dict) -> tuple[list[dict], list[str]]:
    """L3: at most `room` bullets (a paragraph counts as one or two) on a slide; the rest goes to the notes."""
    out, notes, used = [], [], 0
    for b in blocks:
        cost = len(b['items']) if b['type'] == 'bullets' else 1 if len(words(b['text'])) <= BULLET_WORDS else 2
        if used + cost <= room:
            out.append(b)
            used += cost
            continue
        left = room - used
        if b['type'] == 'bullets' and left > 0:
            out.append({**b, 'items': b['items'][:left]})
            notes.extend(b['items'][left:])
        else:
            notes.extend(b['items'] if b['type'] == 'bullets' else [b['text']])
        used = room
        _note(res, 'L3', 'fix', f'more bullets than fit on a slide (at most {SLIDE_BULLETS}); the rest moved to '
                                'the notes')
    return out, notes


def _pptx_slides(sec: dict, res: dict) -> list[dict]:
    """One section -> one or more slides (each a section): at most one table, chart or code block per slide, tables
    split every 12 rows (L2), text cut to 6 bullets or 3 beside a visual, overflow in the notes (L3)."""
    slides = []

    def new():
        return {'text': [], 'visuals': [], 'notes': [], 'items': 0}
    cur = new()
    for b in sec['blocks']:
        if b['type'] in ('paragraph', 'bullets', 'quote'):
            shown, notes, cost = _slide_text(b, res)
            if cur['visuals'] and cur['items'] >= SLIDE_BULLETS_BESIDE:
                slides.append(cur)
                cur = new()
            cur['text'].extend(shown)
            cur['notes'].extend(notes)
            cur['items'] += cost
            continue
        chunks = [b]
        if b['type'] == 'table':
            if len(b['columns']) > SLIDE_COLS:
                _note(res, 'L2', 'fix', f'slide tables show the first {SLIDE_COLS} columns, the rest are named in the '
                                        'notes')
                cur['notes'].append('Columns not shown on the slide: ' + ', '.join(b['columns'][SLIDE_COLS:]) + '.')
                b = {**b, 'columns': b['columns'][:SLIDE_COLS], 'rows': [r[:SLIDE_COLS] for r in b['rows']]}
                if b.get('formats'):
                    b['formats'] = b['formats'][:SLIDE_COLS]
            per = TABLE_ROWS['pptx']
            if len(b['rows']) > per:
                _note(res, 'L2', 'fix', f'tables longer than {per} rows split across slides')
                chunks = [{**b, 'rows': b['rows'][i:i + per]} for i in range(0, len(b['rows']), per)]
            else:
                chunks = [b]
        elif b['type'] == 'code':
            lines = b['text'].split('\n')
            chunks = [{**b, 'text': '\n'.join(lines[i:i + SLIDE_CODE_LINES])}
                      for i in range(0, len(lines), SLIDE_CODE_LINES)]
        for ch in chunks:
            if cur['visuals'] or cur['items'] > SLIDE_BULLETS_BESIDE:
                slides.append(cur)
                cur = new()
            cur['visuals'].append(ch)
    if cur['text'] or cur['visuals'] or cur['notes'] or not slides:
        slides.append(cur)
    out = []
    for i, sl in enumerate(slides):
        room = SLIDE_BULLETS_BESIDE if sl['visuals'] else SLIDE_BULLETS
        kept, extra = _trim_items(sl['text'], room, res)
        notes = ([sec['notes']] if i == 0 and sec.get('notes') else []) + sl['notes'] + extra
        out.append({'heading': sec['heading'] if i == 0 else f'{sec["heading"]} (cont.)', 'level': sec['level'],
                    'blocks': kept + sl['visuals'], 'notes': '\n\n'.join(n for n in notes if n)})
    return out


def normalize(spec: dict, fmt: str) -> tuple[dict, list[RuleResult]]:
    """The spec fixed for one format, plus one RuleResult per content and size rule. Raises SpecError on a block."""
    if fmt not in FORMATS:
        raise SpecError('X1', f'Files are made as {", ".join(FORMATS)} only, not {str(fmt)[:12]!r}.')
    if not isinstance(spec, dict):
        raise SpecError('S1', 'The spec is not a JSON object.')
    res = {rid: RuleResult(rid, sev, True, '') for rid, sev in
           (('S1', 'block'), ('S2', 'block'), ('S3', 'fix'), ('S4', 'fix'), ('S5', 'fix'), ('S6', 'fix'),
            ('S7', 'block'), ('L1', 'block'), ('L2', 'fix'), ('L3', 'fix'))}
    try:
        size = _size_without_rows(spec)
    except (TypeError, ValueError, RecursionError):
        raise SpecError('S1', 'The spec could not be read as JSON.')
    if size > MAX_SPEC_BYTES:
        raise SpecError('L1', f'The spec is {size // 1000} KB; the limit is {MAX_SPEC_BYTES // 1000} KB.')
    _find_fetch(spec)
    sections = spec.get('sections')
    if not isinstance(sections, list):
        raise SpecError('S1', 'The spec needs a list of sections.')
    if len(sections) > MAX_SECTIONS:
        raise SpecError('L1', f'The spec has {len(sections)} sections; the limit is {MAX_SECTIONS}.')
    out_sections = []
    for si, sec in enumerate(sections, 1):
        if not isinstance(sec, dict):
            raise SpecError('S1', f'Section {si} is not an object.')
        blocks = sec.get('blocks') if sec.get('blocks') is not None else []
        if not isinstance(blocks, list):
            raise SpecError('S1', f'Section {si}: blocks must be a list.')
        if len(blocks) > MAX_BLOCKS:
            raise SpecError('L1', f'Section {si} has {len(blocks)} blocks; the limit is {MAX_BLOCKS}.')
        heading = plain(sec.get('heading') or '', emphasis=False).replace('\n', ' ')[:200]
        if heading != str(sec.get('heading') or '').strip():
            _note(res, 'S4', 'fix', 'HTML or Markdown syntax in text converted to plain text')
        cleaned = []
        for bi, b in enumerate(blocks, 1):
            t = _check_block(b, f'Section {si}, block {bi}')
            nb = _block(b, t, res)
            if nb is None:
                continue
            if t == 'paragraph' and '\n' in nb['text']:
                parts = [p.strip() for p in nb['text'].split('\n\n') if p.strip()]
                for p in parts:
                    lines = p.split('\n')
                    items = [_LIST_LINE.match(ln) for ln in lines]
                    if len(lines) > 1 and all(items):
                        _note(res, 'S4', 'fix', 'Markdown lists inside paragraphs turned into bullets')
                        ordered = all(re.match(r'\s*\d', ln) for ln in lines)
                        cleaned.append({'type': 'bullets', 'items': [m.group(1) for m in items], 'ordered': ordered})
                    else:
                        cleaned.append({'type': 'paragraph', 'text': ' '.join(ln.strip() for ln in lines)})
                continue
            cleaned.append(nb)
        notes = plain(sec.get('notes') or '', emphasis=False) if fmt == 'pptx' else ''
        if heading or cleaned:
            out_sections.append({'heading': heading, 'level': sec.get('level'), 'blocks': cleaned, 'notes': notes})
    if not any(s['blocks'] for s in out_sections):
        raise SpecError('S2', 'The spec has no content: it needs at least one section with at least one '
                              'non-empty block.')
    _fix_levels(out_sections, res, fmt)
    title = plain(spec.get('title') or '', emphasis=False).replace('\n', ' ')
    if not title:
        first = next((s['heading'] for s in out_sections if s['heading']), '')
        if not first:
            b = next(b for s in out_sections for b in s['blocks'])
            first = strip_emphasis(b.get('text') or ' '.join(b.get('items') or b.get('columns') or [b.get('title', '')]))
            first = ' '.join(words(first)[:8])
        title = first or 'Document'
        _note(res, 'S3', 'fix', 'the file had no title; took it from the first heading')
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE].rsplit(' ', 1)[0].rstrip(' ,;:') or title[:MAX_TITLE]
        _note(res, 'S3', 'fix', f'title shortened to {MAX_TITLE} characters')
    out = {'title': title, 'subtitle': plain(spec.get('subtitle') or '', emphasis=False).replace('\n', ' ')[:300],
           'format': fmt, 'theme': spec.get('theme') if spec.get('theme') in THEMES else 'clean',
           'paper': 'letter' if str(spec.get('paper') or '').lower() == 'letter' else 'a4', 'sections': out_sections}
    if fmt == 'pptx':
        slides = []
        for s in out_sections:
            if not s['heading']:
                s['heading'] = slides[-1]['heading'].removesuffix(' (cont.)') + ' (cont.)' if slides else 'Overview'
            slides.extend(_pptx_slides(s, res))
        out['sections'] = slides
    else:
        _limit_tables(out_sections, fmt, res)
    order = ['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'L1', 'L2', 'L3'] + [k for k in ('F5', 'A3') if k in res]
    return out, [res[k] for k in order]


# ---------- zero-token specs ----------

_FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)')
_HEADING = re.compile(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$')
_TABLE_SEP = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')
_HR = re.compile(r'^\s{0,3}([-*_])(\s*\1){2,}\s*$')
_BY = re.compile(r'^\s*(?:[-\u2013\u2014~]+|by\s)\s*(.+)$', re.I)


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|') and not s.endswith('\\|'):
        s = s[:-1]
    return [c.strip().replace('\\|', '|') for c in re.split(r'(?<!\\)\|', s)]


def _fit(sections: list[dict]) -> list[dict]:
    """Keep a zero-token spec inside L1: long sections continue in a new section, and past 40 sections the rest is
    folded into the last one."""
    out = []
    for s in sections:
        blocks = s['blocks']
        for i in range(0, max(len(blocks), 1), MAX_BLOCKS):
            out.append({**s, 'heading': s['heading'] if i == 0 else (s['heading'] or 'More') + ' (cont.)',
                        'blocks': blocks[i:i + MAX_BLOCKS]})
    if len(out) > MAX_SECTIONS:
        last, rest = out[MAX_SECTIONS - 1], out[MAX_SECTIONS:]
        for s in rest:
            if s['heading']:
                last['blocks'].append({'type': 'paragraph', 'text': f'**{s["heading"]}**'})
            last['blocks'].extend(s['blocks'])
        if len(last['blocks']) > MAX_BLOCKS:
            last['blocks'] = last['blocks'][:MAX_BLOCKS - 1] + [
                {'type': 'paragraph', 'text': 'More content was left out to keep the file within its size limit.'}]
        out = out[:MAX_SECTIONS]
    return out


def from_markdown(text: str, title: str | None = None) -> dict:
    """An answer in Markdown -> a spec, with no LLM: headings, lists, GFM tables, code fences, quotes, paragraphs."""
    lines = clean_chars(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    # Fenced code can hold lines that look like headings; recount outside fences.
    heads, fence = [], None
    for ln in lines:
        f = _FENCE.match(ln)
        if f and (fence is None or f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence)):
            fence = None if fence else f.group(1)
            continue
        m = None if fence else _HEADING.match(ln)
        if m:
            heads.append((len(m.group(1)), m.group(2)))
    use_h1_title = title is None and heads and heads[0][0] == 1 and sum(1 for h in heads if h[0] == 1) == 1
    levels = sorted({lv for lv, _ in heads[1 if use_h1_title else 0:]})
    level_of = {lv: min(i + 1, 3) for i, lv in enumerate(levels)}
    spec_title = title
    sections = [{'heading': '', 'level': 1, 'blocks': []}]
    para, i, n = [], 0, len(lines)

    def blocks():
        return sections[-1]['blocks']

    def flush_para():
        if para:
            blocks().append({'type': 'paragraph', 'text': ' '.join(p.strip() for p in para)})
            para.clear()
    while i < n:
        ln = lines[i]
        f = _FENCE.match(ln)
        if f:
            flush_para()
            mark, body = f.group(1), []
            i += 1
            while i < n and not re.match(rf'^\s{{0,3}}{re.escape(mark[0])}{{{len(mark)},}}\s*$', lines[i]):
                body.append(lines[i])
                i += 1
            blocks().append({'type': 'code', 'lang': f.group(2), 'text': '\n'.join(body)})
            i += 1
            continue
        h = _HEADING.match(ln)
        if h:
            flush_para()
            lv, txt = len(h.group(1)), h.group(2)
            if use_h1_title and lv == 1 and spec_title is None:
                spec_title = txt
            else:
                if sections[-1]['heading'] or sections[-1]['blocks']:
                    sections.append({'heading': '', 'level': 1, 'blocks': []})
                sections[-1].update(heading=txt, level=level_of.get(lv, 3))
            i += 1
            continue
        if not ln.strip() or _HR.match(ln):
            flush_para()
            i += 1
            continue
        if '|' in ln and i + 1 < n and '|' in lines[i + 1] and _TABLE_SEP.match(lines[i + 1]):
            flush_para()
            cols, rows = _split_row(ln), []
            i += 2
            while i < n and lines[i].strip() and '|' in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            blocks().append({'type': 'table', 'columns': cols, 'rows': rows})
            continue
        if re.match(r'^\s{0,3}>', ln):
            flush_para()
            q = []
            while i < n and re.match(r'^\s{0,3}>', lines[i]):
                q.append(re.sub(r'^\s{0,3}>\s?', '', lines[i]))
                i += 1
            by = ''
            nonblank = [x for x in q if x.strip()]
            last = nonblank[-1] if len(nonblank) > 1 else ''
            m_by = _BY.match(last) or re.match(r'^\s*\*([^*]+)\*\s*$', last)
            if m_by:
                by = m_by.group(1).strip().strip('*_')
                q = q[:len(q) - 1 - q[::-1].index(nonblank[-1])]
            text_q = '\n'.join(q).strip()
            if text_q:
                blocks().append({'type': 'quote', 'text': re.sub(r'\s*\n\s*', ' ', text_q), 'by': by})
            continue
        m = _LIST_LINE.match(ln)
        if m:
            flush_para()
            ordered = bool(re.match(r'^\s*\d', ln))
            items = []
            while i < n:
                cur = lines[i]
                m2 = _LIST_LINE.match(cur)
                if m2 and items and cur[:1] not in ' \t' and bool(re.match(r'^\s*\d', cur)) != ordered:
                    break  # a numbered list after a bulleted one (or the other way round) is a new list
                if m2:
                    items.append(m2.group(1).strip())
                elif cur.strip() and cur[:1] in ' \t' and items:
                    items[-1] += ' ' + cur.strip()
                elif not cur.strip() and i + 1 < n and _LIST_LINE.match(lines[i + 1]):
                    pass
                else:
                    break
                i += 1
            blocks().append({'type': 'bullets', 'items': items, 'ordered': ordered})
            continue
        para.append(ln)
        i += 1
    flush_para()
    sections = [s for s in sections if s['heading'] or s['blocks']]
    if not spec_title:
        spec_title = next((s['heading'] for s in sections if s['heading']), '')
    return {'title': plain(spec_title or '', emphasis=False), 'subtitle': '', 'sections': _fit(sections)}


_MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
_TIME = re.compile(r'^(\d{4}([-/.]\d{1,2}([-/.]\d{1,2})?)?|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|q[1-4]( \d{4})?|\d{4} ?q[1-4]|'
                   r'(' + '|'.join(_MONTHS) + r')[a-z]*\.?( \d{2,4})?|week \d+|w\d+)$', re.I)
_ID_COL = re.compile(r'^(id|.*[_ ]id|#|no\.?|index|zip|postcode|phone|year)$', re.I)


def _title_from_name(name: str) -> str:
    stem = re.sub(r'\.[A-Za-z0-9]{1,5}$', '', str(name or '')).strip()
    stem = re.sub(r'[_\-]+', ' ', stem).strip()
    return stem[:1].upper() + stem[1:] if stem else ''


def from_table(meta: dict, rows: list[list], columns: list[str], title: str | None = None) -> dict:
    """An attached table -> a spec with a table section, plus a chart when there is a label column and numeric
    columns (repeated labels are summed, so "sales by region" charts one bar per region). No LLM."""
    meta = meta or {}
    columns = [str(c) for c in (columns or [])]
    rows = [list(r) for r in (rows or [])]
    name = _title_from_name(meta.get('name', ''))
    heading = name or 'Data'
    spec_title = title or name or 'Data'
    blocks = [{'type': 'table', 'columns': columns, 'rows': rows, 'title': heading}]
    chart = _table_chart(columns, rows)
    if chart:
        blocks.append(chart)
    return {'title': spec_title, 'subtitle': f'{len(rows):,} rows from {meta["name"]}' if meta.get('name') else '',
            'sections': [{'heading': heading, 'level': 1, 'blocks': blocks}]}


def _table_chart(columns: list[str], rows: list[list]) -> dict | None:
    if not rows or len(columns) < 2:
        return None
    na = {'', 'n/a', 'na', 'null', 'none', 'nan', '-', '?'}
    numeric, text = [], []
    for j, c in enumerate(columns):
        vals = [r[j] if j < len(r) else None for r in rows]
        present = [v for v in vals if v is not None and str(v).strip().lower() not in na]
        nums = [number(v, loose=True) for v in present]
        if present and sum(x is not None for x in nums) >= 0.8 * len(present) and not _ID_COL.match(c.strip()):
            numeric.append(j)
        elif present:
            text.append(j)
    if not numeric or not text:
        return None
    label_col = None
    for j in text:
        distinct = {str(r[j]).strip() for r in rows if j < len(r)}
        if len(text) == 1 or len(distinct) < len(rows) and len(distinct) <= 30:
            label_col = j
            break
    if label_col is None:
        return None
    # Percentages are rates, not amounts: they are left out beside amount columns (another scale) and averaged, not
    # summed, when rows share a label.
    pct = {j for j in numeric if all(str(r[j]).strip().endswith('%') for r in rows
                                     if j < len(r) and r[j] is not None and str(r[j]).strip().lower() not in na)}
    series_cols = ([j for j in numeric if j not in pct] or numeric)[:6]
    average = all(j in pct for j in series_cols)
    order, sums, counts = [], {}, {}
    for r in rows:
        lab = str(r[label_col]).strip() if label_col < len(r) and r[label_col] is not None else ''
        if lab not in sums:
            order.append(lab)
            sums[lab], counts[lab] = [None] * len(series_cols), [0] * len(series_cols)
        for k, j in enumerate(series_cols):
            v = number(r[j], loose=True) if j < len(r) else None
            if v is not None:
                sums[lab][k] = (sums[lab][k] or 0) + v
                counts[lab][k] += 1
    if average:
        sums = {lab: [v / n if v is not None and n else v for v, n in zip(sums[lab], counts[lab])] for lab in order}
    aggregated = len(order) < len(rows)
    timeish = sum(bool(_TIME.match(lab)) for lab in order) >= 0.8 * len(order)
    if len(order) > (200 if timeish else 30):
        return None
    kind = 'line' if timeish and len(order) > 2 else 'bar'
    names = [columns[j] for j in series_cols]
    what = ' and '.join(names[:2]) + (' and more' if len(names) > 2 else '')
    title = f'{("Average " if average else "Total ") if aggregated else ""}{what} by {columns[label_col]}'
    return {'type': 'chart', 'kind': kind, 'title': title[:MAX_TITLE], 'labels': order,
            'series': [{'name': nm, 'values': [
                (round(sums[lab][k], 6) if isinstance(sums[lab][k], float) else sums[lab][k]) for lab in order]}
                for k, nm in enumerate(names)]}


# ---------- format and file name ----------

_SHEET = (r'(?<!cheat )(?<!balance )(?<!fact )(?<!spec )(?<!data )(?<!time )(?<!style )(?<!score )(?<!answer )'
          r'(?<!call )(?<!tip )(?<!crib )(?<!rap )(?<!bed )(?<!ice )(?<!term )(?<!work )sheets?')
_FORMAT_WORDS = [  # (format, pattern, weak): weak words count only right after "as", "into", "make a" and the like
    ('pptx', r'pptx|ppt|power ?point|keynote|slides?|slide ?deck|deck|presentation', False),
    ('xlsx', r'xlsx|xls|excel|spreadsheets?|workbook|worksheets?|google sheets?|' + _SHEET, False),
    ('docx', r'docx|ms word|microsoft word|word (?:doc|docs|document|documents|file|files|format)', False),
    ('docx', r'word(?! (?:count|limit|for|of|problem|puzzle|search|cloud|order|choice|salad)s?\b)', True),
    ('md', r'markdown|md file|\.md', False),
    ('md', r'md', True),
    ('pdf', r'pdf', False),
]
_FORMAT_RE = [(f, re.compile(rf'(?<![\w.])(?:{p})\b', re.I), weak) for f, p, weak in _FORMAT_WORDS]
_INTO = re.compile(r'\b(into|to|as|in|export|save|convert|make|create|generate|build|write|produce|give me|want|need|'
                   r'prepare|draft|design|turn)\s+(?:(?:a|an|the|my|me|us|one|some|nice|short|full|new|proper|simple|'
                   r'clean|quick|small|single)\s+){0,3}$', re.I)
_SOURCE = re.compile(r'\b(from|this|that|these|those|attached|uploaded|my|the|of|read|summari[sz]e|open|analy[sz]e|'
                     r'explain|parse|extract)\s+$', re.I)


def detect_format(text: str) -> str | None:
    """The output format a request names, with no LLM, or None. A format named as the target ("into slides",
    "as a PDF") wins over one named as the source ("summarise this pdf", "slides from the attached pdf"). "word" and
    "md" count only in target position, so "word count" or "an MD degree" name no format."""
    t = re.sub(r'\s+', ' ', str(text or ''))
    found = []
    for fmt, rx, weak in _FORMAT_RE:
        for m in rx.finditer(t):
            before = t[max(0, m.start() - 60):m.start()]
            if _INTO.search(before):
                found.append((2, m.start(), fmt))
            elif not weak and not _SOURCE.search(before):
                found.append((1, m.start(), fmt))
    if not found:
        return None
    return max(found, key=lambda x: (x[0], x[1]))[2]


_RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}


def file_name(title: str, fmt: str) -> str:
    """F7: lowercase ASCII words from the title joined by hyphens, the format's extension, at most 80 characters, no
    path separators. A title with no Latin letters becomes "document"."""
    if fmt not in FORMATS:
        raise SpecError('X1', f'Files are made as {", ".join(FORMATS)} only, not {str(fmt)[:12]!r}.')
    ext = EXTENSIONS[fmt]
    folded = unicodedata.normalize('NFKD', str(title or '')).encode('ascii', 'ignore').decode().lower()
    stem = re.sub(r'[^a-z0-9]+', '-', folded).strip('-')
    limit = 80 - len(ext)
    if len(stem) > limit:
        stem = stem[:limit]
        stem = stem.rsplit('-', 1)[0] if '-' in stem else stem
        stem = stem.strip('-')
    if not stem:
        stem = 'document'
    if stem in _RESERVED:
        stem += '-file'
    return stem + ext
