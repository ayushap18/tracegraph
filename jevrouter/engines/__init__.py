"""Engine registry and selection.

TG_ENGINE picks the backend: auto (default), claude-code, codex, agy, anthropic, or none.
auto takes the first available in PREFERENCE, so an installed subscription CLI is used before a paid API key.
"""
import os

from .agy import AgyEngine
from .anthropic_api import AnthropicEngine
from .base import Engine, EngineError, EngineRefusal, Reply, parse_json
from .claude_code import ClaudeCodeEngine
from .codex import CodexEngine

__all__ = ['Engine', 'EngineError', 'EngineRefusal', 'Reply', 'parse_json', 'catalog', 'choose', 'ENGINE_CLASSES']

ENGINE_CLASSES = {'claude-code': ClaudeCodeEngine, 'codex': CodexEngine, 'agy': AgyEngine, 'anthropic': AnthropicEngine}
PREFERENCE = ('claude-code', 'codex', 'agy', 'anthropic')


def catalog() -> dict[str, Engine]:
    return {name: cls() for name, cls in ENGINE_CLASSES.items()}


def choose(engines: dict[str, Engine], wanted: str | None = None) -> Engine | None:
    """The engine to use, or None for keyless mode. An explicit choice that isn't available falls back to auto."""
    wanted = (wanted or os.environ.get('TG_ENGINE') or 'auto').strip().lower()
    if wanted == 'none':
        return None
    if wanted in engines and engines[wanted].available()[0]:
        return engines[wanted]
    for name in PREFERENCE:
        if name in engines and engines[name].available()[0]:
            return engines[name]
    return None
