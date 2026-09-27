"""The create agent (docs/PLAN-files.md): a request becomes one DocSpec, and code turns the spec into a checked file.

Zero-token paths come first, and work keyless: the chat's last created file re-rendered in another format ("now as
slides"), an earlier step's answer or the previous answer in the chat parsed from Markdown ("put that in a PDF"), an
attached table ("turn this CSV into a spreadsheet"). Only when none of them fits is the engine asked, once, for a spec
under DOCSPEC_SCHEMA with a trimmed context and a per-format token cap. Every spec passes Jev's safety check (X4), then
normalize -> render -> verify (docs/RULES-files.md); a block rule means an honest answer and no file.
"""
import asyncio
import csv
import io
import re
import time
import uuid
from dataclasses import dataclass, field

from .. import create as cf
from ..config import (BLOCK_AT, CREATE_CONTEXT_CHARS, CREATE_MAX_TOKENS, CREATE_SAFETY_CHARS, CREATE_SAFETY_CHUNKS,
                      CREATE_TABLE_ROWS)
from ..engines import EngineError, EngineRefusal, parse_json
from ..jev import unsafe_score
from ..planner import FILE_FORMATS, is_file_request

LABELS = {'pdf': 'PDF', 'docx': 'Word', 'pptx': 'PowerPoint', 'xlsx': 'Excel', 'md': 'Markdown'}
TURNS = 3  # earlier turns the spec call may see
LOOKBACK = 10  # finished turns of a chat searched for its last created file and the answer to put in a file

# "now as slides", "in Word please", "and as a spreadsheet": the request is only a format.
LEAD_FORMAT = re.compile(r'^\s*(?:(?:ok(?:ay)?|now|and|also|then|great|thanks|thank you|please|same|same thing|do it|'
                         r'can you do it|could you do it)[\s,!.]+)*(?:as|in|into|to)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?'
                         rf'(?:{FILE_FORMATS})\b', re.I)
CONVERT = re.compile(r'\bconvert\b|\bre-?(?:make|render|export|do)\b|\binstead\b|\bas\s+well\b|\b(?:another|other|'
                     r'different)\s+format\b|\b(?:same|this|that|the)\s+(?:thing|one)\b', re.I)
# "the pdf", "that deck", "the file": the request names a file made earlier.
FILE_REF = re.compile(r'\b(?:the|that|this|your|my)\s+(?:file|document|doc|pdf|deck|slides?|presentation|spreadsheet|'
                      r'workbook|sheet|excel|word\s+(?:doc|document|file)|markdown)\b', re.I)
# "notes of our conversation", "summarise this chat": the file is about every earlier turn, not only the last one.
CONVERSATION = re.compile(r'\b(?:our|this|the|whole|entire|full)\s+(?:chat|conversation|discussion|thread)\b', re.I)
REFERS = re.compile(r'\b(?:that|this|it|these|those|above|previous|last|earlier|same|the\s+(?:answer|reply|response|'
                    r'result|results|summary|explanation|list|table)|your\s+(?:answer|reply|response)|what\s+you\s+'
                    r'(?:said|wrote|found)|our\s+(?:chat|conversation|discussion))\b', re.I)
# Asks for new writing, not a copy of what is already there: a table plus one of these goes to the engine.
GENERATIVE = re.compile(r'\b(?:summari[sz]e|summary|analy[sz]e|analysis|insights?|explain|write\s+(?:up|about)|'
                        r'research|recommend|interpret|findings|trends?)\b', re.I)
DEEP = re.compile(r'\b(?:research|in[- ]depth|detailed|comprehensive|thorough|deep[- ]dive|long|full)\b', re.I)
THEME = {'dark': re.compile(r'\bdark\s+(?:theme|mode|style|background|slides|colou?rs?)\b|\bin\s+dark\b', re.I),
         'warm': re.compile(r'\bwarm\s+(?:theme|style|colou?rs?|tones?)\b', re.I)}
FILLER = set("""a an the it that this these those me us my our your of in into as to on for from with and or please now can
could would you i we want need make create put turn generate export save write build produce give prepare draft
compile format send package file files document doc version copy nice short simple quick clean new proper full out
pdf docx word slide slides deck presentation pptx powerpoint power point spreadsheet excel xlsx workbook sheet
markdown md report also then just some one here there about regarding using based too well get like have another
instead thanks thank ok okay let lets""".split())
LEAD_ASK = re.compile(r"^(?:what(?:'s| is| are| was| were)?|who(?:'s| is| was| were)?|how (?:do|does|did|to|can) (?:i|you|we)?|"
                      r'tell me (?:about)?|explain|describe|research|find(?: out)?|look up|give me|show me|list|'
                      r'summari[sz]e|compare|why (?:is|are|do|does))\s+', re.I)

SYSTEM = ('Do not use tools. You write the content of one downloadable file as JSON matching the schema: a title, a '
          'subtitle (may be empty) and sections, each with a heading, a level (1 to 3), blocks (paragraph, bullets, '
          'table, chart, quote, code) and notes (may be empty). Write content only: no styling, fonts, colours, layout, '
          'HTML or Markdown syntax. Use only facts from the request, the context and the attached data given here, and '
          'never ask for anything to be fetched. Numbers in tables and charts are numbers. {shape}')
SHAPES = {
    'pptx': 'It becomes slides: 4 to 10 sections, one per slide, each a heading and at most 5 bullets of at most 12 '
            'words, or one small table or chart; put anything longer in notes.',
    'xlsx': 'It becomes a spreadsheet: put the content in tables (one per section), with at most a one-line paragraph '
            'beside each.',
    'default': 'It becomes a short document: a clear title, 2 to 8 sections of short paragraphs and bullets, and a '
               'table or chart only where the data calls for one.',
}


@dataclass
class Job:
    request: str
    deps: list[tuple[str, str]] = field(default_factory=list)       # (step text, answer) of the steps this one uses
    context: list[dict] = field(default_factory=list)               # earlier turns [{query, answer}], oldest first
    tables: list[tuple[dict, list, list]] = field(default_factory=list)  # attached tables: (file meta, columns, rows)
    docs: list[tuple[dict, str]] = field(default_factory=list)      # other attached files: (meta, extracted text)
    last_file: tuple[dict, dict, bool] | None = None                # newest file made in this chat: (meta, spec, made
                                                                    # by its latest turn)


@dataclass
class Made:
    answer: str
    ok: bool
    engine: str = 'keyless'
    llm_in: int = 0
    llm_out: int = 0
    jev_tokens: int = 0
    file: dict | None = None   # the CreatedFile (web/src/protocol.ts)
    spec: dict | None = None   # the spec it was rendered from (stored, so a conversion costs no tokens)
    data: bytes | None = None
    effort: str | None = None


# ---------- building a file (also used by POST /api/created/{id}/convert) ----------


def shape_of(meta: dict) -> str:
    n = meta.get('pages') or meta.get('slides') or len(meta.get('sheets') or [])
    what = 'page' if meta.get('pages') else 'slide' if meta.get('slides') else 'sheet' if meta.get('sheets') else None
    if what and n:
        return f'{n} {what}{"" if n == 1 else "s"}'
    return f'{max(1, round(meta["size"] / 1024))} KB'


def build(spec: dict, fmt: str, *, source: str, tokens: int = 0, qid: int | None = None, from_id: str | None = None,
          extra: list | tuple = ()) -> tuple[dict, dict, bytes]:
    """(CreatedFile, the spec to store, the bytes). Blocking (rendering is CPU work), so callers use a thread. Raises
    SpecError when a block rule fails."""
    norm, results = cf.normalize(spec, fmt)
    data = cf.render(spec, fmt)
    checks = cf.verify(spec, fmt, data)
    try:
        pv = cf.preview(fmt, data)
    except Exception:
        pv = {}
    meta = {'id': uuid.uuid4().hex[:12], 'name': cf.file_name(norm['title'], fmt), 'format': fmt, 'size': len(data),
            'created': time.time(), 'qid': qid, 'title': norm['title'], 'pages': pv.get('pages'),
            'slides': pv.get('slides'), 'sheets': [s['name'] for s in pv['sheets']] if pv.get('kind') == 'sheets' else None,
            'tokens': int(tokens), 'source': source, 'from_id': from_id,
            'rules': [r.to_dict() for r in [*results, *extra, *checks]], 'sandbox': None}
    return meta, spec, data


def answer_for(meta: dict, notes: list[str]) -> str:
    warn = [r for r in meta['rules'] if r['severity'] == 'warn' and not r['ok']]
    lines = [f'Created **{meta["name"]}**, {shape_of(meta)}']
    if warn:
        lines.append(f'{len(warn)} check{"" if len(warn) == 1 else "s"} to look at: ' +
                     '; '.join(f'{r["id"]} ({r["note"]})' if r['note'] else r['id'] for r in warn) + '.')
    return '\n\n'.join([lines[0], *notes, *lines[1:]])


# ---------- reading the request ----------


def answered(rec: dict) -> bool:
    """A finished turn with a real answer: some step answered, not only a follow-up question, a block or a failure."""
    return rec.get('status') == 'done' and any(t.get('ok') and t.get('agent') not in ('clarify', 'blocked')
                                               for t in rec.get('tasks') or [])


def only_created(rec: dict) -> bool:
    """A turn whose only answered steps made files: its reply names the file and is no content to build on."""
    done = [t for t in rec.get('tasks') or [] if t.get('ok') and t.get('agent') not in ('clarify', 'blocked')]
    return bool(done) and all(t.get('agent') == 'create' for t in done)


def asks_for_file(text: str) -> bool:
    """A file request ("put that in a PDF") or only a format after one ("now as slides")."""
    return is_file_request(text) or bool(LEAD_FORMAT.match(text))


def refers_back(text: str) -> bool:
    return bool(REFERS.search(text))


def has_topic(text: str) -> bool:
    """True when the request names what the file is about ("slides on solar power"), not only a format."""
    return any(w not in FILLER and len(w) > 1 for w in re.findall(r'[a-z0-9]+', text.lower()))


def theme_of(text: str) -> str | None:
    return next((name for name, rx in THEME.items() if rx.search(text)), None)


def topic_title(text: str) -> str:
    """A title from a question or step: "What is a black hole?" -> "Black hole"."""
    t = re.sub(r'\s+', ' ', str(text or '')).strip().rstrip('?!. ')
    for _ in range(2):
        t = LEAD_ASK.sub('', t).strip()
    t = re.sub(r'^(?:the|a|an)\s+', '', t, flags=re.I)
    t = t[:100].rsplit(' ', 1)[0] if len(t) > 100 else t
    return t[:1].upper() + t[1:] if t else ''


def default_format(text: str, spec: dict) -> str:
    """No format named: slides were caught by detect_format; a report is a PDF, content that is all tables is a
    spreadsheet, anything else Markdown."""
    if re.search(r'\breports?\b', text, re.I):
        return 'pdf'
    blocks = [b for s in spec.get('sections') or [] if isinstance(s, dict) for b in s.get('blocks') or []
              if isinstance(b, dict)]
    if blocks and any(b.get('type') == 'table' for b in blocks) and all(b.get('type') in ('table', 'chart') for b in blocks):
        return 'xlsx'
    return 'md'


def spec_text(spec: dict) -> str:
    """The words of a spec, for Jev's safety check (X4): titles, headings, text, bullets, notes, code, chart labels and
    series names, and every table cell that is text (each distinct one once; numbers carry no words). Call it on the
    normalized spec, so the check reads exactly what the file will hold (entities decoded, markup removed)."""
    out, seen = [str(spec.get('title') or ''), str(spec.get('subtitle') or '')], set()
    for s in spec.get('sections') or []:
        if not isinstance(s, dict):
            continue
        out += [str(s.get('heading') or ''), str(s.get('notes') or '')]
        for b in s.get('blocks') or []:
            if not isinstance(b, dict):
                continue
            out += [str(b.get(k) or '') for k in ('text', 'title', 'by')]
            out += [str(x) for x in b.get('items') or []] + [str(x) for x in b.get('columns') or []]
            out += [str(x) for x in b.get('labels') or []]
            out += [str(x.get('name') or '') for x in b.get('series') or [] if isinstance(x, dict)]
            for r in b.get('rows') or []:
                cells = [v.get('formula', '') if isinstance(v, dict) else v for v in (r if isinstance(r, list) else [r])]
                words = [str(v) for v in cells if isinstance(v, str) and v.strip() and v not in seen]
                seen.update(words)
                if words:
                    out.append(' '.join(words))
    return '\n'.join(x for x in out if x.strip())


def chunks(text: str, size: int = CREATE_SAFETY_CHARS) -> list[str]:
    """The text in pieces of at most `size` characters, cut at line ends where it can be."""
    out, cur = [], ''
    for line in text.split('\n'):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ''
            out.append(line[:size])
            line = line[size:]
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f'{cur}\n{line}' if cur else line
    if cur or not out:
        out.append(cur)
    return out


def csv_rows(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\n').writerows(rows)
    return buf.getvalue().strip()


def context_text(job: Job, budget: int = CREATE_CONTEXT_CHARS) -> str:
    """What the spec call sees besides the request, at most `budget` characters: the earlier steps' answers, attached
    tables as columns plus the first CREATE_TABLE_ROWS rows, attached documents, then earlier turns, newest first."""
    parts = [f'Earlier step "{t[:200]}":\n{a}' for t, a in job.deps]
    parts += [f'Attached table {m["name"]}: {len(rows):,} rows; columns: {", ".join(map(str, cols))}\n'
              f'First {min(CREATE_TABLE_ROWS, len(rows))} rows:\n{csv_rows([cols, *rows[:CREATE_TABLE_ROWS]])}'[:1500]
              for m, cols, rows in job.tables]
    parts += [f'Attached file {m["name"]}:\n{text}' for m, text in job.docs]
    parts += [f'Earlier turn:\nQ: {t["query"]}\nA: {t["answer"] or "(a file was made)"}'
              for t in reversed(job.context[-TURNS:])]
    out, left = [], budget
    for p in parts:
        if left < 200:
            break
        out.append(p if len(p) <= left else p[:left - 3].rstrip() + '...')
        left -= len(out[-1]) + 2
    return '\n\n'.join(out)


# ---------- the agent ----------


async def make(job: Job, engine=None, jev=None, mode: str = 'balanced') -> Made:
    """The file for one create step. engine None is keyless: only the zero-token paths can make a file."""
    req = job.request.strip()
    fmt = cf.detect_format(req)
    theme = theme_of(req)
    last = job.last_file
    last_turn_file = bool(last) and last[2]
    attached = bool(job.tables or job.docs)
    # the file is about the whole chat ("notes of our conversation"): every earlier answer, not only the last one
    conversation = bool(CONVERSATION.search(req)) and any(t.get('answer', '').strip() for t in job.context)
    # the chat's last file in another format: right after it was made ("now as slides", "put that in Word", "I also
    # want an Excel spreadsheet"), or when the request names it as what to convert ("turn the pdf into slides"). New
    # material wins: an earlier step of this run (its answer is what the file is for) or an attached file.
    names_file = FILE_REF.search(req) and (CONVERT.search(req) or re.search(r'\b(?:into|to|as)\b', req, re.I))
    other_format = bool(last) and fmt is not None and fmt != last[0]['format'] and not has_topic(req)
    wants_file = bool(last) and not job.deps and not attached and not conversation and (
        (last_turn_file and (LEAD_FORMAT.match(req) or CONVERT.search(req) or refers_back(req) or other_format))
        or names_file)
    if wants_file:
        return await convert_last(last, fmt, theme)
    # the latest earlier turn that answered something (a turn that only made a file has no answer of its own here)
    prev = next((t for t in reversed(job.context) if (t.get('answer') or '').strip()), None)
    notes, source, spec = [], None, None
    if job.deps:
        spec, source = from_answers(job.deps), 'answer'
        if spec is None:
            return Made('No file was made: the step it was to be made from has no answer to put in it.', False)
    elif conversation and not attached:
        spec, source = conversation_spec(job.context), 'answer'
    elif job.tables and (engine is None or not GENERATIVE.search(req)):
        spec, source = from_tables(job.tables), 'table'
        if engine is None and GENERATIVE.search(req):
            notes.append('Keyless mode made it straight from the table; writing an analysis of it needs an engine.')
    elif not attached and prev is not None and (refers_back(req) or not has_topic(req)):
        spec, source = answer_spec(prev['answer'], topic_title(prev['query'])), 'answer'
    elif refers_back(req) and not attached and not has_topic(req):
        return Made('There is no earlier answer in this chat to put in a file. Ask the question first, or say what the '
                    'file should contain.', False)
    elif engine is None:
        return Made('I can make a PDF, Word, PowerPoint, Excel or Markdown file from an earlier answer in this chat or '
                    'from an attached table with no model at all, but there is nothing like that to use here, and '
                    'writing new content needs an LLM engine. Ask a question first and then say "put that in a PDF", '
                    'attach a CSV or JSON table, or choose an engine.', False)
    if spec is None:
        return await from_engine(job, engine, jev, fmt, theme, mode)
    if fmt is None:
        fmt = default_format(req, spec)
        notes.append(f'No format was named, so this is {LABELS[fmt]}. Convert it to '
                     f'{", ".join(LABELS[f] for f in cf.FORMATS if f != fmt)} from the file card at no cost.')
    if theme:
        spec['theme'] = theme
    return await finish(spec, fmt, jev, source=source, notes=notes)


def from_answers(deps: list[tuple[str, str]]) -> dict | None:
    """The spec of the earlier steps' answers (zero tokens). With several, each answer is a section under its step."""
    deps = [(t, a) for t, a in deps if a.strip()]
    if not deps:
        return None
    if len(deps) == 1:
        return answer_spec(deps[0][1], topic_title(deps[0][0]))
    return answer_spec('\n\n'.join(f'## {topic_title(t) or t}\n\n{a}' for t, a in deps), 'Results')


def conversation_spec(context: list[dict]) -> dict:
    """The spec of every answered turn of the chat (zero tokens): one section per turn, headed by its question."""
    turns = [(t['query'], t['answer']) for t in context if (t.get('answer') or '').strip()]
    if len(turns) == 1:
        return answer_spec(turns[0][1], topic_title(turns[0][0]))
    return answer_spec('\n\n'.join(f'## {topic_title(q) or q}\n\n{a}' for q, a in turns), 'Conversation notes')


H1 = re.compile(r'^\s{0,3}#\s+\S', re.M)


def answer_spec(answer: str, title: str) -> dict:
    """An answer's spec (zero tokens), titled by its one "# " heading when it has exactly one, else by `title` (from
    the question it answered), so a first "## Work" heading doesn't become the file's title."""
    if len(H1.findall(answer)) == 1:
        spec = cf.from_markdown(answer)
    else:
        spec = cf.from_markdown(answer, title=title or None)
    spec['title'] = spec.get('title') or title or 'Document'
    return spec


def from_tables(tables: list[tuple[dict, list, list]]) -> dict:
    """The spec of the attached tables (zero tokens): one table section each, with a chart where one fits."""
    specs = [cf.from_table(m, rows, cols) for m, cols, rows in tables]
    spec = specs[0]
    for s in specs[1:]:
        spec['sections'] += s['sections']
    if len(specs) > 1:
        spec['title'], spec['subtitle'] = 'Attached tables', ''
    return spec


async def convert_last(last, fmt: str | None, theme: str | None) -> Made:
    meta, spec, _ = last
    if fmt is None:
        return Made(f'Which format should **{meta["name"]}** become: PDF, Word, PowerPoint, Excel or Markdown?', False)
    if fmt == meta['format'] and (not theme or theme == spec.get('theme')):
        return Made(f'**{meta["name"]}** is already a {LABELS[fmt]} file.', True, file=meta)
    if theme:
        spec = {**spec, 'theme': theme}
    # The stored spec passed the safety check when it was made; converting it asks no one (0 tokens, rule L5).
    return await finish(spec, fmt, None, source='convert', from_id=meta['id'], extra=carried(meta),
                        notes=[f'Converted from **{meta["name"]}** with no model tokens.'])


def carried(meta: dict) -> list:
    """The source file's X4 result, which a conversion keeps: the content it checked is the same."""
    return [cf.RuleResult(**{k: r[k] for k in ('id', 'severity', 'ok', 'note')}) for r in meta.get('rules') or []
            if r.get('id') == 'X4']


async def from_engine(job: Job, engine, jev, fmt: str | None, theme: str | None, mode: str) -> Made:
    """One engine call for the spec, with the trimmed context and the format's token cap."""
    req = job.request.strip()
    shape = SHAPES.get(fmt, SHAPES['default'])
    effort = 'high' if DEEP.search(req) and mode != 'quick' else 'low'
    ctx = context_text(job)
    prompt = f'Request: {req}\nFormat: {LABELS.get(fmt, "not named")}' + (f'\n\nContext:\n{ctx}' if ctx else '')
    try:
        reply = await engine.stream(system=SYSTEM.format(shape=shape), prompt=prompt, effort=effort,
                                    max_tokens=CREATE_MAX_TOKENS[fmt or 'md'], schema=cf.DOCSPEC_SCHEMA)
    except EngineRefusal as e:
        r = e.reply
        return Made(f'{engine.label} declined to write this file.', False, engine.name, r.input_tokens, r.output_tokens,
                    effort=effort)
    except EngineError as e:
        return Made(f'The create agent needs {engine.label} to write new content, which failed ({e.why}).', False,
                    engine.name, effort=effort)
    used = reply.engine or engine.name
    tin, tout = reply.input_tokens, reply.output_tokens
    try:
        spec = parse_json(reply.text)
        if not isinstance(spec, dict):
            raise ValueError('not an object')
    except ValueError:
        return Made('No file was made. Rule S1 blocked it: the model\'s reply was not a file spec.', False, used, tin,
                    tout, effort=effort)
    notes = []
    if fmt is None:
        fmt = default_format(req, spec)
        notes.append(f'No format was named, so this is {LABELS[fmt]}. Convert it to '
                     f'{", ".join(LABELS[f] for f in cf.FORMATS if f != fmt)} from the file card at no cost.')
    if theme:
        spec['theme'] = theme
    out = await finish(spec, fmt, jev, source='llm', tokens=tin + tout, notes=notes)
    out.engine, out.llm_in, out.llm_out, out.effort = used, tin, tout, effort
    return out


async def finish(spec: dict, fmt: str, jev, *, source: str, tokens: int = 0, from_id: str | None = None,
                 notes: list[str] = (), extra: list = ()) -> Made:
    """X4 (unless this is a conversion of a spec that passed it: jev None), then normalize -> render -> verify."""
    extra, jev_tokens = list(extra), 0
    if jev is not None:
        # X4 reads the text the file will hold: the normalized spec, where "&#115;..." is already "s...", all of it
        try:
            norm, _ = cf.normalize(spec, fmt)
        except cf.SpecError as e:
            return Made(f'No file was made. Rule {e.rule_id} blocked it: {e.message}', False)
        pieces = chunks(spec_text(norm)) or ['(empty)']
        if len(pieces) > CREATE_SAFETY_CHUNKS:
            return Made(f'No file was made. Rule X4 blocked it: the file would hold more text than the safety check '
                        f'reads ({len(pieces) * CREATE_SAFETY_CHARS // 1000:,}K characters; at most '
                        f'{CREATE_SAFETY_CHUNKS * CREATE_SAFETY_CHARS // 1000:,}K).', False)
        gate = asyncio.Semaphore(8)

        async def check(piece):
            async with gate:
                return await unsafe_score(jev, piece or '(empty)')
        try:
            scored = await asyncio.gather(*(check(p) for p in pieces))
        except Exception as e:
            return Made(f'No file was made: the safety check (rule X4) could not run ({str(e)[:120]}).', False)
        score, jev_tokens = max(sc for sc, _ in scored), sum(n for _, n in scored)
        if score >= BLOCK_AT:
            return Made(f'No file was made. Rule X4 blocked it: Jev flagged the content as unsafe ({score:.0%}).', False,
                        jev_tokens=jev_tokens)
        extra.append(cf.RuleResult('X4', 'block', True, f'Jev unsafe score {score:.0%}' +
                                   (f' (highest of {len(pieces)} parts)' if len(pieces) > 1 else '')))
    try:
        meta, spec, data = await asyncio.to_thread(build, spec, fmt, source=source, tokens=tokens, from_id=from_id,
                                                   extra=extra)
    except cf.SpecError as e:
        return Made(f'No file was made. Rule {e.rule_id} blocked it: {e.message}', False, jev_tokens=jev_tokens)
    return Made(answer_for(meta, list(notes)), True, jev_tokens=jev_tokens, file=meta, spec=spec, data=data)
