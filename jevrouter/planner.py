"""plan(query) -> subtasks (with dependencies). The LLM engine splits when one is active; otherwise a conservative text split that Jev must confirm."""
import re

from .config import MAX_SUBTASKS, MULTI_AT
from .engines import parse_json
from .jev import multi_score

SEP = re.compile(r'\s*;\s*|\s*,\s*and\s+|\s+(?:and|then|also)\s+', re.I)
LEAD = re.compile(r'^(?:and|then|also)\s+', re.I)
STEP = {'type': 'object', 'properties': {'text': {'type': 'string'}, 'depends_on': {'type': 'array', 'items': {'type': 'integer'}}},
        'required': ['text', 'depends_on'], 'additionalProperties': False}
SCHEMA = {'type': 'object', 'properties': {'subtasks': {'type': 'array', 'items': STEP}},
          'required': ['subtasks'], 'additionalProperties': False}
SYSTEM = ('You are a query planner. Do not use tools. You split a user query into subtasks for specialist agents (math, weather, '
          'time, currency, knowledge, code, chat, research, report, document, data). Return 1 to 4 subtasks. Each must keep the '
          'exact numbers, currencies and places. If the query is a single request, return it unchanged as the only subtask. '
          'When a subtask needs an earlier subtask\'s answer (e.g. "the time in its capital" after "which country won"), put '
          'the 0-based indices of those earlier subtasks in depends_on and phrase it so it is clear what to take from them; '
          'otherwise depends_on is []. If earlier conversation turns are given and the query is a follow-up, rewrite it into '
          'self-contained subtasks ("and in GBP?" after "convert 100 USD to EUR" becomes "convert 100 USD to GBP"). Never '
          'answer the query.')


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


def parse_steps(raw) -> tuple[list[str], list[list[int]]]:
    """Subtask texts and their dependencies. Accepts the old ["text", ...] shape. A dependency must point at an earlier
    subtask that survived (blank items are skipped and indices remapped), so the plan is always a DAG that runs in order."""
    texts, deps, kept = [], [], {}  # kept: the model's index -> ours
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        text, dep = (item, []) if isinstance(item, str) else (item.get('text'), item.get('depends_on')) if isinstance(item, dict) else (None, [])
        if not isinstance(text, str) or not text.strip() or len(texts) == MAX_SUBTASKS:
            continue
        kept[i] = len(texts)
        texts.append(text.strip())
        deps.append(sorted({kept[d] for d in dep or [] if type(d) is int and d < i and d in kept}))
    return texts, deps


def prompt_for(query: str, context=None, files=None) -> str:
    parts = []
    if context:
        parts.append('Earlier turns in this conversation (oldest first):\n' + '\n'.join(
            f'Q: {t["query"][:600]}\nA: {t["answer"][:600]}' for t in context))
    if files:
        parts.append('Attached files: ' + ', '.join(files) + ' (questions about them go to the document or data agent).')
    return '\n\n'.join([*parts, f'Current query: {query}']) if parts else query


async def plan_llm(query: str, engine, context=None, files=None) -> dict:
    reply = await engine.stream(system=SYSTEM, prompt=prompt_for(query, context, files), effort='low', max_tokens=1024,
                                schema=SCHEMA)
    subtasks, deps = parse_steps(parse_json(reply.text)['subtasks'])
    if not subtasks:
        raise ValueError('planner returned no subtasks')
    return {'planner': engine.name, 'subtasks': subtasks, 'deps': deps, 'multi': None, 'jev_tokens': 0,
            'claude_in': reply.input_tokens, 'claude_out': reply.output_tokens}


# Words that mean a query might hold several requests or refer back to an earlier part ("...then the time there").
# A comma only counts after two or more words, so "hey, how are you?" stays one request but "Paris weather, Tokyo
# time" does not. A leading greeting ("hi there,") is ignored for the same reason.
MAYBE_MULTI = re.compile(r';|\S+\s+\S+,|\b(?:and|then|also|plus|after|there|it|its|that|those|them|same|both)\b', re.I)
GREETING = re.compile(r'^\s*(?:hi|hey|hello|yo|thanks|thank you|ok|okay)(?:\s+there)?\b[\s,!.]*', re.I)


def worth_llm_plan(query: str, context=None) -> bool:
    """An LLM plan costs seconds on a subscription CLI (it runs before any agent can start); single-clause queries don't
    need one, but a follow-up in a chat session does, because only the LLM planner can resolve it against earlier turns."""
    return bool(context) or bool(MAYBE_MULTI.search(GREETING.sub('', query))) or len(query.split()) > 14


async def plan(query: str, jev, engine=None, context=None, files=None) -> dict:
    """context: earlier session turns [{query, answer}]; files: attached file names. The keyless planner ignores both."""
    if engine is not None and worth_llm_plan(query, context):
        try:
            return await plan_llm(query, engine, context, files)
        except Exception:
            pass  # refusal, API error, bad JSON: the heuristic still works
    out = await plan_heuristic(query, jev)
    return {**out, 'deps': [[] for _ in out['subtasks']]}


RESOLVE = ('Do not use tools. Rewrite the request below into one short, self-contained request by substituting the facts it '
           'refers to (names, places, numbers) from the context of earlier steps. Output only the rewritten request, '
           'never an answer. If the context lacks the fact, output the request unchanged.')


async def resolve_step(engine, text_with_context: str) -> tuple[str, int, int]:
    """(self-contained text, tokens in, tokens out). Keyless agents parse places and amounts from plain text, so
    "the time in its capital" plus context must become "the current time in Buenos Aires" before they see it."""
    r = await engine.stream(system=RESOLVE, prompt=text_with_context, effort='low', max_tokens=256)
    text = r.text.strip().strip('"').splitlines()[0].strip() if r.text.strip() else ''
    if not text:
        raise ValueError('empty rewrite')
    return text[:500], r.input_tokens, r.output_tokens
