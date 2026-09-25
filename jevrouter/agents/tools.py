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
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import certifi

SSL = ssl.create_default_context(cafile=certifi.where())
UA = {'User-Agent': 'JevRouter/0.2 (local demo app; python-aiohttp)'}


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


def solve_math(q: str) -> tuple[str, float | int] | None:
    """Returns (expression, value) or None when there's no numeric expression."""
    s = q.lower().replace(',', '')
    for pat, rep in MATH_WORDS:
        s = re.sub(pat, rep, s)
    spans = re.findall(r'(?:sqrt|log|ln|sin|cos|tan|abs|[\d.()+\-*/%\s])+', s)
    expr = max((x.strip() for x in spans), key=len, default='')
    if not re.search(r'\d', expr):
        return None
    value = round(safe_eval(ast.parse(expr, mode='eval')), 10)
    return expr, int(value) if isinstance(value, int) or value.is_integer() else value


async def agent_math(q: str, http=None) -> AgentResult:
    solved = solve_math(q)
    if not solved:
        return AgentResult("I couldn't find a numeric expression in that.", False)
    expr, v = solved
    return AgentResult(f'{expr} = {v:,}' if isinstance(v, int) else f'{expr} = {v}', True)


# ---------- places: weather, time ----------

def find_place(q: str) -> str | None:
    m = re.search(r"\b(?:in|at|for)\s+([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:today|tomorrow|tonight|now|right now|this|next|on)\b|[?.!,]|$)", q, re.I)
    if m:
        return m.group(1).strip()
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


async def agent_time(q: str, http) -> AgentResult:
    place = find_place(q)
    loc = place and await geocode(http, place)
    now = datetime.now(ZoneInfo(loc['timezone']) if loc else None)
    where = f"{loc['name']}, {loc.get('country', '')}".strip(', ') if loc else 'your machine'
    return AgentResult(f"{where}: {now.strftime('%-I:%M %p, %A %-d %B %Y')} ({now.tzname()})", True)


# ---------- currency ----------

CURRENCY_NAMES = {'dollar': 'USD', 'dollars': 'USD', 'usd': 'USD', 'rupee': 'INR', 'rupees': 'INR', 'euro': 'EUR', 'euros': 'EUR',
                  'pound': 'GBP', 'pounds': 'GBP', 'yen': 'JPY', 'yuan': 'CNY', 'franc': 'CHF', 'francs': 'CHF'}


def parse_currency(q: str, known) -> tuple[float, str, str] | str:
    """Returns (amount, src, dst), or an error message. `known` is the set of supported codes."""
    words = re.sub(r'[^\w.\s]', ' ', q.lower()).split()
    codes = [CURRENCY_NAMES.get(w) or (w.upper() if len(w) == 3 and w.isalpha() else None) for w in words]
    found = [(i, c) for i, c in enumerate(codes) if c and c in known]
    if len(found) < 2:
        return 'Tell me two currencies, like "100 USD to INR".'
    num = next((i for i, w in enumerate(words) if re.fullmatch(r'\d+(?:\.\d+)?', w)), None)
    amt = float(words[num]) if num is not None else 1.0
    # The currency right after the amount is the source ("how many euros is 100 pounds" → GBP to EUR).
    src_i = next((k for k, (i, _) in enumerate(found) if num is not None and i == num + 1), 0)
    src = found[src_i][1]
    dst = next((c for k, (_, c) in enumerate(found) if k != src_i and c != src), None)
    if not dst:
        return 'Tell me two different currencies, like "100 USD to INR".'
    return amt, src, dst


async def agent_currency(q: str, http) -> AgentResult:
    parsed = parse_currency(q, await get_json(http, 'https://api.frankfurter.app/currencies'))
    if isinstance(parsed, str):
        return AgentResult(parsed, False)
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


def clarify(top: list) -> AgentResult:
    names = [a for a, _ in top[:2]] + ['another'] * 2
    return AgentResult(f'Not sure what you need. Did you mean something for the {names[0]} agent or the {names[1]} agent? Add a bit more detail.', False)


BLOCKED = AgentResult("I can't help with that one.", False)

KEYLESS_RUNNERS = {'math': agent_math, 'weather': agent_weather, 'time': agent_time, 'currency': agent_currency,
                   'knowledge': agent_knowledge, 'code': agent_code, 'chat': agent_chat}
