"""plan(query) -> subtasks. The LLM engine splits when one is active; otherwise a conservative text split that Jev must confirm."""
import re

from .config import MAX_SUBTASKS, MULTI_AT
from .engines import parse_json
from .jev import multi_score

SEP = re.compile(r'\s*;\s*|\s*,\s*and\s+|\s+(?:and|then|also)\s+', re.I)
LEAD = re.compile(r'^(?:and|then|also)\s+', re.I)
SCHEMA = {'type': 'object', 'properties': {'subtasks': {'type': 'array', 'items': {'type': 'string'}}},
          'required': ['subtasks'], 'additionalProperties': False}
SYSTEM = ('You are a query planner. Do not use tools. You split a user query into independent subtasks for specialist agents (math, weather, time, currency, knowledge, '
          'code, chat, research). Return 1 to 4 subtasks. Each must be self-contained and keep the exact numbers, currencies '
          'and places. If the query is a single request, return it unchanged as the only subtask. Never answer the query.')


def substantial(part: str) -> bool:
    # "salt and pepper" must not split, "weather in Paris and 100 EUR to INR" should.
    words = part.split()
    return len(words) >= 3 or bool(re.search(r'\d', part)) or any(w[:1].isupper() for w in words[1:])


def candidate_split(query: str) -> list[str]:
    parts = [LEAD.sub('', p.strip(' ,.')).strip() for p in SEP.split(query)]
    parts = [p for p in parts if p]
    if len(parts) < 2 or not all(substantial(p) for p in parts):
        return [query]
    if len(parts) > MAX_SUBTASKS:
        parts = parts[:MAX_SUBTASKS - 1] + [' and '.join(parts[MAX_SUBTASKS - 1:])]
    return parts


async def plan_heuristic(query: str, jev) -> dict:
    out = {'planner': 'heuristic', 'subtasks': [query], 'multi': None, 'jev_tokens': 0, 'claude_in': 0, 'claude_out': 0}
    parts = candidate_split(query)
    if len(parts) < 2:
        return out
    try:
        out['multi'], out['jev_tokens'] = await multi_score(jev, query)
    except Exception:
        return out  # without Jev's confirmation, don't guess
    if out['multi'] >= MULTI_AT:
        out['subtasks'] = parts
    return out


async def plan_llm(query: str, engine) -> dict:
    reply = await engine.stream(system=SYSTEM, prompt=query, effort='low', max_tokens=1024, schema=SCHEMA)
    subtasks = [s.strip() for s in parse_json(reply.text)['subtasks'] if isinstance(s, str) and s.strip()][:MAX_SUBTASKS]
    if not subtasks:
        raise ValueError('planner returned no subtasks')
    return {'planner': engine.name, 'subtasks': subtasks, 'multi': None, 'jev_tokens': 0,
            'claude_in': reply.input_tokens, 'claude_out': reply.output_tokens}


# Words that mean a query might hold several requests or refer back to an earlier part ("...then the time there").
MAYBE_MULTI = re.compile(r'[;,]|\b(?:and|then|also|plus|after|there|it|its|that|those|them|same|both)\b', re.I)


def worth_llm_plan(query: str) -> bool:
    """An LLM plan costs seconds on a subscription CLI; single-clause queries don't need one."""
    return bool(MAYBE_MULTI.search(query)) or len(query.split()) > 14


async def plan(query: str, jev, engine=None) -> dict:
    if engine is not None and worth_llm_plan(query):
        try:
            return await plan_llm(query, engine)
        except Exception:
            pass  # refusal, API error, bad JSON: the heuristic still works
    return await plan_heuristic(query, jev)
