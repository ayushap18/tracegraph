"""Checks around Jev's route decision, applied to every subtask on every engine (keyless too):

- cant_do: actions this app cannot perform (bookings, reminders, messages, purchases) get an honest "I can't" instead of
  whatever agent Jev picked ("Remind me at 5pm" used to return the current time).
- confirmed: when Jev's clear/confidence gate says clarify but its top pick is an exact keyless agent whose parser finds
  everything it needs in the text ("What time is it in UTC+5:30?"), that agent runs.
- question: a missing detail an agent can't act without (a conversion with one currency, weather with no place) asks
  the user for it instead of failing.
- meanings: a lone ambiguous term ("Mercury") asks which meaning, using the disambiguation page behind the knowledge
  agent's own source.
- clarify_text: every clarify message is plain English with concrete options; it never names internal agents.
"""
import re
from urllib.parse import quote, unquote

from .agents.tools import (CURRENCY_PLACE, ECB_CODES, currency_question, find_place, get_json, parse_currency, parse_zone,
                           place_in, solve_math)

# ---------- actions we can't perform ----------

POLITE = re.compile(r"^(?:(?:hey|hi|hello|ok|okay|so)\b[\s,!]*)?(?:please\s+)?(?:(?:can|could|would|will) you\s+(?:please\s+)?"
                    r"|i (?:want|need|would like|'d like) (?:you )?to\s+|go ahead and\s+|help me\s+)?(?:please\s+)?", re.I)
# (pattern on the request with polite openers removed, what we can't do). Imperatives aimed at a thing to book, buy,
# set or send, or a person to contact: "how do I book a flight" is a question the knowledge agent can answer, "book me a
# flight" is an action, and "Book of Mormon summary" or "Order the planets by size" are neither.
BOOKABLE = (r'(?:(?:return|one-way|round-trip|cheap|direct|window|aisle|double|single|private|hotel|train|bus|plane|'
            r'movie|concert)\s+)?(?:flights?|hotels?|tables?|tickets?|rooms?|cabs?|taxis?|seats?|trains?|bus(?:es)?|'
            r'cars?|rides?|uber|lyft|stay|trip|holiday|vacation|reservation|appointment|spot|slot|airbnb)\b')
QUANTITY = r'(?:(?:a|an|some|one|two|three|four|five|\d+|a few|a couple of)\s+)?'
PERSON = (r'(?:me|us|him|her|them|mom|mum|dad|my\s+(?:mom|mum|dad|mother|father|boss|wife|husband|friend|brother|sister|'
          r'son|daughter|partner|manager|team|doctor|landlord|colleague|girlfriend|boyfriend|grandma|grandpa|parents|'
          r'family|kids?|neighbou?r|coworker|teacher|client|lawyer)s?|(?-i:[A-Z][a-z]+)(?!\s+(?:API|Stack|function)))\b')
CANT = [
    (re.compile(r'^remind\s+(?:me|us)\b(?!\s*[:?,])(?!\s+(?:what|who|how|which|why|where|when|of|about)\b)'
                r'|^(?:alert|notify|ping|wake)\s+(?:me|us)\b(?!\s*[:?,])'
                r'|^(?:set|create|make|start|schedule|put on)\s+(?:up\s+)?(?:a|an|my|the|one)?\s*'
                r'(?:\d+[\s-]*(?:min(?:ute)?s?|h(?:ou)?rs?|sec(?:ond)?s?|hours?)\s+)?(?:reminders?|alarms?|timers?|countdowns?)\b'
                r'(?=\s*[.!]?$|\s+(?:for|at|in|on|to|every|tomorrow|tonight|that|so|when)\b)', re.I),
     'set reminders, alarms or timers'),
    (re.compile(rf'^(?:book|reserve)\s+(?:(?:me|us)\s+)?{QUANTITY}(?:(?:the|my|our)\s+)?{BOOKABLE}'
                rf'|^(?:get|find)\s+(?:me|us)\s+{QUANTITY}(?:flights?|tickets?|cabs?|taxis?|uber|lyft|rides?)\b'
                rf'|^i\s+need\s+(?:a|an)\s+(?:flight|cab|taxi|uber|lyft|ride)\b'
                rf'|^(?:call|get|order)\s+(?:me\s+|us\s+)?(?:a|an)\s+(?:cab|taxi|uber|lyft|ride)\b', re.I),
     'make bookings or reservations'),
    (re.compile(rf'^(?:order|buy|purchase)\s+(?:(?:me|us)\s+)?{QUANTITY}(?!(?:the|these|those|them|it|this|that|by|of|in|'
                r'from|to|and|or|numbers?|lists?|items?|words?|elements?|planets?|values?|results?)\b)[a-z]{2,}', re.I),
     'place orders or buy things'),
    (re.compile(rf'^(?:send|email|e-mail|text|message|dm|whatsapp|call|phone|ring|facetime)\s+'
                rf'(?:(?:a|an)\s+(?:email|e-mail|text|message|sms|dm|whatsapp)\s+to\s+)?{PERSON}'
                r'|^(?:send|write)\s+(?:a|an)\s+(?:email|e-mail|text|message|sms|dm|whatsapp)\s+to\b', re.I),
     'send messages or make calls'),
    (re.compile(r'^(?:schedule|set up|arrange)\s+(?:a|an|my|the)?\s*(?:meeting|appointment|call|event)\b'
                r'|^(?:add|put)\b.*\b(?:to|in|on|into)\s+(?:my|the)\s+calendar\b'
                r'|^(?:add|put|create)\s+(?:a|an)\s+(?:meeting|appointment|event)\s+(?:with|for|at|on|tomorrow|today|next)\b', re.I),
     'manage your calendar'),
    (re.compile(r'^(?:pay|transfer|wire|send|venmo)\s+(?:(?:my|the)\s+(?:rent|bill|invoice)|money\b|(?:\$|€|£|₹)\s*\d'
                r'|\d[\d,.]*\s*(?:k\s+)?(?:dollars?|euros?|pounds?|rupees?|bucks|usd|eur|gbp|inr)?\s+(?:to|into)\b'
                r'|(?-i:[A-Z][a-z]+)\s+(?:\$|€|£|₹)?\d)', re.I), 'make payments or move money'),
]
# Requests that only look like actions: code ("make a timer in javascript"), media ("... lyrics"), definitions, advice
# between options ("Buy a house or rent?"), sorting ("order the planets by size").
NOT_ACTION = re.compile(
    r'\b(?:in|with|using|for)\s+(?:python|javascript|js|typescript|ts|java|c\+\+|c#|golang|go|rust|ruby|php|swift|kotlin|'
    r'bash|shell|sql|html|css|react|node(?:\.js)?|windows|linux|macos|excel|markdown|unity|django|flask)\b'
    r'|\b(?:lyrics|song|meaning|summary|recommendations?|etiquette|strategy|explained|template|tutorial|example|'
    r'tips|ideas|api|function|method|endpoint|script|code|program|class|recursion)\b'
    r'|\bor\b[^?]*\?\s*$|\balphabetically\b|\bby\s+(?:size|date|name|length|value|price)\b', re.I)
# Jev picked the time agent for a request to be told something later ("ping me at 5pm", "timer for 5 minutes"): the time
# agent would only report the current time.
LATER = re.compile(r'\b(?:remind|reminder|alert|notify|ping|timer|alarm|wake\s+(?:me|us)|countdown)\b', re.I)
QUESTION = re.compile(r'^(?:what|who|how|why|when|which|where|is|are|does|do|did|can|could|should|will|was)\b', re.I)
CAPABILITIES = 'I can do calculations, look up weather, times, exchange rates and facts, and help with code'
# The built-in agents an action request may be mistaken for. A pick of code (and run, report, a file or a custom agent)
# means Jev read it as something those can do ("Make a timer in javascript", a custom email drafter), so it stands.
ACTION_PICKS = {'time', 'weather', 'currency', 'math', 'knowledge', 'chat', 'research'}
# ...and a question-answering pick Jev is this sure of stands too ("Purchase price allocation" is a finance question).
SURE_PICKS, SURE_AT = {'knowledge', 'research', 'math'}, 0.9


def cant_do(text: str, pick: str | None = None, confidence: float = 0.0) -> tuple[str, str] | None:
    """(what, answer) for a request to perform an action this app has no way to do, else None. pick, confidence: Jev's
    top agent and its probability; only a built-in agent that could be mistaken for an action is second-guessed."""
    if pick is not None and (pick not in ACTION_PICKS or (pick in SURE_PICKS and confidence >= SURE_AT)):
        return None
    t = POLITE.sub('', text.strip(), count=1)
    if NOT_ACTION.search(t):
        return None
    for pattern, what in CANT:
        if pattern.search(t):
            return what, f"Sorry, I can't {what}: I'm not able to take actions for you. {CAPABILITIES}."
    if pick == 'time' and LATER.search(t) and not QUESTION.match(t):
        what = 'set reminders, alarms or timers'
        return what, f"Sorry, I can't {what}: I'm not able to take actions for you. {CAPABILITIES}."
    return None


# ---------- keyless parsers that can confirm a pick ----------

TIME_WORDS = re.compile(r'\b(?:time|clock|date|day|hour)\b', re.I)
WEATHER_WORDS = re.compile(r'\b(?:weather|forecast|rain|raining|snow|snowing|sunny|temperature|hot|cold|humid|windy)\b', re.I)


def confirmed(agent: str, text: str) -> bool:
    """True when the keyless agent's own parser finds every detail it needs in the text."""
    try:
        if agent == 'time':
            return bool(TIME_WORDS.search(text) and (parse_zone(text) or place_in(text)))
        if agent == 'weather':
            return bool(WEATHER_WORDS.search(text) and place_in(text))
        if agent == 'currency':
            return not isinstance(parse_currency(text, ECB_CODES), str)
        if agent == 'math':
            solved = solve_math(text)
            return bool(solved and re.search(r'[-+*/%]|sqrt|log|ln|sin|cos|tan', solved[0]))
    except Exception:
        return False
    return False


def question(agent: str, text: str) -> str | None:
    """A follow-up question when the agent can't act without a detail the text lacks, else None."""
    if agent == 'currency':
        return currency_question(text)
    if agent == 'weather' and not find_place(text):
        return 'Which city or place do you want the weather for?'
    return None


# ---------- lone ambiguous terms ----------

REQUEST_WORDS = {'what', 'who', 'where', 'when', 'why', 'how', 'which', 'is', 'are', 'was', 'tell', 'explain', 'define',
                 'show', 'give', 'find', 'weather', 'time', 'convert', 'calculate', 'hi', 'hello', 'hey', 'thanks', 'thank',
                 'yes', 'no', 'ok', 'okay', 'please', 'help'}


def bare_term(text: str) -> str | None:
    """The term, when the whole request is one to three words naming a thing and asking nothing about it."""
    t = text.strip().rstrip('.!?').strip()
    words = t.split()
    if not 1 <= len(words) <= 3 or not all(re.fullmatch(r"[A-Za-z][A-Za-z'-]*", w) for w in words):
        return None
    return None if {w.lower() for w in words} & REQUEST_WORDS else t


async def meanings(http, term: str) -> list[str] | None:
    """Up to four meanings of a term with no main one, else None. Wikipedia first: its page type says outright whether
    the term is a disambiguation page, and it answers when DuckDuckGo is throttling. DuckDuckGo is the fallback."""
    if http is None:
        return None
    try:
        return await wiki_meanings(http, term)
    except Exception:
        return await ddg_meanings(http, term)


async def wiki_meanings(http, term: str) -> list[str] | None:
    """Raises when Wikipedia can't be reached, so the caller can fall back. "Mercury" is a disambiguation page and its
    "Mercury (...)" titles are the meanings, most searched first; "Java" or "Einstein" lead to a standard page: None."""
    page = await get_json(http, 'https://en.wikipedia.org/api/rest_v1/page/summary/' + quote(term.replace(' ', '_')))
    if not isinstance(page, dict) or 'type' not in page:
        raise ValueError('no summary')
    if page['type'] != 'disambiguation':
        return None
    found = await get_json(http, 'https://en.wikipedia.org/w/api.php', action='opensearch', search=term + ' (', limit=10,
                           namespace=0, format='json')
    prefix = term.casefold() + ' ('
    titles = [t for t in (found[1] if isinstance(found, list) and len(found) > 1 else [])
              if isinstance(t, str) and t.casefold().startswith(prefix) and not t.endswith('(disambiguation)')]
    return titles[:4] if len(titles) >= 2 else None


async def ddg_meanings(http, term: str) -> list[str] | None:
    """Up to four meanings of a term with no main one, from DuckDuckGo's disambiguation data (Wikipedia's), else None.
    A term with a main topic ("Java" the island, "Paris" the city) has an entry titled exactly as the term, so it is
    answered as that; "Mercury" (planet, element, god) and "Python" (language, snake, comedy troupe) have none."""
    if http is None:
        return None
    try:
        d = await get_json(http, 'https://api.duckduckgo.com/', q=term, format='json', no_html=1)
    except Exception:
        return None
    # A throttled DuckDuckGo answers 202 or 200 with an empty body (None) or HTML: no page, so nothing to ask about.
    if not isinstance(d, dict) or d.get('Type') != 'D':
        return None
    titles = []
    for topic in d.get('RelatedTopics') or []:
        url = topic.get('FirstURL') if isinstance(topic, dict) else None
        if url:  # grouped topics ("Companies", "Computing") have no URL of their own
            titles.append(re.sub(r'\s+', ' ', unquote(url.rstrip('/').rsplit('/', 1)[-1]).replace('_', ' ')).strip())
    titles = list(dict.fromkeys(t for t in titles if t))
    if any(t.casefold() == term.casefold() for t in titles) or (titles and main_person(term, titles[0])):
        return None
    return titles[:4] if len(titles) >= 2 else None


def main_person(term: str, title: str) -> bool:
    """True when the page's first entry is a person the term is the surname of ("Einstein" -> Albert Einstein,
    "Shakespeare" -> William Shakespeare): Wikipedia lists the main meaning of a surname first, as "Firstname Term"."""
    words = title.split()
    return (2 <= len(words) <= 4 and ' '.join(words[-len(term.split()):]).casefold() == term.casefold()
            and all(re.fullmatch(r"[A-Z][A-Za-z.'-]*", w) for w in words))


def meanings_text(term: str, options: list[str]) -> str:
    return f'"{term}" can mean several things: {join_or(options)}. Which one do you mean?'


# ---------- clarify messages ----------

# What each agent does, as an option a user can pick. Unknown agents (custom, files) use their description.
OPTIONS = {'math': 'a calculation', 'weather': 'the weather somewhere', 'time': 'the time or date somewhere',
           'currency': 'converting money between currencies', 'knowledge': 'facts about something',
           'code': 'help with programming', 'chat': 'just a chat', 'research': 'recent news or a web search',
           'report': 'a written report', 'run': 'running some code', 'document': 'something in your attached document',
           'data': 'something in your attached data file'}
# The detail each agent needs, asked when it is the clear favourite.
NEEDS = {'math': 'What calculation should I do? For example "18% of 2450".',
         'weather': 'Which city or place do you want the weather for?',
         'time': 'Which city or time zone do you want the time for?',
         'currency': 'Which currencies do you mean? For example "100 USD to INR" or "the EUR to GBP rate".',
         'knowledge': 'Who or what would you like to know about?',
         'code': 'Which language, and what are you trying to do?'}


def join_or(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' or ' + items[-1]


def option(agent: str, descriptions: dict | None = None) -> str:
    if agent in OPTIONS:
        return OPTIONS[agent]
    d = (descriptions or {}).get(agent)
    d = d.get('description') if isinstance(d, dict) else d
    return (d[:1].lower() + d[1:]).rstrip('.') if isinstance(d, str) and d else 'something else'


def clarify_text(probabilities: dict, text: str = '', descriptions: dict | None = None) -> str:
    """A plain-English follow-up question built from Jev's route probabilities: the favourite's missing detail when one
    agent clearly leads, otherwise the two or three likeliest readings as options. Never names an agent."""
    ranked = [a for a, p in sorted(probabilities.items(), key=lambda kv: -kv[1]) if a not in ('clarify', 'blocked')]
    if not ranked:
        return 'Could you add a bit more detail about what you need?'
    top = ranked[0]
    # The favourite's own missing detail ("a pound": money or weight?) beats a generic list of readings.
    p_top = probabilities.get(top, 0)
    ask = (question(top, text) if p_top >= 0.3 else None) or (NEEDS.get(top) if p_top >= 0.6 else None)
    if ask and p_top < 0.6:
        return ask
    if ask:  # a clear favourite: its missing detail, then the other readings Jev gave any weight
        others = list(dict.fromkeys(option(a, descriptions) for a in ranked[1:3] if probabilities.get(a, 0) >= 0.05))
        return f'{ask} Or did you mean {join_or(others)}?' if others else f'{ask} If you meant something else, add a bit more detail.'
    picks = [a for a in ranked[:3] if probabilities.get(a, 0) >= 0.05] or ranked[:2]
    if len(picks) == 1:
        picks = ranked[:2]
    opts = list(dict.fromkeys(option(a, descriptions) for a in picks))
    return f'Could you add a bit more detail? Do you mean {join_or(opts)}?'


AGENT_NAME = re.compile(r'\bagents?\b', re.I)
CLARIFY_SYSTEM = ('Do not use tools. The user\'s request below is too ambiguous to answer. Write ONE short, friendly '
                  'follow-up question (under 40 words) that offers two or three concrete interpretations of what is '
                  'actually ambiguous in it, e.g. "Do you mean an exchange rate or an interest rate?". Plain English only: '
                  'never mention agents, routing, classifiers or this system. Do not answer the request.')


async def clarify_llm(engine, text: str, fallback: str) -> tuple[str, int, int]:
    """(question, tokens in, tokens out): the engine writes the question; the template is kept if it fails or leaks."""
    try:
        r = await engine.stream(system=CLARIFY_SYSTEM, prompt=f'Request: {text[:600]}\n\nA fallback question: {fallback}',
                                effort='low', max_tokens=200)
    except Exception:
        return fallback, 0, 0
    q = (r.text or '').strip()
    if not q or len(q) > 400 or AGENT_NAME.search(q):
        return fallback, r.input_tokens, r.output_tokens
    return q, r.input_tokens, r.output_tokens


# ---------- follow-up references ("the time there") ----------

THERE = re.compile(r'\b(?:over\s+)?there\b(?!\s+(?:is|are|was|were|be|been|isn\'t|aren\'t|any|anything)\b)', re.I)
EXISTENTIAL = re.compile(r'\b(?:is|are|was|were|any)\s+there\b', re.I)
REFERS_BACK = re.compile(r'\bthere\b|\bits\b|\btheir\b|\b(?:that|this|the same)\s+(?:city|country|place|town|capital|'
                         r'currency|amount|result|answer|number)\b', re.I)


def refers_back(text: str) -> bool:
    """True when a part of a split query points at an earlier part ("...then what time is it there")."""
    return bool(REFERS_BACK.search(EXISTENTIAL.sub('', text)))


def referent_place(texts: list[str]) -> str | None:
    """The place earlier steps were about, newest first: one named after in/at/for, else where the currency converted
    to is used ("convert 50 EUR to INR" -> New Delhi, India)."""
    for t in reversed(texts):
        if place := place_in(t):
            return place
        parsed = parse_currency(t, ECB_CODES)
        if not isinstance(parsed, str) and parsed[2] in CURRENCY_PLACE:
            return CURRENCY_PLACE[parsed[2]]
    return None


def resolve_there(text: str, earlier: list[str]) -> str | None:
    """The text with a locative "there" replaced by the place the earlier steps were about, or None if there's nothing
    to resolve. Keyless agents parse places from plain text, so "the time there" must name the place."""
    if EXISTENTIAL.search(text) or not THERE.search(text):
        return None
    place = referent_place(earlier)
    return THERE.sub(f'in {place}', text, count=1) if place else None


# ---------- asking for a file ----------

FILE_INTENT = re.compile(
    r"\b(?:turn|make|create|export|save|put|write|generate|convert|build)\b[^.?!]{0,80}?\b(?:into|as|in(?:to)?|to)\s+"
    r"(?:an?\s+|the\s+)?(?:[\w-]+\s+){0,3}?(?:file|document|doc|pdf|docx|word\s+(?:file|doc(?:ument)?)|pptx|slides?|"
    r"slide\s*deck|deck|presentation|powerpoint|xlsx|excel(?:\s+(?:file|sheet|workbook))?|spreadsheet|workbook|"
    r"markdown|md)\b", re.I)


# "make a file from this table", "create a Word document", "generate me a PDF": the file is the direct object
FILE_OBJECT = re.compile(
    r"\b(?:make|create|generate|build|export|produce|give)\s+(?:me\s+)?(?:an?|the|this|that|one)\s+(?:[\w-]+\s+){0,2}?"
    r"(?:file|document|doc|pdf|docx|pptx|xlsx|spreadsheet|workbook|slide\s*deck|deck|presentation|markdown\s+file)\b", re.I)


def wants_file(text: str) -> bool:
    """A request to produce a file ("turn these notes into a summary file", "save this as a PDF"), as opposed to a
    question about one ("what does this file say?")."""
    if re.match(r"\s*(?:how\s+(?:do|can|would|should)\s+(?:i|you|we|one)|how\s+to|what(?:'s| is)\s+the\s+best\s+way)\b",
                text, re.I):
        return False  # "how do I convert a docx to pdf?" asks for steps, not a file
    return bool(FILE_INTENT.search(text) or FILE_OBJECT.search(text))
