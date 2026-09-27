"""Registry: name -> async run(text, emit_delta) -> AgentResult. The active LLM engine takes code/knowledge/chat/research;
math/weather/time/currency stay keyless because they're exact."""
import math
import re
from contextvars import ContextVar

from ..engines import EngineError, EngineRefusal
from .llm import LLM_RUNNERS, custom, run_code
from .tools import BLOCKED, KEYLESS_RUNNERS, AgentResult, clarify, plain

__all__ = ['build', 'extras', 'AgentResult', 'BLOCKED', 'clarify', 'Tuned', 'EFFORTS', 'verify']


def build(http, engine=None, customs=(), prefer_keyless: bool = False) -> dict:
    """prefer_keyless (quick mode): an agent with a keyless version runs keyless even when an engine is active."""
    registry = {}
    for name, fn in KEYLESS_RUNNERS.items():
        registry[name] = keyless(fn, http)
    if engine is not None:
        for name, fn in LLM_RUNNERS.items():
            if (name == 'research' and not engine.supports_web) or (prefer_keyless and name in KEYLESS_RUNNERS):
                continue
            registry[name] = with_engine(name, fn, engine, http, KEYLESS_RUNNERS.get(name))
    return {**registry, **extras(http, engine, customs)}


# The effort each LLM agent call used in this task, newest last (read into `answered.checks.effort`).
EFFORTS: ContextVar[list | None] = ContextVar('efforts', default=None)


class Tuned:
    """A run's engine as its agents see it (docs/PLAN-speed-evals-chat.md, chat modes and styles): every call at the
    mode's effort when it sets one (quick: low, deep: high), with the answer style added to the instructions, and the
    effort used recorded in EFFORTS. Everything else is the engine itself."""

    def __init__(self, engine, effort: str | None = None, style: str = ''):
        self._engine, self._effort, self._style = engine, effort, style

    def __getattr__(self, name):
        return getattr(self._engine, name)

    async def stream(self, *, system, prompt, effort='medium', schema=None, **kw):
        effort = self._effort or effort
        if self._style and schema is None:
            system = f'{system} {self._style}'
        used = EFFORTS.get()
        if used is not None:
            used.append(effort)
        return await self._engine.stream(system=system, prompt=prompt, effort=effort, schema=schema, **kw)


NUMBER = re.compile(r'-?\d[\d,]*(?:\.\d+)?')


def verify(text: str, answer: str) -> dict:
    """Deep mode's check on an LLM agent's answer (AnswerChecks): a keyless recompute when the step is arithmetic the
    math parser can solve on its own; anything else is 'skipped'."""
    from ..gate import confirmed
    from .tools import solve_math
    try:
        solved = solve_math(text) if confirmed('math', text) else None
    except Exception:
        solved = None
    if solved is None:
        return {'verified': 'skipped', 'verify_note': None}
    value = float(solved[1])
    found = [float(n.replace(',', '')) for n in NUMBER.findall(answer or '')]
    if any(math.isclose(n, value, rel_tol=1e-4, abs_tol=0.005) for n in found):
        return {'verified': 'ok', 'verify_note': None}
    return {'verified': 'mismatch', 'verify_note': f'A keyless recompute gives {plain(value)}, which this answer does not show.'}


def extras(http, engine, customs=()) -> dict:
    """Engine-only agents beyond the built-in LLM set: `run` (code execution) and the user's custom agents."""
    if engine is None:
        return {}
    out = {a['name']: with_engine(a['name'], custom(a), engine, http, None) for a in customs}
    if getattr(engine, 'supports_exec', False):
        out['run'] = with_engine('run', run_code, engine, http, None)
    return out


def keyless(fn, http):
    async def run(text, emit_delta):
        out = await fn(text, http)
        emit_delta(out.answer)
        return out
    return run


def with_engine(name, fn, engine, http, fallback):
    async def run(text, emit_delta):
        try:
            return await fn(engine, http, text, emit_delta)
        except EngineRefusal as e:
            r = e.reply
            return AgentResult(f'{engine.label} declined to answer this request.', False, None, engine.name, r.input_tokens, r.output_tokens)
        except EngineError as e:
            why = e.why
        # The engine failed for this call: the keyless agent still gives a real answer where one exists.
        if fallback is None:
            return AgentResult(f'The {name} agent needs {engine.label}, which failed ({why}).', False, None, engine.name)
        emit_delta(f'\n[{engine.label} {why}; keyless fallback]\n')
        return await keyless(fallback, http)(text, emit_delta)
    return run
