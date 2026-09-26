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


async def report(engine, http, q: str, emit_delta) -> AgentResult:
    web = engine.supports_web
    system = (ABOUT.replace(' Do not use tools unless told to.', '' if web else ' Do not use tools.') +
              ' You are the report agent: ' + ('search the web, then ' if web else '') +
              'write a well-structured Markdown report of 400-900 words with headings, and end with a "## Sources" '
              'section listing the URLs or references you relied on.')
    reply = await engine.stream(system=system, prompt=q, effort='high', emit_delta=emit_delta, max_tokens=8192, web=web)
    return result(engine, reply)


async def run_code(engine, http, q: str, emit_delta) -> AgentResult:
    system = (ABOUT.replace(' Do not use tools unless told to.', '') +
              ' You are the run agent, working in an empty scratch directory inside a sandbox with no network access. '
              'Write a small script (Python unless the task names another language), execute it, and reply with: the '
              'script in a fenced code block, its exact output in a fenced block, then a one-line summary.')
    return result(engine, await engine.stream(system=system, prompt=q, effort='medium', emit_delta=emit_delta,
                                              max_tokens=4096, exec=True))


def custom(agent: dict):
    """A user-defined agent: its own prompt on the active engine, with web search only if the engine has it."""
    async def run(engine, http, q: str, emit_delta) -> AgentResult:
        web = bool(agent.get('web')) and engine.supports_web
        base = ABOUT.replace(' Do not use tools unless told to.', '') if web else ABOUT
        system = f"{base} You are the {agent['name']} agent. {agent['prompt']}"
        return result(engine, await engine.stream(system=system, prompt=q, effort='medium', emit_delta=emit_delta,
                                                  max_tokens=4096, web=web))
    return run


LLM_RUNNERS = {'code': code, 'knowledge': knowledge, 'chat': chat, 'research': research, 'report': report}
