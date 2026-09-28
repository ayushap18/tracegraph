"""The verify step (docs/PLAN-speed-evals-chat.md A6): a cheap check on an answer an LLM agent wrote, so a wrong number
or an unsupported claim shows a warning instead of passing silently.

- Math and currency: the step is recomputed keylessly (the math parser; today's European Central Bank rate) and the
  answer must show that value. Runs in every mode, since it costs no LLM call.
- Knowledge and research (deep mode only): a second engine is asked whether the answer matches its sources (the
  reference abstract the knowledge agent was given, or the page the research answer cites). Skipped keyless.

The result is AnswerChecks (web/src/protocol.ts): verified 'ok', 'mismatch' or 'skipped', with a short verify_note.
"""
import math
import re

from .agents.tools import (AMOUNT, ECB_CODES, ISO_CODES, UNUSED_OPERATION, UrlBlocked, UrlError, cached_json, currencies_in,
                           currency_words, ddg_abstract, fetch_page, normalize_numbers, page_text, parse_currency, plain,
                           solve_math, target_word, unsupported_currency)
from .config import CURRENCY_TOLERANCE
from .engines import parse_json
from . import cache

# Agents whose answer is the value itself. Code, reports and file or page answers may quote a sum without it being
# their answer, so their numbers are never checked.
DIRECT = {'knowledge', 'chat', 'research'}
GROUNDED = {'knowledge', 'research'}
NUMBER = re.compile(r'-?\d[\d,]*(?:\.\d+)?')
SOURCE_CHARS = 6000

GROUND_SYSTEM = ('Do not use tools. You check whether an answer is supported by the sources given. Judge only the facts '
                 'the answer states; an answer may leave things out. Reply as JSON: {"supported": true or false, "note": '
                 '"one short sentence naming the claim the sources contradict or do not support, empty when supported"}.')
GROUND_SCHEMA = {'type': 'object', 'properties': {'supported': {'type': 'boolean'}, 'note': {'type': 'string'}},
                 'required': ['supported', 'note'], 'additionalProperties': False}


def skipped(note: str | None = None) -> dict:
    return {'verified': 'skipped', 'verify_note': note}


def shows(answer: str, value: float, rel: float = 1e-4) -> bool:
    """True when the answer writes the value: exactly, within `rel`, or rounded to the decimals it shows (1/3 written as
    0.33). A whole number counts as rounding only for values of 10 or more, so a stray 0 never passes for 0.4."""
    for raw in NUMBER.findall(normalize_numbers(answer or '')):
        try:
            n = float(raw.replace(',', ''))
        except ValueError:
            continue
        if math.isclose(n, value, rel_tol=rel, abs_tol=0.005):
            return True
        decimals = len(raw.split('.')[1]) if '.' in raw else 0
        if (decimals or abs(value) >= 10) and round(value, decimals) == n:
            return True
    return False


def arithmetic(text: str):
    """The keyless value of the step when it is arithmetic the math parser solves on its own, else None."""
    from .gate import confirmed  # gate imports the agents package, which may import this module
    try:
        solved = solve_math(text) if confirmed('math', text) else None
    except Exception:
        return None
    return float(solved[1]) if solved else None


def check_math(text: str, answer: str) -> dict | None:
    value = arithmetic(text)
    if value is None:
        return None
    if shows(answer, value):
        return {'verified': 'ok', 'verify_note': None}
    return {'verified': 'mismatch', 'verify_note': f'A keyless recompute gives {plain(value)}, which this answer does not show.'}


def conversion(text: str):
    """(amount, from, to) when the step is a currency conversion the rate source covers, else None."""
    if unsupported_currency(text, ECB_CODES):
        return None
    parsed = parse_currency(text, ECB_CODES)
    return None if isinstance(parsed, str) else parsed


async def check_currency(text: str, answer: str, http) -> dict | None:
    parsed = conversion(text)
    if parsed is None:
        return None
    amount, src, dst = parsed
    try:
        d = await cached_json(http, cache.RATES_TTL, 'https://api.frankfurter.app/latest', amount=amount,
                              **{'from': src, 'to': dst})
        value = float(d['rates'][dst])
    except Exception:
        return skipped("Today's reference rate could not be fetched to check this conversion.")
    if shows(answer, value, CURRENCY_TOLERANCE):
        return {'verified': 'ok', 'verify_note': None}
    return {'verified': 'mismatch', 'verify_note': f"Today's reference rate gives {amount:,.2f} {src} = {value:,.2f} {dst}, "
                                                   f'which this answer does not show.'}


async def sources_for(agent: str, text: str, source: str | None, http) -> str:
    """The text a grounded answer should match: the knowledge agent's reference abstract, or the page a research
    answer cites (read through the URL reader's egress guard). Empty when there is none."""
    if agent == 'knowledge':
        try:
            _, abstract, _ = await ddg_abstract(http, text)
        except Exception:
            return ''
        return abstract
    if agent == 'research' and source and source.startswith(('http://', 'https://')):
        try:
            _, ctype, body, _ = await fetch_page(source)
        except (UrlBlocked, UrlError, Exception):
            return ''
        title, page = page_text(ctype, body)
        return f'{title}\n{page}'.strip()[:SOURCE_CHARS]
    return ''


async def check_grounding(text: str, answer: str, sources: str, engine) -> tuple[dict, int, int]:
    prompt = f'Question: {text}\n\nSources:\n{sources[:SOURCE_CHARS]}\n\nAnswer to check:\n{answer[:4000]}'
    try:
        reply = await engine.stream(system=GROUND_SYSTEM, prompt=prompt, effort='low', max_tokens=512, schema=GROUND_SCHEMA)
    except Exception as e:
        return skipped(f'The second engine could not check it ({getattr(e, "why", None) or type(e).__name__}).'), 0, 0
    tokens = reply.input_tokens, reply.output_tokens
    try:
        d = parse_json(reply.text)
        supported, note = bool(d['supported']), ' '.join(str(d.get('note') or '').split())
    except Exception:
        return skipped('The second engine gave no clear verdict.'), *tokens
    if supported:
        return {'verified': 'ok', 'verify_note': None}, *tokens
    label = getattr(engine, 'label', None) or 'A second engine'
    note = note.rstrip('.') if note else 'the sources do not support this answer'
    return {'verified': 'mismatch', 'verify_note': f'{label} checked it against the sources: {note}.'[:300]}, *tokens


async def verify(text: str, answer: str, *, agent: str, http=None, deep: bool = False, checker=None,
                 source: str | None = None) -> tuple[dict, int, int]:
    """(checks, tokens in, tokens out) for an answer an LLM agent wrote to the step `text`. checks is {} when no check
    applies outside deep mode; deep mode always reports one, 'skipped' when nothing could be checked. checker is the
    second engine for the grounding check (deep mode, knowledge and research only); None skips that check."""
    if agent in DIRECT:
        out = check_math(text, answer)
        if out is None and http is not None:
            out = await check_currency(text, answer, http)
        if out is not None:
            return out, 0, 0
    if not deep:
        return {}, 0, 0
    if agent not in GROUNDED:
        return skipped(), 0, 0
    if checker is None:
        return skipped('No second engine is available to check this answer against its sources.'), 0, 0
    sources = await sources_for(agent, text, source, http) if http is not None else ''
    if not sources.strip():
        return skipped('There were no sources to check this answer against.'), 0, 0
    return await check_grounding(text, answer, sources, checker)


def warning(checks: list[tuple[str, dict]]) -> str:
    """The short warning added to the merged answer for every step whose check found a mismatch, or ''. checks:
    [(step text, AnswerChecks)] in step order."""
    bad = [(t, c) for t, c in checks if c.get('verified') == 'mismatch']
    if not bad:
        return ''
    if len(bad) == 1 and len(checks) == 1:
        return f"\n\n**Check this answer:** {bad[0][1].get('verify_note') or 'a check did not agree with it.'}"
    lines = [f"- {t}: {c.get('verify_note') or 'a check did not agree with it.'}" for t, c in bad]
    return '\n\n**Check these answers:**\n' + '\n'.join(lines)


# ---------- keyless coverage (docs/PLAN-accuracy-v2.md A5) ----------

COVERED = {'math', 'currency'}
# Word operators whose operands have an order: (pattern, which group comes first in the calculation).
ORDERED = [(re.compile(r'\bsubtract\s+(\S+)\s+from\s+(\S+)', re.I), (2, 1), 'subtracts {0} from {1}'),
           (re.compile(r'\bdivide\s+(\S+)\s+(?:by|into)\s+(\S+)', re.I), (1, 2), 'divides {0} by {1}'),
           (re.compile(r'(\S+)\s+divided\s+by\s+(\S+)', re.I), (1, 2), 'divides {0} by {1}'),
           (re.compile(r'(\S+)\s+minus\s+(\S+)', re.I), (1, 2), 'takes {1} from {0}'),
           (re.compile(r'(\S+)\s+to\s+the\s+power\s+of\s+(\S+)', re.I), (1, 2), 'raises {0} to the power of {1}')]
CONVERSION = re.compile(r'(-?\d[\d,]*(?:\.\d+)?)\s*([A-Z]{3})\s*=\s*(-?\d[\d,]*(?:\.\d+)?)\s*([A-Z]{3})')
# Words between an amount and its currency that name the currency's country ("200 British pounds").
QUALIFIER = {'british', 'us', 'u.s.', 'american', 'canadian', 'australian', 'indian', 'japanese', 'swiss', 'chinese',
             'euro', 'new', 'hong', 'kong', 'singapore', 'mexican', 'south', 'african', 'korean', 'swedish', 'norwegian',
             'danish', 'polish', 'czech', 'hungarian', 'turkish', 'brazilian', 'zealand', 'philippine', 'thai', 'israeli'}


def numbers_in(text: str) -> list[float]:
    """The numbers written in the text, as magnitudes: in "20-5" the minus is an operator, not a sign."""
    out = []
    for raw in NUMBER.findall(normalize_numbers((text or '').replace(',', ''))):
        try:
            out.append(abs(float(raw)))
        except ValueError:
            pass
    return out


def as_number(word: str) -> float | None:
    nums = numbers_in(word)
    return nums[0] if len(nums) == 1 else None


def covers_math(text: str, answer: str) -> dict | None:
    """Every number in the question appears in the calculation (the left of the answer's last "="), and ordered word
    operators ("divide 240 by 8") keep their order."""
    if '=' not in (answer or ''):
        return None
    expr = answer.rsplit('=', 1)[0]
    if not re.search(r'[-+*/%^]|sqrt|log|ln|sin|cos|tan|abs', expr) and UNUSED_OPERATION.search(text):
        # "half of 90" answered "90 = 90": the number is there, the operation isn't
        return {'verified': 'mismatch', 'verify_note': 'The question asks for an operation this calculation does not '
                                                       'do, so this may not answer what was asked.'}
    used = numbers_in(expr)
    pool = list(used)
    for n in numbers_in(text):
        hit = next((i for i, u in enumerate(pool) if math.isclose(u, n, rel_tol=1e-9, abs_tol=1e-9)), None)
        if hit is None:
            return {'verified': 'mismatch', 'verify_note': f'The question has {plain(n)}, but the calculation does not '
                                                           f'use it, so this may not answer what was asked.'}
        pool.pop(hit)
    for rx, (first, second), says in ORDERED:
        if not (m := rx.search(normalize_numbers(text.replace(',', '')))):
            continue
        a, b = as_number(m.group(first)), as_number(m.group(second))
        if a is None or b is None or math.isclose(a, b):
            continue
        ia = next((i for i, u in enumerate(used) if math.isclose(u, a)), None)
        ib = next((i for i, u in enumerate(used) if math.isclose(u, b)), None)
        if ia is not None and ib is not None and ia > ib:
            x, y = (m.group(1), m.group(2))
            return {'verified': 'mismatch', 'verify_note': f'The question {says.format(x, y)}, but the calculation has '
                                                           f'them the other way round.'}
    return {'verified': 'ok', 'verify_note': None}


def asked_conversion(text: str) -> tuple[float, str, str] | None:
    """(amount, from, to) read from the question on its own: the currency written with the amount is the source ("how
    many yen is 200 British pounds" is GBP to JPY), else the first one named. None when it names fewer than two."""
    words = currency_words(text)
    found = currencies_in(words, ISO_CODES)
    codes = list(dict.fromkeys(c for _, c in found))
    if len(codes) < 2:
        return None
    num = next((i for i, w in enumerate(words) if AMOUNT.fullmatch(w)), None)
    amount = float(words[num]) if num is not None else 1.0
    src = None
    if num is None:  # "how many rupees is a dollar": one unit of the currency after "a"
        num = next((i for i, w in enumerate(words[:-1]) if w.lower() in ('a', 'an', 'one') and i + 1 in dict(found)), None)
    if num is not None:
        j = num + 1
        while j < len(words) and words[j].lower() in QUALIFIER:
            j += 1
        at = dict(found)
        # "GBP to USD 100": a currency before the amount that follows "to" is the target, not the source
        src = at.get(j) or (at.get(num - 1) if not target_word(words, num - 1) else None)
    src = src or codes[0]
    dst = next(c for c in codes if c != src)
    return amount, src, dst


def covers_currency(text: str, answer: str) -> dict | None:
    """The answer converts the question's amount from the question's source currency to its target."""
    asked = asked_conversion(text)
    m = CONVERSION.search(answer or '')
    if asked is None or m is None:
        return None
    amount, src, dst = asked
    got_amount, got_src, got_dst = float(m.group(1).replace(',', '')), m.group(2), m.group(4)
    if (got_src, got_dst) == (dst, src):
        return {'verified': 'mismatch', 'verify_note': f'The question converts {src} to {dst}, but this answer converts '
                                                       f'{dst} to {src}.'}
    if {got_src, got_dst} != {src, dst}:
        return {'verified': 'mismatch', 'verify_note': f'The question converts {src} to {dst}, but this answer converts '
                                                       f'{got_src} to {got_dst}.'}
    if not math.isclose(got_amount, amount, rel_tol=1e-6, abs_tol=0.005):
        return {'verified': 'mismatch', 'verify_note': f'The question has {plain(amount)} {src}, but this answer '
                                                       f'converts {plain(got_amount)} {src}.'}
    return {'verified': 'ok', 'verify_note': None}


def coverage(agent: str, text: str, answer: str) -> dict | None:
    """AnswerChecks for a keyless math or currency answer: the numbers and currency codes in the step text must appear
    in the answer, in the right direction. None when the agent is another one or the answer is not a result (a
    question back, an honest miss)."""
    if agent not in COVERED or not answer:
        return None
    try:
        return covers_math(text, answer) if agent == 'math' else covers_currency(text, answer)
    except Exception:
        return None
