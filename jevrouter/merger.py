"""merge(query, results) -> one answer. One subtask passes through; several are combined by the LLM engine or, when an LLM
adds nothing (every answer is exact, docs/PLAN-speed-evals-chat.md A2), joined by a template."""

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
