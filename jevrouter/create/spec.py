"""The DocSpec: the one compact JSON a model writes for a file (no layout), its schema, and the code that cleans it up.

parse_spec() reads a model reply tolerantly and repair() turns whatever it holds into a spec whose every block is
readable (S1, S7, S8 and L1 are fixes: a file is refused only when nothing in it can be shown). normalize() is repair
plus the format steps, and returns the fixed spec with one RuleResult per check. from_markdown() and from_table() build
a spec from an answer or an attached table with no LLM.
"""
import copy
import html
import json
import math
import re
import unicodedata

from . import diagram
from .rules import RuleResult, SpecError

FORMATS = ('pdf', 'docx', 'pptx', 'xlsx', 'md')
EXTENSIONS = {f: '.' + f for f in FORMATS}
# timeline, tree and flow are native diagrams; a figure is a web image the create agent looks up (create/assets.py) and
# turns into the internal `image` block, which the model can never write (strip_internal). The Studio diagram kinds
# (docs/PLAN-designer.md 9.4: cycle, venn, pyramid, matrix, mindmap, process, comparison, labelled, stat-cards,
# scatter) are read and repaired like the others; `labelled` points at an asset-cache image, so only code writes one.
BLOCKS = ('paragraph', 'bullets', 'table', 'chart', 'quote', 'code', 'timeline', 'tree', 'flow', 'figure', 'page_break',
          'image', *diagram.NEW_KINDS)
DIAGRAMS = diagram.ALL_KINDS
CHART_KINDS = ('bar', 'line', 'pie')
CHART_ALIASES = {'column': 'bar', 'columns': 'bar', 'bars': 'bar', 'histogram': 'bar', 'area': 'line', 'lines': 'line',
                 'scatter': 'line', 'donut': 'pie', 'doughnut': 'pie'}
THEMES = ('clean', 'dark', 'warm', 'mono')

NOTES_KEEP = 2_000  # speaker notes are cut to this before any content is (L1)
MAX_SPEC_BYTES = 200_000  # a 40-page document (create/longdoc.py) fits; table rows are limited by L2 instead
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
_NUM = {'anyOf': [{'type': 'number'}, {'type': 'null'}]}
# The Studio kinds a model can write (all of diagram.NEW_KINDS but `labelled`, whose picture only code adds), as one
# generic block so the strict schema stays small: `items` carry what each kind needs and the rest are left empty.
# cycle, pyramid: items[].label (a pyramid top first). venn, matrix, comparison: items[] are the sets, quadrants or
# columns, each a label and its items (venn's `shared` is the overlap; matrix's axes are x_label, y_label). mindmap:
# items[].label and parent (the label of the item it hangs from; empty for the centre idea). process: label and
# detail. stat-cards: value and label. scatter: x, y and label, with x_label, y_label. _generic_diagram reads it back.
DIAGRAM_WRITABLE = tuple(k for k in diagram.NEW_KINDS if k != 'labelled')
_DIAGRAM_BLOCK = _obj({
    'type': {'type': 'string', 'enum': ['diagram']}, 'kind': {'type': 'string', 'enum': list(DIAGRAM_WRITABLE)},
    'title': _STR, 'items': {'type': 'array', 'items': _obj({
        'label': _STR, 'detail': _STR, 'parent': _STR, 'value': _STR, 'x': _NUM, 'y': _NUM, 'items': _STRS})},
    'shared': _STRS, 'x_label': _STR, 'y_label': _STR})
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
            _obj({'type': {'type': 'string', 'enum': ['timeline']}, 'title': _STR,
                  'events': {'type': 'array', 'items': _obj({'date': _STR, 'label': _STR})}}),
            _obj({'type': {'type': 'string', 'enum': ['tree']}, 'title': _STR,
                  'nodes': {'type': 'array', 'items': _obj({'id': _STR, 'parent': _STR, 'label': _STR})}}),
            _obj({'type': {'type': 'string', 'enum': ['flow']}, 'title': _STR,
                  'nodes': {'type': 'array', 'items': _obj({'id': _STR, 'label': _STR})},
                  'edges': {'type': 'array', 'items': _obj({'from': _STR, 'to': _STR, 'label': _STR})}}),
            _DIAGRAM_BLOCK,
            _obj({'type': {'type': 'string', 'enum': ['figure']}, 'query': _STR, 'caption': _STR}),
            _obj({'type': {'type': 'string', 'enum': ['page_break']}}),
        ]}},
    })},
})

# Keys each object may carry after normalize (anything else is dropped).
KEYS = {'spec': ('title', 'subtitle', 'format', 'theme', 'paper', 'font', 'design', 'sections'),
        'section': ('heading', 'level', 'blocks', 'notes'),
        'paragraph': ('type', 'text'), 'bullets': ('type', 'items', 'ordered'),
        'table': ('type', 'title', 'columns', 'rows', 'formats'), 'chart': ('type', 'kind', 'title', 'labels', 'series'),
        'quote': ('type', 'text', 'by'), 'code': ('type', 'lang', 'text'), 'timeline': ('type', 'title', 'events'),
        'tree': ('type', 'title', 'nodes'), 'flow': ('type', 'title', 'nodes', 'edges'),
        'figure': ('type', 'query', 'caption'), 'page_break': ('type',), 'image': ('type', 'asset', 'caption', 'credit'),
        'cycle': ('type', 'title', 'steps'), 'venn': ('type', 'title', 'sets', 'shared'),
        'pyramid': ('type', 'title', 'levels'), 'matrix': ('type', 'title', 'x_axis', 'y_axis', 'quadrants'),
        'mindmap': ('type', 'title', 'nodes'), 'process': ('type', 'title', 'steps'),
        'comparison': ('type', 'title', 'columns'), 'labelled': ('type', 'title', 'image', 'callouts'),
        'stat-cards': ('type', 'title', 'stats'), 'scatter': ('type', 'title', 'x_label', 'y_label', 'points')}
# the list each Studio diagram kind needs (repair reads other spellings into it; _check_block requires a list)
DIAGRAM_LISTS = {'cycle': 'steps', 'venn': 'sets', 'pyramid': 'levels', 'matrix': 'quadrants', 'mindmap': 'nodes',
                 'process': 'steps', 'comparison': 'columns', 'labelled': 'callouts', 'stat-cards': 'stats',
                 'scatter': 'points'}
REQUIRED = {'paragraph': ('text',), 'bullets': ('items',), 'table': ('columns', 'rows'),
            'chart': ('labels', 'series'), 'quote': ('text',), 'code': ('text',), 'timeline': ('events',),
            'tree': ('nodes',), 'flow': ('nodes',), 'figure': (), 'page_break': (), 'image': ('asset',),
            **{k: (v,) for k, v in DIAGRAM_LISTS.items()}}
MAX_FIGURE_QUERY, MAX_CAPTION, MAX_CREDIT = 100, 200, 400
ASSET_ID = re.compile(r'^[0-9a-f]{64}$')
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


_SUFFIX = re.compile(r'^([+-]?[\d.,]*\d)(k|m|b|bn|mn|t)$', re.I)
_SCALE = {'k': 1_000, 'm': 1_000_000, 'mn': 1_000_000, 'b': 1_000_000_000, 'bn': 1_000_000_000,
          't': 1_000_000_000_000}


def number(v, loose: bool = False):
    """A number from a cell or chart value, or None. Strict (tables): plain digits with optional thousands commas, so
    ids with leading zeros, dates and codes stay text. Loose (charts): currency signs, %, spaces, (1,200), a leading ~
    and k/M/B/T suffixes (1.9B) too."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v if finite(v) else None
    s = str(v).strip().replace('\u2212', '-')
    scale = 1
    if loose:
        neg = s.startswith('(') and s.endswith(')')
        s = re.sub(r'[\s\u00a0]', '', s.strip('()')).lstrip('~\u2248').rstrip('%')
        if not s:
            return None
        sign = s[0] if s[:1] and s[0] in '+-' else ''
        s = sign + s[len(sign):].lstrip(_CURRENCY)
        if neg and not sign:
            s = '-' + s
        m = _SUFFIX.match(s)
        if m:  # 3.5k, 1.9B, 12M, 2bn
            s, scale = m.group(1), _SCALE[m.group(2).lower()]
    if not _NUM.match(s):
        return None
    digits = re.sub(r'\D', '', s.split('.')[0])
    if (len(digits) > 1 and digits[0] == '0') or len(digits) > 15:
        return None
    if scale != 1:
        from decimal import Decimal
        f = float(Decimal(s.replace(',', '')) * scale)
        return int(f) if f.is_integer() and abs(f) < 10 ** 15 else f
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
    return _json_len(strip(spec))


def _json_len(x) -> int:
    return len(json.dumps(x, ensure_ascii=False, default=str).encode('utf-8', 'replace'))


def _cell(v, res):
    if isinstance(v, dict) and 'formula' in v:
        return {'formula': clean_chars(v['formula']).strip()}
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, default=str)
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
            out.append(g if isinstance(g, str) and g in NUMBER_FORMATS else None)
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
    lists = {'bullets': ('items',), 'table': ('columns', 'rows'), 'chart': ('labels', 'series'), 'timeline': ('events',),
             'tree': ('nodes',), 'flow': ('nodes',), **{k: (v,) for k, v in DIAGRAM_LISTS.items()}}.get(t, ())
    for k in lists:
        if not isinstance(b[k], list):
            raise SpecError('S1', f'{where} ({t}): {k} must be a list.')
    return t


def _cut_words(s: str, n: int) -> str:
    """s at most n characters, cut at a word when there is one in the second half."""
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(' ')
    return (cut[:sp] if sp > n // 2 else cut).rstrip(' ,;:')


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
    if t in DIAGRAMS:
        if t == 'flow' and not isinstance(b.get('edges') if b.get('edges') is not None else [], list):
            raise SpecError('S1', 'A flow diagram: edges must be a list.')
        clean = lambda x: plain(x, emphasis=False).replace('\n', ' ')  # noqa: E731
        out = diagram.clean_block(b, clean, lambda rid, note: _note(res, rid, 'fix', note))
        if out is None:  # too little for a picture: its words stay as a list
            items = [clean(x) for x in diagram.raw_labels(b)]
            items = [x for x in items if x]
            return {'type': 'bullets', 'items': items, 'ordered': False} if items else None
        return out
    if t == 'page_break':
        return {'type': t}
    if t == 'figure':
        query = plain(b.get('query') or '', emphasis=False).replace('\n', ' ')[:MAX_FIGURE_QUERY]
        caption = plain(b.get('caption') or '', emphasis=False).replace('\n', ' ')[:MAX_CAPTION]
        return {'type': t, 'query': query or caption[:MAX_FIGURE_QUERY], 'caption': caption} if query or caption else None
    if t == 'image':
        return _image(b, res)
    if t == 'table':
        cols = [plain(c, emphasis=False).replace('\n', ' ') for c in b['columns']]
        if any(len(c) > MAX_COLUMN for c in cols):
            cols = [_cut_words(c, MAX_COLUMN) for c in cols]
            _note(res, 'S6', 'fix', f'column names over {MAX_COLUMN} characters shortened')
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
            out['title'] = plain(b['title'], emphasis=False).replace('\n', ' ')[:MAX_TITLE]
        return out if cols else None
    # chart
    kind = str(b.get('kind') or 'bar').lower().strip()
    kind = CHART_ALIASES.get(kind, kind)
    if kind not in CHART_KINDS:
        _note(res, 'S1', 'fix', 'a chart kind that is not bar, line or pie was drawn as a bar chart')
        kind = 'bar'
    given_labels = [plain(x, emphasis=False).replace('\n', ' ') if x is not None else '' for x in b['labels']]
    labels = list(given_labels)
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
            series.append({'name': plain(s.get('name') or '', emphasis=False).replace('\n', ' ') or
                           f'Series {len(series) + 1}', 'values': vals})
    if kind == 'pie' and series:
        s = series[0]
        keep = [(lab, v) for lab, v in zip(labels, s['values']) if v is not None and v > 0]
        if len(keep) < len(labels):
            _note(res, 'S5', 'fix', 'pie slices without a positive value were dropped')
        labels, series = [k[0] for k in keep], [{'name': s['name'], 'values': [k[1] for k in keep]}] if keep else []
    title = plain(b.get('title') or '', emphasis=False).replace('\n', ' ')[:MAX_TITLE]
    if not series or not labels:
        items = ([title] if title else []) + _chart_lines(given_labels, b['series'])
        if not items:
            _note(res, 'S5', 'fix', 'a chart without any numeric values was dropped')
            return None
        _note(res, 'S5', 'fix', 'a chart whose values are not plain numbers was kept as a list of its labels '
                                'and values')
        return {'type': 'bullets', 'items': items, 'ordered': False}
    if not title:
        title = ' and '.join(s['name'] for s in series[:2])
        _note(res, 'A3', 'warn', 'a chart without a title got one from its series names')
    return {'type': 'chart', 'kind': kind, 'title': title, 'labels': labels, 'series': series}


def _raw_text(v) -> str:
    if v is None or isinstance(v, bool):
        return ''
    if isinstance(v, float) and not finite(v):
        return ''
    return plain(str(v), emphasis=False).replace('\n', ' ').strip()


def _chart_lines(labels: list[str], series: list) -> list[str]:
    """A chart that cannot be drawn as lines of text: "label: value" for each label with a value as it was written
    ("2015: 1.9B"), so the numbers stay in the file; labels alone when no value was given."""
    series = [s for s in series if isinstance(s, dict) and isinstance(s.get('values'), list)]
    named = len(series) > 1
    n = max([len(labels)] + [len(s['values']) for s in series])
    out = []
    for j in range(n):
        lab = labels[j] if j < len(labels) else ''
        vals = []
        for s in series:
            v = _raw_text(s['values'][j]) if j < len(s['values']) else ''
            if v:
                name = plain(s.get('name') or '', emphasis=False).replace('\n', ' ').strip()
                vals.append(f'{name} {v}' if named and name else v)
        line = f'{lab}: {", ".join(vals)}' if lab and vals else lab or ', '.join(vals)
        if line:
            out.append(line)
    return out


def _image(b: dict, res: dict | None = None) -> dict | None:
    """X6: an image is only ever bytes from the local asset cache, re-encoded, with a credit line. One that is not in
    the cache, or has no credit, is left out (never embedded)."""
    from . import assets
    asset = str(b.get('asset') or '').lower()
    if not ASSET_ID.match(asset) or not (assets.CACHE / f'{asset}.png').is_file():
        return None
    credit = plain(b.get('credit') or '', emphasis=False).replace('\n', ' ')[:MAX_CREDIT]
    if not credit:
        if res is not None:
            _note(res, 'X6', 'fix', 'an image without a credit line was left out')
        return None
    return {'type': 'image', 'asset': asset, 'caption': plain(b.get('caption') or '', emphasis=False)[:MAX_CAPTION],
            'credit': credit}


def strip_internal(spec):
    """A model's spec with what only code may write removed: internal `image` blocks (their asset ids point at local
    files), a `font` (the brief sets it), a `design` (read from an attached design file) and a `format` (it marks a spec
    normalize already fitted, whose slides may pass 40). Everything else is left for normalize, which repairs it."""
    if not isinstance(spec, dict):
        return spec
    spec = {k: v for k, v in spec.items() if k not in ('font', 'design', 'format')}
    if isinstance(spec.get('sections'), list):
        spec['sections'] = [{**s, 'blocks': [_no_asset(b) for b in s['blocks'] if not (isinstance(b, dict) and
                                                                                       b.get('type') == 'image' and
                                                                                       'asset' in b)]}
                            if isinstance(s, dict) and isinstance(s.get('blocks'), list) else s
                            for s in spec['sections']]
    return spec


def _no_asset(b):
    """A model's labelled figure loses its picture id (only code resolves pictures; normalize then keeps its callouts
    as a list)."""
    if isinstance(b, dict) and _labelled_type(b.get('type')) and 'image' in b:
        return {k: v for k, v in b.items() if k != 'image'}
    return b


def _labelled_type(t) -> bool:
    return isinstance(t, str) and _TYPE_ALIASES.get(re.sub(r'[\s-]+', '_', t.strip().lower())) == 'labelled'


def _fix_levels(sections: list, res: dict, fmt: str):
    """Heading levels 1-3 that never skip a level (F5, and PDF outlines need it too)."""
    prev = 0
    for s in sections:
        try:
            lvl = int(s.get('level') or 1)
        except (TypeError, ValueError, OverflowError):
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
        if b['type'] in ('page_break', 'figure'):
            continue
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


# ---------- repair: whatever the model wrote becomes a readable spec (S1, S7, S8, L1) ----------

MAX_REPLY_BYTES = 2_000_000   # L6: a reply larger than this is not read as a file spec
MAX_NEST = 40                 # deeper values are kept as their JSON text
SALVAGE_CHARS = 2000          # a block that cannot be read keeps at most this much of its text
SHORT_HEADING = 80            # S8: a section given as text up to this long becomes a heading
MAX_FITTED = 2000             # sections of a spec normalize already fitted (a deck's slides can pass 40)
MAX_COLUMN = 80               # S6: column names

_TYPE_ALIASES = {
    **dict.fromkeys(('bullets', 'list', 'ul', 'bullet', 'bulleted_list', 'bullet_list', 'unordered_list', 'points',
                     'bullet_points', 'items'), 'bullets'),
    **dict.fromkeys(('ol', 'numbered_list', 'numbered', 'ordered_list', 'number_list', 'steps'), 'ordered'),
    **dict.fromkeys(('paragraph', 'text', 'para', 'p', 'body', 'markdown', 'description', 'prose', 'summary',
                     'note', 'callout'), 'paragraph'),
    **dict.fromkeys(('quote', 'blockquote', 'quotation'), 'quote'),
    **dict.fromkeys(('code', 'pre', 'snippet', 'source_code', 'code_block', 'codeblock'), 'code'),
    **dict.fromkeys(('table', 'grid', 'matrix', 'data_table', 'datatable'), 'table'),
    **dict.fromkeys(('chart', 'graph', 'plot', *CHART_KINDS, *CHART_ALIASES, 'bar_chart', 'line_chart', 'pie_chart',
                     'column_chart'), 'chart'),
    **dict.fromkeys(('page_break', 'hr', 'divider', 'break', 'pagebreak', 'separator', 'rule'), 'page_break'),
    **dict.fromkeys(('figure', 'picture', 'image', 'photo', 'img', 'illustration'), 'figure'),
    **dict.fromkeys(('heading', 'header', 'h1', 'h2', 'h3', 'h4', 'subheading', 'title'), 'heading'),
    'timeline': 'timeline', 'tree': 'tree', 'hierarchy': 'tree', 'org_chart': 'tree', 'flow': 'flow',
    'flowchart': 'flow', 'process': 'flow', 'diagram': 'diagram',
    # the Studio kinds; "process" and "matrix" keep meaning a flow and a table unless the block has the new kind's
    # fields (steps without arrows, quadrants), and "scatter" stays a line chart unless it has points (_coerce)
    **dict.fromkeys(('cycle', 'cycle_diagram', 'life_cycle', 'lifecycle', 'loop', 'circular_flow'), 'cycle'),
    **dict.fromkeys(('venn', 'venn_diagram', 'overlap'), 'venn'),
    **dict.fromkeys(('pyramid', 'pyramid_diagram', 'hierarchy_pyramid', 'triangle'), 'pyramid'),
    **dict.fromkeys(('quadrant', 'quadrants', '2x2', 'two_by_two', 'matrix_2x2', 'swot', 'quadrant_chart'), 'matrix'),
    **dict.fromkeys(('mindmap', 'mind_map', 'concept_map', 'spider_diagram', 'spidergram'), 'mindmap'),
    **dict.fromkeys(('process_arrows', 'process_diagram', 'stages', 'chevrons'), 'process'),
    **dict.fromkeys(('comparison', 'compare', 'versus', 'vs', 'pros_cons', 'pros_and_cons', 'comparison_cards'),
                    'comparison'),
    **dict.fromkeys(('labelled', 'labeled', 'labelled_figure', 'labeled_figure', 'labelled_diagram',
                     'labeled_diagram', 'annotated_image', 'callouts'), 'labelled'),
    **dict.fromkeys(('stat_cards', 'stats', 'statistics', 'stat', 'kpi', 'kpis', 'key_figures', 'big_numbers',
                     'figures_cards', 'numbers'), 'stat-cards'),
    **dict.fromkeys(('scatter_plot', 'scatterplot', 'scatter_chart', 'scatter_graph'), 'scatter'),
}
# a salvaged block's text leaves these out: they name how to draw, not what to say
_SALVAGE_SKIP = {'type', 'kind', 'lang', 'ordered', 'level', 'id', 'parent', 'from', 'to', 'formats', 'asset', 'credit',
                 'query'} | FETCH_KEYS
_TEXT_KEYS = ('text', 'label', 'title', 'content', 'name', 'value', 'description', 'heading')


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def _first(d: dict, *keys):
    """(key, value) of the first key with a non-empty value; when every key given is empty ('', [], {}), the first
    one present; else (None, None). An empty 'sections' or 'heading' never hides the content under an alias."""
    present = None
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        if not _empty(v):
            return k, v
        if present is None:
            present = k
    return (present, d[present]) if present is not None else (None, None)


def _text(v) -> str:
    """The words in a value: a string as it is, numbers as text, a list joined, a dict by its text-like key."""
    if v is None or isinstance(v, bool):
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v) if finite(v) and not (isinstance(v, int) and abs(v) >= 10 ** 30) else ''
    if isinstance(v, list):
        return ' '.join(t for t in (_text(x) for x in v) if t.strip())
    if isinstance(v, dict):
        for k in _TEXT_KEYS:
            if isinstance(v.get(k), str) and v[k].strip():
                return v[k]
        return next((x for x in v.values() if isinstance(x, str) and x.strip()), '')
    return str(v)


def _strings(v, out: list, key=None) -> list[str]:
    """Every string in a block (a salvaged block keeps these), skipping keys that name how to draw it."""
    if isinstance(v, str):
        if v.strip():
            out.append(v.strip())
    elif isinstance(v, dict):
        for k, x in v.items():
            if str(k).lower() not in _SALVAGE_SKIP:
                _strings(x, out, k)
    elif isinstance(v, list):
        for x in v:
            _strings(x, out, key)
    return out


def _clean_tree(v, depth: int = 0):
    """Step 1: every string without the characters XML cannot hold (a lone surrogate included), every key a string,
    tuples as lists, and nothing deeper than MAX_NEST (deeper values become their JSON text)."""
    if isinstance(v, str):
        return clean_chars(v)
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if depth >= MAX_NEST:
        try:
            return clean_chars(json.dumps(v, ensure_ascii=False, default=str))
        except (ValueError, TypeError, RecursionError):
            return ''
    if isinstance(v, dict):
        return {clean_chars(k): _clean_tree(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean_tree(x, depth + 1) for x in v]
    return clean_chars(str(v))


class _Log:
    """Repairs noted once per kind with the sections they happened in ("bullets without items repaired in sections
    7 and 9"), so a badly written deck gets a few readable notes, not one per block."""

    def __init__(self):
        self.seen: dict[tuple, list] = {}

    def add(self, rid: str, one: str, many: str, where):
        self.seen.setdefault((rid, one, many), []).append(where)

    def flush(self, res: dict):
        for (rid, one, many), wheres in self.seen.items():
            _note(res, rid, 'fix', (many if len(wheres) > 1 else one).format(w=_where(wheres)))


def _where(wheres: list) -> str:
    secs = sorted({w for w in wheres if isinstance(w, int)})
    parts = []
    if None in wheres:
        parts.append('the spec')
    if secs:
        shown = [str(n) for n in secs[:6]] + ([f'{len(secs) - 6} more'] if len(secs) > 6 else [])
        names = shown[0] if len(shown) == 1 else ', '.join(shown[:-1]) + ' and ' + shown[-1]
        parts.append(('section ' if len(secs) == 1 else 'sections ') + names)
    return ' and '.join(parts) or 'the spec'


# the keys a table's rows may be given under (_table reads them in this order)
_ROW_KEYS = ('rows', 'data', 'cells', 'body', 'values')


def _strip_fetch(d, where, log: _Log, top: bool = False):
    """S7: link, image and path keys removed in place (table cells are content and are left alone)."""
    if isinstance(d, dict):
        for k in [k for k in d if k.lower() in FETCH_KEYS]:
            if k == 'image' and _labelled_type(d.get('type')) and isinstance(d[k], str) and ASSET_ID.match(d[k].lower()):
                continue   # a labelled figure's picture is a local asset-cache id (code wrote it), not an address
            del d[k]
            log.add('S7', 'link or image addresses in {w} were removed; nothing is fetched',
                    'link or image addresses in {w} were removed; nothing is fetched', where)
        kind = str(d.get('type') or d.get('kind') or '').lower().strip().replace('-', '_').replace(' ', '_')
        table = _TYPE_ALIASES.get(kind) == 'table' or any(k in d for k in ('columns', 'headers', 'header', 'cols',
                                                                           'column_names'))
        for k, v in d.items():
            if k == 'rows' or (table and k in _ROW_KEYS) or (top and k in ('sections', 'slides', 'pages', 'parts',
                                                                             'chapters')):
                continue
            _strip_fetch(v, where, log)
    elif isinstance(d, list):
        for v in d:
            _strip_fetch(v, where, log)


def _split_items(s: str) -> list[str]:
    """A string where a list was expected: split on list markers, then lines, then "; " when that gives 2 or more."""
    lines = [ln for ln in s.replace('\r\n', '\n').replace('\r', '\n').split('\n') if ln.strip()]
    if len(lines) > 1:
        return lines
    one = lines[0] if lines else ''
    for sep in (' • ', '; '):
        parts = [p.strip() for p in one.strip().lstrip('•').split(sep) if p.strip()]
        if len(parts) >= 2:
            return parts
    return [one] if one.strip() else []


def _items(v, ctx) -> list[str]:
    """Bullet items from whatever was given: a string is split, dicts give their text, nested lists are flattened
    one level (a dict item with sub-items gives its text, then theirs)."""
    if v is None or isinstance(v, bool):
        return []
    if isinstance(v, str):
        got = _split_items(v)
        if len(got) > 1:
            ctx.flag('split')
        return got
    if isinstance(v, dict):
        if any(k in v for k in _TEXT_KEYS + ('items', 'children')):
            v = [v]
        else:
            return [f'{k}: {_text(x)}' if _text(x) else str(k) for k, x in v.items()]
    if not isinstance(v, list):
        return [_text(v)] if _text(v) else []
    out = []
    for x in v:
        if isinstance(x, list):
            out.extend(_text(y) for y in x)
        elif isinstance(x, dict):
            out.append(_text({k: y for k, y in x.items() if k not in ('items', 'children', 'sub', 'subitems')}))
            sub = x.get('items') or x.get('children') or x.get('sub') or x.get('subitems')
            if isinstance(sub, list):
                out.extend(_text(y) for y in sub)
        else:
            out.append(_text(x))
    return [x for x in out if x and x.strip()]


# a comma that separates cells, not one inside a number's thousands ("Apple, $1,200" is two cells, not three)
_CELL_COMMA = re.compile(r',(?!\d{3}(?:\D|$))')


def _cells_line(line: str) -> list[str]:
    s = line.strip().strip('|')
    return [c.strip() for c in (s.split('|') if '|' in s else _CELL_COMMA.split(s))]


def _table(b: dict, ctx) -> dict:
    ck, cols = _first(b, 'columns', 'headers', 'header', 'cols', 'column_names')
    rk, rows = _first(b, *_ROW_KEYS)
    if ck not in (None, 'columns') or rk not in (None, 'rows'):
        ctx.flag('fields')
    if isinstance(cols, str):
        cols = _cells_line(cols)
    elif isinstance(cols, dict):
        cols = list(cols.values()) if all(isinstance(k, str) and k.isdigit() for k in cols) else list(cols)
    elif not isinstance(cols, list):
        cols = []
    cols = [_text(c) for c in cols]
    if isinstance(rows, str):
        lines = [ln for ln in rows.split('\n') if ln.strip() and not _TABLE_SEP.match(ln)]
        rows = [_cells_line(ln) for ln in lines]
        ctx.flag('split')
    elif isinstance(rows, dict):
        rows = [[k, *(x if isinstance(x, list) else [x])] for k, x in rows.items()]
    elif not isinstance(rows, list):
        rows = []
    dict_rows = [r for r in rows if isinstance(r, dict) and not (len(r) == 1 and 'formula' in r)]
    if dict_rows:
        if not cols:
            for r in dict_rows:
                cols += [k for k in r if k not in cols]
        low = {c.lower(): c for c in cols}
        keys = {str(k).lower() for r in dict_rows for k in r}
        if not keys & set(low) and not all(len(r) == len(cols) for r in dict_rows):
            # no row key names a column, and the rows do not line up with the columns: the keys are the columns
            cols = []
            for r in dict_rows:
                cols += [str(k) for k in r if str(k) not in cols]
            low = {c.lower(): c for c in cols}
            ctx.flag('rowkeys')
        fixed, in_order = [], False
        for r in rows:
            if isinstance(r, dict) and r in dict_rows:
                by = {str(k).lower(): x for k, x in r.items()}
                if len(set(low)) == len(cols) and set(by) & set(low):
                    fixed.append([by.get(c.lower()) for c in cols])
                else:  # keys that match no column: the values in the order given
                    fixed.append(list(r.values()))
                    in_order = in_order or len(set(low)) == len(cols)
            else:
                fixed.append(r)
        if in_order:
            ctx.flag('rowkeys')
        rows = fixed
    out = {'type': 'table', 'columns': cols, 'rows': [r if isinstance(r, list) else [r] for r in rows]}
    if b.get('title') is not None:
        out['title'] = _text(b['title'])
    if isinstance(b.get('formats'), list):
        out['formats'] = b['formats']
    return out


def _values(v) -> list:
    """Chart values as a list; {'x': .., 'y': ..} or {'label': .., 'value': ..} points give their value. A string is
    split on commas that are not thousands separators ("10, 20, 30"), else on spaces or semicolons."""
    if isinstance(v, dict):
        v = list(v.values())
    if isinstance(v, str):
        parts = [p.strip() for p in _CELL_COMMA.split(v) if p.strip()]
        if len(parts) < 2:
            parts = [p for p in re.split(r'[\s;]+', v.strip()) if p]
        return parts
    if not isinstance(v, list):
        return [v]
    return [_first(x, 'value', 'y', 'v', 'count', 'amount')[1] if isinstance(x, dict) else x for x in v]


def _chart(b: dict, kind: str | None, ctx) -> dict:
    lk, labels = _first(b, 'labels', 'categories', 'x', 'xlabels', 'x_labels', 'x_axis', 'keys')
    sk, series = _first(b, 'series', 'datasets', 'values', 'data', 'y')
    if lk not in (None, 'labels') or sk not in (None, 'series'):
        ctx.flag('fields')
    if isinstance(series, dict) and ('datasets' in series or 'labels' in series):
        # Chart.js: data: {labels, datasets: [{label, data}]}
        return _chart({**{k: v for k, v in b.items() if k != sk}, **series}, kind, ctx)
    if isinstance(labels, str):
        labels = _split_items(labels) if '\n' in labels or ';' in labels else _cells_line(labels)
    elif isinstance(labels, dict):
        labels = list(labels.values())
    labels = [_text(x) for x in labels] if isinstance(labels, list) else []
    if isinstance(series, dict):
        if any(k in series for k in ('values', 'data', 'points', 'y')):
            series = [series]
        elif all(isinstance(x, list) for x in series.values()):
            series = [{'name': k, 'values': x} for k, x in series.items()]
        else:  # {label: value}
            labels = labels or [str(k) for k in series]
            series = [{'name': '', 'values': list(series.values())}]
    if isinstance(series, list) and series and all(isinstance(x, dict) and _first(x, 'value', 'y', 'count', 'amount')[0]
                                                   and not _first(x, 'values', 'data')[0] for x in series):
        # data: [{label, value}] is labels and one series
        labels = labels or [_text(_first(x, 'label', 'name', 'x', 'category', 'key')[1]) for x in series]
        series = [{'name': '', 'values': _values(series)}]
    out_series, loose = [], []
    for s in series if isinstance(series, list) else []:
        if isinstance(s, dict):
            _, vals = _first(s, 'values', 'data', 'points', 'y', 'value')
            if not labels and isinstance(vals, list) and vals and all(isinstance(x, dict) for x in vals):
                # points written as {label|x: .., value|y: ..} name their own categories
                got = [_text(_first(x, 'label', 'x', 'name', 'category', 'key')[1]) for x in vals]
                if any(got):
                    labels = [g or str(j + 1) for j, g in enumerate(got)]
            out_series.append({'name': _text(_first(s, 'name', 'label', 'title', 'key')[1]), 'values': _values(vals)
                               if vals is not None else []})
        elif isinstance(s, list):
            out_series.append({'name': '', 'values': _values(s)})
        elif isinstance(s, str) and number(s, loose=True) is None:
            ctx.res_note('S5', 'a chart series written as text was dropped')
        else:
            loose.append(s)
    if loose:  # a bare list of numbers is one series
        out_series.insert(0, {'name': '', 'values': loose})
    k = kind or b.get('kind') or b.get('chart_type') or b.get('style')
    k = str(k or 'bar').lower().strip().replace('_chart', '')
    return {'type': 'chart', 'kind': k, 'title': _text(b.get('title') or b.get('caption') or b.get('name')),
            'labels': labels, 'series': out_series}


def _event(e) -> dict | None:
    if isinstance(e, (list, tuple)):
        e = {'date': e[0] if e else '', 'label': ' '.join(_text(x) for x in e[1:])}
    if isinstance(e, dict):
        return {'date': _text(_first(e, 'date', 'year', 'when', 'time', 'period', 'era')[1]),
                'label': _text(_first(e, 'label', 'title', 'text', 'event', 'description', 'name', 'what')[1])}
    s = _text(e).strip()
    return diagram.parse_event(s) if s else None


def _slug(s: str, used: set) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:32] or 'node'
    nid, n = base, 2
    while nid in used:
        nid, n = f'{base}-{n}', n + 1
    used.add(nid)
    return nid


def _nodes(raw, used: set, parent: str = '', depth: int = 0) -> list[dict]:
    """Tree and flow nodes: strings become {id, label}; nested children become parent links."""
    out = []
    if isinstance(raw, dict):
        raw = [raw] if any(k in raw for k in ('id', 'label', 'text', 'name', 'children')) else \
            [{'label': k, 'children': v} if isinstance(v, (list, dict)) else {'label': f'{k}: {_text(v)}'}
             for k, v in raw.items()]
    for n in raw if isinstance(raw, list) else [raw]:
        if isinstance(n, dict):
            label = _text(_first(n, 'label', 'text', 'title', 'name', 'value')[1])
            nid = _text(_first(n, 'id', 'key', 'name')[1]) or label
            if not nid.strip():
                continue
            used.add(nid)
            par = _text(_first(n, 'parent', 'parent_id', 'parentId', 'under')[1]) or parent
            out.append({'id': nid, 'label': label or nid, 'parent': par})
            kids = _first(n, 'children', 'nodes', 'items')[1]
            if kids is not None and depth < 8:
                out += _nodes(kids, used, nid, depth + 1)
        elif _text(n).strip():
            label = _text(n).strip()
            out.append({'id': _slug(label, used), 'label': label, 'parent': parent})
    return out


_ARROW = re.compile(r'\s*(?:-+>|→|=+>|\s+to\s+)\s*')


def _edges(raw) -> list[dict]:
    if isinstance(raw, dict):
        raw = [raw] if any(k in raw for k in ('from', 'to', 'source', 'target')) else \
            [{'from': k, 'to': v} for k, v in raw.items() if isinstance(v, str)]
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, str):
            parts = [p.strip() for p in _ARROW.split(e) if p.strip()]
            out += [{'from': a, 'to': b, 'label': ''} for a, b in zip(parts, parts[1:])]
        elif isinstance(e, (list, tuple)) and len(e) >= 2:
            out.append({'from': _text(e[0]), 'to': _text(e[1]), 'label': _text(e[2]) if len(e) > 2 else ''})
        elif isinstance(e, dict):
            a, b = _first(e, 'from', 'source', 'start', 'a')[1], _first(e, 'to', 'target', 'end', 'b')[1]
            if a is None and b is None and len(e) == 1:
                (a, b), = e.items()
            out.append({'from': _text(a), 'to': _text(b), 'label': _text(_first(e, 'label', 'text', 'name')[1])})
    return out


def _group_list(v) -> list:
    """Sets, quadrants or columns however written: a list of {label, items}, a {label: items} dict, or text."""
    if isinstance(v, dict):
        if any(k in v for k in ('label', 'items', 'name', 'title')):
            v = [v]
        else:
            v = [{'label': str(k), 'items': x} for k, x in v.items()]
    if isinstance(v, str):
        v = _split_items(v)
    out = []
    for g in v if isinstance(v, list) else []:
        if isinstance(g, dict):
            label = _text(_first(g, 'label', 'name', 'title', 'heading', 'text')[1])
            items = _first(g, 'items', 'points', 'members', 'list', 'values', 'bullets')[1]
            if isinstance(items, str):
                items = _split_items(items)
            elif isinstance(items, dict):
                items = [f'{k}: {_text(x)}' for k, x in items.items()]
            out.append({'label': label, 'items': [_text(x) for x in items] if isinstance(items, list) else []})
        elif isinstance(g, (list, tuple)) and g:
            out.append({'label': _text(g[0]), 'items': [_text(x) for x in g[1:]]})
        elif _text(g).strip():
            out.append({'label': _text(g), 'items': []})
    return out


def _texts(v) -> list:
    if isinstance(v, str):
        return _split_items(v)
    if isinstance(v, dict):
        return [f'{k}: {_text(x)}' if _text(x) else str(k) for k, x in v.items()]
    return [x if isinstance(x, dict) else _text(x) for x in v] if isinstance(v, list) else []


def _generic_diagram(b: dict, kind: str) -> dict:
    """The schema's generic diagram block (DOCSPEC_SCHEMA's _DIAGRAM_BLOCK: kind, items[{label, detail, parent, value,
    x, y, items}], shared, x_label, y_label) in the kind's own fields, so the readers below see the usual shape. A
    block that already uses the kind's own list is returned as it is."""
    items = b.get('items')
    own = DIAGRAM_LISTS.get(kind)
    if not isinstance(items, list) or not any(isinstance(x, dict) for x in items) or (own and b.get(own)):
        return b
    rows = [x for x in items if isinstance(x, dict)]
    lab = lambda x: _text(x.get('label'))  # noqa: E731
    out = {'type': kind, 'title': b.get('title')}
    if kind in ('cycle', 'pyramid'):
        out['steps' if kind == 'cycle' else 'levels'] = [lab(x) for x in rows]
    elif kind in ('venn', 'matrix', 'comparison'):
        out[DIAGRAM_LISTS[kind]] = [{'label': lab(x), 'items': [_text(i) for i in x.get('items') or []
                                                                if _text(i).strip()]} for x in rows]
        if kind == 'venn':
            out['shared'] = b.get('shared') or []
        if kind == 'matrix':
            out['x_axis'], out['y_axis'] = _text(b.get('x_label')), _text(b.get('y_label'))
    elif kind == 'mindmap':
        out['nodes'] = [{'id': lab(x), 'label': lab(x), 'parent': _text(x.get('parent'))} for x in rows]
    elif kind == 'process':
        out['steps'] = [{'label': lab(x), 'detail': _text(x.get('detail'))} for x in rows]
    elif kind == 'stat-cards':
        out['stats'] = [{'value': _text(x.get('value')) or _text(x.get('x')), 'label': lab(x) or _text(x.get('detail'))}
                        for x in rows]
    elif kind == 'scatter':
        out['points'] = [{'x': x.get('x'), 'y': x.get('y'), 'label': lab(x)} for x in rows]
        out['x_label'], out['y_label'] = b.get('x_label'), b.get('y_label')
    else:
        return b
    return out


def _studio_diagram(b: dict, kind: str, title: str) -> dict:
    """A Studio diagram kind as the model wrote it, in the DocSpec shape (diagram.clean_block then checks limits)."""
    b = _generic_diagram(b, kind)
    out = {'type': kind, 'title': title}
    if kind in ('cycle', 'pyramid'):
        _, v = _first(b, 'steps' if kind == 'cycle' else 'levels', 'items', 'stages', 'levels', 'steps', 'tiers',
                      'layers', 'nodes', 'events')
        out['steps' if kind == 'cycle' else 'levels'] = [_text(x) for x in _texts(v)]
    elif kind in ('venn', 'matrix', 'comparison'):
        key = {'venn': 'sets', 'matrix': 'quadrants', 'comparison': 'columns'}[kind]
        _, v = _first(b, key, 'sets', 'circles', 'groups', 'quadrants', 'cells', 'columns', 'sides', 'options',
                      'items')
        out[key] = _group_list(v)
        if kind == 'venn':
            out['shared'] = [_text(x) for x in _texts(_first(b, 'shared', 'overlap', 'both', 'common',
                                                             'intersection', 'all')[1])]
        if kind == 'matrix':
            out['x_axis'] = _text(_first(b, 'x_axis', 'x_label', 'horizontal', 'xAxis')[1])
            out['y_axis'] = _text(_first(b, 'y_axis', 'y_label', 'vertical', 'yAxis')[1])
    elif kind == 'mindmap':
        used: set = set()
        _, v = _first(b, 'nodes', 'branches', 'children', 'items', 'ideas')
        nodes = _nodes(v if v is not None else [], used)
        centre = _text(_first(b, 'root', 'center', 'centre', 'topic', 'central')[1])
        if centre.strip():
            rid = _slug(centre, used)
            nodes = [{'id': rid, 'label': centre, 'parent': ''}] + [
                {**n, 'parent': n['parent'] or rid} for n in nodes]
        out['nodes'] = nodes
    elif kind == 'process':
        _, v = _first(b, 'steps', 'stages', 'items', 'nodes', 'phases')
        steps = []
        for x in _texts(v):
            if isinstance(x, dict):
                steps.append({'label': _text(_first(x, 'label', 'title', 'name', 'step', 'text')[1]),
                              'detail': _text(_first(x, 'detail', 'description', 'details', 'text', 'body')[1])
                              if any(x.get(k) for k in ('label', 'title', 'name', 'step')) else ''})
            else:
                steps.append(x)
        out['steps'] = steps
    elif kind == 'labelled':
        _, img = _first(b, 'image', 'asset')
        out['image'] = img if isinstance(img, str) else ''
        _, v = _first(b, 'callouts', 'labels', 'annotations', 'points', 'markers')
        calls = []
        for c in v if isinstance(v, list) else []:
            if isinstance(c, dict):
                calls.append({'label': _text(_first(c, 'label', 'text', 'name', 'title')[1]),
                              'x': _first(c, 'x', 'left', 'across')[1], 'y': _first(c, 'y', 'top', 'down')[1]})
            elif isinstance(c, (list, tuple)) and len(c) >= 3:
                calls.append({'label': _text(c[0]), 'x': c[1], 'y': c[2]})
            elif _text(c).strip():
                calls.append({'label': _text(c), 'x': None, 'y': None})
        out['callouts'] = calls
    elif kind == 'stat-cards':
        _, v = _first(b, 'stats', 'items', 'cards', 'figures', 'numbers', 'values', 'kpis')
        stats = []
        for x in _texts(v):
            if isinstance(x, dict):
                val = _first(x, 'value', 'number', 'stat', 'figure', 'amount')[1]
                stats.append({'value': val if isinstance(val, (int, float)) and not isinstance(val, bool) else _text(val),
                              'label': _text(_first(x, 'label', 'text', 'name', 'title', 'description')[1]),
                              'icon': _text(x.get('icon'))})
            else:
                stats.append(x)
        out['stats'] = stats
    else:   # scatter
        _, v = _first(b, 'points', 'data', 'values', 'items')
        pts = []
        for p in v if isinstance(v, list) else []:
            if isinstance(p, dict):
                pts.append({'x': _first(p, 'x', 'X')[1], 'y': _first(p, 'y', 'Y')[1],
                            'label': _text(_first(p, 'label', 'name', 'text')[1])})
            else:
                pts.append(p)
        out['points'] = pts
        out['x_label'] = _text(_first(b, 'x_label', 'x_axis', 'xlabel', 'xAxis', 'x_title')[1])
        out['y_label'] = _text(_first(b, 'y_label', 'y_axis', 'ylabel', 'yAxis', 'y_title')[1])
    return out


def _diagram(b: dict, kind: str, ctx) -> dict:
    title = _text(b.get('title') or b.get('caption') or b.get('name'))
    if kind in diagram.NEW_KINDS:
        return _studio_diagram(b, kind, title)
    if kind == 'timeline':
        _, events = _first(b, 'events', 'items', 'milestones', 'entries', 'dates', 'steps')
        if isinstance(events, dict):
            events = [{'date': k, 'label': _text(v)} for k, v in events.items()]
        elif isinstance(events, str):
            events = _split_items(events)
        events = [x for x in (_event(e) for e in (events if isinstance(events, list) else [])) if x]
        return {'type': 'timeline', 'title': title, 'events': events}
    used: set = set()
    _, nodes = _first(b, 'nodes', 'items', 'steps', 'children', 'elements')
    nodes = _nodes(nodes if nodes is not None else [], used)
    edges = _edges(_first(b, 'edges', 'connections', 'arrows')[1])
    if kind == 'tree':
        return {'type': 'tree', 'title': title, 'nodes': nodes}
    if not nodes and edges:  # arrows only: their ends are the steps, in order
        for e in edges:
            for end in (e['from'], e['to']):
                if end and end not in used:
                    used.add(end)
                    nodes.append({'id': end, 'label': end, 'parent': ''})
    if not edges and len(nodes) > 1:
        edges = [{'from': a['id'], 'to': c['id'], 'label': ''} for a, c in zip(nodes, nodes[1:])]
        ctx.res_note('S6', 'a flow without arrows was drawn in the order its steps were given')
    return {'type': 'flow', 'title': title, 'nodes': nodes, 'edges': edges}


def _infer_studio(b: dict) -> str | None:
    """A Studio diagram kind from fields only it uses."""
    for key, kind in (('quadrants', 'matrix'), ('sets', 'venn'), ('levels', 'pyramid'), ('callouts', 'labelled'),
                      ('stats', 'stat-cards')):
        if isinstance(b.get(key), (list, dict)) and b.get(key):
            return kind
    pts = b.get('points')
    if isinstance(pts, list) and pts and all(isinstance(p, dict) and 'x' in p and 'y' in p for p in pts):
        return 'scatter'
    return None


def _infer(b: dict) -> str | None:
    """A block's type from its keys, first match wins."""
    has = lambda *ks: any(b.get(k) is not None for k in ks)  # noqa: E731
    if _infer_studio({'points': b.get('points')}) and not has('items', 'bullets'):
        return 'scatter'
    if has('items', 'points', 'bullets'):
        return 'bullets'
    if has('rows', 'columns', 'headers'):
        return 'table'
    if has('series', 'datasets') or (has('data', 'values') and has('labels', 'categories')):
        return 'chart'
    if has('events', 'milestones'):
        return 'timeline'
    new = _infer_studio(b)
    if new:
        return new
    if has('edges'):
        return 'flow'
    if has('nodes'):
        nodes = b['nodes'] if isinstance(b['nodes'], list) else []
        return 'tree' if any(isinstance(n, dict) and (n.get('parent') or n.get('children')) for n in nodes) else 'flow'
    if has('text', 'content', 'body', 'paragraph', 'value', 'description'):
        return 'paragraph'
    if has('code', 'lang', 'language'):
        return 'code'
    if has('quote'):
        return 'quote'
    if has('query', 'caption', 'alt'):
        return 'figure'
    return None


_STUDIO_KIND_ALIAS = {'stat': 'stat-cards', 'stats': 'stat-cards', 'labeled': 'labelled', 'mind-map': 'mindmap',
                      'quadrant': 'matrix', '2x2': 'matrix', 'scatter-plot': 'scatter', 'venn-diagram': 'venn'}


def _studio_kind(t: str, kind: str, b: dict) -> str:
    """The words "process", "matrix" and "scatter" name a Studio kind only when the block has that kind's fields;
    otherwise they keep today's meaning (a flow, a table, a line chart)."""
    if t == 'process' and not any(b.get(k) is not None for k in ('edges', 'nodes', 'connections', 'arrows')) and \
            b.get('steps') is not None:
        return 'process'
    if kind == 'process' and any(b.get(k) is not None for k in ('edges', 'connections', 'arrows')):
        return 'flow'
    if t == 'matrix' and b.get('quadrants') is not None and not any(b.get(k) is not None for k in ('rows', 'columns')):
        return 'matrix'
    if kind == 'matrix' and not any(b.get(k) is not None for k in ('quadrants', 'cells', 'items')) and \
            any(b.get(k) is not None for k in ('rows', 'columns')):
        return 'table'
    pts = b.get('points')
    if t == 'scatter' and isinstance(pts, list) and pts and all(isinstance(p, (dict, list, tuple)) for p in pts):
        return 'scatter'
    if kind == 'scatter' and not (isinstance(pts, list) and pts) and \
            any(b.get(k) is not None for k in ('series', 'labels')):
        return 'chart'
    return kind


class _Ctx:
    """What the block reader needs to note a repair against the section it is in."""

    def __init__(self, res: dict, log: _Log, si: int, level):
        self.res, self.log, self.si, self.level = res, log, si, level

    NOTES = {
        'alias': ('S1', 'block types written another way were read in {w}', 'block types written another way were read in {w}'),
        'infer': ('S1', 'a block without a known type was read by its fields in {w}',
                  'blocks without a known type were read by their fields in {w}'),
        'fields': ('S1', 'fields written another way were read in {w}', 'fields written another way were read in {w}'),
        'split': ('S1', 'text where a list was expected was split into items in {w}',
                  'text where a list was expected was split into items in {w}'),
        'salvage': ('S1', 'a block in {w} could not be read and was kept as text',
                    'blocks in {w} could not be read and were kept as text'),
        'empty': ('S1', 'an empty block in {w} was left out', 'empty blocks in {w} were left out'),
        'heading': ('S8', 'a heading written as a block started a new section in {w}',
                    'headings written as blocks started new sections in {w}'),
        'rowkeys': ('S1', 'table rows with other key names were read in order in {w}',
                    'table rows with other key names were read in order in {w}'),
    }

    def flag(self, what: str):
        rid, one, many = self.NOTES[what]
        self.log.add(rid, one, many, self.si)

    def res_note(self, rid: str, note: str):
        _note(self.res, rid, 'fix', note)


def _coerce(b, ctx: _Ctx) -> list[dict]:
    """One block as the model wrote it -> blocks in the DocSpec shape (a heading block comes back as
    {'type': '_heading'}). Raises on shapes it cannot read, which the caller salvages."""
    if b is None or isinstance(b, bool):
        return []
    if isinstance(b, (int, float)):
        b = _text(b)
    if isinstance(b, str):
        return [{'type': 'paragraph', 'text': b}] if b.strip() else []
    if isinstance(b, list):
        if all(not isinstance(x, (dict, list)) for x in b):
            ctx.flag('fields')
            return [{'type': 'bullets', 'items': _items(b, ctx)}]
        return [c for x in b for c in _coerce(x, ctx)]
    if not isinstance(b, dict):
        raise ValueError('not a block')
    raw_type = b.get('type')
    t = re.sub(r'[\s-]+', '_', str(raw_type).strip().lower()) if isinstance(raw_type, str) else ''
    if t == 'image' and 'asset' in b:
        return [b]  # an internal image block (code wrote it); normalize checks it against the asset cache (X6)
    kind = _TYPE_ALIASES.get(t)
    if kind is not None:
        kind = _studio_kind(t, kind, b)
    if kind is None:
        kind = _infer(b)
        if kind is None:
            raise ValueError('unknown block')
        ctx.flag('infer')
    elif t != kind and t.replace('_', '-') != kind and not (t in CHART_KINDS or t in ('ordered', 'diagram')):
        ctx.flag('alias')
    if kind == 'diagram':
        k = re.sub(r'[\s_]+', '-', str(b.get('kind') or '').strip().lower())
        k = _STUDIO_KIND_ALIAS.get(k, k)
        kind = k if k in diagram.ALL_KINDS else 'timeline' if b.get('events') is not None else 'flow' \
            if b.get('edges') is not None else _infer_studio(b) or _infer({'nodes': b.get('nodes')}) or 'flow'
    if kind == 'heading':
        text = _text(_first(b, 'text', 'content', 'title', 'heading', 'value', 'label')[1])
        level = {'h1': 1, 'h2': 2, 'h3': 3, 'h4': 3}.get(t)
        if level is None:
            try:
                base = int(ctx.level or 1)
            except (TypeError, ValueError, OverflowError):
                base = 1
            level = min(base + 1, 3) if t == 'subheading' else b.get('level') or base
        return [{'type': '_heading', 'text': text, 'level': level}]
    if kind in ('bullets', 'ordered'):
        k, v = _first(b, 'items', 'points', 'list', 'bullets', 'lines', 'content', 'text', 'entries')
        if k not in (None, 'items'):
            ctx.flag('fields')
        ordered = kind == 'ordered' or bool(b.get('ordered')) or t in ('ordered', 'steps')
        return [{'type': 'bullets', 'items': _items(v, ctx), 'ordered': ordered}]
    if kind == 'paragraph':
        k, v = _first(b, 'text', 'content', 'body', 'value', 'paragraph', 'description', 'markdown')
        if k not in (None, 'text'):
            ctx.flag('fields')
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v) and any('\n' in x for x in v):
            v = '\n'.join(v)
        return [{'type': 'paragraph', 'text': _text(v)}]
    if kind == 'quote':
        by = _first(b, 'by', 'author', 'source', 'cite', 'attribution')[1]
        return [{'type': 'quote', 'text': _text(_first(b, 'text', 'quote', 'content', 'body')[1]),
                 'by': by if isinstance(by, str) else ''}]
    if kind == 'code':
        v = _first(b, 'text', 'code', 'content', 'source', 'body')[1]
        if isinstance(v, list):
            v = '\n'.join(_text(x) for x in v)
        return [{'type': 'code', 'lang': _text(_first(b, 'lang', 'language')[1]), 'text': _text(v)}]
    if kind == 'table':
        return [_table(b, ctx)]
    if kind == 'chart':
        return [_chart(b, t if t in CHART_KINDS or t in CHART_ALIASES else
                       t.replace('_chart', '') if t.endswith('_chart') else None, ctx)]
    if kind in diagram.ALL_KINDS:
        return [_diagram(b, kind, ctx)]
    if kind == 'page_break':
        return [{'type': 'page_break'}]
    # figure: a picture the create agent looks up; its address was removed by S7, its caption is the search
    caption = _text(_first(b, 'caption', 'alt', 'title', 'text', 'description', 'label')[1])
    return [{'type': 'figure', 'query': _text(_first(b, 'query', 'search', 'alt', 'prompt')[1]) or caption,
             'caption': caption}]


def _salvage(b, ctx: _Ctx) -> list[dict]:
    """A block that could not be read keeps its words as a paragraph; a figure never does (its query is a search, not
    content); a block with no words is left out."""
    t = b.get('type') if isinstance(b, dict) else None
    if isinstance(t, str) and _TYPE_ALIASES.get(t.strip().lower()) == 'figure':
        ctx.flag('empty')
        return []
    text = plain(' '.join(_strings(b, [])))[:SALVAGE_CHARS].strip()
    if not text:
        ctx.flag('empty')
        return []
    ctx.flag('salvage')
    return [{'type': 'paragraph', 'text': text}]


def _read_block(raw, ctx: _Ctx) -> list:
    """[('block', dict) | ('heading', text, level)] for one block as written: repaired, cleaned (S4-S6), salvaged."""
    try:
        coerced = _coerce(raw, ctx)
    except SpecError:
        raise
    except Exception:
        return [('block', b) for b in _salvage(raw, ctx)]
    out = []
    for cb in coerced:
        if cb.get('type') == '_heading':
            out.append(('heading', cb['text'], cb['level']))
            continue
        try:
            t = _check_block(cb, f'Section {ctx.si}')
            nb = _block(cb, t, ctx.res)
        except Exception:
            out += [('block', b) for b in _salvage(cb, ctx)]
            continue
        if nb is None:
            # nothing readable in its own fields: its words (in fields with other names) are still kept
            src = raw if len(coerced) == 1 else cb
            if t not in ('image', 'figure', 'page_break') and _strings(src, []):
                out += [('block', b) for b in _salvage(src, ctx)]
            elif t != 'image':
                ctx.flag('empty')
            continue
        if nb['type'] == 'paragraph' and '\n' in nb['text']:
            for p in (p.strip() for p in nb['text'].split('\n\n')):
                if not p:
                    continue
                lines = p.split('\n')
                items = [_LIST_LINE.match(ln) for ln in lines]
                if len(lines) > 1 and all(items):
                    _note(ctx.res, 'S4', 'fix', 'Markdown lists inside paragraphs turned into bullets')
                    ordered = all(re.match(r'\s*\d', ln) for ln in lines)
                    out.append(('block', {'type': 'bullets', 'items': [m.group(1) for m in items], 'ordered': ordered}))
                else:
                    out.append(('block', {'type': 'paragraph', 'text': ' '.join(ln.strip() for ln in lines)}))
            continue
        out.append(('block', nb))
    return out


def _level(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = int(float(v)) if isinstance(v, (str, float)) else int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(-1000, min(n, 1000))


def _heading(v, res: dict) -> str:
    h = plain(_text(v), emphasis=False).replace('\n', ' ')[:200]
    if isinstance(v, str) and h != v.strip():
        _note(res, 'S4', 'fix', 'HTML or Markdown syntax in text converted to plain text')
    return h


def _read_section(raw, si: int, res: dict, log: _Log) -> list[dict]:
    """One section as written -> sections in the DocSpec shape (a heading block starts another one)."""
    if raw is None or isinstance(raw, bool):
        return []
    if isinstance(raw, (int, float)):
        raw = _text(raw)
    if isinstance(raw, str):
        text = plain(raw).strip()
        if not text:
            return []
        if '\n' not in text and len(text) <= SHORT_HEADING:
            log.add('S8', 'a section written as text became a heading in {w}',
                    'sections written as text became headings in {w}', si)
            return [{'heading': plain(text, emphasis=False), 'level': None, 'blocks': [], 'notes': ''}]
        log.add('S8', 'a section written as text became a paragraph in {w}',
                'sections written as text became paragraphs in {w}', si)
        raw = {'blocks': [raw]}
    elif isinstance(raw, list):
        log.add('S1', 'a section written as a list of blocks was read in {w}',
                'sections written as lists of blocks were read in {w}', si)
        raw = {'blocks': raw}
    if not isinstance(raw, dict):
        return []
    _strip_fetch(raw, si, log)
    hk, hv = _first(raw, 'heading', 'title', 'name', 'header', 'headline')
    if hk not in (None, 'heading'):
        log.add('S8', 'a section heading written as "{k}" was read in {{w}}'.format(k=hk),
                'section headings written another way were read in {w}', si)
    heading = _heading(hv, res)
    level = _level(raw.get('level'))
    bk, blocks = _first(raw, 'blocks', 'content', 'body', 'items', 'elements')
    if bk is None and isinstance(raw.get('type'), str) and \
            re.sub(r'[\s-]+', '_', raw['type'].strip().lower()) in _TYPE_ALIASES:
        log.add('S1', 'a block written as a section was read in {w}', 'blocks written as sections were read in {w}', si)
        raw = {k: v for k, v in raw.items() if k not in ('heading', 'title', 'name', 'header', 'headline', 'level',
                                                         'notes') or raw.get('type') in ('chart', 'table')}
        blocks, bk = [raw], 'blocks'
    if bk not in (None, 'blocks'):
        log.add('S1', 'section blocks written as "{k}" were read in {{w}}'.format(k=bk),
                'section blocks written another way were read in {w}', si)
    if isinstance(blocks, str):
        blocks = [blocks]
    elif isinstance(blocks, dict):
        blocks = [blocks]
    elif isinstance(blocks, list) and blocks and all(isinstance(x, str) for x in blocks) and bk != 'blocks':
        blocks = [{'type': 'bullets', 'items': blocks}]
    elif not isinstance(blocks, list):
        blocks = []
    lead = [] if blocks and blocks[0] is raw else \
        [{'type': 'paragraph', 'text': raw[k]} for k in ('text', 'paragraph') if isinstance(raw.get(k), str)]
    tail = [{'type': 'bullets', 'items': raw[k]} for k in ('bullets', 'points') if raw.get(k) is not None]
    blocks = lead + blocks + tail
    notes = plain(_text(_first(raw, 'notes', 'speaker_notes', 'speakerNotes', 'note')[1]), emphasis=False)
    ctx = _Ctx(res, log, si, level)
    out = []
    cur = {'heading': heading, 'level': level, 'blocks': [], 'notes': notes}
    for b in blocks:
        for item in _read_block(b, ctx):
            if item[0] == 'heading':
                text = plain(item[1], emphasis=False).replace('\n', ' ')[:200]
                if not text or (text == cur['heading'] and not cur['blocks']):
                    continue
                ctx.flag('heading')
                if cur['heading'] or cur['blocks']:
                    out.append(cur)
                    cur = {'heading': text, 'level': item[2], 'blocks': [], 'notes': ''}
                else:
                    cur.update(heading=text, level=item[2])
                continue
            nb = item[1]
            if nb['type'] == 'page_break' and cur['blocks'] and cur['blocks'][-1]['type'] == 'page_break':
                continue
            cur['blocks'].append(nb)
    out.append(cur)
    kept = []
    for s in out:
        if not s['heading'] and not s['blocks'] and s['notes']:
            # a section with only speaker notes: its words become the section's text
            s = {**s, 'blocks': [{'type': 'paragraph', 'text': s['notes']}], 'notes': ''}
            log.add('S8', 'a section with only notes kept them as its text in {w}',
                    'sections with only notes kept them as their text in {w}', si)
        if s['heading'] or s['blocks']:
            kept.append(s)
    return kept


def _top(spec, log: _Log) -> dict:
    """Step 2: the spec's own fields and its list of sections, however they were written."""
    if isinstance(spec, list):
        log.add('S1', 'the reply was a list of sections and was read as one file', '', None)
        return {'sections': spec}
    if isinstance(spec, str):
        return {'sections': [{'heading': '', 'blocks': [spec]}] if spec.strip() else []}
    if not isinstance(spec, dict):
        return {'sections': []}
    spec = dict(spec)
    _strip_fetch(spec, None, log, top=True)
    k, sections = _first(spec, 'sections', 'slides', 'pages', 'parts', 'chapters')
    if k not in (None, 'sections'):
        log.add('S1', f'sections written as "{k}" were read', '', None)
    if _empty(sections):
        bk, blocks = _first(spec, 'blocks', 'content', 'body')
        if not _empty(blocks):
            sections = [{'heading': '', 'blocks': blocks}]
            log.add('S1', 'blocks written without a section were read as one section', '', None)
    if isinstance(sections, dict):
        if any(x in sections for x in ('heading', 'blocks', 'title', 'content')):
            sections = [sections]
        else:
            sections = [({**v, 'heading': v.get('heading') or v.get('title') or str(key)} if isinstance(v, dict) else
                         {'heading': str(key), 'blocks': v}) for key, v in sections.items()]
        log.add('S1', 'sections written as an object were read as a list', '', None)
    elif isinstance(sections, str):
        sections = [sections]
    elif not isinstance(sections, list):
        sections = []
    out = {'sections': sections, 'title': _first(spec, 'title', 'name', 'heading')[1], 'subtitle': spec.get('subtitle')}
    for key in ('theme', 'paper', 'font', 'design', 'format'):
        if spec.get(key) is not None:
            out[key] = spec[key]
    return out


def _fold(sections: list[dict], res: dict, capped: bool) -> list[dict]:
    """L1: blocks past 30 continue in a section headed "<heading> (cont.)"; sections past 40 are folded into the last
    one while it has room, and the rest are cut from the end with a note saying how many."""
    out, split = [], 0
    for s in sections:
        bl = s['blocks']
        if len(bl) <= MAX_BLOCKS:
            out.append(s)
            continue
        split += 1
        for i in range(0, len(bl), MAX_BLOCKS):
            out.append({**s, 'heading': s['heading'] if i == 0 else f'{s["heading"] or "More"} (cont.)',
                        'blocks': bl[i:i + MAX_BLOCKS], 'notes': s['notes'] if i == 0 else ''})
    if split:
        _note(res, 'L1', 'fix', f'sections with more than {MAX_BLOCKS} blocks continue in a section headed "(cont.)"')
    if not capped and len(out) > MAX_FITTED:  # a spec normalize already fitted (a deck's slides) still has an end
        _note(res, 'L1', 'fix', f'{len(out) - MAX_FITTED:,} sections past {MAX_FITTED:,} were cut from the end')
        out = out[:MAX_FITTED]
    if capped and len(out) > MAX_SECTIONS:
        last = {**out[MAX_SECTIONS - 1], 'blocks': list(out[MAX_SECTIONS - 1]['blocks'])}
        folded = cut = 0
        for s in out[MAX_SECTIONS:]:
            add = ([{'type': 'paragraph', 'text': f'**{s["heading"]}**'}] if s['heading'] else []) + s['blocks']
            if not cut and len(last['blocks']) + len(add) <= MAX_BLOCKS:
                last['blocks'] += add
                folded += 1
            else:
                cut += 1
        out = out[:MAX_SECTIONS - 1] + [last]
        if folded:
            _note(res, 'L1', 'fix', f'{folded} section{"" if folded == 1 else "s"} past the limit of {MAX_SECTIONS} '
                                    f'{"was" if folded == 1 else "were"} folded into the last one')
        if cut:
            _note(res, 'L1', 'fix', f'{cut} section{"" if cut == 1 else "s"} past the limit of {MAX_SECTIONS} '
                                    f'{"was" if cut == 1 else "were"} cut from the end')
    return out


def _shrink(b: dict, budget: int) -> dict | None:
    """A block cut to about `budget` bytes of JSON, or None when it can't be."""
    chars = max(budget // 4, 0)
    if b['type'] in ('paragraph', 'quote', 'code') and chars > 20:
        return {**b, 'text': _cut_words(b['text'], chars) + ' ...'}
    if b['type'] == 'bullets':
        items, used = [], 0
        for it in b['items']:
            if used + len(it) > chars:
                if chars - used > 20:
                    items.append(_cut_words(it, chars - used) + ' ...')
                break
            items.append(it)
            used += len(it) + 4
        return {**b, 'items': items} if items else None
    return None


def _fit_size(doc: dict, res: dict) -> None:
    """L1: past 200 KB of JSON (table rows not counted), blocks are cut from the end; a single block that is still too
    large is shortened. The note says how much."""
    total = _size_without_rows(doc)
    if total <= MAX_SPEC_BYTES:
        return
    # speaker notes are shortened first (the longest first): they never cost a block or a slide
    cut_notes = 0
    for sec in sorted(doc['sections'], key=lambda x: -len(x.get('notes') or '')):
        if total <= MAX_SPEC_BYTES or len(sec.get('notes') or '') <= NOTES_KEEP:
            break
        sec['notes'] = _cut_words(sec['notes'], NOTES_KEEP)
        cut_notes += 1
        total = _size_without_rows(doc)
    if cut_notes:
        _note(res, 'L1', 'fix', f'speaker notes on {cut_notes} slide{"" if cut_notes == 1 else "s"} shortened to '
                                f'{NOTES_KEEP:,} characters to keep the spec within {MAX_SPEC_BYTES // 1000} KB')
    if total <= MAX_SPEC_BYTES:
        return
    sections, cut = doc['sections'], 0
    while total > MAX_SPEC_BYTES and sections:
        s = sections[-1]
        if not s['blocks']:
            if len(sections) == 1:
                break
            sections.pop()
            total = _size_without_rows(doc)
            continue
        n_blocks = sum(len(x['blocks']) for x in sections)
        b = s['blocks'][-1]
        size = _size_without_rows(b)
        if n_blocks == 1 or total - size <= MAX_SPEC_BYTES:
            small = _shrink(b, MAX_SPEC_BYTES - (total - size) - 2000)
            s['blocks'][-1:] = [small] if small else []
            if small:
                _note(res, 'L1', 'fix', f'a block was shortened to keep the spec within {MAX_SPEC_BYTES // 1000} KB')
            else:
                cut += 1
        else:
            s['blocks'].pop()
            cut += 1
        total = _size_without_rows(doc)
    if cut:
        _note(res, 'L1', 'fix', f'{cut} block{"" if cut == 1 else "s"} cut from the end to keep the spec within '
                                f'{MAX_SPEC_BYTES // 1000} KB')


def _repair(spec, res: dict, keep_notes: bool = True) -> dict:
    """Steps 1 to 6 of the repair (docs/PLAN-files-robust.md 2.3), recording notes in `res`. keep_notes False (a
    format with no speaker notes) clears the notes before the size check, so they never cost content."""
    log = _Log()
    try:
        cleaned = _clean_tree(spec)
    except RecursionError:
        cleaned = None
    top = _top(cleaned, log)
    sections = []
    for si, raw in enumerate(top['sections'], 1):
        sections += _read_section(raw, si, res, log)
    log.flush(res)
    sections = _fold(sections, res, capped=top.get('format') not in FORMATS)
    title = plain(_text(top.get('title')), emphasis=False).replace('\n', ' ')[:1000]
    subtitle = plain(_text(top.get('subtitle')), emphasis=False).replace('\n', ' ')[:1000]
    doc = {'title': title, 'subtitle': subtitle, 'sections': sections}
    if not keep_notes:
        for sec in sections:
            sec['notes'] = ''
    _fit_size(doc, res)
    for key in ('theme', 'paper', 'font', 'design', 'format'):
        if key in top:
            doc[key] = top[key]
    return doc


_REPAIR_RULES = (('S1', 'fix'), ('S7', 'fix'), ('S8', 'fix'), ('L1', 'fix'))


def _fresh() -> dict:
    return {rid: RuleResult(rid, sev, True, '') for rid, sev in
            (('S1', 'fix'), ('S2', 'block'), ('S3', 'fix'), ('S4', 'fix'), ('S5', 'fix'), ('S6', 'fix'),
             ('S7', 'fix'), ('L1', 'fix'), ('L2', 'fix'), ('L3', 'fix'), ('S8', 'fix'))}


def repair(spec) -> tuple[dict, list[RuleResult]]:
    """The format-independent half of normalize: never raises. {'title', 'subtitle', 'sections': [{'heading',
    'level', 'blocks', 'notes'}], plus 'theme', 'paper', 'font', 'design' when present}, with every block valid for
    the renderers, and the S1, S7, S8 and L1 results (and any other rule a repair touched)."""
    res = _fresh()
    try:
        doc = _repair(spec, res)
    except Exception as e:  # never raises: garbage is an empty spec
        _note(res, 'S1', 'fix', f'the spec could not be read ({type(e).__name__})')
        doc = {'title': '', 'subtitle': '', 'sections': []}
    keep = [res[r] for r, _ in _REPAIR_RULES] + [r for k, r in res.items() if k not in dict(_REPAIR_RULES) and
                                                 k != 'S2' and not r.ok]
    return doc, keep


def _shows(sections: list[dict]) -> bool:
    """S2: something to show: a heading, or a block that is not a page break or a figure that found no image."""
    return any(s['heading'] or any(b['type'] not in ('page_break', 'figure') for b in s['blocks']) for s in sections)


def has_text(spec) -> bool:
    """True when repair(spec) keeps at least one section with a heading or a non-empty block (the S2 test)."""
    return _shows(repair(spec)[0]['sections'])


_JSON_FENCE = re.compile(r'```[ \t]*(?:json|JSON|javascript|js)?[ \t]*\n?(.*?)(?:```|$)', re.S)


def _close_json(t: str) -> str:
    """A cut-off reply closed: an open string ended, a dangling key, colon, comma or partial word dropped or given a
    null, and every open bracket closed."""
    stack, in_str, esc = [], False, False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in '{[':
            stack.append('}' if ch == '{' else ']')
        elif ch in '}]' and stack:
            stack.pop()
    if in_str:
        t = (t[:-1] if esc else t) + '"'
    t = t.rstrip()
    for _ in range(4):
        m = re.search(r'([\[{,:])\s*(?:t|tr|tru|f|fa|fal|fals|n|nu|nul|-|-?\d+\.|-?\d+[eE][-+]?)$', t)
        if m:
            t = t[:m.end(1)]
        if t.endswith(','):
            t = t[:-1].rstrip()
        elif t.endswith(':'):
            t += ' null'
        elif stack and stack[-1] == '}' and re.search(r'[{,]\s*"(?:[^"\\]|\\.)*"$', t):
            t += ': null'
        else:
            break
    return t + ''.join(reversed(stack))


def parse_spec(text: str) -> dict | None:
    """Tolerant JSON read of a model reply: strips code fences and text around the outermost {...} or [...], removes
    trailing commas, closes brackets and quotes left open by a cut-off reply. A top-level list becomes
    {'sections': list}. None when no JSON object can be read. Raises SpecError('L6') for a reply over 2 MB."""
    if isinstance(text, (dict, list)):
        got = text
    else:
        text = '' if text is None else str(text)
        if len(text) > MAX_REPLY_BYTES or len(text.encode('utf-8', 'replace')) > MAX_REPLY_BYTES:
            raise SpecError('L6', f'The reply is {len(text.encode("utf-8", "replace")) / 1e6:.1f} MB; a file spec is '
                                  f'read only up to {MAX_REPLY_BYTES // 1_000_000} MB.')
        got = _parse_json_text(text)
    if isinstance(got, list):
        return {'sections': got}
    return got if isinstance(got, dict) else None


def _open_depth(t: str) -> int:
    """How many brackets are still open at the end of t (strings skipped)."""
    depth, in_str, esc = 0, False, False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in '{[':
            depth += 1
        elif ch in '}]' and depth:
            depth -= 1
    return depth


def _useful(got) -> bool:
    return isinstance(got, dict) or isinstance(got, list) and any(isinstance(x, (dict, str)) for x in got)


def _parse_json_text(text: str):
    t = text.strip().lstrip('\ufeff')
    fenced = _JSON_FENCE.search(t)
    candidates = [t] + ([fenced.group(1).strip()] if fenced else [])
    for c in candidates:
        try:
            return json.loads(c)
        except (ValueError, RecursionError):
            pass
    dec = json.JSONDecoder()
    for c in candidates:
        starts = [i for i in (c.find('{'), c.find('[')) if i >= 0]
        if not starts:
            continue
        body = c[min(starts):]
        # 1. the outermost object complete, with text after it
        try:
            got = dec.raw_decode(body)[0]
            if _useful(got):
                return got
        except (ValueError, RecursionError):
            pass
        # 2. the outermost object cut off (max_tokens): closed as a whole, so the title and every complete section
        # before the cut are kept (an inner object alone would lose them)
        end = max(body.rfind('}'), body.rfind(']'))
        for x in [body] + ([body[:end + 1]] if end > 0 else []):
            fixed = re.sub(r',(\s*[}\]])', r'\1', x)
            for y in (fixed, _close_json(fixed)):
                try:
                    got = json.loads(y, strict=False)
                except (ValueError, RecursionError):
                    continue
                if _useful(got):
                    return got
        # 3. a complete object after prose that has brackets of its own ("Sure {here}: {...}"); a hit nested inside
        # an unclosed bracket is part of a cut-off object, not the reply
        opens = [m.start() for m in list(re.finditer(r'[{\[]', c))[:50]]
        for i in opens:
            if _open_depth(c[:i]):
                continue
            try:
                got = dec.raw_decode(c[i:])[0]
            except (ValueError, RecursionError):
                continue
            if _useful(got):
                return got
        for i in opens:
            if _open_depth(c[:i]):
                continue
            fixed = re.sub(r',(\s*[}\]])', r'\1', c[i:])
            try:
                got = json.loads(_close_json(fixed), strict=False)
            except (ValueError, RecursionError):
                continue
            if _useful(got):
                return got
    return None


def normalize(spec: dict, fmt: str) -> tuple[dict, list[RuleResult]]:
    """The spec fixed for one format, plus one RuleResult per content and size rule. The spec is repaired first
    (S1, S7, S8, L1 are fixes); raises SpecError only for S2 (nothing to show), X1 (unknown format) or X6 when an
    image check itself fails."""
    if fmt not in FORMATS:
        raise SpecError('X1', f'Files are made as {", ".join(FORMATS)} only, not {str(fmt)[:12]!r}.')
    res = _fresh()
    rep = _repair(spec, res, keep_notes=fmt == 'pptx')
    out_sections = rep['sections']
    if not _shows(out_sections):
        raise SpecError('S2', 'The spec has no content: it needs at least one section with a heading or a '
                              'non-empty block.')
    for s in out_sections:
        if fmt != 'pptx':
            s['notes'] = ''
    _fix_levels(out_sections, res, fmt)
    title = rep['title']
    if not title:
        first = next((s['heading'] for s in out_sections if s['heading']), '')
        if not first:
            b = next(b for s in out_sections for b in s['blocks'] if b['type'] not in ('page_break', 'figure'))
            first = strip_emphasis(b.get('text') or ' '.join(b.get('items') or b.get('columns') or
                                                             [b.get('title') or '']))
            first = ' '.join(words(first)[:8])
        title = first or 'Document'
        _note(res, 'S3', 'fix', 'the file had no title; took it from the first heading')
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE].rsplit(' ', 1)[0].rstrip(' ,;:') or title[:MAX_TITLE]
        _note(res, 'S3', 'fix', f'title shortened to {MAX_TITLE} characters')
    theme = rep.get('theme')
    out = {'title': title, 'subtitle': rep['subtitle'][:300], 'format': fmt,
           'theme': theme if isinstance(theme, str) and theme in THEMES else 'clean',
           'paper': 'letter' if str(rep.get('paper') or '').lower() == 'letter' else 'a4', 'sections': out_sections}
    font = ' '.join(plain(_text(rep.get('font')), emphasis=False).lower().split())[:60]
    if font:
        out['font'] = font
    if rep.get('design') is not None:
        design = _clean_design(rep['design'])
        if design is not None:
            out['design'] = design
    if fmt == 'pptx':
        slides = []
        for s in out_sections:
            if not s['heading']:
                s['heading'] = slides[-1]['heading'].removesuffix(' (cont.)') + ' (cont.)' if slides else 'Overview'
                _note(res, 'S8', 'fix', 'slides without a heading continue the previous slide\'s heading')
            slides.extend(_pptx_slides(s, res))
        out['sections'] = slides
    else:
        named = 0
        for i, s in enumerate(out_sections):
            # S8: a section after the first always has a heading (the first may be the text under the title): its
            # first line of text when that is a short line of its own, else "Section N"
            if not s['heading'] and i and any(b['type'] not in ('page_break', 'figure') for b in s['blocks']):
                first = s['blocks'][0]
                line = strip_emphasis(first['text']) if first['type'] == 'paragraph' else ''
                if line and len(line) <= SHORT_HEADING and len(s['blocks']) > 1:
                    s['heading'], s['blocks'] = line, s['blocks'][1:]
                else:
                    s['heading'] = f'Section {i + 1}'
                named += 1
        if named:
            _note(res, 'S8', 'fix', f'{named} section{"" if named == 1 else "s"} without a heading '
                                    f'{"was" if named == 1 else "were"} given one')
        _limit_tables(out_sections, fmt, res)
    order = ['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'L1', 'L2', 'L3', 'S8'] + \
        [k for k in ('F5', 'A3', 'X6') if k in res]
    return out, [res[k] for k in order]


def _clean_design(d):
    """spec['design'] validated by the design module (create/design.py via themes.clean_design) when it exists."""
    from . import themes
    fn = getattr(themes, 'clean_design', None)
    if fn is None:
        return None
    try:
        return fn(d)
    except Exception:
        return None


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
