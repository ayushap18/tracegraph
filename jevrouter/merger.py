"""merge(query, results) -> one answer. One subtask passes through; several are combined by the LLM engine or, when an LLM
adds nothing (every answer is exact, docs/PLAN-speed-evals-chat.md A2), joined by a template. compose(query, steps) is the
same with each step's files, caveats and assumption (docs/PLAN-accuracy-v2.md B2): it never prints agent names, leads
with the file the user asked for, and ends with what the run could not do."""
import re
from typing import NotRequired, TypedDict

SYSTEM = ('Do not use tools. You combine answers from specialist agents into one short reply to the user. Keep every number, unit and '
          'source exactly as given, mention failures plainly, add nothing new. Under 120 words, no preamble.')

# Answer styles (chat variety): an instruction for the LLM merger and for a lone LLM agent. The template merger applies
# what a template can: bullets, a table for multi-part answers, concise.
STYLES = {
    'default': '',
    'concise': 'Answer style: as short as possible, one or two sentences.',
    'detailed': 'Answer style: detailed, with the context and reasoning behind the answer.',
    'bullets': 'Answer style: a short bulleted list.',
    'steps': 'Answer style: numbered steps, one action per step.',
    'simple': 'Answer style: explain it simply for someone new to the topic, with no jargon.',
    'table': 'Answer style: a Markdown table where the answer has several parts or values.',
}


def concat(results: list[tuple[str, str]]) -> str:
    return '\n\n'.join(f'**{agent}**: {answer}' for agent, answer in results)


def first_line(text: str) -> str:
    return next((l.strip() for l in text.splitlines() if l.strip()), text.strip())


def cell(text: str) -> str:
    return ' '.join(l.strip() for l in text.splitlines() if l.strip()).replace('|', '\\|')


def template(results: list[tuple[str, str]], style: str = 'default', steps: list[str] | None = None) -> str:
    """The answers joined without an LLM, in the answer style where a template can follow it."""
    if style == 'concise':
        results = [(a, first_line(t)) for a, t in results]
    if style == 'bullets':
        return '\n'.join(f'- **{a}**: ' + '\n  '.join(l.strip() for l in t.splitlines() if l.strip()) for a, t in results)
    if style == 'table' and len(results) > 1:
        labels = steps if steps and len(steps) == len(results) else [a for a, _ in results]
        rows = [f'| {cell(label)} | {cell(t)} |' for label, (_, t) in zip(labels, results)]
        return '\n'.join(['| Question | Answer |', '| --- | --- |', *rows])
    return concat(results)


def styled_single(answer: str, style: str) -> str:
    """One exact answer (keyless or a guard) in the answer style, where a template can follow it."""
    if style == 'concise':
        return first_line(answer)
    if style == 'bullets':
        lines = [l.strip() for l in answer.splitlines() if l.strip()]
        return '\n'.join(f'- {l}' for l in lines) if len(lines) > 1 else answer
    return answer


def system_for(style: str) -> str:
    note = STYLES.get(style) or ''
    base = SYSTEM.replace('Under 120 words', 'Under 300 words') if style == 'detailed' else SYSTEM
    return f'{base} {note}' if note else base


async def merge(query: str, results: list[tuple[str, str]], emit_delta, engine=None, style: str = 'default',
                steps: list[str] | None = None, exact: bool = False) -> dict:
    """results: [(agent, answer)] in subtask order; steps: their subtask texts (table rows). Returns {answer, engine,
    claude_in, claude_out, kind}; kind is the RunTimings merger ('single', 'template' or 'llm'). With engine None the
    answers are joined by the template. exact: every answer is exact (keyless or a guard), so a lone one may be styled."""
    if len(results) == 1:
        answer = styled_single(results[0][1], style) if exact else results[0][1]
        return {'answer': answer, 'engine': 'single', 'claude_in': 0, 'claude_out': 0, 'kind': 'single'}
    if engine is not None:
        prompt = f'User query: {query}\n\n' + '\n\n'.join(f'[{a} agent]\n{t}' for a, t in results)
        sent = []
        track = lambda text: (sent.append(text), emit_delta(text))
        why = 'returned nothing'
        try:
            r = await engine.stream(system=system_for(style), prompt=prompt, effort='low', emit_delta=track, max_tokens=1024)
            if r.text:
                return {'answer': r.text, 'engine': engine.name, 'claude_in': r.input_tokens, 'claude_out': r.output_tokens,
                        'kind': 'llm'}
        except Exception as e:
            why = getattr(e, 'why', None) or type(e).__name__
        if any(sent):  # the merge stream is append-only: mark where the partial Claude text stops
            emit_delta(f'\n[{engine.label} merge failed ({why}); concatenated]\n')
    answer = template(results, style, steps)
    emit_delta(answer)
    return {'answer': answer, 'engine': 'concat', 'claude_in': 0, 'claude_out': 0, 'kind': 'llm' if engine else 'template'}



# ---------- compose (docs/PLAN-accuracy-v2.md B2) ----------

# B6 honesty wording, set by the decision policy (jevrouter/policy.py) and printed here.
ADVICE_NEEDS_ENGINE = 'This needs an engine to answer well: it is advice, not a lookup.'
TIME_SENSITIVE_HEDGE = 'Figures like these change every year; check the official source.'
# Caveats that qualify an answer rather than report a miss: printed at the end of their step's part, never under
# "What I couldn't do" and never returned as caveats.
HEDGES = (TIME_SENSITIVE_HEDGE,)

COULDNT = "What I couldn't do:"
MAX_CAVEATS = 6
# A spec or file note that reports something left undone (B2; the plan's pattern plus "none were fetched" and
# "wasn't checked", the wording the 2741 notes used).
CAVEAT_NOTE = re.compile(r"\b(not (fetched|included|checked|verified)|(none|nothing) (was|were) (fetched|included|checked|"
                         r"verified)|(was|were)(n't| not) (fetched|included|checked|verified)|could not|couldn't|"
                         r"no (image|source)s?)\b", re.I)
PREAMBLE = re.compile(r"^(?:I('ll| will)|Let me) (check|look|search|find|research)[^\n]*\n+")
CREATED_LINE_M = re.compile(r'^Created \*\*.+?\*\*(?:, [^\n]*)?$\n*', re.M)
NO_FORMAT_M = re.compile(r'^No format was named\b[^\n]*$\n*', re.M)
# the research notes' `IMAGE: <query> | <caption>` lines (agents/llm.py IMAGE_LINE), backticked or not: file seeds only
IMAGE_IDEA = re.compile(r'^\s*(?:[-*]\s*)?`?IMAGE:\s*[^\n|`]+\|[^\n]*$', re.M)
LABEL = re.compile(r'^\*\*([a-z_]+)\*\*:\s*', re.M)
GUARD_AGENTS = ('clarify', 'unsupported', 'blocked')


class StepOut(TypedDict):
    tid: str
    text: str
    agent: str
    answer: str
    ok: bool
    files: list[dict]          # CreatedFile (web/src/protocol.ts)
    caveats: list[str]
    assumption: str | None
    # Optional, additive: set by the pipeline when it has them.
    feeds_file: NotRequired[bool]            # a create step built its file from this answer (B5)
    probabilities: NotRequired[dict]         # Jev's route probabilities, for one question when every step clarifies
    asked: NotRequired[bool]                 # a clarify step's question names the detail it lacks (policy), not generic


def strip_preamble(text: str) -> str:
    """The answer without a leading "I'll check ..." or "Let me search ..." line (CLI engines narrate their tools)."""
    out = text or ''
    while (m := PREAMBLE.match(out)) and m.end() < len(out):
        out = out[m.end():]
    return out


def one_line(text: str) -> str:
    return ' '.join(str(text or '').split())


def sentence_with(text: str, m: re.Match) -> str:
    """The sentence of `text` that holds the match."""
    start = max(text.rfind('. ', 0, m.start()), text.rfind('\n', 0, m.start()))
    start = 0 if start < 0 else start + 1
    ends = [i for i in (text.find('. ', m.end()), text.find('\n', m.end())) if i >= 0]
    end = min(ends) + 1 if ends else len(text)
    return one_line(text[start:end])


def note_caveats(notes) -> list[str]:
    """The sentences of spec or file notes that report something left undone, e.g. "No image files were fetched or
    checked in preparing this brief, so it gives no specific image URLs."."""
    out = []
    for note in notes:
        note = str(note or '')
        for m in CAVEAT_NOTE.finditer(note):
            s = sentence_with(note, m)
            if s and s not in out:
                out.append(s)
    return out


def key(text: str) -> str:
    return one_line(text).lower().rstrip('.')


def agent_names() -> set[str]:
    from .config import AGENTS, GUARDS, REPORT, RESEARCH, RUN, SQL_AGENT
    return {*AGENTS, *RESEARCH, *REPORT, *RUN, *SQL_AGENT, *GUARDS, 'document', 'data'}


def shape(meta: dict) -> str:
    from .agents.create import shape_of
    return shape_of(meta)


def asked_format(query: str, files: list[dict]) -> str | None:
    """The format the request asked for: the brief a file carries (C1), else the query's."""
    for f in reversed(files):
        fmt = (f.get('brief') or {}).get('format')
        if fmt:
            return fmt
    from .create.spec import detect_format
    try:
        return detect_format(query)
    except Exception:
        return None


def pick_primary(query: str, steps: list[StepOut]) -> dict | None:
    """The file the answer leads with: the one marked primary, else the requested format from the last file step, else
    the last file in that format, else the last file made."""
    made = [(i, f) for i, s in enumerate(steps) for f in s.get('files') or []]
    if not made:
        return None
    if marked := [f for _, f in made if f.get('role') == 'primary']:
        return marked[-1]
    fmt = asked_format(query, [f for _, f in made])
    last = max(i for i, _ in made)
    for pool in ([f for i, f in made if i == last], [f for _, f in made]):
        if hit := [f for f in pool if fmt and f.get('format') == fmt]:
            return hit[-1]
    return made[-1][1]


def files_block(query: str, steps: list[StepOut]) -> tuple[str, dict | None]:
    primary = pick_primary(query, steps)
    if primary is None:
        return '', None
    lines = [f'Created **{primary["name"]}**, {shape(primary)}.']
    working = [f for s in steps for f in s.get('files') or [] if f is not primary]
    if working:
        lines += ['', 'Working files:', *(f'- **{f["name"]}**, {shape(f)}' for f in working)]
    return '\n'.join(lines), primary


def body_of(s: StepOut, query_format: str | None) -> str:
    """What a step contributes to the answer: its text without a narrating preamble, image seeds, agent labels, the
    "Created ..." line (the files block names every file) or a "No format was named" note when the query named one.
    An answer with none of these is returned as it is."""
    raw = str(s.get('answer') or '')
    text = strip_preamble(raw)
    text = IMAGE_IDEA.sub('', text)
    text = LABEL.sub(lambda m: '' if m.group(1) in agent_names() else m.group(0), text)
    if s.get('files'):
        text = CREATED_LINE_M.sub('', text)
    if query_format:
        text = NO_FORMAT_M.sub('', text)
    return raw if text == raw else re.sub(r'\n{3,}', '\n\n', text).strip()


def styled(parts: list[tuple[str, str]], style: str) -> str:
    """parts: [(step text, body)] with bodies already final. The template layout: one part as it is; one line each as a
    list; otherwise each under a short heading from its step."""
    from .agents.create import topic_title
    if not parts:
        return ''
    if style == 'concise':
        parts = [(t, first_line(b)) for t, b in parts]
    if len(parts) == 1:
        return styled_single(parts[0][1], style) if style in ('concise', 'bullets') else parts[0][1]
    if style == 'table':
        rows = [f'| {cell(t)} | {cell(b)} |' for t, b in parts]
        return '\n'.join(['| Question | Answer |', '| --- | --- |', *rows])
    if all('\n' not in b for _, b in parts):
        return '\n'.join(f'- {b}' for _, b in parts)
    if style == 'bullets':
        return '\n'.join(f'- **{topic_title(t) or "Answer"}**: ' + '\n  '.join(l.strip() for l in b.splitlines() if l.strip())
                         for t, b in parts)
    return '\n\n'.join(f'**{topic_title(t) or "Answer"}**\n\n{b}' for t, b in parts)


def couldnt(caveats: list[str]) -> str:
    return COULDNT + '\n' + '\n'.join(f'- {c}' for c in caveats)


async def compose(query: str, steps: list[StepOut], emit_delta, engine=None, style='default', exact=False) -> dict:
    """steps in subtask order. Returns {answer, engine, claude_in, claude_out, kind, caveats, primary_file}: kind is the
    RunTimings merger ('single', 'template' or 'llm'); caveats are what the run could not do, in plain words (also
    listed at the end of the answer); primary_file is the CreatedFile id the answer leads with, or None. With engine
    None the parts are joined by the template. exact: every answer is exact (keyless or a guard)."""
    from .create.spec import detect_format
    try:
        query_format = detect_format(query)
    except Exception:
        query_format = None
    head, primary = files_block(query, steps)
    primary_id = primary['id'] if primary else None

    # Caveats: each step's own (create conformance, policy), notes that report a miss, and unsupported steps.
    caveats, hedges = [], {}
    def add(c: str):
        c = one_line(c)
        if c and key(c) not in {key(x) for x in caveats}:
            caveats.append(c)
    for s in steps:
        for c in s.get('caveats') or []:
            if c in HEDGES:
                hedges.setdefault(s['tid'], []).append(c)
            else:
                add(c)
        for f in s.get('files') or []:
            spec = f.get('spec') if isinstance(f.get('spec'), dict) else {}
            for c in note_caveats(sec.get('notes') for sec in spec.get('sections') or [] if isinstance(sec, dict)):
                add(c)
        if s.get('files'):
            for c in note_caveats(body_of(s, query_format).split('\n\n')):
                add(c)

    # The parts: guard texts said once, unsupported steps said as caveats when other parts answer, and a step whose
    # answer went into a file left to the file.
    made_file = primary is not None
    parts: list[tuple[StepOut, str]] = []
    seen_guard = set()
    for s in steps:
        if made_file and s.get('feeds_file') and s.get('ok'):
            continue
        body = body_of(s, query_format)
        if s.get('agent') in GUARD_AGENTS:
            if key(body) in seen_guard:
                continue
            seen_guard.add(key(body))
        if s.get('assumption'):
            body = f'{one_line(s["assumption"])}\n\n{body}' if body else one_line(s['assumption'])
        if hedges.get(s['tid']):
            body = '\n\n'.join([body, *hedges[s['tid']]]) if body else '\n\n'.join(hedges[s['tid']])
        if body:
            parts.append((s, body))
    # What a part of the request could not get is never cut: it comes first, and only the other caveats are capped.
    answering = [p for p in parts if p[0].get('agent') != 'unsupported']
    refusals = [one_line(body) for s, body in (parts if answering or head else []) if s.get('agent') == 'unsupported']
    if answering or head:
        parts = answering
    else:
        for s, body in parts:
            add(body)
    refusals = [c for c in dict.fromkeys(refusals) if c]
    rest = [c for c in caveats if key(c) not in {key(r) for r in refusals}]
    caveats = refusals + rest[:max(0, MAX_CAVEATS - len(refusals))]
    # Every step clarifies: generic questions (Jev unsure) become one question; a question that names the detail its
    # step lacks ("Which currency do you want 100 USD in?") is kept, so no step's question is lost.
    clarifies = [p for p in parts if p[0].get('agent') == 'clarify']
    if len(clarifies) > 1 and len(clarifies) == len(parts) and not head:
        from .gate import clarify_text
        generic = [p for p in clarifies if not p[0].get('asked')]
        if len(generic) > 1 or (generic and len(generic) == len(clarifies)):
            probs: dict = {}
            for s, _ in generic:
                for a, p in (s.get('probabilities') or {}).items():
                    probs[a] = probs.get(a, 0) + p / len(generic)
            one = (generic[0][0], clarify_text(probs, query))
            parts = [one if p is generic[0] else p for p in clarifies if p not in generic[1:]]

    def finish(text: str) -> str:
        """The answer with the caveats not already in it listed at the end."""
        missing = [c for c in caveats if key(c) not in key(text)]
        return '\n\n'.join(x for x in (text, couldnt(missing) if missing else '') if x)

    out = {'caveats': caveats, 'primary_file': primary_id}
    # One step: its answer passes through, as merge() does, with any caveat it does not already say.
    if len(steps) == 1:
        answer = parts[0][1] if parts else str(steps[0].get('answer') or '')
        if steps[0].get('files'):
            answer = strip_preamble(str(steps[0].get('answer') or ''))
        elif exact:
            answer = styled_single(answer, style)
        return {**out, 'answer': finish(answer), 'engine': 'single', 'claude_in': 0, 'claude_out': 0, 'kind': 'single'}

    if engine is not None and len(parts) > 1:
        prompt = f'User query: {query}\n\n' + '\n\n'.join(f'[Part {i}: {one_line(s["text"])[:200]}]\n{b}'
                                                          for i, (s, b) in enumerate(parts, 1))
        if caveats:
            prompt += ('\n\nLimits (what could not be done):\n' + '\n'.join(f'- {c}' for c in caveats) +
                       f'\nList these limits plainly at the end, under the line "{COULDNT}".')
        if head:
            prompt += '\n\nThe reply is shown after a line naming the created file; do not name the file again.'
        sent = []

        def track(text):
            sent.append(text)
            emit_delta(text)
        if head:
            emit_delta(head + '\n\n')
        why = 'returned nothing'
        try:
            r = await engine.stream(system=system_for(style) + ' Never name the agents or the parts.', prompt=prompt,
                                    effort='low', emit_delta=track, max_tokens=1024)
            if r.text:
                text = '\n\n'.join(x for x in (head, strip_preamble(r.text).strip()) if x)
                # a hedge (B6) the merged text dropped is added back: the answer must end with it
                if lost := [h for hs in hedges.values() for h in dict.fromkeys(hs) if key(h) not in key(text)]:
                    tail = '\n\n' + '\n\n'.join(dict.fromkeys(lost))
                    emit_delta(tail)
                    text += tail
                if COULDNT.lower() not in text.lower() and (missing := [c for c in caveats if key(c) not in key(text)]):
                    tail = '\n\n' + couldnt(missing)
                    emit_delta(tail)
                    text += tail
                return {**out, 'answer': text, 'engine': engine.name, 'claude_in': r.input_tokens,
                        'claude_out': r.output_tokens, 'kind': 'llm'}
        except Exception as e:
            why = getattr(e, 'why', None) or type(e).__name__
        if any(sent):  # the merge stream is append-only: mark where the partial text stops
            emit_delta(f'\n[{engine.label} merge failed ({why}); joined]\n')
        body = finish(styled([(s['text'], b) for s, b in parts], style))
        emit_delta(body)  # the file line, if any, was streamed ahead of the call
        return {**out, 'answer': '\n\n'.join(x for x in (head, body) if x), 'engine': 'concat', 'claude_in': 0,
                'claude_out': 0, 'kind': 'llm'}

    answer = finish('\n\n'.join(x for x in (head, styled([(s['text'], b) for s, b in parts], style)) if x))
    emit_delta(answer)
    return {**out, 'answer': answer, 'engine': 'concat', 'claude_in': 0, 'claude_out': 0,
            'kind': 'llm' if engine else 'template'}
