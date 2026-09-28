"""Uploaded files and the two agents that read them: `document` (passage search) and `data` (table statistics).

Both work keyless; with an engine they hand the engine the best passages or the stats plus a sample of rows, so its
answer stays grounded in the user's file rather than in whatever the model remembers.
"""
import csv
import io
import json
import math
import re
import time
import uuid
from collections import Counter

from .agents.tools import AgentResult

MAX_BYTES = 10 * 1024 * 1024
KINDS = {'.txt': 'text', '.md': 'text', '.csv': 'csv', '.json': 'json', '.pdf': 'pdf'}
FILE_AGENTS = {
    'document': 'Questions about the attached documents or files',
    'data': 'Statistics, totals, averages or analysis of an attached CSV/JSON table',
}


class FileError(ValueError):
    pass


def kind_of(name: str) -> str:
    ext = ('.' + name.rsplit('.', 1)[-1].lower()) if '.' in name else ''
    if ext not in KINDS:
        raise FileError(f'unsupported file type {ext or "(none)"}; use {" ".join(KINDS)}')
    return KINDS[ext]


def pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(raw))
        return '\n\n'.join((p.extract_text() or '').strip() for p in reader.pages).strip()
    except Exception as e:
        raise FileError(f'could not read the PDF: {str(e)[:120]}')


def extract(name: str, raw: bytes) -> tuple[dict, str]:
    """(metadata, extracted text). Blocking for PDFs, so callers run it in a thread."""
    kind = kind_of(name)
    text = pdf_text(raw) if kind == 'pdf' else raw.decode('utf-8', errors='replace')
    meta = {'id': uuid.uuid4().hex[:12], 'name': name, 'size': len(raw), 'kind': kind, 'chars': len(text),
            'created': time.time()}
    if kind == 'json':
        try:
            json.loads(text)
        except ValueError as e:
            raise FileError(f'invalid JSON: {e}')
    table = to_table(kind, text)
    if table:
        meta.update(rows=len(table[1]), columns=table[0])
    return meta, text


# ---------- tables ----------

def to_table(kind: str, text: str) -> tuple[list[str], list[list[str]]] | None:
    """(columns, rows) for a CSV, or a JSON list of objects (also one nested under a top-level key)."""
    if kind == 'csv':
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
        return (rows[0], rows[1:]) if rows else None
    if kind == 'json':
        try:
            d = json.loads(text)
        except ValueError:
            return None
        if isinstance(d, dict):
            d = next((v for v in d.values() if isinstance(v, list) and v and isinstance(v[0], dict)), None)
        if isinstance(d, list) and d and all(isinstance(x, dict) for x in d):
            cols = list(dict.fromkeys(k for x in d for k in x))
            return cols, [['' if x.get(c) is None else str(x.get(c)) for c in cols] for x in d]
    return None


def number(v: str) -> float | None:
    try:
        f = float(v.strip().replace(',', '').replace('$', '').replace('%', ''))
        return f if math.isfinite(f) else None
    except (ValueError, AttributeError):
        return None


def fmt(v: float) -> str:
    return f'{v:,.0f}' if float(v).is_integer() else f'{v:,.4g}' if abs(v) < 1000 else f'{v:,.2f}'


NA = {'', 'n/a', 'na', 'null', 'none', 'nan', '-', '?'}  # missing-value markers, not text
CELL_CHARS = 40  # longer text cells are free text (notes, comments): never repeated word for word keyless


def clip(v: str) -> str:
    v = ' '.join(v.split())
    return v if len(v) <= CELL_CHARS else v[:CELL_CHARS - 3].rstrip() + '...'


def table_stats(name: str, cols: list[str], rows: list[list[str]]) -> str:
    lines = [f'{name}: {len(rows):,} rows × {len(cols)} columns ({", ".join(cols)})']
    for i, c in enumerate(cols):
        vals = [r[i] for r in rows if i < len(r) and r[i].strip().lower() not in NA]
        nums = [n for n in map(number, vals) if n is not None]
        # A column counts as numeric when most filled cells parse; stray "n/a" cells shouldn't hide it.
        if nums and len(nums) >= 0.8 * len(vals):
            lines.append(f'- {c}: min {fmt(min(nums))}, mean {fmt(sum(nums) / len(nums))}, max {fmt(max(nums))}, '
                         f'sum {fmt(sum(nums))} ({len(nums)} values)')
        elif vals:
            top = Counter(vals).most_common(3)
            lines.append(f'- {c}: {len(set(vals))} distinct, most common ' + ', '.join(f'{clip(v)} ({n})' for v, n in top))
    return '\n'.join(lines)


# ---------- keyless table questions ----------
# One aggregate over one table, read from the question: a filter on cell values it names, an optional group (a column
# it names, or the month/year of a date column), a measure column and an operation. Anything it can't read returns
# None and the agent shows the column statistics instead, so a guess is never presented as the answer.

MONEY_COL = re.compile(r'amount|revenue|sales|income|turnover|value|price|cost|salary|salaries|pay|wage|total|spend|'
                       r'eur|usd|gbp|inr|jpy|dollar|euro', re.I)
QTY_COL = re.compile(r'qty|quantity|stock|units?|count|on_?hand|pieces', re.I)
MEASURE_WORDS = [  # question word -> the kind of column it means
    (r'sales?|revenue|income|turnover|earned|takings', 'revenue|sales|income|turnover|amount|value'),
    (r'paid|pay|salary|salaries|wages?|earns?|earning', 'salary|pay|wage|compensation|income'),
    (r'value|worth|amount|cost|costs|spend|spent|money|how much', MONEY_COL.pattern),
    (r'sold|stock|on hand|left|inventory|quantity', QTY_COL.pattern),
]
MAX_WORDS = r'highest|most|largest|biggest|best|top|maximum|max|greatest|the most'
MIN_WORDS = r'lowest|least|fewest|smallest|worst|minimum|min|cheapest'
EARLY_WORDS = r'longest|earliest|oldest|first|most senior'
LATE_WORDS = r'newest|latest|most recent|most junior|shortest'
BELOW = r'below|under|less than|lower than|beneath'
ABOVE = r'above|over|more than|greater than|higher than|exceeds?'
ISO_DATE = re.compile(r'^(\d{4})-(\d{2})(?:-\d{2})?')
MONTHS = 'January February March April May June July August September October November December'.split()
GENERIC = {'usd', 'eur', 'gbp', 'inr', 'jpy', 'id', 'no', 'num', 'number', 'the', 'of', 'per', 'in'}


def stem(w: str) -> str:
    w = w.lower()
    for suf, rep in (('ies', 'y'), ('ses', 's'), ('s', '')):
        if w.endswith(suf) and len(w) > len(suf) + 2:
            return w[:-len(suf)] + rep
    return w


def col_words(c: str) -> set[str]:
    return {stem(w) for w in re.findall(r'[a-z]+', c.lower()) if w not in GENERIC and len(w) > 2}


def has(pattern: str, text: str) -> bool:
    return bool(re.search(rf'\b(?:{pattern})\b', text, re.I))


def answer_table(q: str, cols: list[str], rows: list[list[str]]) -> str | None:
    """A one-line answer to an aggregate question about one table, or None when the question isn't one it can read."""
    ql = ' '.join(q.lower().split())
    qwords = {stem(w) for w in re.findall(r'[a-z]+', ql)}
    cell = lambda r, i: r[i].strip() if i < len(r) else ''
    filled = lambda i: [cell(r, i) for r in rows if cell(r, i).lower() not in NA]
    numeric, dates, text = [], [], []
    for i, c in enumerate(cols):
        vals = filled(i)
        if not vals:
            continue
        if sum(bool(ISO_DATE.match(v)) for v in vals) >= 0.8 * len(vals):
            dates.append(i)
        elif sum(number(v) is not None for v in vals) >= 0.8 * len(vals):
            (dates if re.search(r'year|date', c, re.I) else numeric).append(i)
        else:
            text.append(i)
    if not rows or not (numeric or dates):
        return None
    named = lambda i: bool(col_words(cols[i]) & qwords)

    # Filters: text cells the question names ("Engineering", "pending", "wooden pallets"). Short values only.
    filters: dict[int, dict[str, str]] = {}  # column -> {lowercased value: value as written in the table}
    used = set()
    for i in text:
        for v in set(filled(i)):
            if 2 <= len(v) <= CELL_CHARS and not number(v) and re.search(rf'\b{re.escape(v.lower())}(?:e?s)?\b', ql):
                filters.setdefault(i, {})[v.lower()] = v
                used |= {stem(w) for w in re.findall(r'[a-z]+', v.lower())}
    sel = [r for r in rows if all(cell(r, i).lower() in vs for i, vs in filters.items())]
    if filters and not sel:
        return None
    where = ' and '.join(f'{cols[i]} {" or ".join(sorted(vs.values()))}' for i, vs in filters.items())
    scope = f' where {where}' if where else ''
    having = f' have {where}' if where else ''
    rest = ql
    for vs in filters.values():
        for v in vs:
            rest = re.sub(rf'\b{re.escape(v)}(?:e?s)?\b', ' ', rest)

    def measure() -> int | None:
        lit = [i for i in numeric if named(i) and not (col_words(cols[i]) <= used)]
        if lit:
            return lit[0]
        for words, colpat in MEASURE_WORDS:
            if has(words, rest):
                hit = [i for i in numeric if re.search(colpat, cols[i], re.I)]
                if hit:
                    return hit[0]
        money = [i for i in numeric if MONEY_COL.search(cols[i])]
        return money[0] if len(money) == 1 and has(r'how much|total|sum|average|mean|value', rest) else None

    def label(r) -> str:
        """How to name one row: its identifying text cells (not free text), then the rest of the short cells."""
        ids = [i for i in text if len(set(filled(i))) == len(filled(i)) and max(map(len, filled(i))) <= CELL_CHARS]
        head = [cell(r, i) for i in ids[:2] if cell(r, i)]
        return ' '.join([head[0], f'({", ".join(head[1:])})'] if len(head) > 1 else head) or clip(', '.join(r))

    def row_text(r) -> str:
        return ', '.join(f'{cols[i]} {clip(cell(r, i))}' for i in range(len(cols))
                         if cell(r, i) and len(cell(r, i)) <= CELL_CHARS)

    m = measure()
    agg = 'mean' if has(r'average|mean|avg|typical', rest) else 'sum'
    agg_word = 'average' if agg == 'mean' else 'total'

    def total(rs, i):
        nums = [n for r in rs if (n := number(cell(r, i))) is not None]
        return (sum(nums) / len(nums) if agg == 'mean' else sum(nums)) if nums else None

    # Compare two columns of the same row: "items below their reorder level".
    cmp = re.search(rf'\b({BELOW}|{ABOVE})\s+(?:their|its|the)?\s*([a-z ]+)', rest)
    if cmp:
        other = [i for i in numeric if col_words(cols[i]) and col_words(cols[i]) <= {stem(w) for w in cmp.group(2).split()}]
        if other:
            j = other[0]
            base = [i for i in numeric if i != j and QTY_COL.search(cols[i])] or [i for i in numeric if i != j]
            if base:
                i = base[0]
                below = has(BELOW, cmp.group(1))
                hit = [r for r in sel if (a := number(cell(r, i))) is not None and (b := number(cell(r, j))) is not None
                       and (a < b if below else a > b)]
                side = 'below' if below else 'above'
                if not hit:
                    return f'No rows have {cols[i]} {side} {cols[j]}{scope}.'
                return (f'{len(hit)} of {len(sel)} rows have {cols[i]} {side} {cols[j]}{scope}: ' +
                        '; '.join(f'{label(r)}, {cols[i]} {fmt(number(cell(r, i)))} vs {fmt(number(cell(r, j)))}'
                                  for r in hit) + '.')

    def group_key():
        if dates and has(r'month|monthly', rest):
            return 'month', lambda r: (lambda d: f'{MONTHS[int(d.group(2)) - 1]} {d.group(1)}' if d else None)(
                ISO_DATE.match(cell(r, dates[0])))
        if dates and has(r'year|yearly|annual', rest):
            return 'year', lambda r: (lambda d: d.group(1) if d else None)(ISO_DATE.match(cell(r, dates[0])))
        g = [i for i in text if named(i) and i not in filters]
        if g:
            return cols[g[0]], lambda r, i=g[0]: cell(r, i) or None
        return None, None

    top = has(MAX_WORDS, rest) and not has(r'\bhow (much|many)\b', rest)
    low = has(MIN_WORDS, rest) and not has(r'\bhow (much|many)\b', rest)
    single = has(r'single|individual|one', rest)
    gname, gkey = (None, None) if single else group_key()
    if (top or low) and m is not None:
        if gkey:
            groups: dict[str, list] = {}
            for r in sel:
                if (k := gkey(r)) is not None:
                    groups.setdefault(k, []).append(r)
            vals = {k: v for k, rs in groups.items() if (v := total(rs, m)) is not None}
            if len(vals) < 2:
                return None
            order = sorted(vals, key=lambda k: vals[k], reverse=top)
            best = order[0]
            word = 'highest' if top else 'lowest'
            return (f'{best} has the {word} {agg_word} {cols[m]}{scope}: {fmt(vals[best])}. By {gname}: ' +
                    ', '.join(f'{k} {fmt(vals[k])}' for k in order) + '.')
        rs = [r for r in sel if number(cell(r, m)) is not None]
        if not rs:
            return None
        r = (max if top else min)(rs, key=lambda r: number(cell(r, m)))
        word = 'highest' if top else 'lowest'
        return f'The {word} {cols[m]}{scope} is {fmt(number(cell(r, m)))}, in the row {row_text(r)}.'
    if (has(EARLY_WORDS, rest) or has(LATE_WORDS, rest)) and dates and m is None:
        d = dates[0]
        rs = [r for r in sel if cell(r, d)]
        if not rs:
            return None
        iso = all(ISO_DATE.match(cell(r, d)) for r in rs)
        rs = rs if iso else [r for r in rs if number(cell(r, d)) is not None]
        if not rs:
            return None
        key = (lambda r: cell(r, d)) if iso else (lambda r: number(cell(r, d)))
        early = has(EARLY_WORDS, rest)
        r = (min if early else max)(rs, key=key)
        return f'The {"earliest" if early else "latest"} {cols[d]}{scope} is {cell(r, d)}, in the row {row_text(r)}.'
    count = re.search(r'\bhow many\s+(?:of\s+(?:the|these|those)\s+)?([a-z]+)', rest)
    if count:
        noun = stem(count.group(1))
        lit = [i for i in numeric if noun in col_words(cols[i])]
        qty = lit or ([i for i in numeric if QTY_COL.search(cols[i])] if has(r'in stock|on hand|left', rest) else [])
        if qty:
            v = sum(n for r in sel if (n := number(cell(r, qty[0]))) is not None)
            return f'The total {cols[qty[0]]}{scope} is {fmt(v)} (over {len(sel)} of {len(rows)} rows).'
        return f'{len(sel)} of {len(rows)} rows{having}.' if filters else f'The table has {len(rows)} rows.'
    if m is not None and (has(r'total|sum|how much|average|mean|avg|altogether|so far|in all', rest) or filters):
        v = total(sel, m)
        if v is None:
            return None
        return f'The {agg_word} {cols[m]}{scope} is {fmt(v)} (over {len(sel)} of {len(rows)} rows).'
    if filters and has(r'^(which|what|list|show)\b', rest.strip()):
        return f'{len(sel)} of {len(rows)} rows{having}: ' + '; '.join(label(r) for r in sel) + '.'
    return None


# ---------- passages ----------

STOP = set('a an and are as at be by did do does for from how i in is it its me of on or tell than that the their '
           'there these this to was were what when where which who why with you your about please file document'.split())


def words(text: str) -> list[str]:
    return [w for w in re.findall(r'[a-z0-9]+', text.lower()) if w not in STOP and len(w) > 1]


def chunks(text: str, size: int = 800) -> list[str]:
    """~size-char passages, cut at paragraph or sentence breaks so a passage reads as a unit."""
    pieces = [p.strip() for p in re.split(r'\n\s*\n|(?<=[.!?])\s+', text) if p.strip()]
    out, cur = [], ''
    for p in pieces:
        while len(p) > size:  # one huge unbroken block (a CSV, a minified JSON): hard-cut it
            if cur:
                out.append(cur)
                cur = ''
            out.append(p[:size])
            p = p[size:]
        if cur and len(cur) + len(p) + 1 > size:
            out.append(cur)
            cur = p
        else:
            cur = f'{cur} {p}'.strip()
    return out + ([cur] if cur else [])


def search(files: list[tuple[str, str]], query: str, k: int = 3) -> list[tuple[float, str, str]]:
    """BM25 over every file's passages: [(score, file name, passage)], best first, score > 0 only."""
    docs = [(name, c) for name, text in files for c in chunks(text)]
    toks = [words(c) for _, c in docs]
    if not docs:
        return []
    avg = sum(map(len, toks)) / len(toks) or 1
    df = Counter(w for t in toks for w in set(t))
    q = set(words(query))
    scored = []
    for (name, c), t in zip(docs, toks):
        tf = Counter(t)
        s = sum(math.log(1 + (len(docs) - df[w] + 0.5) / (df[w] + 0.5)) * tf[w] * 2.5 / (tf[w] + 1.5 * (0.25 + 0.75 * len(t) / avg))
                for w in q if tf[w])
        if s > 0:
            scored.append((s, name, c))
    return sorted(scored, key=lambda x: -x[0])[:k]


# ---------- agents ----------

DOC_SYSTEM = ('You are the document agent inside TraceGraph. Do not use tools. Answer the question using only the passages '
              'from the user\'s attached files below, citing the file name in brackets like [report.pdf] after each claim. '
              'If the passages do not contain the answer, say so plainly. Light Markdown, no preamble.')
DATA_SYSTEM = ('You are the data agent inside TraceGraph. Do not use tools. Answer the question about the user\'s attached '
               'table using the computed statistics (exact, over every row) and the sample rows. Prefer the computed '
               'numbers; say when a question needs rows beyond the sample. Light Markdown, no preamble.')


QUESTION = re.compile(r'\?|^\s*(who|whom|whose|what|when|where|which|why|how|is|are|was|were|can|could|do|does|did|'
                      r'should|will|would|has|have|may|must)\b', re.I)


def focus(q: str, passage: str) -> str:
    """For a question, the passage's lines and sentences that share a word with it, so a keyless answer quotes what
    answers the question rather than whatever else sits next to it (an injected instruction, say). A request that
    isn't a question ("summarize this") keeps the whole passage, as does a passage no sentence of which matches."""
    if not QUESTION.search(q):
        return passage
    qw = set(words(q))
    keep = [p.strip() for p in re.split(r'\n+|(?<=[.!?])\s+', passage) if p.strip() and qw & set(words(p))]
    return ' '.join(keep) or passage


def file_agents(files: list[dict], texts: dict[str, str], engine=None) -> dict:
    """Per-run runners for the attached files. files: metadata dicts; texts: id -> extracted text."""
    named = [(f['name'], texts[f['id']]) for f in files]

    async def document(q, emit_delta):
        if engine is None:
            hits = search(named, q, 3)
            if hits:
                text = '\n\n'.join(f'**{name}**: {focus(q, c)}' for _, name, c in hits)
            else:  # nothing overlaps the question ("summarize this"): the opening passage is the best keyless answer
                text = 'No passage matched the question closely. The start of each file:\n\n' + '\n\n'.join(
                    f'**{name}**: {(chunks(t) or [""])[0]}' for name, t in named[:3])
            emit_delta(text)
            return AgentResult(text, True, ', '.join(dict.fromkeys(h[1] for h in hits)) or named[0][0])
        hits = search(named, q, 6) or [(0, name, c) for name, t in named for c in chunks(t)[:2]][:6]
        prompt = f'Question: {q}\n\n' + '\n\n'.join(f'[{name}] passage {i}:\n{c}' for i, (_, name, c) in enumerate(hits, 1))
        reply = await engine.stream(system=DOC_SYSTEM, prompt=prompt, effort='medium', emit_delta=emit_delta)
        return AgentResult(reply.text, bool(reply.text), ', '.join(dict.fromkeys(h[1] for h in hits)), engine.name,
                           reply.input_tokens, reply.output_tokens)

    async def data(q, emit_delta):
        tables = [(f['name'], t) for f in files if (t := to_table(f['kind'], texts[f['id']]))]
        if not tables:
            text = 'None of the attached files is a table (CSV, or JSON list of objects).'
            emit_delta(text)
            return AgentResult(text, False)
        stats = '\n\n'.join(table_stats(name, cols, rows) for name, (cols, rows) in tables)
        if engine is None:
            answers = [(name, a) for name, (cols, rows) in tables if (a := answer_table(q, cols, rows))]
            text = stats
            if answers:
                lead = '\n\n'.join(a if len(tables) == 1 else f'**{name}**: {a}' for name, a in answers)
                text = f'{lead}\n\nColumn summary:\n{stats}'
            emit_delta(text)
            return AgentResult(text, True, ', '.join(n for n, _ in tables))
        sample = '\n\n'.join(f'{name} (first {min(30, len(rows))} rows):\n' + to_csv([cols, *rows[:30]])
                             for name, (cols, rows) in tables)
        prompt = f'Question: {q}\n\nComputed statistics:\n{stats}\n\n{sample}'
        reply = await engine.stream(system=DATA_SYSTEM, prompt=prompt, effort='medium', emit_delta=emit_delta)
        return AgentResult(reply.text, bool(reply.text), ', '.join(n for n, _ in tables), engine.name,
                           reply.input_tokens, reply.output_tokens)

    return {'document': document, 'data': data}


def to_csv(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\n').writerows(rows)
    return buf.getvalue().strip()
