"""merge(query, results) -> one answer. One subtask passes through; several are combined by Claude or concatenated."""
from .agents.claude import stream

SYSTEM = ('You combine answers from specialist agents into one short reply to the user. Keep every number, unit and '
          'source exactly as given, mention failures plainly, add nothing new. Under 120 words, no preamble.')


def concat(results: list[tuple[str, str]]) -> str:
    return '\n\n'.join(f'**{agent}**: {answer}' for agent, answer in results)


async def merge(query: str, results: list[tuple[str, str]], emit_delta, claude=None) -> dict:
    """results: [(agent, answer)] in subtask order. Returns {answer, engine, claude_in, claude_out}."""
    if len(results) == 1:
        return {'answer': results[0][1], 'engine': 'single', 'claude_in': 0, 'claude_out': 0}
    if claude is not None:
        prompt = f'User query: {query}\n\n' + '\n\n'.join(f'[{a} agent]\n{t}' for a, t in results)
        sent = []
        track = lambda text: (sent.append(text), emit_delta(text))
        why = 'returned nothing'
        try:
            r = await stream(claude, system=SYSTEM, prompt=prompt, effort='low', emit_delta=track, max_tokens=1024)
            if r.text:
                return {'answer': r.text, 'engine': 'claude', 'claude_in': r.input_tokens, 'claude_out': r.output_tokens}
        except Exception as e:
            why = type(e).__name__
        if any(sent):  # the merge stream is append-only: mark where the partial Claude text stops
            emit_delta(f'\n[Claude merge failed ({why}); concatenated]\n')
    answer = concat(results)
    emit_delta(answer)
    return {'answer': answer, 'engine': 'concat', 'claude_in': 0, 'claude_out': 0}
