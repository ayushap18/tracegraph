"""Anthropic API engine: pay-per-token with ANTHROPIC_API_KEY."""
import os

from .base import Engine, EngineError, EngineRefusal, Reply

MODEL = os.environ.get('TG_ANTHROPIC_MODEL', 'claude-opus-5')
BETAS = ['server-side-fallback-2026-07-01']
WEB_SEARCH = {'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 3}


def first_url(content: list) -> str | None:
    """Pulls a source link from web search results or text citations, whichever the final message carries."""
    for b in content:
        if getattr(b, 'type', '') == 'web_search_tool_result' and isinstance(getattr(b, 'content', None), list):
            for item in b.content:
                if getattr(item, 'url', None):
                    return item.url
        for c in getattr(b, 'citations', None) or []:
            if getattr(c, 'url', None):
                return c.url
    return None


class AnthropicEngine(Engine):
    name = 'anthropic'
    label = 'Anthropic API'
    billing = 'api'
    supports_web = True

    def __init__(self, client=None):
        self.client = client

    def available(self):
        if self.client is not None or os.environ.get('ANTHROPIC_API_KEY'):
            return True, ''
        return False, 'ANTHROPIC_API_KEY is not set'

    def _client(self):
        if self.client is None:
            import anthropic
            self.client = anthropic.AsyncAnthropic()
        return self.client

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, schema=None):
        import anthropic
        output_config = {'effort': effort}
        if schema:
            output_config['format'] = {'type': 'json_schema', 'schema': schema}
        kwargs = dict(model=MODEL, max_tokens=max_tokens, thinking={'type': 'adaptive'}, output_config=output_config,
                      betas=BETAS, fallbacks='default', system=system, messages=[{'role': 'user', 'content': prompt}])
        if web:
            kwargs['tools'] = [WEB_SEARCH]
        parts = []
        try:
            async with self._client().beta.messages.stream(**kwargs) as s:
                async for text in s.text_stream:
                    parts.append(text)
                    if emit_delta:
                        emit_delta(text)
                final = await s.get_final_message()
        except anthropic.RateLimitError:
            raise EngineError('rate limited', ''.join(parts))
        except anthropic.APIStatusError as e:
            raise EngineError(f'API error {e.status_code}', ''.join(parts))
        except anthropic.APIConnectionError:
            raise EngineError('could not connect', ''.join(parts))
        usage = getattr(final, 'usage', None)
        content = list(getattr(final, 'content', None) or [])
        text = ''.join(parts) or ''.join(getattr(b, 'text', '') for b in content if getattr(b, 'type', '') == 'text')
        reply = Reply(text.strip(), getattr(usage, 'input_tokens', 0) or 0, getattr(usage, 'output_tokens', 0) or 0,
                      first_url(content), content)
        if getattr(final, 'stop_reason', None) == 'refusal':
            raise EngineRefusal(reply)
        return reply

    async def aclose(self):
        if self.client is not None:
            await self.client.close()
