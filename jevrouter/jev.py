import re
import time

from typesafe_sdk import Choice, Noul, Score

from .config import BLOCK_AT, EXAMPLE_CHARS, MAX_EXAMPLES, MAX_NOT, MIN_CLEAR, MIN_CONFIDENCE


UNSAFE = 'The query asks for help with something harmful, illegal, sexual, or hateful.'
# A5 (docs/PLAN-speed-evals-chat.md): how much a good answer takes, so easy steps can go to a fast engine at low effort
# and hard ones to the strongest at high effort.
HARD = ('How much reasoning, expertise or writing does a good answer to this request need? A greeting, a lookup or a '
        'one-line fact is easy; a report, working code, a multi-step analysis or a careful comparison is hard.')


def questions(agents: dict) -> dict:
    return {
        'route': Choice(instructions='Which specialist agent should handle this user query?', criteria=agents),
        'urgency': Score(instructions='How urgently does the user need an answer?', criteria=['No rush', 'Soon', 'Right now']),
        'hard': Score(instructions=HARD, criteria=['Easy', 'Moderate', 'Hard']),
        'unsafe': Noul(instructions=UNSAFE),
        'clear': Noul(instructions='The query is clear enough to answer without asking a follow-up question.'),
    }


def clean(text: str) -> str:
    """An example as Jev sees it: one line, trimmed, at most EXAMPLE_CHARS."""
    t = re.sub(r'\s+', ' ', str(text or '')).strip()
    return t if len(t) <= EXAMPLE_CHARS else t[:EXAMPLE_CHARS - 3].rstrip() + '...'


def examples_for(agents, labels: list[dict]) -> dict[str, dict]:
    """{agent: {examples, not}} for the agents that have any, from labels (any order; newest wins).
    examples: the latest MAX_EXAMPLES texts the user said belong to the agent. not: the latest MAX_NOT texts Jev sent
    to the agent that the user said were wrong. A text that is also a `not` for the agent is never an example, so one
    bad label can't pull the agent both ways, and a deleted label is gone as soon as the caller's list is refreshed."""
    right, wrong = {}, {}
    for l in sorted(labels, key=lambda l: -(l.get('at') or 0)):
        text = clean(l.get('text'))
        if not text:
            continue
        if l.get('correct') in agents:
            right.setdefault(l['correct'], []).append(text)
        if l.get('verdict') == 'wrong' and l.get('picked') in agents and l['picked'] != l.get('correct'):
            wrong.setdefault(l['picked'], []).append(text)
    out = {}
    for agent in agents:
        banned = {t.casefold() for t in wrong.get(agent, ())}
        ex = dedupe([t for t in right.get(agent, ()) if t.casefold() not in banned], MAX_EXAMPLES)
        no = dedupe(wrong.get(agent, ()), MAX_NOT)
        if ex or no:
            out[agent] = {'examples': ex, 'not': no}
    return out


def dedupe(texts, n: int) -> list[str]:
    seen, out = set(), []
    for t in texts:
        if t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out[:n]


def criteria(agents: dict, labels: list[dict]) -> dict:
    """The route Choice's criteria: an agent with labels gets {description, examples, not}; the rest keep the plain
    description, so with no labels the request is exactly what it was without examples."""
    ex = examples_for(agents, labels)
    return {a: ({'description': d, **ex[a]} if a in ex else d) for a, d in agents.items()}


MULTI = {'multi': Noul(instructions='The query contains two or more separate requests that need different kinds of answers')}


def decide(route, unsafe: float, clear: float) -> tuple[str, str]:
    if unsafe >= BLOCK_AT:
        return 'blocked', f'Jev flagged it as unsafe ({unsafe:.0%})'
    if clear < MIN_CLEAR:
        return 'clarify', f'unclear ({clear:.0%})'
    if route.confidence < MIN_CONFIDENCE:
        return 'clarify', f'low confidence ({route.confidence:.0%})'
    return route.choice, f'{route.choice} at {route.confidence:.0%}'


async def route_one(jev, text: str, agents: dict) -> dict:
    """One Jev call per subtask; returns the `routed` event fields plus input_tokens. `agents` are the route criteria:
    plain descriptions, or criteria() output with route examples on; `examples` says whether any criterion had some."""
    crit = agents
    t0 = time.perf_counter()
    r = await jev.system_one(text, questions(crit))
    jev_ms = round((time.perf_counter() - t0) * 1000)
    route, urgency, unsafe, clear = (r.answers[k] for k in ('route', 'urgency', 'unsafe', 'clear'))
    hard = r.answers.get('hard') if hasattr(r.answers, 'get') else None
    agent, reason = decide(route, unsafe.noul, clear.noul)
    return {
        'agent': agent, 'pick': route.choice, 'reason': reason,
        'probabilities': dict(sorted(route.probabilities.items(), key=lambda kv: -kv[1])),
        'confidence': route.confidence, 'urgency': round(urgency.score, 2), 'unsafe': unsafe.noul, 'clear': clear.noul,
        'jev_ms': jev_ms, 'model': r.model, 'input_tokens': getattr(r.usage, 'input_tokens', 0) or 0,
        'examples': any(isinstance(c, dict) for c in crit.values()),
        'hard': round(min(1.0, max(0.0, hard.score)), 2) if hard is not None else None,  # not a routed field: the pipeline reads it (A5)
    }


async def multi_score(jev, text: str) -> tuple[float, int]:
    r = await jev.system_one(text, MULTI)
    return r.answers['multi'].noul, getattr(r.usage, 'input_tokens', 0) or 0


async def unsafe_score(jev, text: str) -> tuple[float, int]:
    """Jev's unsafe score for a text on its own (the same question route_one asks), and the tokens it used."""
    r = await jev.system_one(text, {'unsafe': Noul(instructions=UNSAFE)})
    return r.answers['unsafe'].noul, getattr(r.usage, 'input_tokens', 0) or 0
