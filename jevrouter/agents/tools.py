"""Keyless agents from v1. Jev only classifies, so each agent pulls what it needs from the text with plain
parsing (pure functions below, unit tested), then calls a free API. The tools of docs/PLAN-speed-evals-chat.md A6 live
here too: unit conversion, date maths, the URL reader (behind an egress guard) and SQL over attached tables."""
import ast
import asyncio
import calendar
import html
import ipaddress
import math
import operator
import re
import socket
import sqlite3
import ssl
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
import certifi
from aiohttp.abc import AbstractResolver
from yarl import URL

from .. import cache
from ..config import (SQL_MAX_ROWS, SQL_TIMEOUT, URL_MAX_BYTES, URL_MAX_REDIRECTS, URL_TIMEOUT, URL_TYPES)

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


async def cached_json(http, ttl: float, url: str, **params):
    """get_json through the live-data cache (jevrouter/cache.py): identical requests within ttl seconds reuse the answer."""
    return await cache.fetch(cache.live_key(url, params), ttl, lambda: get_json(http, url, **params))


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
    data = await cached_json(http, cache.LOOKUP_TTL, 'https://geocoding-api.open-meteo.com/v1/search', name=place, count=1)
    return (data.get('results') or [None])[0]


WMO = {0: 'clear sky', 1: 'mostly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'fog', 48: 'freezing fog', 51: 'light drizzle',
       53: 'drizzle', 55: 'heavy drizzle', 61: 'light rain', 63: 'rain', 65: 'heavy rain', 71: 'light snow', 73: 'snow',
       75: 'heavy snow', 80: 'rain showers', 81: 'heavy showers', 82: 'violent showers', 95: 'thunderstorms', 96: 'thunderstorms with hail'}


async def agent_weather(q: str, http) -> AgentResult:
    place = find_place(q)
    loc = place and await geocode(http, place)
    if not loc:
        return AgentResult('Which city? I need a place to look up the weather.', False)
    d = await cached_json(http, cache.WEATHER_TTL, 'https://api.open-meteo.com/v1/forecast',
                          latitude=loc['latitude'], longitude=loc['longitude'],
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
        known = set(await cached_json(http, cache.LOOKUP_TTL, 'https://api.frankfurter.app/currencies')) or ECB_CODES
    except Exception:
        known = ECB_CODES
    if what := unsupported_currency(q, known):
        return AgentResult(unsupported_answer(what, known), False)
    parsed = parse_currency(q, known)
    if isinstance(parsed, str):
        return AgentResult(ambiguous_currency(q, known) or parsed, False)
    amt, src, dst = parsed
    d = await cached_json(http, cache.RATES_TTL, 'https://api.frankfurter.app/latest', amount=amt, **{'from': src, 'to': dst})
    return AgentResult(f"{amt:,.2f} {src} = {d['rates'][dst]:,.2f} {dst}  (rate from {d['date']})", True, 'frankfurter.app')


# ---------- knowledge, code, chat ----------

FILLER = r"^(?:who|what|when|where|why|how)\s+(?:is|was|are|were|did|does|do)\s+(?:(?:a|an|the)\s+)?|^tell me about\s+(?:the\s+)?|^explain\s+|\?$"


def knowledge_term(q: str) -> str:
    return re.sub(FILLER, '', q.strip(), flags=re.I).strip(' ?') or q


async def ddg_abstract(http, q: str) -> tuple[str, str, str | None]:
    """(term, abstract, url). DuckDuckGo instant answers serve Wikipedia abstracts without Wikipedia's strict bot rate
    limits; when DuckDuckGo fails or has no abstract (it throttles some networks for hours), Wikipedia's own search."""
    term = knowledge_term(q)
    try:
        d = await cached_json(http, cache.LOOKUP_TTL, 'https://api.duckduckgo.com/', q=term, format='json', no_html=1,
                              skip_disambig=1)
    except Exception:
        d = None
    if isinstance(d, dict) and d.get('AbstractText'):
        return term, d['AbstractText'], d.get('AbstractURL') or None
    try:
        text, url = await wiki_abstract(http, term)
    except Exception:
        text, url = '', None
    return term, text, url


async def wiki_abstract(http, term: str) -> tuple[str, str | None]:
    """The lead of the best-matching Wikipedia article: search, then the page summary. Disambiguation pages don't count."""
    found = await cached_json(http, cache.LOOKUP_TTL, 'https://en.wikipedia.org/w/api.php', action='query', list='search',
                              srsearch=term, srlimit=1, format='json')
    hits = ((found or {}).get('query') or {}).get('search') or []
    if not hits:
        return '', None
    title = hits[0]['title']
    page = await cached_json(http, cache.LOOKUP_TTL,
                             'https://en.wikipedia.org/api/rest_v1/page/summary/' + quote(title.replace(' ', '_'), safe=''))
    if not isinstance(page, dict) or page.get('type') == 'disambiguation' or not page.get('extract'):
        return '', None
    return page['extract'], ((page.get('content_urls') or {}).get('desktop') or {}).get('page')


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


# ---------- units ----------

# (category, canonical name, size in the category's base unit, aliases separated by |). Bases: metre, kilogram, litre,
# metre per second, byte, second (a year is 365 days). Temperature has its own conversion. Cups, pints, quarts and gallons are US sizes unless named.
UNIT_DEFS = [
    ('length', 'mm', 0.001, 'mm|millimeter|millimeters|millimetre|millimetres'),
    ('length', 'cm', 0.01, 'cm|centimeter|centimeters|centimetre|centimetres'),
    ('length', 'm', 1.0, 'm|meter|meters|metre|metres'),
    ('length', 'km', 1000.0, 'km|kms|kilometer|kilometers|kilometre|kilometres'),
    ('length', 'inches', 0.0254, 'in|inch|inches|"'),
    ('length', 'ft', 0.3048, 'ft|foot|feet|\''),
    ('length', 'yards', 0.9144, 'yd|yds|yard|yards'),
    ('length', 'miles', 1609.344, 'mi|mile|miles'),
    ('length', 'nautical miles', 1852.0, 'nmi|nautical mile|nautical miles'),
    ('mass', 'mg', 1e-6, 'mg|milligram|milligrams|milligramme|milligrammes'),
    ('mass', 'g', 0.001, 'g|gram|grams|gramme|grammes|gm|gms'),
    ('mass', 'kg', 1.0, 'kg|kgs|kilo|kilos|kilogram|kilograms|kilogramme|kilogrammes'),
    ('mass', 'tonnes', 1000.0, 'tonne|tonnes|metric ton|metric tons'),
    ('mass', 'oz', 0.028349523125, 'oz|ounce|ounces'),
    ('mass', 'lb', 0.45359237, 'lb|lbs|pound|pounds'),
    ('mass', 'stone', 6.35029318, 'st|stone|stones'),
    ('volume', 'ml', 0.001, 'ml|milliliter|milliliters|millilitre|millilitres'),
    ('volume', 'L', 1.0, 'l|liter|liters|litre|litres'),
    ('volume', 'm³', 1000.0, 'm3|m³|cubic meter|cubic meters|cubic metre|cubic metres'),
    ('volume', 'tsp', 0.00492892159375, 'tsp|teaspoon|teaspoons'),
    ('volume', 'tbsp', 0.01478676478125, 'tbsp|tablespoon|tablespoons'),
    ('volume', 'US fl oz', 0.0295735295625, 'fl oz|fluid ounce|fluid ounces'),
    ('volume', 'US cups', 0.2365882365, 'cup|cups'),
    ('volume', 'US pints', 0.473176473, 'pt|pint|pints'),
    ('volume', 'US quarts', 0.946352946, 'qt|quart|quarts'),
    ('volume', 'US gallons', 3.785411784, 'gal|gallon|gallons|us gallon|us gallons'),
    ('volume', 'imperial gallons', 4.54609, 'imperial gallon|imperial gallons|uk gallon|uk gallons'),
    ('speed', 'm/s', 1.0, 'm/s|mps|meters per second|metres per second'),
    ('speed', 'km/h', 1 / 3.6, 'km/h|kmh|kph|km/hr|kmph|kilometers per hour|kilometres per hour'),
    ('speed', 'mph', 0.44704, 'mph|mi/h|miles per hour'),
    ('speed', 'knots', 0.514444, 'kn|kt|kts|knot|knots'),
    ('speed', 'ft/s', 0.3048, 'ft/s|fps|feet per second'),
    ('data', 'bits', 0.125, 'bit|bits'),
    ('data', 'bytes', 1.0, 'b|byte|bytes'),
    ('data', 'KB', 1e3, 'kb|kilobyte|kilobytes'),
    ('data', 'MB', 1e6, 'mb|megabyte|megabytes'),
    ('data', 'GB', 1e9, 'gb|gigabyte|gigabytes'),
    ('data', 'TB', 1e12, 'tb|terabyte|terabytes'),
    ('data', 'PB', 1e15, 'pb|petabyte|petabytes'),
    ('data', 'KiB', 1024.0, 'kib|kibibyte|kibibytes'),
    ('data', 'MiB', 1024.0 ** 2, 'mib|mebibyte|mebibytes'),
    ('data', 'GiB', 1024.0 ** 3, 'gib|gibibyte|gibibytes'),
    ('data', 'TiB', 1024.0 ** 4, 'tib|tebibyte|tebibytes'),
    ('data', 'Mbit', 1e6 / 8, 'mbit|megabit|megabits'),
    ('data', 'Gbit', 1e9 / 8, 'gbit|gigabit|gigabits'),
    ('time', 's', 1.0, 's|sec|secs|second|seconds'),
    ('time', 'min', 60.0, 'min|mins|minute|minutes'),
    ('time', 'h', 3600.0, 'h|hr|hrs|hour|hours'),
    ('time', 'days', 86400.0, 'day|days'),
    ('time', 'weeks', 604800.0, 'wk|wks|week|weeks'),
    ('time', 'fortnights', 1209600.0, 'fortnight|fortnights'),
    ('time', 'years', 31536000.0, 'yr|yrs|year|years|common year|common years'),
    ('time', 'leap years', 31622400.0, 'leap year|leap years'),
    ('temperature', '°C', None, '°c|c|celsius|centigrade|degrees c|degrees celsius|degree celsius'),
    ('temperature', '°F', None, '°f|f|fahrenheit|degrees f|degrees fahrenheit|degree fahrenheit'),
    ('temperature', 'K', None, 'k|kelvin|kelvins'),
]
UNIT_ALIAS = {a: (cat, name, size) for cat, name, size, aliases in UNIT_DEFS for a in aliases.split('|')}
UNIT_RE = '|'.join(re.escape(a) for a in sorted(UNIT_ALIAS, key=len, reverse=True))
UNIT = rf'(?:{UNIT_RE})(?![\w/])'  # always after an amount or a space, so no look-behind
UNIT_AMOUNT = r'(-?\d+(?:\.\d+)?)'
# "5 km to miles", "100°F in Celsius", "convert 3 gallons into litres"
UNITS_TO = re.compile(rf'{UNIT_AMOUNT}\s*({UNIT})\s+(?:to|in|into|as|in terms of|->|=)\s+({UNIT})', re.I)
# "how many feet in a mile", "how many cm are there in 5 inches", "how many pounds is 70 kg"
UNITS_HOW_MANY = re.compile(rf'\bhow many\s+({UNIT})\s+(?:(?:are|is|does|do)\s+(?:there\s+)?)?(?:(?:in|per|make up|makes?|'
                            rf'equals?)\s+)?(?:{UNIT_AMOUNT}\s*|an?\s+|one\s+)?({UNIT})', re.I)
# "6 feet 2 inches", "3 hours and 45 minutes": a larger unit then a smaller one of the same kind, read as one amount.
UNITS_PAIR = re.compile(rf'{UNIT_AMOUNT}\s*({UNIT})\s*(?:,\s*|and\s+)?{UNIT_AMOUNT}\s*({UNIT})', re.I)


def unit_of(word: str) -> tuple[str, str, float | None] | None:
    return UNIT_ALIAS.get(re.sub(r'\s+', ' ', word.strip().lower()))


def to_kelvin(v: float, name: str) -> float:
    return v + 273.15 if name == '°C' else (v - 32) * 5 / 9 + 273.15 if name == '°F' else v


def from_kelvin(v: float, name: str) -> float:
    return v - 273.15 if name == '°C' else (v - 273.15) * 9 / 5 + 32 if name == '°F' else v


def parse_units(q: str) -> tuple[float, tuple, tuple, str] | str | None:
    """(amount, source unit, target unit, the amount as written), a message when the units can't be converted into each
    other, or None when the text holds no conversion. A unit is (category, name, size in the category's base unit)."""
    pairs: dict[str, str] = {}  # a merged "6 feet 2 inches" -> the words it replaced

    def merged(m):
        out = merge_pair(m)
        if out != m.group(0):
            pairs[out] = m.group(0)
        return out
    text = UNITS_PAIR.sub(merged, normalize_numbers(q.replace(',', '')))
    m = UNITS_TO.search(text)
    if m:
        amount, src, dst, raw = float(m.group(1)), unit_of(m.group(2)), unit_of(m.group(3)), f'{m.group(1)} {m.group(2)}'
    elif m := UNITS_HOW_MANY.search(text):
        amount, src, dst = float(m.group(2) or 1), unit_of(m.group(3)), unit_of(m.group(1))
        raw = f'{m.group(2)} {m.group(3)}' if m.group(2) else ''
    else:
        return None
    if not src or not dst:
        return None
    if src[0] != dst[0]:
        return f"I can't convert {src[1]} ({src[0]}) to {dst[1]} ({dst[0]}): they measure different things."
    written = pairs.get(raw.strip()) or f'{fmt_num(amount)} {unit_label(src[1], amount)}'
    return amount, src, dst, written


# Unit names written in the plural, and their singular for an amount of exactly 1.
SINGULAR = {'inches': 'inch', 'yards': 'yard', 'miles': 'mile', 'nautical miles': 'nautical mile', 'tonnes': 'tonne',
            'US cups': 'US cup', 'US pints': 'US pint', 'US quarts': 'US quart', 'US gallons': 'US gallon',
            'imperial gallons': 'imperial gallon', 'knots': 'knot', 'bits': 'bit', 'bytes': 'byte', 'days': 'day',
            'weeks': 'week', 'fortnights': 'fortnight', 'years': 'year', 'leap years': 'leap year'}


def unit_label(name: str, amount: float) -> str:
    return SINGULAR.get(name, name) if amount == 1 else name


def merge_pair(m) -> str:
    big, small = unit_of(m.group(2)), unit_of(m.group(4))
    if not big or not small or big[0] != small[0] or big[0] == 'temperature' or big[2] <= small[2]:
        return m.group(0)
    return f'{plain(float(m.group(1)) * big[2] / small[2] + float(m.group(3)))} {m.group(4)}'


def convert_units(amount: float, src: tuple, dst: tuple) -> float:
    if src[0] == 'temperature':
        return from_kelvin(to_kelvin(amount, src[1]), dst[1])
    return amount * src[2] / dst[2]


def fmt_num(v: float) -> str:
    """Six significant digits with thousands separators, trailing zeros dropped: 1,609.34, 0.621371, 3.28084."""
    if v == 0:
        return '0'
    if abs(v) >= 1e15 or abs(v) < 1e-4:
        return f'{v:.6g}'
    decimals = min(6, max(0, 5 - math.floor(math.log10(abs(v)))))
    out = f'{v:,.{decimals}f}'
    return out.rstrip('0').rstrip('.') if '.' in out else out


def units_question(q: str) -> str | None:
    """A follow-up question when the text asks for a conversion the parser can't read ("How much is a pound?"), else
    None. A word that is also a currency gets the currency agent's question, which offers both meanings."""
    if parse_units(q) is not None:
        return None
    if any(w.lower() in OTHER_SENSE for w in currency_words(q)):
        return currency_question(q)
    return 'What should I convert? Tell me an amount and two units, like "5 km to miles" or "100 °F in Celsius".'


async def agent_units(q: str, http=None) -> AgentResult:
    parsed = parse_units(q)
    if parsed is None:
        return AgentResult('Tell me an amount and two units, like "5 km to miles" or "100 °F in Celsius".', False)
    if isinstance(parsed, str):
        return AgentResult(parsed, False)
    amount, src, dst, written = parsed
    value = convert_units(amount, src, dst)
    if src[0] == 'temperature' and to_kelvin(amount, src[1]) < 0:
        return AgentResult(f'{written} is below absolute zero, so it is not a real temperature.', False)
    return AgentResult(f'{written} = {fmt_num(value)} {unit_label(dst[1], value)}', True)


# ---------- dates ----------

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS['sept'] = 9
MON = '|'.join(sorted(MONTHS, key=len, reverse=True))
DATE_FORMS = [
    re.compile(r'\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b'),
    re.compile(rf'\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m>{MON})\b\.?(?:,?\s+(?P<y>\d{{4}})\b)?', re.I),
    re.compile(rf'\b(?P<m>{MON})\b\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<y>\d{{4}})\b)?', re.I),
    re.compile(r'\b(?P<rel>today|tomorrow|yesterday|now)\b', re.I),
]
SHIFT = re.compile(r'(?P<n>\d+)\s*(?P<u>days?|weeks?|months?|years?)\s+(?P<dir>from|after|before|since|later than|'
                   r'earlier than|past|ago|later|earlier|prior to)?', re.I)
SHIFT_ADD = re.compile(r'\b(?:add|plus)\s+(?P<n>\d+)\s*(?P<u>days?|weeks?|months?|years?)\b|\+\s*(?P<n2>\d+)\s*'
                       r'(?P<u2>days?|weeks?|months?|years?)\b|\bin\s+(?P<n3>\d+)\s*(?P<u3>days?|weeks?|months?|years?)\b'
                       r'(?!\s+(?:from|after|before|ago|since|later|earlier|prior))', re.I)
SHIFT_SUB = re.compile(r'\b(?:subtract|minus|take away|take)\s+(?P<n>\d+)\s*(?P<u>days?|weeks?|months?|years?)\b|-\s*'
                       r'(?P<n2>\d+)\s*(?P<u2>days?|weeks?|months?|years?)\b', re.I)
BETWEEN = re.compile(r'\b(?:between|until|till|since|how long|how many (?:days|weeks)|days? (?:from|to|left)|apart)\b', re.I)
WEEKDAY = re.compile(r'\b(?:what|which)\s+day\b|\bweekday\b|\bday of the week\b', re.I)


def dates_in(q: str, today: date, invalid: list | None = None) -> list[tuple[int, date, bool]]:
    """[(position, date, year given)] for every date written in the text, in reading order. A date without a year is in
    today's year. Written dates that don't exist ("31 February") are added to `invalid` when it is given."""
    found = []
    for form in DATE_FORMS:
        for m in form.finditer(q):
            if any(a <= m.start() < b for a, b, _ in found):
                continue
            g = m.groupdict()
            if g.get('rel'):
                d = today + timedelta(days={'tomorrow': 1, 'yesterday': -1}.get(g['rel'].lower(), 0))
                found.append((m.start(), m.end(), (d, True)))
                continue
            month = int(g['m']) if g['m'].isdigit() else MONTHS[g['m'].lower().rstrip('.')]
            try:
                d = date(int(g['y'] or today.year), month, int(g['d']))
            except ValueError:
                if invalid is not None:
                    invalid.append(m.group(0).strip())
                continue
            found.append((m.start(), m.end(), (d, bool(g['y']))))
    return [(a, *v) for a, _, v in sorted(found, key=lambda x: x[0])]


def shift(d: date, n: int, unit: str) -> date:
    unit = unit.lower().rstrip('s')
    if unit in ('day', 'week'):
        return d + timedelta(days=n * (7 if unit == 'week' else 1))
    months = d.month - 1 + n * (12 if unit == 'year' else 1)
    y, m = d.year + months // 12, months % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))  # 31 January + 1 month is 28 or 29 February


def long_date(d: date) -> str:
    return f'{d:%A} {d.day} {d:%B %Y}'


LEAP = re.compile(r'\bleap\s+years?\b', re.I)
YEAR = re.compile(r'\b(\d{4})\b')
AGE = re.compile(r'\bhow old\b|\byears old\b|\bage\b', re.I)


def leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def full_years(a: date, b: date) -> int:
    a, b = sorted((a, b))
    return b.year - a.year - ((b.month, b.day) < (a.month, a.day))


OUT_OF_RANGE = 'That date is outside the calendar I can work with (years 1 to 9999).'


def date_maths(q: str, today: date | None = None) -> tuple[str, bool]:
    """(answer, ok) for date arithmetic in the text: days between two dates, the weekday of a date, a date some days,
    weeks, months or years before or after another (today when none is given), an age, whether a year is a leap year,
    or a duration in other units ("how many hours in a week"). A date past the calendar's range ("3000000 days after
    today") is an answer that says so, never an exception."""
    try:
        return _date_maths(q, today or date.today())
    except (OverflowError, ValueError):
        return OUT_OF_RANGE, False


def _date_maths(q: str, today: date) -> tuple[str, bool]:
    duration = parse_units(q)
    if isinstance(duration, tuple) and duration[1][0] == 'time':
        amount, src, dst, written = duration
        value = convert_units(amount, src, dst)
        return f'{written} = {fmt_num(value)} {unit_label(dst[1], value)}', True
    invalid: list[str] = []
    found = dates_in(q, today, invalid)
    if invalid:  # never answer about another date (today, or the other one) in place of a date that doesn't exist
        return f'{invalid[0]} is not a real date. Check the day and the month.', False
    if LEAP.search(q) and not found and (y := YEAR.search(q)):
        year = int(y.group(1))
        if leap(year):
            why = 'it is divisible by 400' if year % 400 == 0 else 'it is divisible by 4 and not by 100'
            return f'Yes, {year} is a leap year: {why}.', True
        why = 'it is divisible by 100 but not by 400' if year % 100 == 0 else 'it is not divisible by 4'
        return f'No, {year} is not a leap year: {why}.', True
    if AGE.search(q) and found:
        born = found[0][1]
        at = found[1][1] if len(found) >= 2 else today
        return f'Someone born on {long_date(born)} is {full_years(born, at)} years old on {long_date(at)}.', True
    add, sub = SHIFT_ADD.search(q), SHIFT_SUB.search(q)
    rel = next((m for m in SHIFT.finditer(q) if m.group('dir')), None)
    if add or sub or rel:
        m = add or sub or rel
        g = m.groupdict()
        n = int(next(g[k] for k in ('n', 'n2', 'n3') if g.get(k)))
        unit = next(g[k] for k in ('u', 'u2', 'u3') if g.get(k))
        back = bool(sub) or (rel is not None and not add and rel.group('dir').lower() in
                             ('before', 'ago', 'earlier', 'earlier than', 'prior to'))
        base = next((d for pos, d, _ in found if pos != m.start()), today)
        out = shift(base, -n if back else n, unit)
        word = 'before' if back else 'after'
        return f"{n} {unit.lower()} {word} {long_date(base)} is {long_date(out)}.", True
    if len(found) >= 2 or (found and BETWEEN.search(q)):
        a, b = (found[0][1], found[1][1]) if len(found) >= 2 else (today, found[0][1])
        days = abs((b - a).days)
        first, second = sorted((a, b))
        weeks = f' ({days // 7} weeks and {days % 7} days)' if days >= 14 and days % 7 else f' ({days // 7} weeks)' if days >= 14 else ''
        years = f', {full_years(first, second)} full years' if re.search(r'\byears?\b', q, re.I) else ''
        return f'There are {days:,} days between {long_date(first)} and {long_date(second)}{weeks}{years}.', True
    if found:
        d = found[0][1]
        verb = 'was' if d < today else 'is'
        return f'{d.day} {d:%B %Y} {verb} a {d:%A}.', True
    if WEEKDAY.search(q):
        return f'Today is {long_date(today)}.', True
    return 'Tell me the dates, like "days between 1 March 2025 and 4 July 2025" or "what weekday is 2026-12-25".', False


def dates_question(q: str) -> str | None:
    """A follow-up question when the text has no dates the date agent can work with, else None."""
    answer, ok = date_maths(q)
    return None if ok else answer


async def agent_dates(q: str, http=None) -> AgentResult:
    answer, ok = date_maths(q)
    return AgentResult(answer, ok)


# ---------- URL reader, behind an egress guard ----------

class UrlBlocked(ValueError):
    """The URL points somewhere the reader must never go: a private, local or cloud metadata address."""


class UrlError(ValueError):
    """The page could not be read (bad status, wrong type, too many redirects, timeout)."""


LOCAL_NAMES = re.compile(r'(?:^|\.)(?:localhost|localdomain|local|internal|intranet|lan|home\.arpa|corp|'
                         r'metadata\.google\.internal)\.?$', re.I)
# Numeric host forms a browser or resolver reads as an IPv4 address (2130706433, 0x7f.1, 0177.0.0.1): only the plain
# dotted quad is accepted as written; anything else numeric is refused rather than guessed at.
ODD_NUMERIC = re.compile(r'^(?:0x[0-9a-f]*|\d+)(?:\.(?:0x[0-9a-f]*|\d*))*\.?$', re.I)
NAT64 = ipaddress.ip_network('64:ff9b::/96')
URL_PORTS = {80, 443, 8080, 8443}
URL_IN_TEXT = re.compile(r'https?://[^\s<>"\'`]+', re.I)


def blocked_reason(ip) -> str | None:
    """Why the reader must not connect to this address, or None when it is a public one. IPv6 forms that carry an IPv4
    address (mapped, 6to4, NAT64) are judged by that address; Teredo tunnels are refused outright."""
    try:
        ip = ipaddress.ip_address(str(ip).split('%')[0])
    except ValueError:
        return 'not an IP address'
    if ip.version == 6:
        if ip.teredo:
            return 'a tunnelled address'
        inner = ip.ipv4_mapped or ip.sixtofour or (ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip in NAT64 else None)
        if inner is not None:
            ip = inner
    if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return 'a private or local network address'
    if not ip.is_global:
        return 'a private or local network address'
    return None


async def lookup(host: str, port: int) -> list[str]:
    """Every address the host resolves to (tests replace this to fake DNS)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


async def public_addresses(host: str, port: int) -> list[str]:
    """The host's addresses when every one of them is public; raises UrlBlocked otherwise, so a name that resolves to a
    public and a private address is refused too."""
    host = (host or '').strip('[]').rstrip('.').lower()
    if not host:
        raise UrlBlocked('the link has no host')
    try:
        literal = ipaddress.ip_address(host.split('%')[0])
    except ValueError:
        literal = None
    if literal is None:
        if ODD_NUMERIC.match(host):
            raise UrlBlocked('it writes an IP address in an unusual form')
        if LOCAL_NAMES.search(host) or '.' not in host:
            raise UrlBlocked('it names a local or internal host')
        try:
            addresses = await lookup(host, port)
        except OSError:
            raise UrlError(f'could not find the host {host}')
    else:
        addresses = [str(literal)]
    if not addresses:
        raise UrlError(f'could not find the host {host}')
    for a in addresses:
        if why := blocked_reason(a):
            raise UrlBlocked(f'it points to {why}')
    return addresses


class GuardedResolver(AbstractResolver):
    """Resolves and checks at connect time, and the connection uses exactly the addresses checked, so DNS that changes
    its answer between the check and the connection (rebinding) can't slip a private address in."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict]:
        out = []
        for a in await public_addresses(host, port):
            fam = socket.AF_INET6 if ':' in a else socket.AF_INET
            out.append({'hostname': host, 'host': a, 'port': port, 'family': fam, 'proto': 0,
                        'flags': socket.AI_NUMERICHOST})
        return out

    async def close(self) -> None:
        pass


def check_url(raw: str) -> URL:
    """The URL when its form is acceptable: http or https, no user name or password, one of the usual web ports."""
    try:
        u = URL(raw.strip())
    except (ValueError, TypeError):
        raise UrlError('that is not a valid link')
    if u.scheme not in ('http', 'https') or not u.host:
        raise UrlBlocked('only http and https links can be read')
    if u.user or u.password:
        raise UrlBlocked('links with a user name or password are not read')
    if u.port not in URL_PORTS:
        raise UrlBlocked(f'port {u.port} is not a web port')
    return u


async def fetch_page(raw: str) -> tuple[str, str, str, bool]:
    """(final URL, content type, text, truncated) of a public web page. Every hop, redirects included, passes the guard
    before a connection is made; the body is read up to URL_MAX_BYTES and only text-like content types are accepted."""
    connector = aiohttp.TCPConnector(resolver=GuardedResolver(), ssl=SSL, use_dns_cache=False, limit=2)
    timeout = aiohttp.ClientTimeout(total=URL_TIMEOUT, connect=URL_TIMEOUT / 2)
    url = raw
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=UA, trust_env=False) as s:
            for _ in range(URL_MAX_REDIRECTS + 1):
                u = check_url(str(url))
                await public_addresses(u.host, u.port)  # IP literals never reach the resolver, so check here too
                async with s.get(u, allow_redirects=False, headers={'Accept': 'text/html,text/plain;q=0.9,*/*;q=0.1'}) as r:
                    if r.status in (301, 302, 303, 307, 308):
                        loc = r.headers.get('Location')
                        if not loc:
                            raise UrlError(f'the page redirected ({r.status}) without saying where')
                        url = u.join(URL(loc))
                        continue
                    if r.status >= 400:
                        raise UrlError(f'the site answered {r.status}')
                    ctype = (r.content_type or '').lower()
                    if ctype not in URL_TYPES:
                        raise UrlError(f'it is {ctype or "an unknown type"}, not a text page')
                    if r.content_length and r.content_length > URL_MAX_BYTES * 4:
                        raise UrlError('the page is too large to read')
                    body, truncated = bytearray(), False
                    async for chunk in r.content.iter_chunked(65536):
                        body += chunk
                        if len(body) > URL_MAX_BYTES:
                            del body[URL_MAX_BYTES:]
                            truncated = True
                            break
                    return str(u), ctype, body.decode(r.charset or 'utf-8', errors='replace'), truncated
            raise UrlError('it redirected too many times')
    except (UrlBlocked, UrlError):
        raise
    except asyncio.TimeoutError:
        raise UrlError(f'the site did not answer within {URL_TIMEOUT:.0f} seconds')
    except aiohttp.ClientConnectorError as e:
        if isinstance(e.os_error, (UrlBlocked, UrlError)):
            raise e.os_error
        raise UrlError('could not connect to the site')
    except aiohttp.ClientError as e:
        cause = e.__cause__ or e.__context__
        if isinstance(cause, (UrlBlocked, UrlError)):
            raise cause
        raise UrlError(f'the request failed ({type(e).__name__})')


class TextOf(HTMLParser):
    """Readable text of an HTML page: the title, and the body without scripts, styles and navigation chrome."""
    SKIP = {'script', 'style', 'noscript', 'svg', 'template', 'iframe', 'head', 'nav', 'footer', 'form'}
    BLOCK = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article', 'pre', 'blockquote',
             'table', 'ul', 'ol', 'dd', 'dt'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.parts, self.skip, self.in_title = '', [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self.in_title = True
        elif tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        elif tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def page_text(ctype: str, body: str) -> tuple[str, str]:
    """(title, readable text) of a fetched page."""
    if 'html' not in ctype and 'xml' not in ctype:
        return '', body.strip()
    p = TextOf()
    try:
        p.feed(body)
        p.close()
    except Exception:
        pass
    lines = [' '.join(l.split()) for l in ''.join(p.parts).splitlines()]
    return ' '.join(p.title.split()), '\n'.join(l for l in lines if l)


def url_in(text: str) -> str | None:
    m = URL_IN_TEXT.search(text or '')
    return m.group(0).rstrip('.,;:!?)]}') if m else None


async def read_page(q: str) -> tuple[str, str, str, bool]:
    """(final URL, title, text, truncated) of the link in the text; raises UrlBlocked or UrlError with a plain reason."""
    url = url_in(q)
    if not url:
        raise UrlError('there is no http or https link in the request')
    final, ctype, body, truncated = await fetch_page(url)
    title, text = page_text(ctype, body)
    return final, title, text, truncated


def url_failure(e: Exception) -> AgentResult:
    if isinstance(e, UrlError) and 'no http or https link' in str(e):
        return AgentResult('Which page? Send the full link, starting with http:// or https://.', False)
    if isinstance(e, UrlBlocked):
        return AgentResult(f"I can't open that link: {e}.", False)
    return AgentResult(f"I couldn't read that page: {e}.", False)


async def agent_url(q: str, http=None) -> AgentResult:
    """Keyless: the page's title and its opening paragraphs."""
    try:
        final, title, text, truncated = await read_page(q)
    except (UrlBlocked, UrlError) as e:
        return url_failure(e)
    if not text:
        return AgentResult(f'The page at {final} has no readable text.', False, final)
    # Prose paragraphs first: menus, captions and infobox lines are short and rarely end a sentence.
    paras = [p for p in text.split('\n') if len(p) >= 80 and re.search(r'[.!?]', p)] or text.split('\n')
    opening, n = [], 0
    for para in paras:
        opening.append(para)
        n += len(para)
        if n > 700:
            break
    head = f'**{title}**\n\n' if title else ''
    return AgentResult(head + '\n\n'.join(opening)[:1200], True, final)


# ---------- SQL over attached tables ----------

class SqlError(ValueError):
    pass


SQL_START = re.compile(r'^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*(?:select|with)\b', re.I | re.S)
SQL_IN_TEXT = re.compile(r'```(?:sql)?\s*(.+?)```|(\b(?:select|with)\b.+)', re.I | re.S)
SQL_NA = {'', 'n/a', 'na', 'null', 'none', 'nan', '-', '?'}
# What a query may do: read tables and call functions. Everything else (writes, schema changes, ATTACH, PRAGMA,
# transactions) is refused by SQLite itself before it runs.
SQL_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, 'SQLITE_RECURSIVE', 33)}
SQL_DENY_FUNCS = {'load_extension', 'readfile', 'writefile', 'edit', 'fts3_tokenizer', 'zipfile', 'sqlar_compress'}


# SQLite's keywords: a table named after one ("order.csv") would need quoting in every query the SQL writer produces.
SQL_KEYWORDS = set("""abort action add after all alter always analyze and as asc attach autoincrement before begin
between by cascade case cast check collate column commit conflict constraint create cross current current_date
current_time current_timestamp database default deferrable deferred delete desc detach distinct do drop each else end
escape except exclude exclusive exists explain fail filter first following for foreign from full generated glob group
groups having if ignore immediate in index indexed initially inner insert instead intersect into is isnull join key
last left like limit match materialized natural no not nothing notnull null nulls of offset on or order others outer
over partition plan pragma preceding primary query raise range recursive references regexp reindex release rename
replace restrict returning right rollback row rows savepoint select set table temp temporary then ties to transaction
trigger unbounded union unique update using vacuum values view virtual when where window with without""".split())


def table_name(name: str, taken: set) -> str:
    base = re.sub(r'[^a-z0-9_]+', '_', name.rsplit('.', 1)[0].lower()).strip('_') or 'data'
    base = f't_{base}' if base[0].isdigit() or base in SQL_KEYWORDS else base
    out, n = base, 2
    while out in taken:
        out, n = f'{base}_{n}', n + 1
    taken.add(out)
    return out


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sql_value(v: str):
    t = v.strip()
    if t.lower() in SQL_NA:
        return None
    try:
        f = float(t.replace(',', '').replace('$', ''))
    except ValueError:
        return v
    return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f


def sql_tables(files: list[dict], texts: dict[str, str]) -> list[tuple[str, str, list[str], list[list]]]:
    """[(table, file name, columns, typed rows)] for every attached table. A column is numeric when every filled cell
    is a number, so SUM and comparisons work on it; otherwise its cells stay text."""
    from ..files import to_table  # files imports this module
    taken, out = set(), []
    for f in files:
        t = to_table(f.get('kind', ''), texts.get(f['id'], ''))
        if not t:
            continue
        cols, rows = t
        cols = [c.strip() or f'column_{i + 1}' for i, c in enumerate(cols)]
        cols = [c if cols.index(c) == i else f'{c}_{i + 1}' for i, c in enumerate(cols)]  # duplicate headers
        width = len(cols)
        typed = [[sql_value(r[i]) if i < len(r) else None for i in range(width)] for r in rows]
        for i in range(width):
            if any(isinstance(r[i], str) for r in typed):
                for r, raw in zip(typed, rows):
                    r[i] = None if i >= len(raw) or raw[i].strip().lower() in SQL_NA else raw[i]
        out.append((table_name(f['name'], taken), f['name'], cols, typed))
    return out


def schema_text(tables) -> str:
    """The tables as the SQL writer sees them: CREATE TABLE lines plus a few sample rows each."""
    parts = []
    for name, file, cols, rows in tables:
        kinds = ['REAL' if all(r[i] is None or isinstance(r[i], (int, float)) for r in rows) else 'TEXT'
                 for i in range(len(cols))]
        parts.append(f'-- from {file}, {len(rows)} rows\nCREATE TABLE {name} (' +
                     ', '.join(f'{quote_ident(c)} {k}' for c, k in zip(cols, kinds)) + ');')
        for r in rows[:5]:
            parts.append(f'-- sample: {r}')
    return '\n'.join(parts)


def run_sql(tables, sql: str, max_rows: int = SQL_MAX_ROWS, timeout: float = SQL_TIMEOUT) -> tuple[list[str], list, bool]:
    """(columns, rows, truncated) of one read-only SELECT over the tables, in a private in-memory database. Blocking:
    callers run it in a thread. Only SELECT (or WITH ... SELECT) is accepted, one statement, and SQLite's authorizer
    refuses anything but reading; the query stops after `timeout` seconds and at most max_rows rows are returned."""
    sql = sql.strip().rstrip(';').strip()
    if not SQL_START.match(sql):
        raise SqlError('only SELECT queries can be run')
    con = sqlite3.connect(':memory:')
    try:
        try:
            for name, _, cols, rows in tables:
                con.execute(f'CREATE TABLE {quote_ident(name)} (' + ', '.join(quote_ident(c) for c in cols) + ')')
                con.executemany(f'INSERT INTO {quote_ident(name)} VALUES ({", ".join("?" * len(cols))})', rows)
            con.commit()
        except sqlite3.Error as e:
            raise SqlError(f'the attached tables could not be loaded: {e}')
        con.execute('PRAGMA query_only = ON')
        for limit, value in ((sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000), (sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20_000),
                             (sqlite3.SQLITE_LIMIT_ATTACHED, 0)):
            con.setlimit(limit, value)

        def authorize(action, arg1, arg2, db, source):
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in SQL_DENY_FUNCS:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if action in SQL_ALLOWED else sqlite3.SQLITE_DENY
        con.set_authorizer(authorize)
        deadline = clock.monotonic() + timeout
        con.set_progress_handler(lambda: 1 if clock.monotonic() > deadline else 0, 2000)
        try:
            cur = con.execute(sql)
            got = cur.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as e:
            if 'interrupted' in str(e):
                raise SqlError(f'the query ran longer than {timeout:g} seconds')
            raise SqlError(str(e))
        except (sqlite3.DatabaseError, sqlite3.Warning, sqlite3.ProgrammingError) as e:
            raise SqlError(str(e))
        cols = [d[0] for d in cur.description or []]
        return cols, [list(r) for r in got[:max_rows]], len(got) > max_rows
    finally:
        con.close()


def sql_cell(v) -> str:
    if v is None:
        return ''
    if isinstance(v, float):
        return fmt_num(v) if not v.is_integer() else f'{int(v):,}'
    if isinstance(v, int):
        return f'{v:,}' if abs(v) >= 10000 else str(v)
    return str(v).replace('|', '\\|').replace('\n', ' ')[:200]


def sql_answer(sql: str, cols: list[str], rows: list, truncated: bool) -> str:
    """The result as Markdown: one value on its own, otherwise a table, then the query that produced it."""
    query = f'\n\nQuery:\n```sql\n{sql.strip()}\n```'
    if not rows:
        return 'The query returned no rows.' + query
    if len(rows) == 1 and len(cols) == 1:
        return f'**{cols[0]}**: {sql_cell(rows[0][0])}' + query
    lines = ['| ' + ' | '.join(sql_cell(c) for c in cols) + ' |', '| ' + ' | '.join('---' for _ in cols) + ' |']
    lines += ['| ' + ' | '.join(sql_cell(v) for v in r) + ' |' for r in rows]
    more = f'\n\nShowing the first {len(rows)} rows.' if truncated else ''
    return '\n'.join(lines) + more + query


def sql_in(text: str) -> str | None:
    """A SELECT the user wrote themselves, in a code fence or inline."""
    m = SQL_IN_TEXT.search(text or '')
    if not m:
        return None
    sql = (m.group(1) or m.group(2) or '').strip().strip('`').strip()
    return sql if SQL_START.match(sql) and re.search(r'\bfrom\b|\bselect\s+\S', sql, re.I) else None


def sql_agent(files: list[dict], texts: dict[str, str], engine=None):
    """The `sql` agent for a run's attached tables. A SELECT in the question runs as written; otherwise the engine writes
    one from the schema (keyless: the tables and an example are shown). The answer is the exact result and the query."""
    from .llm import write_sql  # llm imports this module
    tables = sql_tables(files, texts)

    async def run(q: str, emit_delta) -> AgentResult:
        source = ', '.join(f for _, f, _, _ in tables)
        if not tables:
            return done(AgentResult('None of the attached files is a table (CSV, or JSON list of objects).', False), emit_delta)
        sql, tin, tout, eng = sql_in(q), 0, 0, 'keyless'
        if sql is None and engine is None:
            name, _, cols, _ = tables[0]
            listing = '\n'.join(f'- {t}: ' + ', '.join(c) for t, _, c, _ in tables)
            example = f'SELECT {quote_ident(cols[0])}, COUNT(*) FROM {name} GROUP BY 1'
            return done(AgentResult(f'Write the SELECT query to run. The tables are:\n{listing}\n\nFor example: '
                                    f'`{example}`', False), emit_delta)
        error = None
        for attempt in range(1 if sql is not None else 2):  # the engine gets a second try, told why its first failed
            if eng == 'keyless' and sql is None or error:
                sql, i, o = await write_sql(engine, q, schema_text(tables), sql, error)
                tin, tout, eng = tin + i, tout + o, engine.name
            try:
                cols, rows, truncated = await asyncio.to_thread(run_sql, tables, sql)
            except SqlError as e:
                error = str(e)
                continue
            return done(AgentResult(sql_answer(sql, cols, rows, truncated), True, source, eng, tin, tout), emit_delta)
        return done(AgentResult(f'The query failed: {error}\n\n```sql\n{sql}\n```', False, source, eng, tin, tout),
                    emit_delta)
    return run


def done(out: AgentResult, emit_delta) -> AgentResult:
    emit_delta(out.answer)
    return out


KEYLESS_RUNNERS = {'math': agent_math, 'weather': agent_weather, 'time': agent_time, 'currency': agent_currency,
                   'knowledge': agent_knowledge, 'code': agent_code, 'chat': agent_chat, 'units': agent_units,
                   'dates': agent_dates, 'url': agent_url}
