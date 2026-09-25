"""Registry: name -> async run(text, emit_delta) -> AgentResult. Claude takes code/knowledge/chat/research when a key
is present; math/weather/time/currency stay keyless because they're exact."""
import anthropic

from .claude import CLAUDE_RUNNERS, ClaudeRefusal
from .tools import BLOCKED, KEYLESS_RUNNERS, AgentResult, clarify

__all__ = ['build', 'AgentResult', 'BLOCKED', 'clarify']


def build(http, claude=None) -> dict:
    registry = {}
    for name, fn in KEYLESS_RUNNERS.items():
        registry[name] = keyless(fn, http)
    if claude is not None:
        for name, fn in CLAUDE_RUNNERS.items():
            registry[name] = with_claude(name, fn, claude, http, KEYLESS_RUNNERS.get(name))
    return registry


def keyless(fn, http):
    async def run(text, emit_delta):
        out = await fn(text, http)
        emit_delta(out.answer)
        return out
    return run


def with_claude(name, fn, claude, http, fallback):
    async def run(text, emit_delta):
        try:
            return await fn(claude, http, text, emit_delta)
        except ClaudeRefusal as e:
            r = e.reply
            return AgentResult('Claude declined to answer this request.', False, None, 'claude', r.input_tokens, r.output_tokens)
        except anthropic.RateLimitError:
            why = 'rate limited'
        except anthropic.APIStatusError as e:
            why = f'API error {e.status_code}'
        except anthropic.APIConnectionError:
            why = 'could not connect'
        # Claude is down for this call: the keyless agent still gives a real answer where one exists.
        if fallback is None:
            return AgentResult(f'The {name} agent needs Claude, which is unavailable right now ({why}).', False, None, 'claude')
        emit_delta(f'\n[Claude {why}; keyless fallback]\n')
        return await keyless(fallback, http)(text, emit_delta)
    return run
