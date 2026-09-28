"""plan(query) -> subtasks (with dependencies). A conservative text split that Jev must confirm, or the LLM engine when one
is active and the split needs it (dependent steps, follow-ups, files, long or unclear queries)."""
import asyncio
import re

from .agents.tools import DEMONYM_CURRENCY
from .cache import normalize
from .config import BLOCK_AT, MAX_SUBTASKS, MULTI_AT
from .engines import parse_json
from . import gate
from .gate import refers_back
from .jev import multi_score, unsafe_score

# A bare comma splits only before a new question or command ("time in Paris, what's 10% of 90"), never inside
# "Paris, France" or a list of things.
OPENER = (r"(?:what(?:'s|s)?|how|who|when|where|which|why|convert|tell|give|show|find|translate|calculate|compute|"
          r"is|are|can|could|do|does|will)\b")
SEP = re.compile(r"\s*;\s*|\s*,\s*and\s+|\s+(?:and|then|also)\s+|\s*,\s*(?=" + OPENER + ")", re.I)
LEAD = re.compile(r'^(?:and|then|also)\s+', re.I)
STEP = {'type': 'object', 'properties': {'text': {'type': 'string'}, 'depends_on': {'type': 'array', 'items': {'type': 'integer'}}},
        'required': ['text', 'depends_on'], 'additionalProperties': False}
SCHEMA = {'type': 'object', 'properties': {'subtasks': {'type': 'array', 'items': STEP}},
          'required': ['subtasks'], 'additionalProperties': False}
SYSTEM = ('You are a query planner. Do not use tools. You split a user query into subtasks for specialist agents (math, weather, '
          'time, currency, knowledge, code, chat, research, report, document, data, create). Return 1 to 4 subtasks. Each must keep the '
          'exact numbers, currencies and places. If the query is a single request, return it unchanged as the only subtask. '
          'When a subtask needs an earlier subtask\'s answer (e.g. "the time in its capital" after "which country won"), put '
          'the 0-based indices of those earlier subtasks in depends_on and phrase it so it is clear what to take from them; '
          'otherwise depends_on is []. A request for a file (PDF, Word, slides, spreadsheet, Markdown) of content another '
          'subtask must produce first is its own last subtask that depends on that one ("research X and make slides" becomes '
          '"research X" then "make slides from it" with depends_on [0]). '
          'If earlier conversation turns are given and the query is a follow-up, rewrite it into '
          'self-contained subtasks ("and in GBP?" after "convert 100 USD to EUR" becomes "convert 100 USD to GBP"), except '
          'a request for a file of what came before ("put that in a PDF", "now as slides"): keep that word for word. '
          'A file step\'s text must restate every format, length, page or slide count, style, colour, font, image and '
          'diagram requirement from the user\'s query. Never refer to another subtask by its number ("subtask 0", '
          '"step 1"); say "the previous answer" instead. Never answer the query.')
NO_WEB = ' No agent can search the web or fetch images.'


def system_for(web: bool = False) -> str:
    """The planner's system prompt: without a web engine it says no agent can search the web, so it never plans
    "find images with URLs"."""
    return SYSTEM if web else SYSTEM + NO_WEB


def substantial(part: str) -> bool:
    # "salt and pepper" must not split, "weather in Paris and 100 EUR to INR" should.
    words = part.split()
    return len(words) >= 3 or bool(re.search(r'\d', part)) or any(w[:1].isupper() for w in words[1:])


# "Add 120 and 380", "between 1 and 5": the operands of one operation are never split apart.
OPERANDS = re.compile(r'\b(?:add|multiply|subtract|sum\s+of|product\s+of|difference\s+between|between|average\s+of)\s+'
                      r'[^,;]*?\d[\d.,]*\s*(?:[A-Za-z]+\s+)?(?:and|&)\s+(?=[\d$€£₹¥])', re.I)
# Pairs that name one thing: "black and white", "pros and cons".
BINOMIAL = re.compile(r'\b(?:black|salt|rock|pros|trial|research|bread|back|up|more|q|r|b|arts|terms|ladies|law|'
                      r'supply|cause|question|fish|mac|pen|cheese|peanut\s+butter)\s+(?:and|&)\s+(?:white|pepper|roll|cons|'
                      r'error|development|butter|forth|down|more|a|d|w|crafts|conditions|gentlemen|order|demand|effect|'
                      r'answers?|chips|cheese|paper|crackers|jelly)\b', re.I)
KEEP_AND = '\x00'
# The object of a request's head: "Weather in [Vienna]", "Convert 100 USD to [EUR]".
HEAD_OBJECT = re.compile(r"^(?P<head>.*\b(?:in|to|into|for|at)\s+)(?P<obj>[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})$",
                         re.I)
BARE = re.compile(r"[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2}")
NOT_OBJECT = {'it', 'that', 'this', 'them', 'there', 'me', 'us', 'you', 'him', 'her', 'now', 'today', 'tomorrow', 'please',
              'too', 'also', 'more', 'less', 'everything', 'something', 'anything', 'both', 'all'}


def coordinated(first: str, part: str) -> str | None:
    """"Weather in Vienna and Prague": a later part that is only another object ("Prague") gets the first part's head
    ("Weather in Prague"); None when the part has a head of its own or the two objects are one name ("Bosnia and
    Herzegovina")."""
    m = HEAD_OBJECT.match(first.strip(' ?.!'))
    obj = part.strip(' ?.!')
    if not m or not BARE.fullmatch(obj) or obj.lower() in NOT_OBJECT or m.group('obj').lower() in NOT_OBJECT:
        return None
    if not (obj[:1].isupper() or (len(obj) == 3 and obj.isalpha())) and not m.group('obj').islower():
        return None
    from .agents.tools import COUNTRY_NAME
    if f"{m.group('obj')} and {obj}".lower() in COUNTRY_NAME:
        return None
    return m.group('head') + obj


# "vienna weather, prague weather", "Paris time, London time": a comma list where every item names its own request.
# "weather in Paris, Texas" is one place: "Texas" names no request.
LIST_REQUEST = re.compile(r'\b(?:weather|forecast|temperature|time|exchange\s+rate|rate|price)\b', re.I)


def comma_list(query: str) -> list[str] | None:
    items = [p.strip(' ,.?!') for p in re.split(r'\s*,\s*(?:and\s+)?', query.strip(' ?.!'))]
    if len(items) < 2 or not all(len(p.split()) >= 2 and LIST_REQUEST.search(p) for p in items):
        return None
    return items[:MAX_SUBTASKS]


def candidate_split(query: str) -> list[str]:
    keep = lambda m: m.group(0).replace(' and ', f' {KEEP_AND} ').replace(' & ', f' {KEEP_AND} ')
    protected = BINOMIAL.sub(keep, OPERANDS.sub(keep, query))
    parts = [LEAD.sub('', p.strip(' ,.')).strip().replace(KEEP_AND, 'and') for p in SEP.split(protected)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return comma_list(query) or [query]
    parts = [parts[0], *((coordinated(parts[0], p) or p) for p in parts[1:])]
    if not all(substantial(p) for p in parts):
        return comma_list(query) or [query]
    if len(parts) > MAX_SUBTASKS:
        parts = parts[:MAX_SUBTASKS - 1] + [' and '.join(parts[MAX_SUBTASKS - 1:])]
    return parts


# A file request (docs/PLAN-files.md): a verb that makes something, then a file format. As the last part of a query
# ("research X and make slides about it", "weather in Paris, then put it in a PDF") it is a step of its own that waits
# for everything before it, so the file is built from those answers. No multi score is needed for this cue.
FILE_FORMATS = (r'pdf|docx?|word\s+(?:doc|docs|document|file)|slides?|slide\s*deck|deck|presentation|pptx|power\s*point|'
                r'spreadsheet|excel|xlsx|workbook|markdown|md\s+file')
FILE_VERBS = (r'make|put|turn|create|export|save|convert|generate|build|write|produce|prepare|draft|compile|format|'
              r'give\s+me|send\s+me|package')
FILE_REQUEST = re.compile(rf'\b(?:{FILE_VERBS})\b.*?(?<![\w.])(?:{FILE_FORMATS})\b', re.I | re.S)
FILE_SPLIT = re.compile(rf'^(?P<head>.*?\S)\s*(?:[;,]\s*|\s)(?:and\s+then|and|then|also|,)\s+(?P<file>(?:{FILE_VERBS})\b.*?'
                        rf'(?<![\w.])(?:{FILE_FORMATS})\b.*)$', re.I | re.S)
# "How do I open a file and save it as a PDF?" asks how, and wants an answer, not a file.
HOW_TO = re.compile(r'^\s*how\b|\bhow\s+(?:to|do|does|can|could|would|should)\b', re.I)


def is_file_request(text: str) -> bool:
    """True when the text asks for a file to be made ("put that in a PDF", "make slides about it")."""
    return bool(FILE_REQUEST.search(text or ''))


def split_file_request(query: str) -> tuple[str, str] | None:
    """(what comes first, the file request) when a query ends in a file request after other work, else None."""
    m = None if HOW_TO.search(query) else FILE_SPLIT.match(query.strip())
    if not m:
        return None
    head = m.group('head').strip(' ,;')
    return (head, m.group('file').strip(' .')) if len(head.split()) >= 2 else None


# A part that uses the part before it: "...then convert the result to EUR", "...and double it".
RESULT_REF = re.compile(r'\b(?:the|that|this)\s+(?:result|answer|total|sum|amount|number|figure|value)\b|'
                        r'^(?:then\s+|and\s+)?(?:convert|divide|multiply|add|subtract|double|halve|triple|square)\s+'
                        r'(?:it|that|this)\b', re.I)
# "this", "that", "it", "there", "these": a follow-up that points at the earlier turn is one request, never split.
DEICTIC = re.compile(r'\b(?:this|that|it|there|these|those)\b', re.I)


def one_file(parts: list[str]) -> bool:
    """True when the parts are one file request and what it should look like ("pdf on AI with diagrams also 12-13 pages
    also anthropic sans font"): every part names the file or only describes it (font, theme, pages, images, diagrams),
    at least one only describes it, and one format is named. Split, each describing part would be a step of its own
    that nothing can answer."""
    from .create.brief import parse_brief
    from .create.spec import detect_format
    formats, describes = set(), False
    for p in parts:
        b = parse_brief(p)
        fmt = b.format or detect_format(p)
        if fmt or is_file_request(p):
            formats.add(fmt)
        elif b.pages or b.slides or b.theme or b.font or b.images or b.diagrams or b.words:
            describes = True
        else:
            return False
    return describes and len(formats - {None}) == 1


# Keyless agents whose parser can tell one request from two of the same kind.
PARSED_SPLIT = ('weather', 'time', 'currency')


def parsed_split(query: str, parts: list[str]) -> bool:
    """"time in London and Paris", "Convert 100 USD to EUR and GBP": one keyless parser can't use the whole query (it
    names a second place or currency), yet it reads every part in full. Such a split is sure without Jev's multi score,
    which sees one kind of request and scores it as one."""
    return any(all(gate.confirmed(a, p) for p in parts) and not gate.confirmed(a, query) for a in PARSED_SPLIT)


async def plan_heuristic(query: str, jev, scores=None, follow_up: bool = False) -> dict:
    """scores: an optional cache of Jev's multi score by query text (jevrouter/cache.py); a hit sets `cached`.
    follow_up: the chat has earlier turns, so a query that points back at them (a deictic word) stays one step."""
    if split := split_file_request(query):
        head, file_part = split
        out = await plan_heuristic(head, jev, scores)
        if len(out['subtasks']) >= MAX_SUBTASKS:  # no room for the file step: what comes first stays one step
            out = {**out, 'subtasks': [head], 'deps': [[]]}
        n = len(out['subtasks'])
        deps = out.get('deps') or [[] for _ in range(n)]
        return {**out, 'subtasks': [*out['subtasks'], file_part], 'deps': [*deps, list(range(n))]}
    out = {'planner': 'heuristic', 'subtasks': [query], 'multi': None, 'jev_tokens': 0, 'claude_in': 0, 'claude_out': 0}
    parts = candidate_split(query)
    if len(parts) < 2 and ',' in query:
        # "vienna weather, prague weather": a bare comma splits only where every piece is a whole request of one kind
        pieces = [p.strip(' .') for p in query.split(',') if p.strip(' .')]
        parts = pieces if 1 < len(pieces) <= MAX_SUBTASKS and parsed_split(query, pieces) else parts
    if len(parts) < 2 or (follow_up and DEICTIC.search(query)) or one_file(parts):
        return out
    if parsed_split(query, parts):
        return {**out, 'subtasks': parts, 'deps': [[] for _ in parts], 'parsed': True}
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
        out['deps'] = [[i - 1] if i and (refers_back(p) or RESULT_REF.search(p)) else [] for i, p in enumerate(parts)]
    return out


# "from subtask 0", "the result of step 1": the plan's own numbering leaks into a step's text, where no user or agent
# can read it.
# Only a reference to the plan itself is replaced, in a step that depends on another: "the result of step 1", "from
# subtask 0", "subtask 2". "Explain step 3 of the TCP handshake" is the user's own content.
INDEX_REF = re.compile(r'\b(?:the\s+(?:result|answer|output)\s+(?:of|from)\s+(?:sub-?task|step)\s*#?(?P<a>\d+)'
                       r'|(?P<prep>from|in|use|using|with|by|after|on)\s+(?:sub-?task|step)\s*#?(?P<b>\d+)(?!\s+of\b)'
                       r'|sub-?task\s*#?(?P<c>\d+)(?!\s+of\b))\b', re.I)


def index_refs(text: str, steps: int) -> str:
    """The text with references to plan steps (numbered 0 or 1 up to `steps`) as "the previous answer"."""
    def repl(m):
        if int(m.group('a') or m.group('b') or m.group('c')) > steps:
            return m.group(0)
        return (m.group('prep') + ' ' if m.group('prep') else '') + 'the previous answer'
    return INDEX_REF.sub(repl, text)


def parse_steps(raw) -> tuple[list[str], list[list[int]]]:
    """Subtask texts and their dependencies. Accepts the old ["text", ...] shape. A dependency must point at an earlier
    subtask that survived (blank items are skipped and indices remapped), so the plan is always a DAG that runs in order."""
    texts, deps, kept = [], [], {}  # kept: the model's index -> ours
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        text, dep = (item, []) if isinstance(item, str) else (item.get('text'), item.get('depends_on')) if isinstance(item, dict) else (None, [])
        if not isinstance(text, str) or not text.strip() or len(texts) == MAX_SUBTASKS:
            continue
        kept[i] = len(texts)
        own = sorted({kept[d] for d in dep or [] if type(d) is int and d < i and d in kept})
        texts.append(index_refs(text.strip(), len(raw)) if own else text.strip())
        deps.append(own)
    return texts, deps


def prompt_for(query: str, context=None, files=None, forced: str | None = None) -> str:
    parts = []
    if context:
        parts.append('Earlier turns in this conversation (oldest first):\n' + '\n'.join(
            f'Q: {t["query"][:600]}\nA: {t["answer"][:600]}' for t in context))
    if files:
        parts.append('Attached files: ' + ', '.join(files) + ' (questions about them go to the document or data agent).')
    text = '\n\n'.join([*parts, f'Current query: {query}']) if parts else query
    if forced:
        text += (f'\n\nThe user picked @{forced}. Plan exactly one step for it; if the request also needs content '
                 'gathered first, make that a separate earlier step with no file, format, page, font, style or image '
                 'instructions in its text.')
    return text


async def plan_llm(query: str, engine, context=None, files=None, forced: str | None = None, web: bool = False) -> dict:
    reply = await engine.stream(system=system_for(web), prompt=prompt_for(query, context, files, forced), effort='low',
                                max_tokens=1024, schema=SCHEMA)
    subtasks, deps = parse_steps(parse_json(reply.text)['subtasks'])
    if not subtasks:
        raise ValueError('planner returned no subtasks')
    return {'planner': engine.name, 'subtasks': subtasks, 'deps': deps, 'multi': None, 'jev_tokens': 0,
            'claude_in': reply.input_tokens, 'claude_out': reply.output_tokens}


# Words that mean a query might hold several requests or refer back to an earlier part ("...then the time there").
# A comma only counts after two or more words, so "hey, how are you?" stays one request but "Paris weather, Tokyo
# time" does not. A leading greeting ("hi there,") is ignored for the same reason.
# "it" only as a real pronoun: "What time is it", "Is it safe", "Will it rain" hold a dummy subject.
MAYBE_MULTI = re.compile(r';|\S+\s+\S+,|\b(?:and|then|also|plus|after|there|its|that|those|them|same|both)\b'
                         r'|(?<!\bis )(?<!\bwill )(?<!\bwas )(?<!\bdoes )(?<!\bwould )\bit\b(?!\s+(?:is|was|will)\b)', re.I)
DUMMY_IT = re.compile(r"\b(?:what\s+time\s+is\s+it|is\s+it|will\s+it|was\s+it)\b", re.I)
GREETING = re.compile(r'^\s*(?:hi|hey|hello|yo|thanks|thank you|ok|okay)(?:\s+there)?\b[\s,!.]*', re.I)


def worth_llm_plan(query: str, context=None) -> bool:
    """An LLM plan costs seconds on a subscription CLI (it runs before any agent can start); single-clause queries don't
    need one, but a follow-up in a chat session does, because only the LLM planner can resolve it against earlier turns."""
    return bool(context) or bool(MAYBE_MULTI.search(DUMMY_IT.sub(' ', GREETING.sub('', query)))) or len(query.split()) > 14


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
    files, a long query, parts that may depend on each other ("...the time there", "...then which is better value",
    "...how old is he", "...and divide by 2"), a second question or a ";", a comma between clauses the text splitter
    left joined ("Paris weather, Tokyo time"), or a file request with other content in it. A single question is not
    one of these: "What time is it in New York?" never waits seconds for an LLM plan (A7)."""
    if context or files or len(query.split()) > LONG_QUERY:
        return True
    parts = candidate_split(query)
    if len(parts) >= 2:
        return depends(parts)
    # "convert 100 USD to INR and double it": a short tail the splitter keeps joined may still work on the head
    raw = [p for p in (LEAD.sub('', x.strip(' ,.')).strip() for x in SEP.split(query)) if p]
    if len(raw) >= 2 and depends(raw):
        return True
    q = GREETING.sub('', query)
    if q.count('?') >= 2 or ';' in q or re.search(r'\S+\s+\S+,\s*\S', q):
        return True
    return split_file_request(query) is None and is_file_request(query) and len(query.split()) > 8


def sure(p: dict) -> bool:
    """Jev's multi score is decisive either way (or a keyless parser settled the split), so the heuristic plan can stand
    without the LLM."""
    if p.get('parsed'):
        return True
    return p['multi'] is not None and (p['multi'] >= SURE_MULTI or p['multi'] <= SURE_SINGLE)


def kind(p: dict) -> str:
    """The RunTimings planner: 'llm', 'heuristic' (Jev's multi score was asked) or 'single' (no planner call at all)."""
    if p['planner'] == 'pinned':  # a plan given with the run (evals, docs/PLAN-accuracy-v2.md D3): no planner call
        return 'single'
    if p['planner'] != 'heuristic':
        return 'llm'
    return 'heuristic' if p['multi'] is not None else 'single'


async def plan(query: str, jev, engine=None, context=None, files=None, mode: str = 'balanced', on_llm=None,
               scores=None, forced: str | None = None, web: bool = False) -> dict:
    """context: earlier session turns [{query, answer}]; files: attached file names. The keyless planner ignores both.
    mode (docs/PLAN-speed-evals-chat.md): 'quick' never calls the LLM planner; 'deep' calls it whenever the query may
    hold several requests (the behaviour before the A2 skips); 'balanced' and 'research' let a sure heuristic plan stand.
    on_llm() is called just before the LLM planner starts, so the caller can route the whole query alongside it.
    scores: a cache of Jev's multi score by query text (the Router's; evals pass none).
    forced: the user's @agent, which the LLM planner plans exactly one step for (A1); web: the run's engine can search
    the web (without one, the planner is told no agent can)."""
    heuristic, safety = None, None
    follow_up = bool(context)
    if engine is not None and mode != 'quick' and worth_llm_plan(query, context):
        if mode != 'deep' and not needs_llm_plan(query, context, files):
            # The whole query's safety check runs alongside the multi score. The heuristic keeps every part as written,
            # and each part is routed (and blocked) on its own; a query that is unsafe as a whole still goes to the LLM
            # path below, whose unplanned_harm check makes sure the harmful part becomes a step of its own.
            heuristic, safety = await asyncio.gather(plan_heuristic(query, jev, scores, follow_up),
                                                     unsafe_score(jev, query), return_exceptions=True)
            if isinstance(heuristic, BaseException):
                heuristic = None
            elif not isinstance(safety, BaseException):
                heuristic['jev_tokens'] += safety[1]
                single = heuristic['multi'] is None and len(heuristic['subtasks']) == 1
                if (sure(heuristic) or single) and safety[0] < BLOCK_AT:
                    return with_deps(heuristic)
        if on_llm is not None:
            on_llm()
        # Jev's safety check on the whole query runs alongside the LLM planner, so it adds no wait.
        if safety is None or isinstance(safety, BaseException):
            llm, safety = await asyncio.gather(plan_llm(query, engine, context, files, forced, web),
                                               unsafe_score(jev, query), return_exceptions=True)
        else:  # already known from the heuristic check (its tokens are counted there)
            safety = (safety[0], 0)
            try:
                llm = await plan_llm(query, engine, context, files, forced, web)
            except Exception as e:
                llm = e
        p = None if isinstance(llm, BaseException) else llm  # refusal, API error, bad JSON: the heuristic still works
        if not isinstance(safety, BaseException) and safety[0] >= BLOCK_AT:
            # Never let an LLM rephrase an unsafe request: its rewrite ("slides on defending against phishing") can
            # route around the block. Plan from the literal text instead, so Jev judges each part as the user wrote it.
            return await literal_plan(query, jev, safety[1] + (heuristic or {}).get('jev_tokens', 0))
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
    return with_deps(heuristic or await plan_heuristic(query, jev, scores, follow_up))


async def literal_plan(query: str, jev, tokens: int = 0) -> dict:
    """The plan for a query Jev found unsafe as a whole: its parts exactly as written, so the safe ones are answered and
    the harmful ones blocked. If no single part scores unsafe (the harm is in how they combine), the whole query is one
    step, and it is blocked."""
    # Split finer than candidate_split: sentences too, and short fragments ("also build bombs") count, since Jev
    # judges every piece anyway.
    pieces = re.split(r'(?<=[.?!])\s+', query.strip())
    parts = [q for piece in pieces for q in (LEAD.sub('', x.strip(' ,.')).strip() for x in SEP.split(piece)) if q]
    parts = [p for p in parts if not re.fullmatch(r'(?i)(?:and|then|also|plus)\W*', p)][:MAX_SUBTASKS]
    if len(parts) > 1:
        try:
            scored = await asyncio.gather(*(unsafe_score(jev, t) for t in parts))
        except Exception:
            scored = []
        tokens += sum(n for _, n in scored)
        if not any(sc >= BLOCK_AT for sc, _ in scored):
            parts = [query]
    return {'planner': 'heuristic', 'subtasks': parts, 'deps': [[] for _ in parts], 'multi': None, 'jev_tokens': tokens,
            'claude_in': 0, 'claude_out': 0, 'literal': True}


def with_deps(out: dict) -> dict:
    return {**out, 'deps': out.get('deps') or [[] for _ in out['subtasks']]}


RESOLVE = ('Do not use tools. Rewrite the request below into one short, self-contained request by substituting the facts it '
           'refers to (names, places, numbers and units only) from the context of earlier steps. Output only the '
           'rewritten request, never an answer, and never copy sentences from the context. "There" or "its" means the '
           'place the earlier steps were about; after a currency conversion that is the country whose currency it was '
           'converted to (INR: India). If the context lacks the fact, output the request unchanged.')
SHINGLE = 8  # words: a rewrite sharing this many consecutive words with an earlier answer copied it


def shingles(text: str, n: int = SHINGLE) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def copies(text: str, upstream) -> bool:
    """True when the text holds a sentence taken from one of the upstream answers (a shared 8-word run)."""
    mine = shingles(text)
    return bool(mine) and any(mine & shingles(a) for a in upstream if a)


async def resolve_step(engine, text_with_context: str, *, step: str | None = None,
                       upstream=()) -> tuple[str, int, int]:
    """(self-contained text, tokens in, tokens out). Keyless agents parse places and amounts from plain text, so
    "the time in its capital" plus context must become "the current time in Buenos Aires" before they see it.
    step, upstream: the step's own text and the earlier answers. A rewrite more than 2.5 times the step's length, or one
    that copies a sentence of an earlier answer, pasted answer prose in ("the time in Sydney is ahead of London by..."):
    the step's own text is kept instead."""
    r = await engine.stream(system=RESOLVE, prompt=text_with_context, effort='low', max_tokens=256)
    text = r.text.strip().strip('"').splitlines()[0].strip() if r.text.strip() else ''
    if not text:
        raise ValueError('empty rewrite')
    if step is not None and (len(text) > 2.5 * max(len(step), 20) or copies(text, upstream)):
        return step, r.input_tokens, r.output_tokens
    return text[:500], r.input_tokens, r.output_tokens


# ---------- keyless follow-ups (docs/PLAN-accuracy-v2.md A4) ----------

# "switching topics", "new question": nothing carries over from the previous turn
TOPIC_SWITCH = re.compile(r'\b(?:switch(?:ing)?\s+(?:topics?|subjects?|gears)|change\s+of\s+(?:topic|subject)|new\s+(?:topic|'
                          r'question|subject)|different\s+(?:topic|question|subject)|unrelated|on\s+another\s+note|'
                          r'something\s+else)\b', re.I)
ELLIPTIC_LEAD = re.compile(r"^\s*(?:and\b|what\s+about\b|how\s+about\b|make\s+it\b|in\b|to\b|same\b|now\b|also\b)", re.I)
PRONOUN_REF = re.compile(r'\b(?:he|she|they|him|her|his|hers|its|their|it|there|that|this|these|those)\b', re.I)
OPERATION = re.compile(r'^\s*(?:and\s+|then\s+|now\s+)?(?P<op>times|multiplied\s+by|divided\s+by|plus|minus|over|'
                       r'to\s+the\s+power\s+of|[-+*/^x])\s*(?P<n>\d+(?:\.\d+)?)\s*\??$', re.I)
RESULT = r'(?:it|that|this|the\s+result|the\s+answer)'
END = r'\s*[?.!]?\s*$'
# "divide it by 3", "and subtract 10", "add 5 to it", "subtract it from 100": the whole follow-up is the operation
OPERATION_VERB = re.compile(rf'^\s*(?:and\s+|then\s+|now\s+)?(?P<verb>divide|multiply|add|subtract)\s+(?:(?P<pro>{RESULT})\s+)?'
                            rf'(?:(?P<prep>by|to|from|with)\s+)?(?P<n>\d+(?:\.\d+)?)(?:\s+(?:to|from)\s+{RESULT})?{END}', re.I)
# "now double that", "halve it", "square the result"
OPERATION_UNARY = re.compile(rf'^\s*(?:and\s+|then\s+|now\s+)?(?P<verb>double|triple|halve|square|cube)\s+{RESULT}{END}', re.I)
UNARY = {'double': '{v} * 2', 'triple': '{v} * 3', 'halve': '{v} / 2', 'square': '{v} ** 2', 'cube': '{v} ** 3'}


def self_contained(query: str) -> bool:
    """The text has every detail a keyless agent needs on its own ("convert 100 EUR to INR"), or it asks for a file:
    create reads the conversation itself, and the previous turn shown to Jev would pull the pick back to its agent
    ("Write markdown notes of our conversation" after the weather in Lisbon)."""
    return is_file_request(query) or any(gate.confirmed(a, query) for a in ('currency', 'math', 'weather', 'time'))


def elliptical(query: str) -> bool:
    """A follow-up that only makes sense with the previous turn: short, led by "and/what about/make it/in/to", or
    pointing back with a pronoun, and with no slot of its own."""
    q = query.strip()
    leans = len(q.split()) <= 6 or bool(ELLIPTIC_LEAD.match(q)) or bool(PRONOUN_REF.search(q))
    return leans and not self_contained(q)


# Words a weather or time follow-up may hold besides a place and a day ("and what time is it there?", "how about in
# Rome tomorrow?"). Any other word is a request of its own, which the rewrite would drop: "Now tell me how to make a
# bomb" after the weather in Rome must never become "Weather in Rome".
FOLLOW_UP_WORDS = {
    'and', 'then', 'now', 'so', 'also', 'ok', 'okay', 'what', "what's", 'whats', 'about', 'how', "how's", 'hows', 'in',
    'at', 'for', 'of', 'on', 'there', 'here', 'it', "it's", 'its', 'that', 'this', 'is', 'be', 'will', 'the', 'a',
    'same', 'instead', 'please', 'city', 'place', 'today', 'tomorrow', 'like', 'right', 'current', 'currently', 'local',
    'time', 'weather', 'forecast', 'temperature', 'rain', 'raining', 'snow', 'snowing', 'sunny', 'hot', 'cold', 'humid',
    'windy', 'clock', 'date', 'day', 'hour', 'going', 'to', 'do', 'does', 'can', 'you', 'check', 'tell', 'me',
}


# ... and what a currency follow-up may hold besides amounts and currencies ("make it 500", "what about in pounds?")
CURRENCY_FOLLOW_UP_WORDS = FOLLOW_UP_WORDS | {
    'make', 'convert', 'exchange', 'into', 'from', 'much', 'many', 'amount', 'rate', 'rates', 'change', 'try', 'with',
    'as', 'but', 'use', 'using', 'would', 'give', 'show', 'want', 'i', 'money',
}


# "in US dollars", "to Hong Kong dollars": the nationality words of a currency's name
DEMONYM_WORDS = {w for d in DEMONYM_CURRENCY for w in d.split()}


def covers_follow_up(query: str, rewrite: str, allowed: set[str] = FOLLOW_UP_WORDS) -> bool:
    """Every word of a follow-up is a follow-up word or is kept by the rewrite (the new place, currency or amount)."""
    kept = {w.lower() for w in WORD.findall(rewrite)}
    return all(w.lower() in allowed or w.lower() in kept for w in WORD.findall(query))


# A pronoun that points at nothing: "what time is it", "is it raining", "is it true that...", "will it rain"
DUMMY_PRONOUN = re.compile(r"\bwhat\s+time\s+is\s+it\b|^\W*(?:is|was|will|does|did|would|isn't|wasn't)\s+it\b|"
                           r"\bit(?:'s|\s+is|\s+was)?\s+(?:true|possible|likely|raining|snowing|worth|necessary|okay|ok|"
                           r"safe|better|best|time|late|early)\b", re.I)
TOPIC_PRONOUN = re.compile(r'\b(?:he|she|they|him|her|his|hers|its|their|it)\b', re.I)


def topic_follow_up(q: str, topic: str) -> str | None:
    """"When was she born?" after "Who was Marie Curie?" -> "When was Marie Curie born?". Only a lone short question
    whose one pronoun has no subject of its own takes the topic: never a dummy "it" ("What time is it?", "Is it
    raining?"), never one that asks for the time or weather, names someone of its own, or goes on to a clause of its
    own ("...and why is it constant?")."""
    if gate.TIME_WORDS.search(q) or gate.WEATHER_WORDS.search(q) or DUMMY_PRONOUN.search(q):
        return None
    if len(q.split()) > 10 or len([x for x in re.split(r'[.?!;]+', q) if x.strip()]) > 1:
        return None
    body = re.sub(r'^\W*(?:(?:and|so|then|also|ok|okay|now)\b\W*)+', '', q, flags=re.I)
    if re.search(r'\b(?:and|but|or|also|because|that|which|while|if)\b', body, re.I):
        return None
    if {w for w in anchors(q) if w not in ('I',)}:
        return None
    hits = list(TOPIC_PRONOUN.finditer(q))
    if len(hits) != 1:
        return None
    m = hits[0]
    word = m.group(0).lower()
    name = f"{topic}'s" if word in ('his', 'hers', 'its', 'their') else topic
    if word == 'her' and not re.search(r'\bher\s+(?:\w+\s+)?(?:is|was|were|born|died)\b', q, re.I) and \
            re.search(r'\bher\s+[a-z]+\b', q, re.I):
        name = f"{topic}'s"
    return q[:m.start()] + name + q[m.end():]


def fill_from_frame(query: str, frame: dict | None) -> str | None:
    """The follow-up rewritten from the previous turn's frame ({agent, slots}), or None when it can't be (or needn't
    be). "make it 500" after "Convert 50 EUR to USD" -> "Convert 500 EUR to USD"; "and in GBP?" -> "Convert 50 EUR to
    GBP"; "And what time is it there?" after the weather in Lisbon -> "What time is it in Lisbon?". A rewrite for a
    keyless agent is kept only when that agent's parser confirms it; a topic switch inherits nothing."""
    from .agents.tools import AMOUNT, ECB_CODES, currencies_in, currency_words, plain
    # a request that stands on its own ("convert 100 EUR to INR", "is it raining in Oslo?") takes nothing from the frame
    if not frame or not query.strip() or TOPIC_SWITCH.search(query) or not elliptical(query):
        return None
    agent, slots = frame.get('agent'), frame.get('slots') or {}
    q = query.strip()
    target = ('time' if gate.TIME_WORDS.search(q) and not gate.WEATHER_WORDS.search(q)
              else 'weather' if gate.WEATHER_WORDS.search(q) else agent)
    # it must lean on the previous turn: "and tomorrow?", "what about Rome", "the time there", "make it 500"
    leans = bool(ELLIPTIC_LEAD.match(q) or PRONOUN_REF.search(q) or len(q.split()) <= 3)
    place = slots.get('city') or slots.get('zone')
    if not place and target in ('time', 'weather') and (topic := slots.get('topic')):
        # "What's the weather there?" after "What's the capital of Kenya?": the place the previous turn was about
        probe = f'weather in the {topic}'
        resolved = gate.resolve_described(probe)
        place = resolved[len('weather in '):] if resolved and resolved.lower().startswith('weather in ') else None
    out = None
    if not leans and target == agent:
        return None
    if target in ('time', 'weather') and place:
        new_place = gate.place_in(q) or (None if PRONOUN_REF.search(q) else gate.find_place(q))
        where = new_place or place
        when = ' tomorrow' if re.search(r'\btomorrow\b', q, re.I) else ''
        out = f'What time is it in {where}?' if target == 'time' else f'Weather in {where}{when}'
        if not covers_follow_up(q, out):
            return None
    elif target == 'currency' and agent == 'currency' and {'amount', 'from'} <= set(slots):
        words = currency_words(q)
        num = next((i for i, w in enumerate(words) if AMOUNT.fullmatch(w)), None)
        found = currencies_in(words, ECB_CODES)
        if num is None and not found:
            return None
        src, dst = slots['from'], slots.get('to')  # no target yet when the previous turn asked for it
        amt = words[num] if num is not None else plain(float(slots['amount']))
        for i, code in found:
            if num is not None and 0 < i - num <= 2:
                src = code  # "what about 200 GBP?": the new amount's own currency
            else:
                dst = code  # "and in GBP?": a new target
        if not dst or src == dst:
            return None
        out = f'Convert {amt} {src} to {dst}'
        # every word must be an amount, a currency or a follow-up word: "and in GBP? also who won the 2018 World Cup"
        # is a request of its own, planned and judged as written
        if not all(w.lower() in CURRENCY_FOLLOW_UP_WORDS or w.lower() in DEMONYM_WORDS or currencies_in([w], ECB_CODES)
                   for w in WORD.findall(q)) \
                or len(AMOUNT.findall(' '.join(words))) > 1:
            return None
    elif target == 'units' and agent == 'units' and {'amount', 'from'} <= set(slots):
        # "and how many metres is that?" after "How many kilometres is 10 miles?": the same amount in a new unit
        from .agents.tools import UNIT
        m = re.search(rf'\b(?:how\s+many|in|into|to|as)\s+({UNIT})', q, re.I)
        if not m or AMOUNT.search(q) or not all(w.lower() in CURRENCY_FOLLOW_UP_WORDS or w.lower() in m.group(1).lower()
                                                for w in WORD.findall(q)):
            return None
        out = f"Convert {plain(float(slots['amount']))} {slots['from']} to {m.group(1)}"
    elif target == 'math' and agent == 'math' and 'value' in slots:
        value = plain(float(slots['value']))
        if m := OPERATION.match(q):
            out = f"{value} {m.group('op')} {m.group('n')}"
        elif m := OPERATION_VERB.match(q):
            verb, n = m.group('verb').lower(), m.group('n')
            if verb == 'subtract' and m.group('pro') and m.group('prep') == 'from':
                out = f'{n} - {value}'  # "subtract it from 100"
            else:
                out = {'divide': f'{value} / {n}', 'multiply': f'{value} * {n}', 'add': f'{value} + {n}',
                       'subtract': f'{value} - {n}'}[verb]
        elif m := OPERATION_UNARY.match(q):
            out = UNARY[m.group('verb').lower()].format(v=value)
    elif agent not in ('math', 'weather', 'time', 'currency', 'units', 'dates') and (topic := slots.get('topic')):
        # "When was she born?" after "Who was Marie Curie?": the pronoun is the previous turn's topic
        return topic_follow_up(q, topic)
    if out is None:
        return None
    return out if gate.confirmed(target, out) else None


def expand_described(text: str) -> list[tuple[str, list[int]]]:
    """A keyless request whose place or currency is named only by a description the static tables can't settle, as two
    steps: a lookup, then the request with depends_on [0] ("Weather in William Shakespeare's birthplace" -> "What is
    William Shakespeare's birthplace? Give only the name." then the weather). Anything else stays one step."""
    kind = gate.described(text)
    keyless = gate.WEATHER_WORDS.search(text) or gate.TIME_WORDS.search(text) or re.search(
        r'\b(?:convert|exchange|currency|money)\b', text, re.I)
    if not kind or not keyless or gate.resolve_described(text):
        return [(text, [])]
    rx = gate.DESCRIBED_CURRENCY if kind == 'described currency' else gate.DESCRIBED_PLACE
    m = rx.search(text)
    if m is None:  # "Which country hosted the 2016 Olympics? Convert 100 EUR into its currency": what comes before asks
        ref = gate.currency_ref(text)
        before = text[:ref.start()] if ref else ''
        ask = re.split(r'(?<=[?.!])\s+', before.strip())[0] if re.search(r'[?.!]\s', before) else ''
        return [(f'{ask} Give only the currency code.', []), (text, [0])] if ask else [(text, [])]
    tail = re.split(r'[?.!,;]', text[m.start():], maxsplit=1)[0].strip()
    head = text[:m.start()]
    # "in William Shakespeare's birthplace": the possessive owner comes before the match
    owner = re.search(r"((?:[A-Z][A-Za-z.'-]*\s+){0,3}[A-Z][A-Za-z.'-]*'s\s*)$", head)
    phrase = ((owner.group(1) if owner else '') + tail).strip()
    what = 'currency code' if kind == 'described currency' else 'place name'
    return [(f'What is {phrase}? Give only the {what}.', []), (text, [0])]
