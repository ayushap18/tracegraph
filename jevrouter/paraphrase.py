"""Deterministic paraphrases of eval cases (docs/PLAN-accuracy-v2.md D8): the same seed gives the same variants, byte
for byte, on any machine (random.Random seeded with a string is independent of PYTHONHASHSEED).

Each variant is a new case with the source case's expectations, id `<source id>.g-<transform><n>`, the tags
`paraphrase-gen` and `transform:<name>` (so the route report's by-tag table is the robustness table per transform)
and a note naming its source. Multi-turn cases and one-word queries are left alone: a bare term is the test itself.

CLI: python -m jevrouter.evals paraphrase --seed 7 --n 3 --tags ... > evals/paraphrases.gen.jsonl
"""
import random
import re

TRANSFORMS = ('lower', 'typos', 'filler', 'currency', 'reorder', 'style', 'agent', 'long')
KEEP = ('expect_agents', 'expect_outcome', 'must_match', 'must_not_match', 'must_mention', 'forbid_agents', 'run_on',
        'expect_steps', 'unordered', 'no_repeat',
        'expect_file', 'files', 'split', 'judge', 'judge_min', 'chat', 'expect_plan', 'budget')
FILLERS = ('hey', 'pls', 'quickly', 'hi there,', 'ok so', 'um', 'can you', 'real quick')
STYLES = ('in bullets', 'keep it short', 'step by step please', 'one line only', 'in simple words')
# Keyboard neighbours on a QWERTY layout, for typos a person would make.
NEAR = {
    'a': 'qwsz', 'b': 'vghn', 'c': 'xdfv', 'd': 'serfcx', 'e': 'wsdr', 'f': 'drtgvc', 'g': 'ftyhbv', 'h': 'gyujnb',
    'i': 'ujko', 'j': 'huikmn', 'k': 'jiolm', 'l': 'kop', 'm': 'njk', 'n': 'bhjm', 'o': 'iklp', 'p': 'ol',
    'q': 'wa', 'r': 'edft', 's': 'awedxz', 't': 'rfgy', 'u': 'yhji', 'v': 'cfgb', 'w': 'qase', 'x': 'zsdc',
    'y': 'tghu', 'z': 'asx'}
CURRENCY_FORMS = {
    'USD': ('{n} USD', '${n}', '{n} dollars', '{n} bucks', '{n} US dollars'),
    'EUR': ('{n} EUR', '€{n}', '{n} euros'),
    'GBP': ('{n} GBP', '£{n}', '{n} pounds', '{n} quid'),
    'INR': ('{n} INR', '₹{n}', '{n} rupees'),
    'JPY': ('{n} JPY', '¥{n}', '{n} yen'),
}
AMOUNT = re.compile(
    r'(?:(?P<sym>[$€£₹¥])\s?(?P<n1>\d[\d,]*(?:\.\d+)?))'
    r'|(?:(?P<n2>\d[\d,]*(?:\.\d+)?)\s?(?P<word>USD|EUR|GBP|INR|JPY|dollars?|bucks|euros?|pounds?|quid|rupees?|yen)\b)',
    re.I)
SYMBOL = {'$': 'USD', '€': 'EUR', '£': 'GBP', '₹': 'INR', '¥': 'JPY'}
WORD = {'usd': 'USD', 'dollar': 'USD', 'dollars': 'USD', 'bucks': 'USD', 'eur': 'EUR', 'euro': 'EUR', 'euros': 'EUR',
        'gbp': 'GBP', 'pound': 'GBP', 'pounds': 'GBP', 'quid': 'GBP', 'inr': 'INR', 'rupee': 'INR', 'rupees': 'INR',
        'jpy': 'JPY', 'yen': 'JPY'}
CLAUSE = re.compile(r'\s*(?:;|,? and (?:also )?|, also )\s*', re.I)
LONG_TAIL = (
    'Please answer in plain English and keep the tone friendly.',
    'Use short paragraphs, no more than three sentences each.',
    'If you give numbers, round them to two decimal places and name the units.',
    'Keep the whole answer under 150 words.',
    'Put the most important point first, then any detail.',
    'Avoid jargon; if you must use a technical term, explain it in a few words.',
    'Format any list as bullets, one idea per bullet.',
    'Do not repeat the question back to me.',
    'If something is uncertain, say so plainly instead of guessing.',
    'Finish with a one-line summary.',
)


def words(text: str) -> list[str]:
    return text.split()


def lower(text: str, rng: random.Random, case: dict) -> str | None:
    out = re.sub(r"[^\w\s%$€£₹¥.+\-*/^']", ' ', text.lower())
    out = re.sub(r'(?<!\d)\.(?!\d)', ' ', out)  # sentence dots go, decimal points stay
    out = re.sub(r'\s+', ' ', out).strip()
    return out if out != text else None


def typo(word: str, rng: random.Random) -> str:
    letters = [i for i, ch in enumerate(word) if ch.isalpha()]
    i = rng.choice(letters[1:-1] or letters)
    kind = rng.choice(('swap', 'near', 'drop'))
    if kind == 'swap' and i + 1 < len(word) and word[i + 1].isalpha():
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if kind == 'near' and word[i].lower() in NEAR:
        ch = rng.choice(NEAR[word[i].lower()])
        return word[:i] + (ch.upper() if word[i].isupper() else ch) + word[i + 1:]
    return word[:i] + word[i + 1:]


def typos(text: str, rng: random.Random, case: dict) -> str | None:
    ws = words(text)
    long = [i for i, w in enumerate(ws) if sum(ch.isalpha() for ch in w) >= 5 and w.isascii()]
    if not long:
        return None
    for i in sorted(rng.sample(long, min(len(long), rng.choice((1, 2))))):
        ws[i] = typo(ws[i], rng)
    out = ' '.join(ws)
    return out if out != text else None


def filler(text: str, rng: random.Random, case: dict) -> str:
    f = rng.choice(FILLERS)
    body = text[0].lower() + text[1:] if text[:1].isupper() and not text[:2].isupper() else text
    return f'{f} {body}' if rng.random() < 0.7 else f'{text} {rng.choice(("pls", "thanks", "quickly"))}'


def currency(text: str, rng: random.Random, case: dict) -> str | None:
    m = AMOUNT.search(text)
    if not m:
        return None
    n = m.group('n1') or m.group('n2')
    code = SYMBOL.get(m.group('sym') or '') or WORD.get((m.group('word') or '').lower())
    if not code:
        return None
    forms = [f.format(n=n) for f in CURRENCY_FORMS[code] if f.format(n=n).lower() != m.group(0).lower()]
    return text[:m.start()] + rng.choice(forms) + text[m.end():] if forms else None


def reorder(text: str, rng: random.Random, case: dict) -> str | None:
    if re.search(r'\b(then|after that|that|it|there|the result)\b', text, re.I):
        return None  # a dependent step can't move before what it depends on
    parts = [p for p in CLAUSE.split(text.strip().rstrip('?.!')) if p.strip()]
    if len(parts) != 2 or min(len(words(p)) for p in parts) < 2:
        return None
    a, b = parts
    return f'{b[0].upper() + b[1:]} and {a[0].lower() + a[1:] if not a[:2].isupper() else a}'


def style(text: str, rng: random.Random, case: dict) -> str:
    return f'{text.rstrip()} {rng.choice(STYLES)}'


def agent(text: str, rng: random.Random, case: dict) -> str | None:
    agents = case.get('expect_agents') or []
    if len(agents) != 1 or (case.get('chat') or {}).get('agent'):
        return None
    return f'@{agents[0]} {text}'


def long(text: str, rng: random.Random, case: dict) -> str | None:
    n = len(words(text))
    tail = list(LONG_TAIL)
    rng.shuffle(tail)
    out, i = text.rstrip(), 0
    target = rng.randint(40, 80)
    while n < target and i < len(tail):
        out += ' ' + tail[i]
        n += len(words(tail[i]))
        i += 1
    return out if 40 <= n <= 80 else None


FUNCS = {'lower': lower, 'typos': typos, 'filler': filler, 'currency': currency, 'reorder': reorder, 'style': style,
         'agent': agent, 'long': long}


def variant_id(case_id: str, transform: str, k: int) -> str:
    suffix = f'.g-{transform}{k}'
    return case_id[:64 - len(suffix)] + suffix


def generate(cases: list[dict], seed: int, n: int, transforms=TRANSFORMS) -> list[dict]:
    """Up to `n` variants per single-query case, each from a different transform, in case order. Deterministic."""
    unknown = [t for t in transforms if t not in FUNCS]
    if unknown:
        raise ValueError(f'unknown transform {unknown[0]!r}; choose from {", ".join(TRANSFORMS)}')
    out = []
    for c in cases:
        q = c.get('query')
        if c.get('turns') or not isinstance(q, str) or len(words(q)) < 2:
            continue
        rng = random.Random(f'{seed}:{c["id"]}')
        order = list(transforms)
        rng.shuffle(order)
        seen, k = {q}, 0
        for t in order:
            if k >= n:
                break
            text = FUNCS[t](q, random.Random(f'{seed}:{c["id"]}:{t}'), c)
            if not text or text in seen:
                continue
            seen.add(text)
            k += 1
            v = {'id': variant_id(c['id'], t, k), 'query': text,
                 **{key: c[key] for key in KEEP if key in c},
                 'tags': [*c.get('tags', []), 'paraphrase-gen', f'transform:{t}'],
                 'note': f'generated from {c["id"]} by the {t} transform (seed {seed})'}
            if t == 'agent':
                v['chat'] = {**(c.get('chat') or {}), 'agent': c['expect_agents'][0]}
            if t == 'long':
                v.pop('max_ms', None)
            out.append(v)
    return out
