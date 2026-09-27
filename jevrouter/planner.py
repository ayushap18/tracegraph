"""plan(query) -> subtasks (with dependencies). A conservative text split that Jev must confirm, or the LLM engine when one
is active and the split needs it (dependent steps, follow-ups, files, long or unclear queries)."""
import asyncio
import re

from .cache import normalize
from .config import BLOCK_AT, MAX_SUBTASKS, MULTI_AT
from .engines import parse_json
from .gate import refers_back
from .jev import multi_score, unsafe_score

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


async def plan_heuristic(query: str, jev, scores=None) -> dict:
    """scores: an optional cache of Jev's multi score by query text (jevrouter/cache.py); a hit sets `cached`."""
    out = {'planner': 'heuristic', 'subtasks': [query], 'multi': None, 'jev_tokens': 0, 'claude_in': 0, 'claude_out': 0}
    parts = candidate_split(query)
    if len(parts) < 2:
        return out
    key = normalize(query)
    hit = scores.get(key) if scores is not None else None
    if hit is not None:
        out['multi'], out['cached'] = hit, True
    else:
        try:
            out['multi'], out['jev_tokens'] = await multi_score(jev, query)
        except Exception:
            return out  # without Jev's confirmation, don't guess
        if scores is not None:
            scores.put(key, out['multi'])
    if out['multi'] >= MULTI_AT:
        out['subtasks'] = parts
        # "...and then what time is it there": a part that points back waits for the part before it, which gives it
        # the context (and the place) it refers to.
        out['deps'] = [[i - 1] if i and refers_back(p) else [] for i, p in enumerate(parts)]
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


# Every clause of a query, however short ("make meth"): sentences, ";", commas and and/then/also/plus.
CLAUSE = re.compile(r'(?<=[.?!])\s+|\s*[;,]\s*|\s+(?:and|then|also|plus)\b[\s,]*', re.I)


def clauses(query: str) -> list[str]:
    parts = [LEAD.sub('', p.strip(' ,.')).strip(' ,') for p in CLAUSE.split(query)]
    return [p for p in dict.fromkeys(parts) if p and p.lower() not in ('and', 'then', 'also', 'plus') and p != query.strip()]


async def unplanned_harm(query: str, subtasks: list[str], jev, whole: float) -> tuple[list[str], int]:
    """(the parts of an unsafe query no planned step carries, Jev tokens). whole is Jev's unsafe score for the query.
    A planner that quietly leaves out a request (often a harmful one, which it declines to rephrase) must not make it
    disappear: when the query is unsafe but no planned step is, the clauses Jev finds unsafe (or, if no single clause
    is, the whole query) become steps of their own, so they are routed and blocked. Only Jev's safety check decides
    this, never word overlap: a correct rewrite ("a hundred bucks" -> "100 USD") is not a dropped part."""
    if whole < BLOCK_AT:
        return [], 0
    parts = clauses(query)
    scored = await asyncio.gather(*(unsafe_score(jev, t) for t in [*subtasks, *parts]))
    tokens = sum(n for _, n in scored)
    if any(score >= BLOCK_AT for score, _ in scored[:len(subtasks)]):
        return [], tokens  # a planned step carries it, and routing blocks that step
    harmful = [p for p, (score, _) in zip(parts, scored[len(subtasks):]) if score >= BLOCK_AT]
    return harmful or [query], tokens


# A2 (docs/PLAN-speed-evals-chat.md): the heuristic plans a multi-part query on its own when the parts are clear, so an
# engine run doesn't wait seconds for an LLM plan of "time in Tokyo and 15% of 380". Jev's multi score decides how sure
# the split is: at SURE_MULTI or above the parts stand, at SURE_SINGLE or below it's one request; in between the LLM plans.
SURE_MULTI = 0.8
SURE_SINGLE = 0.2
LONG_QUERY = 24  # words: a query this long may hold requests the text splitter can't see


# A later part that may use an earlier part's answer: "...then which is better value", "...and double it", "...convert
# that to GBP". Broader than gate.refers_back on purpose: a false alarm only costs the LLM plan the query had before.
USES_EARLIER = re.compile(r'\b(?:which|whichever|better|worse|cheaper|more|less|bigger|smaller|higher|lower|compare|'
                          r'difference|both|them|they|those|these|that|this|same|result|answer|total|sum|together|'
                          r'combined|previous|above|former|latter)\b|(?<!\bis )(?<!\bwas )\bit\b', re.I)


# A person or thing named earlier: "...and how old is he", "...what is his net worth", "...and what is her name".
PRONOUN = re.compile(r'\b(?:he|she|him|his|her|hers|its|their|theirs|there|they|them)\b', re.I)
# A later part that is only an operation on an earlier answer: "...and divide by 2", "...then multiply by 3".
OPERATES = re.compile(r'^(?:then\s+|and\s+)?(?:divide|multiply|add|subtract|double|halve|triple|square|cube|times|minus|'
                      r'plus|round|increase|decrease|reduce|raise|take|convert\s+(?:that|it|this)|split)\b', re.I)
# Words that qualify a definite noun on the spot: "the time in Tokyo", "the capital of France", "the date today".
QUALIFIERS = {'of', 'in', 'for', 'at', 'on', 'from', 'to', 'between', 'near', 'by', 'with', 'today', 'tomorrow',
              'yesterday', 'now'}
# A part that needs a place or an amount to be answered: "What is the weather", "convert 100".
NEEDS_ANCHOR = re.compile(r'\b(?:weather|temperature|forecast|rain|time|convert|exchange|rate|sunrise|sunset)\b', re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


def anchors(part: str) -> set[str]:
    """Places, names and codes in a part: capitalised words after the first ("in Paris", "to EUR")."""
    return {w for w in WORD.findall(part)[1:] if w[:1].isupper()}


def dangling_the(part: str, earlier: str) -> bool:
    """A definite noun that nothing in the part itself pins down and no earlier part names: "when was the author born",
    "the winning country". "the time in Tokyo" and "the Eiffel Tower" are pinned down where they stand."""
    words = WORD.findall(part)
    seen = {w.lower() for w in WORD.findall(earlier)}
    for i, w in enumerate(words):
        if w.lower() != 'the' or i + 1 >= len(words):
            continue
        head, after = words[i + 1], [x.lower() for x in words[i + 1:i + 5]]
        if head[:1].isupper() or any(x in QUALIFIERS for x in after) or head.lower() in seen:
            continue
        return True
    return False


def depends(parts: list[str]) -> bool:
    """True when some part may need another part's answer or detail, so the parts can't simply run side by side. Errs
    towards True: a false alarm only costs the LLM plan the query had before the heuristic shortcut."""
    for i, p in enumerate(parts):
        if i and (refers_back(p) or USES_EARLIER.search(p) or PRONOUN.search(p) or OPERATES.search(p)
                  or dangling_the(p, ' '.join(parts[:i]))):
            return True
        # "What is the weather and the time in Tokyo", "convert 100 and 200 USD to EUR": an earlier part that needs a
        # place or an amount a later part has
        if NEEDS_ANCHOR.search(p) and not anchors(p) and any(anchors(q) for q in parts[i + 1:]):
            return True
    return False


def needs_llm_plan(query: str, context=None, files=None) -> bool:
    """True when only the LLM planner can plan this query well: a follow-up (it resolves against earlier turns), attached
    files, a long query, one the text splitter can't split, or parts that may depend on each other ("...the time there",
    "...then which is better value", "...how old is he", "...and divide by 2")."""
    if context or files or len(query.split()) > LONG_QUERY:
        return True
    parts = candidate_split(query)
    return len(parts) < 2 or depends(parts)


def sure(p: dict) -> bool:
    """Jev's multi score is decisive either way, so the heuristic plan can stand without the LLM."""
    return p['multi'] is not None and (p['multi'] >= SURE_MULTI or p['multi'] <= SURE_SINGLE)


def kind(p: dict) -> str:
    """The RunTimings planner: 'llm', 'heuristic' (Jev's multi score was asked) or 'single' (no planner call at all)."""
    if p['planner'] != 'heuristic':
        return 'llm'
    return 'heuristic' if p['multi'] is not None else 'single'


async def plan(query: str, jev, engine=None, context=None, files=None, mode: str = 'balanced', on_llm=None,
               scores=None) -> dict:
    """context: earlier session turns [{query, answer}]; files: attached file names. The keyless planner ignores both.
    mode (docs/PLAN-speed-evals-chat.md): 'quick' never calls the LLM planner; 'deep' calls it whenever the query may
    hold several requests (the behaviour before the A2 skips); 'balanced' and 'research' let a sure heuristic plan stand.
    on_llm() is called just before the LLM planner starts, so the caller can route the whole query alongside it.
    scores: a cache of Jev's multi score by query text (the Router's; evals pass none)."""
    heuristic, safety = None, None
    if engine is not None and mode != 'quick' and worth_llm_plan(query, context):
        if mode != 'deep' and not needs_llm_plan(query, context, files):
            # The whole query's safety check runs alongside the multi score. The heuristic keeps every part as written,
            # and each part is routed (and blocked) on its own; a query that is unsafe as a whole still goes to the LLM
            # path below, whose unplanned_harm check makes sure the harmful part becomes a step of its own.
            heuristic, safety = await asyncio.gather(plan_heuristic(query, jev, scores), unsafe_score(jev, query),
                                                     return_exceptions=True)
            if isinstance(heuristic, BaseException):
                heuristic = None
            elif not isinstance(safety, BaseException):
                heuristic['jev_tokens'] += safety[1]
                if sure(heuristic) and safety[0] < BLOCK_AT:
                    return with_deps(heuristic)
        if on_llm is not None:
            on_llm()
        # Jev's safety check on the whole query runs alongside the LLM planner, so it adds no wait.
        if safety is None or isinstance(safety, BaseException):
            llm, safety = await asyncio.gather(plan_llm(query, engine, context, files), unsafe_score(jev, query),
                                               return_exceptions=True)
        else:  # already known from the heuristic check (its tokens are counted there)
            safety = (safety[0], 0)
            try:
                llm = await plan_llm(query, engine, context, files)
            except Exception as e:
                llm = e
        p = None if isinstance(llm, BaseException) else llm  # refusal, API error, bad JSON: the heuristic still works
        if p is not None and not isinstance(safety, BaseException):  # without Jev's check, keep every part as written
            # a step that points back ("what time is it there") but was planned as independent waits for the one before
            p['deps'] = [d or ([i - 1] if i and refers_back(t) else []) for i, (t, d) in enumerate(zip(p['subtasks'], p['deps']))]
            p['jev_tokens'] += safety[1] + (heuristic or {}).get('jev_tokens', 0)
            try:
                missing, tokens = await unplanned_harm(query, p['subtasks'], jev, safety[0])
            except Exception:
                missing, tokens = None, 0
            p['jev_tokens'] += tokens
            if missing == []:
                return p
            if missing and len(p['subtasks']) + len(missing) <= MAX_SUBTASKS:  # put them back, verbatim, as their own steps
                return {**p, 'subtasks': p['subtasks'] + missing, 'deps': p['deps'] + [[] for _ in missing]}
            # no room, or Jev couldn't check: the keyless plan keeps every part of the query as written
    return with_deps(heuristic or await plan_heuristic(query, jev, scores))


def with_deps(out: dict) -> dict:
    return {**out, 'deps': out.get('deps') or [[] for _ in out['subtasks']]}


RESOLVE = ('Do not use tools. Rewrite the request below into one short, self-contained request by substituting the facts it '
           'refers to (names, places, numbers) from the context of earlier steps. Output only the rewritten request, '
           'never an answer. "There" or "its" means the place the earlier steps were about; after a currency '
           'conversion that is the country whose currency it was converted to (INR: India). If the context lacks the '
           'fact, output the request unchanged.')


async def resolve_step(engine, text_with_context: str) -> tuple[str, int, int]:
    """(self-contained text, tokens in, tokens out). Keyless agents parse places and amounts from plain text, so
    "the time in its capital" plus context must become "the current time in Buenos Aires" before they see it."""
    r = await engine.stream(system=RESOLVE, prompt=text_with_context, effort='low', max_tokens=256)
    text = r.text.strip().strip('"').splitlines()[0].strip() if r.text.strip() else ''
    if not text:
        raise ValueError('empty rewrite')
    return text[:500], r.input_tokens, r.output_tokens
