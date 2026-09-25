"""Jev Router: Jev routes any query to a specialist agent, and the page graphs every decision live.

Run: .venv/bin/python server.py   then open http://localhost:8777
The key is read from TYPESAFE_API_KEY, or from a .env file next to this script.
"""
import ast
import html
import asyncio
import itertools
import json
import math
import operator
import os
import random
import re
import ssl
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

import aiohttp
import certifi
from aiohttp import web
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

HERE = Path(__file__).parent
PAGE = HERE / 'index.html'
SSL = ssl.create_default_context(cafile=certifi.where())
UA = {'User-Agent': 'JevRouter/0.1 (local demo app; python-aiohttp)'}
PRICE_PER_INPUT_TOKEN = 0.042 / 1e6

# The route choice Jev makes. Descriptions are what Jev reads, so they say when each agent applies.
AGENTS = {
    'math': 'Arithmetic, percentages, or evaluating a numeric expression',
    'weather': 'Current weather or a forecast for a place',
    'time': 'The current local time or date in a city or timezone',
    'currency': 'Converting an amount of money between currencies, or an exchange rate',
    'knowledge': 'A factual question about a person, place, event, thing, or concept',
    'code': 'Programming, software errors, or how to do something in code',
    'chat': 'Greetings, small talk, or questions about the assistant itself',
}
# Guard outcomes that are decided from Jev's other answers rather than picked by the route question.
GUARDS = ['clarify', 'blocked']

QUESTIONS = {
    'route': Choice(instructions='Which specialist agent should handle this user query?', criteria=AGENTS),
    'urgency': Score(instructions='How urgently does the user need an answer?', criteria=['No rush', 'Soon', 'Right now']),
    'unsafe': Noul(instructions='The query asks for help with something harmful, illegal, sexual, or hateful.'),
    'clear': Noul(instructions='The query is clear enough to answer without asking a follow-up question.'),
}
MIN_CONFIDENCE = 0.45
BLOCK_AT = 0.7

SAMPLES = [
    "What's 18% of 2450?", '(45 * 12) / 7 + 3^2', 'square root of 1764',
    'Will it rain in Mumbai tomorrow?', "What's the weather in Tokyo right now?", 'Is it cold in Oslo today?',
    'What time is it in New York?', "What's the date in Sydney right now?",
    'Convert 250 USD to INR', 'How many euros is 100 pounds?', 'JPY to EUR rate',
    'Who was Ada Lovelace?', 'What is a black hole?', 'Tell me about the Eiffel Tower',
    'How do I reverse a list in Python?', 'TypeError: undefined is not a function in JavaScript',
    'git undo last commit', 'How to center a div with CSS',
    'hey, how are you?', 'What can you do?', 'thanks, that helped!',
    'hmm', 'the thing from before',
]

client: AsyncTypeSafeClient | None = None
http: aiohttp.ClientSession | None = None
subscribers: set[asyncio.Queue] = set()
ids = itertools.count(1)
history: deque = deque(maxlen=120)
stats = {
    'queries': 0, 'errors': 0, 'input_tokens': 0,
    'by_agent': {a: 0 for a in [*AGENTS, *GUARDS]},
}
state = {'autopilot': False, 'interval': 3.0}


def broadcast(event: dict):
    msg = json.dumps(event)
    for q in list(subscribers):
        if q.qsize() < 300:
            q.put_nowait(msg)


async def get_json(url: str, **params) -> dict:
    for attempt in range(3):
        async with http.get(url, params=params or None, headers=UA, ssl=SSL, timeout=aiohttp.ClientTimeout(total=8)) as r:
            # Free APIs rate-limit bursts (autopilot); back off briefly instead of failing the query.
            if r.status == 429 and attempt < 2:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            r.raise_for_status()
            return await r.json(content_type=None)


# ---------- specialist agents ----------
# Jev only classifies, so each agent pulls what it needs from the text with plain parsing, then calls a free API.

MATH_WORDS = [
    (r'\bsquare root of\s*([\d.]+)', r'sqrt(\1)'), (r'([\d.]+)\s*%\s*of\s*([\d.]+)', r'(\1/100*\2)'),
    (r'\bmultiplied by\b|\btimes\b|(?<=\d)\s*x\s*(?=\d)', '*'), (r'\bdivided by\b|\bover\b', '/'),
    (r'\bplus\b', '+'), (r'\bminus\b', '-'), (r'\bto the power of\b|\^', '**'),
]
MATH_FUNCS = {'sqrt': math.sqrt, 'log': math.log10, 'ln': math.log, 'sin': math.sin, 'cos': math.cos, 'tan': math.tan, 'abs': abs}
MATH_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
            ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_eval(node):
    if isinstance(node, ast.Expression):
        return safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in MATH_OPS:
        left, right = safe_eval(node.left), safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError('exponent too large')
        return MATH_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in MATH_OPS:
        return MATH_OPS[type(node.op)](safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in MATH_FUNCS and len(node.args) == 1:
        return MATH_FUNCS[node.func.id](safe_eval(node.args[0]))
    raise ValueError('unsupported expression')


async def agent_math(q: str) -> dict:
    s = q.lower().replace(',', '')
    for pat, rep in MATH_WORDS:
        s = re.sub(pat, rep, s)
    spans = re.findall(r'(?:sqrt|log|ln|sin|cos|tan|abs|[\d.()+\-*/%\s])+', s)
    expr = max((x.strip() for x in spans), key=len, default='')
    if not re.search(r'\d', expr):
        return {'answer': "I couldn't find a numeric expression in that.", 'ok': False}
    value = safe_eval(ast.parse(expr, mode='eval'))
    value = round(value, 10)
    shown = int(value) if float(value).is_integer() else value
    return {'answer': f'{expr} = {shown:,}' if isinstance(shown, int) else f'{expr} = {shown}', 'ok': True}


def find_place(q: str) -> str | None:
    m = re.search(r"\b(?:in|at|for)\s+([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:today|tomorrow|tonight|now|right now|this|next|on)\b|[?.!,]|$)", q, re.I)
    if m:
        return m.group(1).strip()
    caps = re.findall(r"(?<!^)(?<![.?!]\s)\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)", q)
    return caps[-1] if caps else None


async def geocode(place: str) -> dict | None:
    data = await get_json('https://geocoding-api.open-meteo.com/v1/search', name=place, count=1)
    return (data.get('results') or [None])[0]


WMO = {0: 'clear sky', 1: 'mostly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'fog', 48: 'freezing fog', 51: 'light drizzle',
       53: 'drizzle', 55: 'heavy drizzle', 61: 'light rain', 63: 'rain', 65: 'heavy rain', 71: 'light snow', 73: 'snow',
       75: 'heavy snow', 80: 'rain showers', 81: 'heavy showers', 82: 'violent showers', 95: 'thunderstorms', 96: 'thunderstorms with hail'}


async def agent_weather(q: str) -> dict:
    place = find_place(q)
    loc = place and await geocode(place)
    if not loc:
        return {'answer': 'Which city? I need a place to look up the weather.', 'ok': False}
    d = await get_json('https://api.open-meteo.com/v1/forecast', latitude=loc['latitude'], longitude=loc['longitude'],
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
    return {'answer': '\n'.join(lines), 'ok': True, 'source': 'open-meteo.com'}


async def agent_time(q: str) -> dict:
    place = find_place(q)
    loc = place and await geocode(place)
    tz = ZoneInfo(loc['timezone']) if loc else None
    now = datetime.now(tz)
    where = f"{loc['name']}, {loc.get('country', '')}".strip(', ') if loc else 'your machine'
    return {'answer': f"{where}: {now.strftime('%-I:%M %p, %A %-d %B %Y')} ({now.tzname()})", 'ok': True}


CURRENCY_NAMES = {'dollar': 'USD', 'dollars': 'USD', 'usd': 'USD', 'rupee': 'INR', 'rupees': 'INR', 'euro': 'EUR', 'euros': 'EUR',
                  'pound': 'GBP', 'pounds': 'GBP', 'yen': 'JPY', 'yuan': 'CNY', 'franc': 'CHF', 'francs': 'CHF'}


async def agent_currency(q: str) -> dict:
    words = re.sub(r'[^\w.\s]', ' ', q.lower()).split()
    codes = [CURRENCY_NAMES.get(w) or (w.upper() if len(w) == 3 and w.isalpha() else None) for w in words]
    rates = await get_json('https://api.frankfurter.app/currencies')
    found = [(i, c) for i, c in enumerate(codes) if c and c in rates]
    if len(found) < 2:
        return {'answer': 'Tell me two currencies, like "100 USD to INR".', 'ok': False}
    num = next((i for i, w in enumerate(words) if re.fullmatch(r'\d+(?:\.\d+)?', w)), None)
    amt = float(words[num]) if num is not None else 1.0
    # The currency right after the amount is the source ("how many euros is 100 pounds" → GBP to EUR).
    src_i = next((k for k, (i, _) in enumerate(found) if num is not None and i == num + 1), 0)
    src = found[src_i][1]
    dst = next((c for k, (_, c) in enumerate(found) if k != src_i and c != src), None)
    if not dst:
        return {'answer': 'Tell me two different currencies, like "100 USD to INR".', 'ok': False}
    d = await get_json('https://api.frankfurter.app/latest', amount=amt, **{'from': src, 'to': dst})
    return {'answer': f"{amt:,.2f} {src} = {d['rates'][dst]:,.2f} {dst}  (rate from {d['date']})", 'ok': True, 'source': 'frankfurter.app'}


FILLER = r"^(?:who|what|when|where|why|how)\s+(?:is|was|are|were|did|does|do)\s+(?:(?:a|an|the)\s+)?|^tell me about\s+(?:the\s+)?|^explain\s+|\?$"


async def agent_knowledge(q: str) -> dict:
    # DuckDuckGo instant answers serve Wikipedia abstracts without Wikipedia's strict bot rate limits.
    term = re.sub(FILLER, '', q.strip(), flags=re.I).strip(' ?') or q
    d = await get_json('https://api.duckduckgo.com/', q=term, format='json', no_html=1, skip_disambig=1)
    text = d.get('AbstractText') or ''
    if not text:
        return {'answer': f'No summary found for "{term}".', 'ok': False}
    short = ' '.join(re.split(r'(?<=[.!?])\s+', text)[:3])
    return {'answer': short, 'ok': True, 'source': d.get('AbstractURL') or 'duckduckgo.com'}


async def agent_code(q: str) -> dict:
    term = re.sub(r'^(?:how (?:do|can) i|how to)\s+', '', q.strip(), flags=re.I).strip(' ?')
    d = await get_json('https://api.stackexchange.com/2.3/search/advanced', q=term, site='stackoverflow', accepted='True',
                       sort='relevance', order='desc', pagesize=3)
    items = d.get('items') or []
    if not items:
        return {'answer': 'No accepted Stack Overflow answers matched that.', 'ok': False}
    lines = [f"• {html.unescape(i['title'])} ({i['score']} votes)" for i in items]
    return {'answer': 'Top answered threads on Stack Overflow:\n' + '\n'.join(lines), 'ok': True, 'source': items[0]['link']}


async def agent_chat(q: str) -> dict:
    s = q.lower()
    if re.search(r'\b(thanks|thank you|thx)\b', s):
        text = "You're welcome!"
    elif re.search(r'\b(what can you|help|who are you|what are you)\b', s):
        text = 'Jev reads each query and routes it to a specialist: math, weather, time, currency, knowledge, or code. Try one.'
    else:
        text = "Hey! Ask me a calculation, the weather, a time zone, a currency conversion, a fact, or a coding question."
    return {'answer': text, 'ok': True}


async def agent_clarify(q: str, top: list) -> dict:
    a, b = top[0][0], top[1][0]
    return {'answer': f'Not sure what you need. Did you mean something for the {a} agent or the {b} agent? Add a bit more detail.', 'ok': False}


RUNNERS = {'math': agent_math, 'weather': agent_weather, 'time': agent_time, 'currency': agent_currency,
           'knowledge': agent_knowledge, 'code': agent_code, 'chat': agent_chat}


# ---------- routing pipeline ----------

async def handle(query: str, source: str):
    qid = next(ids)
    t0 = time.perf_counter()
    broadcast({'type': 'query', 'id': qid, 'text': query, 'source': source})
    try:
        r = await client.system_one(query, QUESTIONS)
    except Exception as e:
        stats['errors'] += 1
        broadcast({'type': 'error', 'id': qid, 'message': f'Jev: {str(e)[:200]}'})
        return
    jev_ms = round((time.perf_counter() - t0) * 1000)
    route, urgency, unsafe, clear = (r.answers[k] for k in ('route', 'urgency', 'unsafe', 'clear'))
    probs = dict(sorted(route.probabilities.items(), key=lambda kv: -kv[1]))
    if unsafe.noul >= BLOCK_AT:
        agent, reason = 'blocked', f'Jev flagged it as unsafe ({unsafe.noul:.0%})'
    elif route.confidence < MIN_CONFIDENCE or clear.noul < 0.25:
        agent, reason = 'clarify', f'low confidence ({route.confidence:.0%}) or unclear ({clear.noul:.0%})'
    else:
        agent, reason = route.choice, f'{route.choice} at {route.confidence:.0%}'
    stats['queries'] += 1
    stats['input_tokens'] += r.usage.input_tokens or 0
    stats['by_agent'][agent] += 1
    routed = {
        'type': 'routed', 'id': qid, 'agent': agent, 'pick': route.choice, 'reason': reason,
        'probabilities': probs, 'confidence': route.confidence, 'urgency': round(urgency.score, 2),
        'unsafe': unsafe.noul, 'clear': clear.noul, 'jev_ms': jev_ms, 'model': r.model, 'stats': stats,
    }
    broadcast(routed)

    t1 = time.perf_counter()
    try:
        if agent == 'blocked':
            out = {'answer': "I can't help with that one.", 'ok': False}
        elif agent == 'clarify':
            out = await agent_clarify(query, list(probs.items()))
        else:
            out = await RUNNERS[agent](query)
    except Exception as e:
        out = {'answer': f'{agent} agent failed: {str(e)[:160]}', 'ok': False}
    done = {'type': 'answered', 'id': qid, 'agent': agent, 'agent_ms': round((time.perf_counter() - t1) * 1000), **out}
    broadcast(done)
    history.append({**routed, **done, 'type': 'record', 'text': query, 'source': source, 'at': time.time()})


async def autopilot():
    while True:
        await asyncio.sleep(state['interval'])
        if state['autopilot'] and subscribers:
            asyncio.create_task(handle(random.choice(SAMPLES), 'autopilot'))


# ---------- http ----------

async def index(_):
    return web.FileResponse(PAGE)


async def ask(request):
    body = await request.json()
    text = str(body.get('query', '')).strip()[:500]
    if not text:
        return web.json_response({'error': 'empty query'}, status=400)
    asyncio.create_task(handle(text, 'you'))
    return web.json_response({'ok': True})


async def control(request):
    body = await request.json()
    if 'autopilot' in body:
        state['autopilot'] = bool(body['autopilot'])
    if 'interval' in body:
        state['interval'] = max(1.0, min(15.0, float(body['interval'])))
    broadcast({'type': 'state', 'state': state})
    return web.json_response(state)


async def events(request):
    resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'})
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)
    hello = {'type': 'hello', 'agents': AGENTS, 'guards': GUARDS, 'state': state, 'stats': stats,
             'history': list(history), 'samples': SAMPLES, 'price': PRICE_PER_INPUT_TOKEN}
    try:
        await resp.write(f'data: {json.dumps(hello)}\n\n'.encode())
        while True:
            await resp.write(f'data: {await q.get()}\n\n'.encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        subscribers.discard(q)
    return resp


async def startup(app):
    global client, http
    client = AsyncTypeSafeClient()
    http = aiohttp.ClientSession()
    app['autopilot'] = asyncio.create_task(autopilot())


async def cleanup(app):
    app['autopilot'].cancel()
    await http.close()
    await client.aclose()


def load_env():
    env = HERE / '.env'
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition('=')
            if k.strip() and not k.startswith('#'):
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


app = web.Application()
app.add_routes([web.get('/', index), web.get('/events', events), web.post('/ask', ask), web.post('/control', control)])
app.on_startup.append(startup)
app.on_cleanup.append(cleanup)

if __name__ == '__main__':
    load_env()
    if not os.environ.get('TYPESAFE_API_KEY'):
        raise SystemExit('Set TYPESAFE_API_KEY (or put it in .env)')
    web.run_app(app, host='127.0.0.1', port=8777, print=lambda *_: print('Jev Router on http://localhost:8777'))
