"""Engine registry and selection.

Two kinds of backend:
- CLI engines drive an official agent CLI on the user's own login: Claude Code, Codex, Antigravity, OpenCode. To add
  one, write a CliEngine subclass with a Parser for its JSON events (see opencode.py) and list it in CLI_ENGINES.
- API engines (api.py) call any OpenAI-compatible endpoint with an API key: one engine per provider whose key is set.
  To add one, list it in api.BUILTIN or in data/providers.json; no code needed.

TG_ENGINE picks the backend: auto (default), none, or any engine's name. auto tries every available engine in order
(TG_ENGINE_ORDER, default: the CLIs, then the API keys) and falls through to the next when one fails, so a subscription
CLI is used before a paid API key and a used-up plan doesn't fail the run.
"""
import os

from .agy import AgyEngine
from .api import ApiEngine, Provider, api_engines
from .auto import AutoEngine
from .base import Engine, EngineError, EngineRefusal, Reply, parse_json
from .claude_code import ClaudeCodeEngine
from .codex import CodexEngine
from .opencode import OpenCodeEngine

__all__ = ['Engine', 'EngineError', 'EngineRefusal', 'Reply', 'parse_json', 'catalog', 'choose', 'CLI_ENGINES',
           'AutoEngine', 'ApiEngine', 'Provider']

CLI_ENGINES = {'claude-code': ClaudeCodeEngine, 'codex': CodexEngine, 'agy': AgyEngine, 'opencode': OpenCodeEngine}


def catalog() -> dict[str, Engine]:
    """Every engine, CLIs first and then the configured API keys (Auto's default order), plus Auto over all of them."""
    engines: dict[str, Engine] = {name: cls() for name, cls in CLI_ENGINES.items()}
    engines.update((n, e) for n, e in api_engines().items() if n not in engines)
    return {'auto': AutoEngine(dict(engines)), **engines}


def choose(engines: dict[str, Engine], wanted: str | None = None) -> Engine | None:
    """The engine to use, or None for keyless mode. An explicit choice that isn't available falls back to auto."""
    wanted = (wanted or os.environ.get('TG_ENGINE') or 'auto').strip().lower()
    if wanted == 'none':
        return None
    if wanted in engines and engines[wanted].available()[0]:
        return engines[wanted]
    if 'auto' in engines and engines['auto'].available()[0]:
        return engines['auto']
    return next((e for n, e in engines.items() if n != 'auto' and e.available()[0]), None)
