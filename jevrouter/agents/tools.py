"""Keyless agents from v1. Jev only classifies, so each agent pulls what it needs from the text with plain
parsing (pure functions below, unit tested), then calls a free API."""
import ast
import asyncio
import html
import math
import operator
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import aiohttp
import certifi

SSL = ssl.create_default_context(cafile=certifi.where())
UA = {'User-Agent': 'TraceGraph/0.2 (https://github.com/ayushap18/tracegraph) python-aiohttp'}


@dataclass
class AgentResult:
    answer: str
    ok: bool
    source: str | None = None
    engine: str = 'keyless'
    claude_in: int = 0
    claude_out: int = 0


async def get_json(http: aiohttp.ClientSession, url: str, **params) -> dict:
    for attempt in range(3):
        async with http.get(url, params=params or None, headers=UA, ssl=SSL, timeout=aiohttp.ClientTimeout(total=8)) as r:
            # Free APIs rate-limit bursts (autopilot); back off briefly instead of failing the query.
            if r.status == 429 and attempt < 2:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            r.raise_for_status()
            return await r.json(content_type=None)


# ---------- math ----------

MATH_WORDS = [
    (r'\bsquare root of\s*([\d.]+)', r'sqrt(\1)'), (r'([\d.]+)\s*%\s*of\s*([\d.]+)', r'(\1/100*\2)'),
    (r'\bmultiplied by\b|\btimes\b|(?<=\d)\s*x\s*(?=\d)', '*'), (r'\bdivided by\b|\bover\b', '/'),
    (r'\bplus\b', '+'), (r'\bminus\b', '-'), (r'\bto the power of\b|\^', '**'),
]
MATH_FUNCS = {'sqrt': math.sqrt, 'log': math.log10, 'ln': math.log, 'sin': math.sin, 'cos': math.cos, 'tan': math.tan, 'abs': abs}
MATH_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
            ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


MAX_DIGITS = 300  # bounds every intermediate value: big-int arithmetic runs on the event loop, so (((9**99)**99)**99) would hang it


def bounded(v):
    if isinstance(v, int) and v.bit_length() > MAX_DIGITS * 3.33 or isinstance(v, float) and not math.isfinite(v):
        raise ValueError('result too large')
    return v


def safe_eval(node):
    if isinstance(node, ast.Expression):
        return safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return bounded(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in MATH_OPS:
        left, right = safe_eval(node.left), safe_eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 100:
                raise ValueError('exponent too large')
            if left and abs(right) * abs(math.log10(abs(left))) > MAX_DIGITS:  # check before computing, not after
                raise ValueError('result too large')
        try:
            return bounded(MATH_OPS[type(node.op)](left, right))
        except OverflowError:
            raise ValueError('result too large')
    if isinstance(node, ast.UnaryOp) and type(node.op) in MATH_OPS:
        return MATH_OPS[type(node.op)](safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in MATH_FUNCS and len(node.args) == 1:
        return MATH_FUNCS[node.func.id](safe_eval(node.args[0]))
    raise ValueError('unsupported expression')


# ---------- numbers written as words ----------

UNITS = {w: i for i, w in enumerate('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen '
                                     'fifteen sixteen seventeen eighteen nineteen'.split())}
TENS = {w: 10 * i for i, w in enumerate('twenty thirty forty fifty sixty seventy eighty ninety'.split(), 2)}
SCALES = {'hundred': 100, 'thousand': 1_000, 'lakh': 100_000, 'lakhs': 100_000, 'million': 1_000_000,
          'crore': 10_000_000, 'crores': 10_000_000, 'billion': 1_000_000_000, 'trillion': 1_000_000_000_000}
AND_AFTER_SCALE = '(?:' + '|'.join(f'(?<={w})' for w in ('hundred', 'thousand', 'million', 'billion')) + r')\s+and\s+'
NUM_WORD = '|'.join(sorted([*UNITS, *TENS, *SCALES], key=len, reverse=True))
# A run of number words: "a thousand", "two hundred and fifty" ("and" only after a scale, so "five and six" stays two), "twenty-five", "one million". A leading "a"/"an" only
# counts before a scale word, so "a pound" keeps its article.
WORD_RUN = re.compile(rf"\b(?:(?:an?|one)\s+(?=(?:{'|'.join(SCALES)})\b))?(?:{NUM_WORD})(?:(?:{AND_AFTER_SCALE}|[\s-]+)(?:{NUM_WORD}))*\b", re.I)
# Digits with a scale: "1.5k", "2 million", "3.2bn", "5 lakh". Bare m/b ("5m") only count for money, where they mean
# million/billion rather than metres or minutes.
SUFFIX = {'k': 1_000, 'hundred': 100, 'thousand': 1_000, 'lakh': 100_000, 'lakhs': 100_000, 'mn': 1_000_000, 'million': 1_000_000,
          'crore': 10_000_000, 'crores': 10_000_000, 'bn': 1_000_000_000, 'billion': 1_000_000_000,
          'trillion': 1_000_000_000_000}
MONEY_SUFFIX = {**SUFFIX, 'm': 1_000_000, 'b': 1_000_000_000}


def words_value(run: str) -> int:
    total = current = 0
    for w in re.findall(r'[a-z]+', run.lower()):
        if w in UNITS:
            current += UNITS[w]
        elif w in TENS:
            current += TENS[w]
        elif w == 'hundred':
            current = max(current, 1) * 100
        elif w in SCALES:
            total += max(current, 1) * SCALES[w]
            current = 0
    return total + current


def plain(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else repr(float(n))


def normalize_numbers(text: str, money: bool = False) -> str:
    """Writes numbers as digits: "a thousand" -> 1000, "two hundred and fifty" -> 250, "1.5k" -> 1500, "3 million" ->
    3000000 (and "5m" -> 5000000 when money is True). Everything else is left as it was."""
    scales = MONEY_SUFFIX if money else SUFFIX
    names = '|'.join(sorted(scales, key=len, reverse=True))
    # Digits with a scale first: "2 million" is 2000000, not "2" followed by a number-word run "million" (1000000).
    text = re.sub(rf'(\d+(?:\.\d+)?)\s*({names})\b', lambda m: plain(float(m.group(1)) * scales[m.group(2).lower()]),
                  text, flags=re.I)
    return WORD_RUN.sub(lambda m: str(words_value(m.group(0))), text)


def solve_math(q: str) -> tuple[str, float | int] | None:
    """Returns (expression, value) or None when there's no numeric expression."""
    s = normalize_numbers(q.replace(',', '')).lower()
    for pat, rep in MATH_WORDS:
        s = re.sub(pat, rep, s)
    spans = re.findall(r'(?:sqrt|log|ln|sin|cos|tan|abs|[\d.()+\-*/%\s])+', s)
    expr = max((x.strip() for x in spans), key=len, default='')
    if not re.search(r'\d', expr):
        return None
    if not re.search(r'\d', q) and not re.search(r'[-+*/%]|[a-z]', expr):
        return None  # a number word on its own ("which one is it") is not a calculation
    value = round(safe_eval(ast.parse(expr, mode='eval')), 10)
    return expr, int(value) if isinstance(value, int) or value.is_integer() else value


async def agent_math(q: str, http=None) -> AgentResult:
    solved = solve_math(q)
    if not solved:
        return AgentResult("I couldn't find a numeric expression in that.", False)
    expr, v = solved
    return AgentResult(f'{expr} = {v:,}' if isinstance(v, int) else f'{expr} = {v}', True)


# ---------- places: weather, time ----------

PLACE_AFTER = re.compile(r"\b(?:in|at|for)\s+([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:today|tomorrow|tonight|now|right now|this|next|on)\b|[?.!,]|$)", re.I)


def place_in(q: str) -> str | None:
    """A place named after in/at/for ("weather in Paris"), and nothing else."""
    m = PLACE_AFTER.search(q)
    return m.group(1).strip() if m else None


def find_place(q: str) -> str | None:
    if place := place_in(q):
        return place
    caps = re.findall(r"(?<!^)(?<![.?!]\s)\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)", q)
    return caps[-1] if caps else None


async def geocode(http, place: str) -> dict | None:
    data = await get_json(http, 'https://geocoding-api.open-meteo.com/v1/search', name=place, count=1)
    return (data.get('results') or [None])[0]


WMO = {0: 'clear sky', 1: 'mostly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'fog', 48: 'freezing fog', 51: 'light drizzle',
       53: 'drizzle', 55: 'heavy drizzle', 61: 'light rain', 63: 'rain', 65: 'heavy rain', 71: 'light snow', 73: 'snow',
       75: 'heavy snow', 80: 'rain showers', 81: 'heavy showers', 82: 'violent showers', 95: 'thunderstorms', 96: 'thunderstorms with hail'}


async def agent_weather(q: str, http) -> AgentResult:
    place = find_place(q)
    loc = place and await geocode(http, place)
    if not loc:
        return AgentResult('Which city? I need a place to look up the weather.', False)
    d = await get_json(http, 'https://api.open-meteo.com/v1/forecast', latitude=loc['latitude'], longitude=loc['longitude'],
                       current='temperature_2m,weather_code,wind_speed_10m',
                       daily='temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code',
                       timezone='auto', forecast_days=2)
    cur, day = d['current'], d['daily']
    where = f"{loc['name']}, {loc.get('country', '')}".strip(', ')
    tomorrow = 'tomorrow' in q.lower()
    i = 1 if tomorrow else 0
    lines = [
        f"{where}: {'tomorrow' if tomorrow else 'now'} "
        + (f"{WMO.get(day['weather_code'][1], 'mixed')}, {day['temperature_2m_min'][1]:.0f}–{day['temperature_2m_max'][1]:.0f}°C"
           if tomorrow else f"{cur['temperature_2m']:.0f}°C, {WMO.get(cur['weather_code'], 'mixed')}, wind {cur['wind_speed_10m']:.0f} km/h"),
        f"Chance of rain {'tomorrow' if tomorrow else 'today'}: {day['precipitation_probability_max'][i]}%",
    ]
    return AgentResult('\n'.join(lines), True, 'open-meteo.com')


# "UTC+5:30", "GMT-3", "utc +05:45", "UTC": a fixed offset from UTC.
UTC_OFFSET = re.compile(r'\b(?:UTC|GMT)(?:\s*([+\-\u2212\u2013])\s*(\d{1,2})(?:[:.]?(\d{2}))?)?(?![\w:+\-])', re.I)
# Common abbreviations, written in capitals, mapped to the zone people usually mean by them (so summer time applies).
TZ_ABBR = {'EST': 'America/New_York', 'EDT': 'America/New_York', 'CST': 'America/Chicago', 'CDT': 'America/Chicago',
           'MST': 'America/Denver', 'MDT': 'America/Denver', 'PST': 'America/Los_Angeles', 'PDT': 'America/Los_Angeles',
           'CET': 'Europe/Paris', 'CEST': 'Europe/Paris', 'BST': 'Europe/London', 'IST': 'Asia/Kolkata',
           'JST': 'Asia/Tokyo', 'KST': 'Asia/Seoul', 'HKT': 'Asia/Hong_Kong', 'SGT': 'Asia/Singapore',
           'AEST': 'Australia/Sydney', 'AEDT': 'Australia/Sydney'}


ABBR_WORD = re.compile(r'\(?\b(?:' + '|'.join(TZ_ABBR) + r')\b\)?')


def parse_zone(q: str, abbreviations: bool = True) -> tuple[str, tzinfo] | None:
    """(label, tzinfo) for a time zone written in the text: a UTC/GMT offset, an IANA name, or (unless abbreviations is
    False) a common abbreviation."""
    m = UTC_OFFSET.search(q)
    if m:
        if not m.group(1):
            return 'UTC', timezone.utc
        hours, minutes = int(m.group(2)), int(m.group(3) or 0)
        if hours > 14 or minutes > 59:
            return None
        sign = -1 if m.group(1) in '-\u2212\u2013' else 1
        label = f"UTC{'+' if sign > 0 else '-'}{hours:02d}:{minutes:02d}"
        return label, timezone(sign * timedelta(hours=hours, minutes=minutes), label)
    for name in re.findall(r'\b[A-Z][A-Za-z_]+/[A-Z][A-Za-z_]+(?:/[A-Z][A-Za-z_]+)?\b', q):
        try:
            return name, ZoneInfo(name)
        except Exception:
            continue
    for word in re.findall(r'\b[A-Z]{3,4}\b', q) if abbreviations else ():
        if word in TZ_ABBR:
            return word, ZoneInfo(TZ_ABBR[word])
    return None


def stamp(now: datetime) -> str:
    return now.strftime('%-I:%M %p, %A %-d %B %Y')


def zone_answer(zone: tuple[str, tzinfo]) -> AgentResult:
    label, tz = zone
    now = datetime.now(tz)
    return AgentResult(f'{label}: {stamp(now)} ({now.tzname() or label})', True)


async def agent_time(q: str, http) -> AgentResult:
    # An explicit offset or IANA name says exactly which zone. A named place beats an abbreviation next to it: "Beijing
    # (CST)" is China Standard Time, not the US Central Time that "CST" usually means on its own.
    if zone := parse_zone(q, abbreviations=False):
        return zone_answer(zone)
    place = find_place(ABBR_WORD.sub('', q))
    loc = place and await geocode(http, place)
    if loc and not loc.get('timezone'):  # a country spanning several zones ("United States") has no single one
        return AgentResult(f"{loc['name']} spans more than one time zone. Which city there do you mean?", False)
    if loc:
        now = datetime.now(ZoneInfo(loc['timezone']))
        where = f"{loc['name']}, {loc.get('country', '')}".strip(', ')
        return AgentResult(f"{where}: {stamp(now)} ({now.tzname() or loc['timezone']})", True)
    if zone := parse_zone(q):
        return zone_answer(zone)
    now = datetime.now().astimezone()  # aware, so the local zone has a name
    return AgentResult(f"Your local time: {stamp(now)} ({now.tzname() or 'local time'})", True)


# ---------- currency ----------

CURRENCY_NAMES = {'dollar': 'USD', 'dollars': 'USD', 'usd': 'USD', 'buck': 'USD', 'bucks': 'USD', 'rupee': 'INR',
                  'rupees': 'INR', 'euro': 'EUR', 'euros': 'EUR', 'pound': 'GBP', 'pounds': 'GBP', 'sterling': 'GBP',
                  'yen': 'JPY', 'yuan': 'CNY', 'renminbi': 'CNY', 'franc': 'CHF', 'francs': 'CHF', 'baht': 'THB',
                  'ringgit': 'MYR', 'rand': 'ZAR', 'zloty': 'PLN', 'forint': 'HUF', 'lira': 'TRY', 'shekel': 'ILS',
                  'shekels': 'ILS', 'rupiah': 'IDR', 'reais': 'BRL', 'leu': 'RON', 'lei': 'RON', 'quid': 'GBP'}
# Every active ISO 4217 code, so an unsupported one is recognised in any case ("100 aed to usd").
ISO_CODES = frozenset('''AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD
CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG
HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT
MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG
SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD
XOF XPF YER ZAR ZMW ZWL'''.split())
# Lowercase words that are also ISO codes: only counted when written in capitals.
CODE_LOOKALIKES = {'all', 'top', 'cup', 'mop', 'mad', 'gel', 'bam', 'sos', 'lak', 'try', 'pen', 'bob', 'dop', 'bnd', 'php'}
# Currency names with no rate source here (keyed without a plural "s"), for an honest "I can't convert" answer.
UNSUPPORTED_NAMES = {'dirham': 'AED', 'naira': 'NGN', 'dinar': '', 'riyal': '', 'rial': '', 'dong': 'VND', 'shilling': '',
                     'kwacha': '', 'ruble': 'RUB', 'rouble': 'RUB', 'hryvnia': 'UAH', 'taka': 'BDT', 'cedi': 'GHS',
                     'birr': 'ETB', 'kyat': 'MMK', 'riel': 'KHR', 'tenge': 'KZT', 'lari': 'GEL', 'dram': 'AMD',
                     'manat': '', 'afghani': 'AFN', 'quetzal': 'GTQ', 'lempira': 'HNL', 'guarani': 'PYG', 'bolivar': 'VES',
                     'kina': 'PGK', 'pula': 'BWP', 'metical': 'MZN', 'kwanza': 'AOA', 'ouguiya': 'MRU', 'rufiyaa': 'MVR'}
# Names shared by several currencies, only some of which have rates here: which one is meant has to be asked.
AMBIGUOUS_NAMES = {'peso': ('peso', 'the Mexican peso (MXN) or the Philippine peso (PHP)')}
# The currencies frankfurter.app (European Central Bank reference rates) serves; the live list is used when it loads.
ECB_CODES = frozenset('AUD BGN BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF IDR ILS INR ISK JPY KRW MXN MYR NOK NZD PHP PLN '
                      'RON SEK SGD THB TRY USD ZAR'.split())
# Where each currency is used, for "convert 50 EUR to INR, then the time there". The euro has no single country.
CURRENCY_PLACE = {'AUD': 'Canberra, Australia', 'BGN': 'Sofia, Bulgaria', 'BRL': 'Brasilia, Brazil', 'CAD': 'Ottawa, Canada',
                  'CHF': 'Bern, Switzerland', 'CNY': 'Beijing, China', 'CZK': 'Prague, Czechia', 'DKK': 'Copenhagen, Denmark',
                  'GBP': 'London, United Kingdom', 'HKD': 'Hong Kong', 'HUF': 'Budapest, Hungary', 'IDR': 'Jakarta, Indonesia',
                  'ILS': 'Jerusalem, Israel', 'INR': 'New Delhi, India', 'ISK': 'Reykjavik, Iceland', 'JPY': 'Tokyo, Japan',
                  'KRW': 'Seoul, South Korea', 'MXN': 'Mexico City, Mexico', 'MYR': 'Kuala Lumpur, Malaysia',
                  'NOK': 'Oslo, Norway', 'NZD': 'Wellington, New Zealand', 'PHP': 'Manila, Philippines',
                  'PLN': 'Warsaw, Poland', 'RON': 'Bucharest, Romania', 'SEK': 'Stockholm, Sweden', 'SGD': 'Singapore',
                  'THB': 'Bangkok, Thailand', 'TRY': 'Ankara, Turkey', 'USD': 'Washington, United States',
                  'ZAR': 'Pretoria, South Africa'}
CURRENCY_LABEL = {'GBP': 'British pound', 'USD': 'US dollar', 'EUR': 'euro', 'INR': 'Indian rupee', 'JPY': 'Japanese yen',
                  'CHF': 'Swiss franc', 'CNY': 'Chinese yuan'}
# Currency words that are also something else, and what else they could mean.
OTHER_SENSE = {'pound': 'a pound as a unit of weight (about 0.45 kg)', 'pounds': 'pounds as a unit of weight (1 lb is about 0.45 kg)'}
# Cryptocurrencies: no exchange-rate source here covers them, so the answer says so instead of guessing.
CRYPTO = {'bitcoin': 'BTC', 'bitcoins': 'BTC', 'btc': 'BTC', 'ethereum': 'ETH', 'ether': 'ETH', 'eth': 'ETH',
          'litecoin': 'LTC', 'ltc': 'LTC', 'dogecoin': 'DOGE', 'doge': 'DOGE', 'solana': 'SOL', 'xrp': 'XRP',
          'ripple': 'XRP', 'tether': 'USDT', 'usdt': 'USDT', 'usdc': 'USDC', 'cardano': 'ADA', 'monero': 'XMR',
          'bnb': 'BNB', 'crypto': '', 'cryptocurrency': '', 'cryptocurrencies': ''}
AMOUNT = re.compile(r'\d+(?:\.\d+)?')


def currency_words(q: str) -> list[str]:
    # Keep decimal points in amounts but drop sentence dots: an LLM planner writes "Convert 20 USD to JPY."
    text = normalize_numbers(q.replace(',', ''), money=True)
    return [w for w in (w.strip('.') for w in re.sub(r'[^\w.\s]', ' ', text).split()) if w]


def currencies_in(words: list[str], known) -> list[tuple[int, str]]:
    """[(word index, code)] for every supported currency named in the words."""
    codes = [CURRENCY_NAMES.get(w.lower()) or (w.upper() if len(w) == 3 and w.isalpha() else None) for w in words]
    return [(i, c) for i, c in enumerate(codes) if c and c in known]


def singular(word: str) -> str:
    w = word.lower()
    return w[:-1] if w.endswith('s') and w[:-1] in {**UNSUPPORTED_NAMES, **AMBIGUOUS_NAMES} else w


def unsupported_currency(q: str, known) -> str | None:
    """The currency the text asks about that no rate source here covers: a cryptocurrency, a currency named in words
    ("50 dirhams"), or an ISO code in any case right after an amount or after to/into/from/in ("100 aed to usd")."""
    words = currency_words(q)
    for w in words:
        if w.lower() in CRYPTO:
            code = CRYPTO[w.lower()]
            return w.lower() + (f' ({code})' if code and code.lower() != w.lower() else '')
        if (name := singular(w)) in UNSUPPORTED_NAMES:
            code = UNSUPPORTED_NAMES[name]
            return w.lower() + (f' ({code})' if code else '')
    for i, w in enumerate(words[1:], 1):
        prev = words[i - 1].lower()
        code = w.upper()
        if (len(w) == 3 and code in ISO_CODES and code not in known and (w.isupper() or w.lower() not in CODE_LOOKALIKES)
                and (AMOUNT.fullmatch(prev) or prev in ('to', 'into', 'from', 'in'))):
            return code
    return None


def ambiguous_currency(q: str, known) -> str | None:
    """A question when the text names a currency shared by several countries ("50 pesos") and no code settles it."""
    words = currency_words(q)
    for w in words:
        if (name := singular(w)) in AMBIGUOUS_NAMES:
            label, options = AMBIGUOUS_NAMES[name]
            return f'Which {label} do you mean? I can convert {options}; for example "50 MXN to USD".'
    return None


def unsupported_answer(what: str, known) -> str:
    sample = ', '.join(c for c in ('USD', 'EUR', 'GBP', 'INR', 'JPY') if c in known)
    kind = 'cryptocurrency' if what.split()[0].lower() in CRYPTO else 'currency'
    return (f"I can't convert {what}: I don't have exchange rates for that {kind}. I only have the "
            f'European Central Bank reference rates for {len(known)} national currencies (such as {sample}).')


def parse_currency(q: str, known) -> tuple[float, str, str] | str:
    """Returns (amount, src, dst), or an error message. `known` is the set of supported codes."""
    words = currency_words(q)
    found = currencies_in(words, known)
    if len(found) < 2:
        return 'Tell me two currencies, like "100 USD to INR".'
    num = next((i for i, w in enumerate(words) if AMOUNT.fullmatch(w)), None)
    if num is None:  # "how many rupees is a dollar": the currency after "a"/"an" is one unit of the source
        num = next((i for i, w in enumerate(words[:-1]) if w.lower() in ('a', 'an', 'one') and i + 1 in dict(found)), None)
        amt = 1.0
    else:
        amt = float(words[num])
    # The currency right after the amount is the source ("how many euros is 100 pounds" → GBP to EUR).
    src_i = next((k for k, (i, _) in enumerate(found) if num is not None and i == num + 1), 0)
    src = found[src_i][1]
    dst = next((c for k, (_, c) in enumerate(found) if k != src_i and c != src), None)
    if not dst:
        return 'Tell me two different currencies, like "100 USD to INR".'
    return amt, src, dst


def currency_question(q: str, known=ECB_CODES) -> str | None:
    """A plain-English follow-up question when a conversion is missing a currency ("How much is a pound?"), or None when
    the text has what a conversion needs (or names a currency we can't convert, which gets an honest answer instead)."""
    if unsupported_currency(q, known):
        return None
    if not isinstance(parse_currency(q, known), str):
        return None
    if ask := ambiguous_currency(q, known):
        return ask
    words = currency_words(q)
    found = currencies_in(words, known)
    if not found:
        return 'Which currencies do you mean? For example "100 USD to INR" or "the EUR to GBP rate".'
    i, code = found[0]
    other = 'USD' if code != 'USD' else 'EUR'
    name = CURRENCY_LABEL.get(code, code)
    if words[i].lower() in OTHER_SENSE:
        return (f'Do you mean the {name} ({code}) as money, or {OTHER_SENSE[words[i].lower()]}? If it is money, which '
                f'currency should I convert it to? For example "1 {code} to {other}".')
    if i and words[i - 1].lower() in ('to', 'into', 'in'):  # "convert that amount to EUR": the target is given
        return f'Which currency should I convert to the {name} ({code}) from? For example "100 {other} to {code}".'
    return f'Which currency should I convert the {name} ({code}) to? For example "1 {code} to {other}".'


async def agent_currency(q: str, http) -> AgentResult:
    try:
        known = set(await get_json(http, 'https://api.frankfurter.app/currencies')) or ECB_CODES
    except Exception:
        known = ECB_CODES
    if what := unsupported_currency(q, known):
        return AgentResult(unsupported_answer(what, known), False)
    parsed = parse_currency(q, known)
    if isinstance(parsed, str):
        return AgentResult(ambiguous_currency(q, known) or parsed, False)
    amt, src, dst = parsed
    d = await get_json(http, 'https://api.frankfurter.app/latest', amount=amt, **{'from': src, 'to': dst})
    return AgentResult(f"{amt:,.2f} {src} = {d['rates'][dst]:,.2f} {dst}  (rate from {d['date']})", True, 'frankfurter.app')


# ---------- knowledge, code, chat ----------

FILLER = r"^(?:who|what|when|where|why|how)\s+(?:is|was|are|were|did|does|do)\s+(?:(?:a|an|the)\s+)?|^tell me about\s+(?:the\s+)?|^explain\s+|\?$"


def knowledge_term(q: str) -> str:
    return re.sub(FILLER, '', q.strip(), flags=re.I).strip(' ?') or q


async def ddg_abstract(http, q: str) -> tuple[str, str, str | None]:
    """(term, abstract, url). DuckDuckGo instant answers serve Wikipedia abstracts without Wikipedia's strict bot rate limits."""
    term = knowledge_term(q)
    d = await get_json(http, 'https://api.duckduckgo.com/', q=term, format='json', no_html=1, skip_disambig=1)
    return term, d.get('AbstractText') or '', d.get('AbstractURL') or None


async def agent_knowledge(q: str, http) -> AgentResult:
    term, text, url = await ddg_abstract(http, q)
    if not text:
        return AgentResult(f'No summary found for "{term}".', False)
    return AgentResult(' '.join(re.split(r'(?<=[.!?])\s+', text)[:3]), True, url or 'duckduckgo.com')


async def agent_code(q: str, http) -> AgentResult:
    term = re.sub(r'^(?:how (?:do|can) i|how to)\s+', '', q.strip(), flags=re.I).strip(' ?')
    d = await get_json(http, 'https://api.stackexchange.com/2.3/search/advanced', q=term, site='stackoverflow', accepted='True',
                       sort='relevance', order='desc', pagesize=3)
    items = d.get('items') or []
    if not items:
        return AgentResult('No accepted Stack Overflow answers matched that.', False)
    lines = [f"• {html.unescape(i['title'])} ({i['score']} votes)" for i in items]
    return AgentResult('Top answered threads on Stack Overflow:\n' + '\n'.join(lines), True, items[0]['link'])


async def agent_chat(q: str, http=None) -> AgentResult:
    s = q.lower()
    if re.search(r'\b(thanks|thank you|thx)\b', s):
        text = "You're welcome!"
    elif re.search(r'\b(what can you|help|who are you|what are you)\b', s):
        text = 'Jev reads each query and routes it to a specialist: math, weather, time, currency, knowledge, or code. Try one.'
    else:
        text = 'Hey! Ask me a calculation, the weather, a time zone, a currency conversion, a fact, or a coding question.'
    return AgentResult(text, True)


def clarify(top: list, text: str = '', descriptions: dict | None = None) -> AgentResult:
    """A plain-English follow-up question from Jev's [(agent, probability)] (jevrouter/gate.py); never names agents."""
    from ..gate import clarify_text
    return AgentResult(clarify_text(dict(top), text, descriptions), False)


BLOCKED = AgentResult("I can't help with that one.", False)

KEYLESS_RUNNERS = {'math': agent_math, 'weather': agent_weather, 'time': agent_time, 'currency': agent_currency,
                   'knowledge': agent_knowledge, 'code': agent_code, 'chat': agent_chat}
