"""Engine registry and selection.

TG_ENGINE picks the backend: auto (default), claude-code, codex, agy, anthropic, or none.
auto tries every available engine in order (TG_ENGINE_ORDER, default PREFERENCE) and falls through to the next when
one fails, so an installed subscription CLI is used before a paid API key and a used-up plan doesn't fail the run.
"""
import os

from .agy import AgyEngine
from .anthropic_api import AnthropicEngine
from .auto import AutoEngine
from .base import Engine, EngineError, EngineRefusal, Reply, parse_json
from .claude_code import ClaudeCodeEngine
from .codex import CodexEngine

__all__ = ['Engine', 'EngineError', 'EngineRefusal', 'Reply', 'parse_json', 'catalog', 'choose', 'ENGINE_CLASSES', 'AutoEngine']

ENGINE_CLASSES = {'claude-code': ClaudeCodeEngine, 'codex': CodexEngine, 'agy': AgyEngine, 'anthropic': AnthropicEngine}
PREFERENCE = ('claude-code', 'codex', 'agy', 'anthropic')


def catalog() -> dict[str, Engine]:
    engines = {name: cls() for name, cls in ENGINE_CLASSES.items()}
    return {'auto': AutoEngine({n: engines[n] for n in PREFERENCE}), **engines}


def choose(engines: dict[str, Engine], wanted: str | None = None) -> Engine | None:
    """The engine to use, or None for keyless mode. An explicit choice that isn't available falls back to auto."""
    wanted = (wanted or os.environ.get('TG_ENGINE') or 'auto').strip().lower()
    if wanted == 'none':
        return None
    if wanted in engines and engines[wanted].available()[0]:
        return engines[wanted]
    if 'auto' in engines and engines['auto'].available()[0]:
        return engines['auto']
    for name in PREFERENCE:
        if name in engines and engines[name].available()[0]:
            return engines[name]
    return None
