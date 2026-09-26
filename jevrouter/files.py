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
            lines.append(f'- {c}: {len(set(vals))} distinct, most common ' + ', '.join(f'{v} ({n})' for v, n in top))
    return '\n'.join(lines)


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


def file_agents(files: list[dict], texts: dict[str, str], engine=None) -> dict:
    """Per-run runners for the attached files. files: metadata dicts; texts: id -> extracted text."""
    named = [(f['name'], texts[f['id']]) for f in files]

    async def document(q, emit_delta):
        if engine is None:
            hits = search(named, q, 3)
            if hits:
                text = '\n\n'.join(f'**{name}**: {c}' for _, name, c in hits)
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
            emit_delta(stats)
            return AgentResult(stats, True, ', '.join(n for n, _ in tables))
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
