"""Signs that a finished run went wrong (docs/PLAN-accuracy-v2.md D6), stored as `rec.suspects` so real traffic can be
turned into eval cases. Pure: it reads the run record (and, when the caller has them, the stored specs of the files the
run made) and never touches the store or the network.
"""
import json
import re

from .config import AGENTS, GUARDS, REPORT, RESEARCH, ROOT, RUN, SQL_AGENT

CODES = ('pages_short', 'forced_non_file', 'reply_template_body', 'extra_format', 'unfulfilled', 'dup_clarify',
         'agent_label_leak', 'dead_end', 'cut_off')

# The @dead_end matcher (evals/matchers.json); this copy is used when the file is missing or has no such entry.
DEAD_END = 'No summary found|Not sure what you need|Tell me two'
CUT_OFF = re.compile(r'cut off|unavailable here|truncated', re.I)
REPLY_TEMPLATE = re.compile(r'^Created \*\*.+\*\*, \d|No format was named', re.M)
CREATED_REPLY = re.compile(r'^\s*Created \*\*.+?\*\*')
LABEL = re.compile(r'^\s*(?:[-*]\s+)?\*\*([a-z_]+)\*\*:', re.M)
FILE_AGENTS = {'create', 'document', 'data'}


def dead_end_rx() -> re.Pattern:
    try:
        rx = json.loads((ROOT / 'evals' / 'matchers.json').read_text()).get('dead_end') or DEAD_END
    except Exception:
        rx = DEAD_END
    try:
        return re.compile(rx)
    except re.error:
        return re.compile(DEAD_END)


def agent_names(rec: dict) -> set[str]:
    names = {*AGENTS, *RESEARCH, *REPORT, *RUN, *SQL_AGENT, *GUARDS, *FILE_AGENTS}
    return names | {t['agent'] for t in rec.get('tasks') or [] if isinstance(t.get('agent'), str)}


def spec_text(spec: dict) -> str:
    """The words of a stored spec: title, headings, notes and every block's text."""
    out = [str(spec.get('title') or ''), str(spec.get('subtitle') or '')]
    for s in spec.get('sections') or []:
        if not isinstance(s, dict):
            continue
        out += [str(s.get('heading') or ''), str(s.get('notes') or '')]
        for b in s.get('blocks') or []:
            if isinstance(b, dict):
                out += [str(b.get(k) or '') for k in ('text', 'code', 'title', 'caption')]
                out += [str(i) for i in b.get('items') or []]
    return '\n'.join(x for x in out if x)


def requested(rec: dict):
    """The brief of the run's query (docs/PLAN-accuracy-v2.md C1), or None when it can't be read."""
    try:
        from .create.brief import parse_brief
        return parse_brief(rec.get('text') or '')
    except Exception:
        return None


def query_format(rec: dict, brief) -> str | None:
    if brief is not None and getattr(brief, 'format', None):
        return brief.format
    try:
        from .create.spec import detect_format
        return detect_format(rec.get('text') or '')
    except Exception:
        return None


def file_brief(meta: dict, brief, fmt: str | None):
    """(pages, slides) targets for one file: its own stored brief, else the query's when the file is in the format the
    query asked for."""
    own = meta.get('brief')
    if isinstance(own, dict):
        return own.get('pages'), own.get('slides')
    if brief is not None and fmt and meta.get('format') == fmt:
        return getattr(brief, 'pages', None), getattr(brief, 'slides', None)
    return None, None


def suspects(rec: dict, specs: dict | None = None) -> list[dict]:
    """[{code, note}] for the run record `rec`; code is one of CODES. specs: {CreatedFile id: stored spec} for the files
    the run made, when the caller has them (the file-body checks then read the files themselves)."""
    specs = specs or {}
    tasks = [t for t in rec.get('tasks') or [] if isinstance(t, dict)]
    merged = rec.get('merged') or {}
    answer = str(merged.get('answer') or '')
    by_tid = {t.get('tid'): t for t in tasks}
    files = [(t, f) for t in tasks for f in t.get('created_files') or [] if isinstance(f, dict)]
    brief = requested(rec)
    fmt = query_format(rec, brief)
    out: list[dict] = []

    def flag(code: str, note: str):
        item = {'code': code, 'note': note}
        if item not in out:
            out.append(item)

    for _, f in files:
        pages, slides = file_brief(f, brief, fmt)
        for want, got, unit in ((pages, f.get('pages'), 'page'), (slides, f.get('slides'), 'slide')):
            if want and got is not None and got < want[0]:
                lo, hi = want[0], want[1]
                asked = f'{lo}' if lo == hi else f'{lo}-{hi}'
                flag('pages_short', f'**{f.get("name")}** has {got} {unit}{"" if got == 1 else "s"}; the request '
                                    f'asked for {asked}.')

    if len(tasks) > 1:
        from .agents.create import asks_for_file
        for t in tasks:
            if t.get('forced') and t.get('agent') == 'create' and not asks_for_file(str(t.get('text') or '')):
                flag('forced_non_file', f'Step {t.get("tid")} was made to create a file, but its text asks for none.')

    for t, f in files:
        spec = specs.get(f.get('id'))
        if isinstance(spec, dict):
            hit = REPLY_TEMPLATE.search(spec_text(spec))
        else:  # no spec to read: a file made from earlier answers that were only "Created ..." replies
            deps = [by_tid.get(d) for d in t.get('depends_on') or []]
            hit = (f.get('source') == 'answer' and deps and
                   all(d is not None and CREATED_REPLY.match(str(d.get('answer') or '')) for d in deps))
        if hit:
            flag('reply_template_body', f'**{f.get("name")}** contains a reply line ("Created ...") instead of '
                                        f'content.')

    if fmt:
        for _, f in files:
            if f.get('format') and f.get('format') != fmt:
                flag('extra_format', f'**{f.get("name")}** is {str(f["format"]).upper()}; the request asked for '
                                     f'{fmt.upper()}.')

    from .merger import HEDGES
    caveats = [c for c in [*(merged.get('caveats') or []), *(c for t in tasks for c in t.get('caveats') or [])]
               if c not in HEDGES]
    if caveats:
        flag('unfulfilled', f'The run could not do everything asked: {caveats[0]}' +
             (f' (and {len(caveats) - 1} more)' if len(caveats) > 1 else ''))

    guards: dict[str, int] = {}
    for t in tasks:
        if t.get('agent') in GUARDS and t.get('answer'):
            k = ' '.join(str(t['answer']).split()).lower()
            guards[k] = guards.get(k, 0) + 1
    if any(n > 1 for n in guards.values()):
        flag('dup_clarify', 'The same follow-up text was given for more than one step.')

    names = agent_names(rec)
    leaks = [m.group(1) for m in LABEL.finditer(answer) if m.group(1) in names]
    for f_id, spec in specs.items():
        if isinstance(spec, dict):
            leaks += [m.group(1) for m in LABEL.finditer(spec_text(spec)) if m.group(1) in names]
    if leaks:
        flag('agent_label_leak', f'An internal label (**{leaks[0]}**:) reached the answer or a file.')

    dead = dead_end_rx()
    texts = [answer, *(str(t.get('answer') or '') for t in tasks)]
    if hit := next((m for x in texts if (m := dead.search(x))), None):
        flag('dead_end', f'An answer is a dead end ("{hit.group(0)}").')
    if hit := next((m for x in texts if (m := CUT_OFF.search(x))), None):
        flag('cut_off', f'An answer says something was "{hit.group(0)}".')
    return out
