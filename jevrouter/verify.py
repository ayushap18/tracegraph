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

from .agents.tools import (ECB_CODES, UrlBlocked, UrlError, cached_json, ddg_abstract, fetch_page, normalize_numbers,
                           page_text, parse_currency, plain, solve_math, unsupported_currency)
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
