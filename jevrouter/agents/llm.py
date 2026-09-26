"""LLM agents (code, knowledge, chat, research). They run on whichever engine is active: an Anthropic API key or a
subscription CLI (Claude Code, Codex, Antigravity). Every call streams so the browser can show text as it arrives."""
from ..engines import Engine, Reply
from .tools import AgentResult, ddg_abstract

ABOUT = ('You are one specialist agent inside TraceGraph, where a classifier (Jev) routes each user request to agents '
         '(math, weather, time, currency, knowledge, code, chat, research). Answer only the request you are given. '
         'Plain text or light Markdown, no preamble. Do not use tools unless told to.')


def result(engine: Engine, reply: Reply, source: str | None = None) -> AgentResult:
    return AgentResult(reply.text, bool(reply.text), source or reply.source, engine.name, reply.input_tokens, reply.output_tokens)


async def code(engine, http, q: str, emit_delta) -> AgentResult:
    system = ABOUT + ' You are the code agent: a short explanation plus a minimal example in a fenced code block, under 200 words.'
    return result(engine, await engine.stream(system=system, prompt=q, effort='medium', emit_delta=emit_delta))


async def knowledge(engine, http, q: str, emit_delta) -> AgentResult:
    # Ground the answer in the same abstract the keyless agent would show, so the model has a source to cite.
    try:
        term, abstract, url = await ddg_abstract(http, q)
    except Exception:
        abstract, url = '', None
    prompt = q if not abstract else f'{q}\n\nReference (Wikipedia abstract via DuckDuckGo):\n{abstract}'
    system = ABOUT + ' You are the knowledge agent: answer factually in 2-4 sentences, preferring the reference when given.'
    return result(engine, await engine.stream(system=system, prompt=prompt, effort='medium', emit_delta=emit_delta), url)


async def chat(engine, http, q: str, emit_delta) -> AgentResult:
    system = ABOUT + ' You are the chat agent: reply warmly in 1-3 sentences.'
    return result(engine, await engine.stream(system=system, prompt=q, effort='low', emit_delta=emit_delta, max_tokens=1024))


async def research(engine, http, q: str, emit_delta) -> AgentResult:
    system = (ABOUT.replace(' Do not use tools unless told to.', '') +
              ' You are the research agent: search the web, then answer in under 150 words with the key facts and dates, '
              'ending with the most relevant source URL.')
    reply = await engine.stream(system=system, prompt=q, effort='medium', emit_delta=emit_delta, max_tokens=4096, web=True)
    return result(engine, reply)


LLM_RUNNERS = {'code': code, 'knowledge': knowledge, 'chat': chat, 'research': research}
