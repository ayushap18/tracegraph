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
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, unquote

from .agents.tools import (AMOUNT, CAPITAL_OF, COUNTRY_NAME, CURRENCY_OF, CURRENCY_PLACE, ECB_CODES, currencies_in,
                           currency_question, currency_words, date_maths, dates_in, find_place, get_json, math_spans,
                           math_text, parse_currency, parse_units, parse_zone, place_in, solve_math)

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
    """True when the keyless agent's own parser finds every detail it needs in the text, and uses every constraint and
    number the text gives (parse_for(...).full): "weather in Madrid next year" names a place but asks for a date the
    forecast can't reach, so it is not confirmed."""
    try:
        p = parse_for(agent, text)
        if p is None or not p.slots or not p.full:
            return False
        if agent == 'time':
            return bool(TIME_WORDS.search(text) and (parse_zone(text) or place_in(text)))
        if agent == 'weather':
            return bool(WEATHER_WORDS.search(text) and place_in(text))
        if agent == 'math':
            return bool(re.search(r'[-+*/%]|sqrt|log|ln|sin|cos|tan', p.slots['expr']))
        return agent in ('currency', 'units', 'dates')
    except Exception:
        return False


def declines(agent: str, text: str) -> bool:
    """True when the keyless agent's own parser recognises the request as one it must turn down honestly, so its answer
    beats a clarify: a currency that isn't real ("100 USD to Wakandan dollars", "100 USD in WKD")."""
    try:
        if agent == 'currency':
            from .agents.tools import unknown_currency
            return bool(unknown_currency(text))
    except Exception:
        return False
    return False


def question(agent: str, text: str) -> str | None:
    """A follow-up question when the agent can't act without a detail the text lacks, else None. A detail given by
    description ("the currency of Brazil") is not missing: it is looked up (A5), never asked for."""
    if described(text):
        return None
    if agent == 'currency':
        return currency_question(text)
    if agent == 'weather' and not find_place(text):
        return 'Which city or place do you want the weather for?'
    return None


# ---------- keyless parser contracts (docs/PLAN-accuracy-v2.md A5) ----------

@dataclass
class Parse:
    slots: dict            # parsed values
    unused: list[str]      # constraints in the text it could not use: 'future date', 'second place', 'source time', 'described currency'
    numbers_used: int
    numbers_seen: int

    @property
    def full(self) -> bool:
        return not self.unused and self.numbers_used >= self.numbers_seen


NUMBER = re.compile(r'\d+(?:\.\d+)?')
FORECAST_DAYS = 16  # the weather API's forecast range
# "at 3pm", "15:30": a clock time the user gives, to be converted from, which the time agent can't use
CLOCK_TIME = re.compile(r'(?<![+\-\u2212\u2013\d:.])\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)(?!\w)'
                        r'|(?<![+\-\u2212\u2013\d:.])\b\d{1,2}:\d{2}\b', re.I)
# A place or currency named by what it is rather than by name
DESCRIBED_PLACE = re.compile(
    r"\b(?:the\s+)?capital(?:\s+city)?\s+of\b|\b(?:birth\s*place|home\s*town|hometown)\b|\bwhere\s+(?:\w+\s+){1,4}"
    r"(?:was|were|is|are)\s+(?:born|from|based|headquartered)\b|\b(?:the\s+)?(?:city|town|country|place)\s+(?:where|that|which)\b"
    r"|\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*'s\s+capital\b", re.I)
DESCRIBED_CURRENCY = re.compile(r"\b(?:the\s+)?(?:local\s+)?(?:currency|money)\s+(?:of|used\s+in|in)\s+(?!\d)|"
                                r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*'s\s+(?:currency|money)\b|\bthe\s+local\s+currency\b", re.I)
COUNTRY_END = r"(?=\s*[?.!,;]|\s+(?:and|then|to|into|in|right|now|today|tomorrow|please|at|on)\b|\s*$)"
CAPITAL_PHRASE = re.compile(rf"\b(?:the\s+)?capital(?:\s+city)?\s+of\s+(?:the\s+)?(?P<c>[A-Za-z .'-]+?){COUNTRY_END}"
                            rf"|\b(?P<c2>(?-i:[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*)*))'s\s+capital(?:\s+city)?\b", re.I)
CURRENCY_PHRASE = re.compile(rf"\b(?:the\s+)?(?:local\s+)?(?:currency|money)\s+(?:of|used\s+in|in)\s+(?:the\s+)?"
                             rf"(?P<c>[A-Za-z .'-]+?){COUNTRY_END}"
                             rf"|\b(?P<c2>(?-i:[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*)*))'s\s+(?:currency|money)\b", re.I)


# "Convert 100 USD into it" after "What currency does Japan use?", "Convert 100 EUR into its currency": the target
# currency is the one the text asks about, named only by what it refers back to.
CURRENCY_REF = re.compile(r"\b(?P<prep>to|into|in|as)\s+(?:it|that(?:\s+currency)?|its\s+(?:currency|money)|their\s+"
                          r"(?:currency|money))(?=\s*[?.!,;]|\s*$)", re.I)
CURRENCY_CUE = re.compile(r'\b(?:currency|currencies|money)\b', re.I)


def currency_ref(text: str) -> re.Match | None:
    """The conversion target that points back at a currency the text asks about, or None."""
    m = CURRENCY_REF.search(text)
    return m if m and CURRENCY_CUE.search(text) and re.search(r'\d', text) else None


def described(text: str) -> str | None:
    """'described place' or 'described currency' when the text names one only by a description, else None."""
    if DESCRIBED_CURRENCY.search(text) or currency_ref(text):
        return 'described currency'
    if DESCRIBED_PLACE.search(text):
        return 'described place'
    return None


def country(name: str) -> str | None:
    key = re.sub(r'\s+', ' ', name.strip(" .'")).lower()
    key = re.sub(r'^the\s+', '', key) if key not in CAPITAL_OF else key
    return key if key in CAPITAL_OF else None


# A capital city -> its country ("the currency used in Bern" is Switzerland's)
COUNTRY_OF_CAPITAL = {cap.lower(): c for c, cap in CAPITAL_OF.items()}


def country_of_place(name: str) -> str | None:
    """The country a currency phrase is about: a country, a capital city, or a capital named by description ("the
    currency used in the capital of Switzerland")."""
    if key := country(name):
        return key
    bare = re.sub(r'\s+', ' ', name.strip(" .'"))
    if (m := CAPITAL_PHRASE.match(bare)) and m.end() == len(bare):
        return country(m.group('c') or m.group('c2') or '')
    return COUNTRY_OF_CAPITAL.get(re.sub(r'^the\s+', '', bare.lower()))


# A keyless slot request: weather, a clock time, or a conversion. "What is the currency of Japan?" and "Is Sydney the
# capital of Australia?" are questions about the description itself, never rewritten.
SLOT_CUE = re.compile(r'\b(?:weather|forecast|rain(?:ing)?|snow(?:ing)?|sunny|temperature|humid|windy|time|clock|'
                      r'convert|exchange)\b|\d', re.I)
# the description is the place or currency the request is about: "weather in the capital of X", "100 USD to X's money"
SLOT_OF = re.compile(r"\b(?:in|at|for|to|into|from|near|around)\s*$", re.I)


def resolve_described(text: str) -> str | None:
    """The text with each capital or currency named by description replaced from the static tables ("weather in the
    capital of Switzerland" -> "weather in Bern"; "100 USD to the currency of Brazil" -> "100 USD to BRL"), or None when
    there is nothing to replace or a country isn't in the tables. Only a keyless slot request (SLOT_CUE) is rewritten,
    and only where the description is the object of in/at/to/into...: a question about the capital or the currency
    itself is left as the user wrote it."""
    if not SLOT_CUE.search(text):
        return None
    out, missing = text, False

    def sub(table):
        def repl(m):
            nonlocal missing
            if not SLOT_OF.search(m.string[:m.start()]):
                return m.group(0)
            key = (country_of_place if table is CURRENCY_OF else country)(m.group('c') or m.group('c2') or '')
            if key is None:
                missing = True
                return m.group(0)
            return table[key]
        return repl
    out = CURRENCY_PHRASE.sub(sub(CURRENCY_OF), out)
    out = CAPITAL_PHRASE.sub(sub(CAPITAL_OF), out)
    if ref := currency_ref(out):
        # "What currency does Japan use? Convert 100 USD into it": the one country the text names
        named = {country(c) for c in COUNTRY_MENTION.findall(out[:ref.start()] + out[ref.end():])} - {None}
        if len(named) != 1:
            return None
        out = out[:ref.start()] + f"{ref.group('prep')} {CURRENCY_OF[named.pop()]}" + out[ref.end():]
    return None if missing or out == text else out


# A country named in the text, by its own name ("Japan", "South Korea"), for a currency the text refers back to.
COUNTRY_MENTION = re.compile(r'\b(' + '|'.join(re.escape(c) for c in sorted((c for c in COUNTRY_NAME.values()),
                                                                           key=len, reverse=True)) + r')\b')


NOT_PLACE = {'what', 'the', 'current', 'local', 'today', 'whats', 'now', 'tomorrow', 'tonight', 'here', 'there', 'please',
             'thanks', 'me', 'it', 'too', 'also', 'then', 'both'}


AND_COUNTRIES = sorted((c for c in COUNTRY_NAME if ' and ' in c), key=len, reverse=True)
# States, provinces and territories written after a city ("Portland, Oregon", "Sydney, Nova Scotia"): they qualify the
# place before them, like a country does. Any two-letter capital code ("Cambridge, MA") counts too.
REGIONS = {
    'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado', 'connecticut', 'delaware', 'florida', 'georgia',
    'hawaii', 'idaho', 'illinois', 'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana', 'maine', 'maryland',
    'massachusetts', 'michigan', 'minnesota', 'mississippi', 'missouri', 'montana', 'nebraska', 'nevada',
    'new hampshire', 'new jersey', 'new mexico', 'new york', 'north carolina', 'north dakota', 'ohio', 'oklahoma',
    'oregon', 'pennsylvania', 'rhode island', 'south carolina', 'south dakota', 'tennessee', 'texas', 'utah', 'vermont',
    'virginia', 'washington', 'west virginia', 'wisconsin', 'wyoming', 'district of columbia', 'd.c.', 'dc',
    'ontario', 'quebec', 'british columbia', 'alberta', 'manitoba', 'saskatchewan', 'nova scotia', 'new brunswick',
    'newfoundland', 'newfoundland and labrador', 'prince edward island', 'yukon', 'nunavut', 'northwest territories',
    'new south wales', 'victoria', 'queensland', 'south australia', 'western australia', 'tasmania',
    'northern territory', 'australian capital territory', 'england', 'scotland', 'wales', 'northern ireland',
    'bavaria', 'catalonia', 'tuscany', 'punjab', 'maharashtra', 'kerala', 'karnataka', 'tamil nadu', 'gujarat',
    'rajasthan', 'uttar pradesh', 'west bengal', 'bihar', 'goa', 'haryana', 'telangana', 'andhra pradesh',
}


def region(part: str) -> bool:
    """True when a comma-joined part is a state, province or region code qualifying the place before it."""
    bare = part.strip(' ?.!')
    return bare.lower() in REGIONS or bool(re.fullmatch(r'[A-Z]{2}|[A-Z]\.[A-Z]\.', bare))


def places_in(text: str) -> list[str]:
    """Every place a weather or time request names, in order: "Weather in Vienna and Prague", "vienna weather, prague
    weather" -> two places."""
    found = []
    # a country whose name holds "and" ("Trinidad and Tobago") is one place, not two
    for name in AND_COUNTRIES:
        text = re.sub(rf'\b{re.escape(name)}\b', lambda m: m.group(0).replace(' and ', ' \x00 '), text, flags=re.I)
    pieces = re.split(r'\s*([,;&]|\band\b|\bor\b|\bvs\.?|\bversus\b)\s*', text)
    for n, part in enumerate(pieces[::2]):
        part = part.replace('\x00', 'and')
        if not part.strip():
            continue
        if found and n and pieces[2 * n - 1] == ',' and region(part):
            continue  # "Portland, Oregon": the state of the place before it
        place = part.strip(' ?.!') if part.strip(' ?.!').lower() in AND_COUNTRIES else find_place(part)
        if not place:
            m = re.match(r"\s*([a-z][a-z'-]+(?:\s+[a-z][a-z'-]+)?)\s+(?:weather|forecast|temperature|time)\b", part, re.I)
            place = m.group(1) if m and m.group(1).lower() not in NOT_PLACE else None
        # "... in Vienna and Prague": a bare name after a place is another place
        bare = part.strip(' ?.!')
        if not place and found and bare.lower() in COUNTRY_NAME:
            continue  # "New Delhi, India": the country of the place before it
        if (not place and found and re.fullmatch(r"[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2}", bare)
                and not WEATHER_WORDS.search(bare) and not TIME_WORDS.search(bare) and bare.lower() not in NOT_PLACE):
            place = bare
        if place and place.lower() not in (p.lower() for p in found):
            found.append(place)
    return found


def future_days(text: str, today: date | None = None) -> int | None:
    """How many days ahead the text asks about, when it names a future date or period, else None."""
    today = today or date.today()
    t = text.lower()
    days = []
    if m := re.search(r'\bin\s+(\d+)\s*(day|week|month|year)s?\b', t):
        days.append(int(m.group(1)) * {'day': 1, 'week': 7, 'month': 30, 'year': 365}[m.group(2)])
    if re.search(r'\bnext\s+year\b', t):
        days.append(max(1, (date(today.year + 1, 1, 1) - today).days))
    if re.search(r'\bnext\s+month\b', t):
        days.append(30)
    for y in re.findall(r'\b(2\d{3})\b', t):
        if int(y) > today.year:
            days.append((date(int(y), 1, 1) - today).days)
    try:
        days += [(d - today).days for _, d, _ in dates_in(text, today) if d > today]
    except Exception:
        pass
    return max(days) if days else None


def future_year(text: str, today: date | None = None) -> int | None:
    """A year after this one that the text asks about ("in 2090", "next year"), else None."""
    this = (today or date.today()).year
    years = [int(y) for y in re.findall(r'\b(2\d{3})\b', text) if int(y) > this]
    if re.search(r'\bnext\s+year\b', text, re.I):
        years.append(this + 1)
    return max(years) if years else None


def parse_for(agent: str, text: str) -> Parse | None:
    """What the keyless agent's parser reads from the text, and what in the text it can't use. None for agents that
    don't parse (every LLM agent)."""
    unused: list[str] = []
    if agent in ('weather', 'time', 'currency') and (kind := described(text)) and not resolve_described(text):
        unused.append(kind)
    if agent == 'math':
        s = math_text(text)
        seen = sum(len(NUMBER.findall(x)) for x in math_spans(s))
        try:
            solved = solve_math(text)
        except Exception:
            solved = None
        if not solved:
            return Parse({}, unused, 0, seen)
        return Parse({'expr': solved[0], 'value': solved[1]}, unused, seen, seen)
    if agent == 'currency':
        words = currency_words(text)
        seen = sum(1 for w in words if AMOUNT.fullmatch(w))
        parsed = parse_currency(text, ECB_CODES)
        if len({c for _, c in currencies_in(words, ECB_CODES)}) > 2:
            unused.append('second currency')
        if (days := future_days(text)) and days > 0:
            unused.append('future date')
        if isinstance(parsed, str):
            return Parse({}, unused, 0, seen)
        amount, src, dst = parsed
        return Parse({'amount': amount, 'from': src, 'to': dst}, unused, min(seen, 1), seen)
    if agent == 'weather':
        places = places_in(text)
        if len(places) > 1:
            unused.append('second place')
        if (days := future_days(text)) and days > FORECAST_DAYS:
            unused.append('future date')
        place = find_place(text)
        return Parse({'city': place} if place else {}, unused, 0, 0)
    if agent == 'time':
        if CLOCK_TIME.search(text):
            unused.append('source time')
        if len(places_in(text)) > 1:
            unused.append('second place')
        zone, place = parse_zone(text), find_place(text)
        slots = {'zone': zone[0]} if zone else {'city': place} if place else {}
        return Parse(slots, unused, 0, 0)
    if agent == 'units':
        parsed = parse_units(text)
        if isinstance(parsed, tuple):
            return Parse({'amount': parsed[0], 'from': parsed[1][1], 'to': parsed[2][1]}, unused, 1, 1)
        return Parse({}, unused, 0, 0)
    if agent == 'dates':
        try:
            answer, ok = date_maths(text)
        except Exception:
            answer, ok = '', False
        return Parse({'date': answer} if ok else {}, unused, 0, 0)
    return None


def partial_frame(pick: str | None, text: str) -> dict | None:
    """The frame of a step that asked a question instead of answering, when what it did parse is worth keeping: "My
    budget for the trip is 2,000 euros" (a currency pick with no target) leaves the amount and its currency, so "How
    much is that in US dollars?" can convert them. None otherwise."""
    if pick != 'currency':
        return None
    words = currency_words(text)
    amounts = [w for w in words if AMOUNT.fullmatch(w)]
    found = {c for _, c in currencies_in(words, ECB_CODES)}
    if len(amounts) != 1 or len(found) != 1:
        return None
    return {'agent': 'currency', 'slots': {'amount': float(amounts[0]), 'from': found.pop()}}


def frame_slots(agent: str, text: str) -> dict:
    """The slots a finished step carries to the next turn (A4): the keyless parser's, or the topic for an LLM agent."""
    p = parse_for(agent, text)
    if p is not None:
        return {k: v for k, v in p.slots.items() if isinstance(v, (str, int, float))}
    topic = topic_of(text)
    return {'topic': topic} if topic else {}


TOPIC_LEAD = re.compile(r"^(?:(?:please|hey|hi|ok|so)\s+)*(?:what(?:'s| is| are| was| were)?|who(?:'s| is| was| were)?|"
                        r"tell me (?:about|more about)?|explain|describe|how (?:do|does|did|to|can) (?:i|you|we)?|"
                        r"why (?:is|are|do|does)|write (?:about|a \w+ (?:on|about))?)\s+", re.I)


def topic_of(text: str) -> str:
    """The first noun phrase of a request: "Who was Ada Lovelace?" -> "Ada Lovelace"."""
    t = re.sub(r'\s+', ' ', text).strip().rstrip('?!. ')
    for _ in range(2):
        t = TOPIC_LEAD.sub('', t).strip()
    t = re.sub(r'^(?:the|a|an)\s+', '', t, flags=re.I)
    return ' '.join(t.split()[:6])


# ---------- honest outcomes (A3, B6) ----------

def unsupported_text(kind: str, text: str) -> str:
    """The answer for a request this app can't do: 'action', 'personal', 'live' or 'described'."""
    if kind == 'action':
        what = cant_do(text)
        if what:
            return what[1]
        return ("Sorry, I can't do that for you: I'm not able to take actions in the world, like booking, buying, sending "
                f"or scheduling things. {CAPABILITIES}.")
    if kind == 'personal':
        return ("I can't see your private information, such as your location, account, calendar, contacts or files you "
                "haven't attached, or anyone else's personal details. If you tell me what you know, I can help with that.")
    if kind == 'live':
        return ("I don't have live data here, so I can't give you that as it is right now. An engine that can search "
                "the web can look it up, or check a live source such as the official site.")
    if kind == 'described':
        return ("I can't look that up here: the place or currency is named only by a description, and finding out what "
                "it is needs an LLM engine. Name it directly (for example \"weather in Bern\") or choose an engine in "
                "Settings.")
    return "Sorry, I can't do that here."


def future_note(text: str) -> str | None:
    """The answer for a request about a date no source here can know yet, else None: weather beyond the forecast's
    range, or anything in a year after this one."""
    days = future_days(text)
    if days is None:
        return None
    if WEATHER_WORDS.search(text) and days > FORECAST_DAYS:
        return (f"I can't tell you the weather that far ahead: forecasts only reach about {FORECAST_DAYS} days, and "
                'no one can know the weather beyond that. Ask again closer to the day.')
    if (year := future_year(text)) is not None:
        return (f"I can't tell you that: {year} hasn't happened yet, so no one can know it now. I can help with what "
                'is known today.')
    return None


# Predictions: "who will win", "what will ... be", "going to", "forecast for 2030".
PREDICTION = re.compile(r"\b(?:will|going\s+to|gonna|predict(?:ion)?s?|forecast|wins?|winners?|next\s+week'?s?|tomorrow'?s?\s+"
                        r"(?:winning|lottery|close|closing))\b", re.I)
ADVICE = re.compile(r"\b(?:should\s+I|what\s+should\s+I\s+(?:learn|do|choose|pick)|is\s+it\s+worth|which\s+is\s+better\s+"
                    r"for\s+me|\w+\s+or\s+\w+\s+for\s+(?:me|a|an|the|my|someone|beginners?)|either\b.+\bor\b|"
                    r"for\s+the\s+\w+(?:\s+\w+)?\s+role)\b", re.I)
CODE_ARTIFACT = re.compile(r'\b(?:write|fix|debug|example|snippet|function|error)\b', re.I)
TIME_SENSITIVE = re.compile(r'\b(?:cut-?off|rank(?:ing)?s?|admissions?|fees?|top\s+college|this\s+year|latest|'
                            r'current(?:ly)?|20\d\d)\b', re.I)
# words that make rank, current or latest a figure rather than "the ranks of cards" or "electric current"
FIGURE_FRAME = re.compile(r'\b(?:cut-?off|admissions?|fees?|tuition|top\s+college|this\s+year(?:\W?s)?|20\d\d|'
                          r'(?:how\s+much|what|which|closing|opening|required|expected|minimum|air|all\s+india)\s+rank|'
                          r'rank(?:ing)?s?\s+(?:of|for|in|needed|required|list)|placements?|seats?|packages?|salary|'
                          r'acceptance\s+rate|eligibility|marks?|scores?|percentile)\b', re.I)
# a named institution: an institution word, or an acronym such as IIT, JEE or NEET. A capitalised word alone is not one
# ("Ohm's law", "the ranks of cards in Poker", "how Google ranks pages").
INSTITUTION = re.compile(r'\b(?i:colleges?|universit(?:y|ies)|institutes?|iits?|nits?|schools?|boards?|exams?|entrance|'
                         r'agenc(?:y|ies)|ministr(?:y|ies)|compan(?:y|ies)|clubs?|teams?)\b|\b[A-Z]{2,}[a-z]?\b')
TIME_SENSITIVE_CAVEAT = 'Figures like these change every year; check the official source.'
ADVICE_KEYLESS = 'This needs an engine to answer well: it is advice, not a lookup.'


def advice(text: str) -> bool:
    """A request for advice on a choice ("Should I learn Rust or Go first?"), not for code."""
    return bool(ADVICE.search(text)) and not CODE_ARTIFACT.search(text)


def time_sensitive(text: str, today: date | None = None) -> bool:
    """A figure that changes from year to year about a named institution or thing: cut-off ranks, fees, rankings."""
    this = (today or date.today()).year
    hits = [m.group(0) for m in TIME_SENSITIVE.finditer(text)]
    hits = [h for h in hits if not re.fullmatch(r'20\d\d', h) or int(h) >= this - 1]
    if not hits or not INSTITUTION.search(text.strip()):
        return False
    # "current" or "latest" alone, or "ranks" as a verb, is not a yearly figure: one of the figure words must be there
    return bool(FIGURE_FRAME.search(text))


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


def clarify_text(probabilities: dict, text: str = '', descriptions: dict | None = None, earlier: bool = True) -> str:
    """A plain-English follow-up question built from Jev's route probabilities: the favourite's missing detail when one
    agent clearly leads, otherwise the two or three likeliest readings as options. Never names an agent. `earlier`:
    whether the chat has anything before this request (an earlier turn or an attached file) that "that" could mean."""
    ranked = [a for a, p in sorted(probabilities.items(), key=lambda kv: -kv[1]) if a not in ('clarify', 'blocked',
                                                                                              'unsupported')]
    if not ranked:
        return 'Could you add a bit more detail about what you need?'
    top = ranked[0]
    if top == 'create' and (ask := topic_question(text, earlier)):
        return ask
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


FORMAT_WORD = {'pdf': 'PDF', 'docx': 'document', 'pptx': 'presentation', 'xlsx': 'spreadsheet', 'md': 'document'}


def topic_question(text: str, earlier: bool = True) -> str | None:
    """'What should the presentation be about?' for a file request that names no topic and points at nothing earlier
    ("Make me a presentation"), else None. A request that points back ("put that in a PDF") in a chat with nothing
    earlier says so and asks for the topic."""
    from . import create as cf
    from .agents import create as create_agent
    if not text or create_agent.has_topic(text):
        return None
    what = FORMAT_WORD.get(cf.detect_format(text), 'file')
    if create_agent.refers_back(text):
        return None if earlier else (f"There's nothing earlier in this chat to put in a {what}. "
                                     f'What should the {what} be about?')
    return f'What should the {what} be about?'


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


RESULT_WORDS = re.compile(r'\b(?:the|that|this)\s+(?:result|answer|total|sum|amount|number|figure|value)\b|'
                          r'(?<=\bconvert )(?:it|that|this)\b', re.I)


def resolve_result(text: str, earlier: list[tuple[str, str]]) -> str | None:
    """Keyless: "convert the result to EUR" after "Add 120 and 380 dollars" (answered "(120+380) = 500") names the
    number the earlier step worked out, with the currency that step was in: "convert 500 USD to EUR". None when there is
    nothing to replace or no number to put in."""
    if not earlier or not RESULT_WORDS.search(text) or NUMBER.search(text):
        return None
    if not isinstance(parse_currency(text, ECB_CODES), str):
        return None  # "convert that amount from EUR to GBP" parses on its own: its own text stands
    step, answer = earlier[-1]
    m = re.search(r'=\s*([\d,]+(?:\.\d+)?)\s*([A-Z]{3})?', answer or '')
    if not m:
        return None
    unit = m.group(2) or next((c for _, c in currencies_in(currency_words(step), ECB_CODES)), '')
    return RESULT_WORDS.sub(f'{m.group(1).replace(",", "")} {unit}'.strip(), text, count=1)


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
