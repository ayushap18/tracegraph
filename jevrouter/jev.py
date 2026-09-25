import time

from typesafe_sdk import Choice, Noul, Score

from .config import BLOCK_AT, MIN_CLEAR, MIN_CONFIDENCE


def questions(agents: dict) -> dict:
    return {
        'route': Choice(instructions='Which specialist agent should handle this user query?', criteria=agents),
        'urgency': Score(instructions='How urgently does the user need an answer?', criteria=['No rush', 'Soon', 'Right now']),
        'unsafe': Noul(instructions='The query asks for help with something harmful, illegal, sexual, or hateful.'),
        'clear': Noul(instructions='The query is clear enough to answer without asking a follow-up question.'),
    }


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
    """One Jev call per subtask; returns the `routed` event fields plus input_tokens."""
    t0 = time.perf_counter()
    r = await jev.system_one(text, questions(agents))
    jev_ms = round((time.perf_counter() - t0) * 1000)
    route, urgency, unsafe, clear = (r.answers[k] for k in ('route', 'urgency', 'unsafe', 'clear'))
    agent, reason = decide(route, unsafe.noul, clear.noul)
    return {
        'agent': agent, 'pick': route.choice, 'reason': reason,
        'probabilities': dict(sorted(route.probabilities.items(), key=lambda kv: -kv[1])),
        'confidence': route.confidence, 'urgency': round(urgency.score, 2), 'unsafe': unsafe.noul, 'clear': clear.noul,
        'jev_ms': jev_ms, 'model': r.model, 'input_tokens': getattr(r.usage, 'input_tokens', 0) or 0,
    }


async def multi_score(jev, text: str) -> tuple[float, int]:
    r = await jev.system_one(text, MULTI)
    return r.answers['multi'].noul, getattr(r.usage, 'input_tokens', 0) or 0
