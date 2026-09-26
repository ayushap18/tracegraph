"""Claude agents (code, knowledge, chat, research). Only used when ANTHROPIC_API_KEY is set; every call streams so
the browser can show text as it arrives."""
from dataclasses import dataclass, field

from ..config import BETAS, MODEL
from .tools import AgentResult, ddg_abstract

WEB_SEARCH = {'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 3}
ABOUT = ('You are one specialist agent inside Jev Router, a demo where a classifier (Jev) routes each user request to agents '
         '(math, weather, time, currency, knowledge, code, chat, research). Answer only the request you are given. '
         'Plain text or light Markdown, no preamble.')


class ClaudeRefusal(Exception):
    def __init__(self, reply):
        super().__init__('refusal')
        self.reply = reply


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    content: list = field(default_factory=list)


async def stream(client, *, system: str, prompt: str, effort: str, emit_delta=None, max_tokens: int = 2048,
                 tools: list | None = None, schema: dict | None = None) -> Reply:
    output_config = {'effort': effort}
    if schema:
        output_config['format'] = {'type': 'json_schema', 'schema': schema}
    kwargs = dict(model=MODEL, max_tokens=max_tokens, thinking={'type': 'adaptive'}, output_config=output_config,
                  betas=BETAS, fallbacks='default', system=system, messages=[{'role': 'user', 'content': prompt}])
    if tools:
        kwargs['tools'] = tools
    parts = []
    async with client.beta.messages.stream(**kwargs) as s:
        async for text in s.text_stream:
            parts.append(text)
            if emit_delta:
                emit_delta(text)
        final = await s.get_final_message()
    usage = getattr(final, 'usage', None)
    content = list(getattr(final, 'content', None) or [])
    text = ''.join(parts) or ''.join(getattr(b, 'text', '') for b in content if getattr(b, 'type', '') == 'text')
    reply = Reply(text.strip(), getattr(usage, 'input_tokens', 0) or 0, getattr(usage, 'output_tokens', 0) or 0, content)
    if getattr(final, 'stop_reason', None) == 'refusal':
        raise ClaudeRefusal(reply)
    return reply


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


def result(reply: Reply, source: str | None = None) -> AgentResult:
    return AgentResult(reply.text, bool(reply.text), source, 'claude', reply.input_tokens, reply.output_tokens)


async def code(client, http, q: str, emit_delta) -> AgentResult:
    system = ABOUT + ' You are the code agent: a short explanation plus a minimal example in a fenced code block, under 200 words.'
    return result(await stream(client, system=system, prompt=q, effort='medium', emit_delta=emit_delta))


async def knowledge(client, http, q: str, emit_delta) -> AgentResult:
    # Ground the answer in the same abstract the keyless agent would show, so Claude has a source to cite.
    try:
        term, abstract, url = await ddg_abstract(http, q)
    except Exception:
        abstract, url = '', None
    prompt = q if not abstract else f'{q}\n\nReference (Wikipedia abstract via DuckDuckGo):\n{abstract}'
    system = ABOUT + ' You are the knowledge agent: answer factually in 2-4 sentences, preferring the reference when given.'
    return result(await stream(client, system=system, prompt=prompt, effort='medium', emit_delta=emit_delta), url)


async def chat(client, http, q: str, emit_delta) -> AgentResult:
    system = ABOUT + ' You are the chat agent: reply warmly in 1-3 sentences.'
    return result(await stream(client, system=system, prompt=q, effort='low', emit_delta=emit_delta, max_tokens=1024))


async def research(client, http, q: str, emit_delta) -> AgentResult:
    system = ABOUT + ' You are the research agent: search the web, then answer in under 150 words with the key facts and dates.'
    reply = await stream(client, system=system, prompt=q, effort='medium', emit_delta=emit_delta, max_tokens=4096, tools=[WEB_SEARCH])
    return result(reply, first_url(reply.content))


CLAUDE_RUNNERS = {'code': code, 'knowledge': knowledge, 'chat': chat, 'research': research}
