"""Registry: name -> async run(text, emit_delta) -> AgentResult. The active LLM engine takes code/knowledge/chat/research;
math/weather/time/currency stay keyless because they're exact."""
from ..engines import EngineError, EngineRefusal
from .llm import LLM_RUNNERS
from .tools import BLOCKED, KEYLESS_RUNNERS, AgentResult, clarify

__all__ = ['build', 'AgentResult', 'BLOCKED', 'clarify']


def build(http, engine=None) -> dict:
    registry = {}
    for name, fn in KEYLESS_RUNNERS.items():
        registry[name] = keyless(fn, http)
    if engine is not None:
        for name, fn in LLM_RUNNERS.items():
            if name == 'research' and not engine.supports_web:
                continue
            registry[name] = with_engine(name, fn, engine, http, KEYLESS_RUNNERS.get(name))
    return registry


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
