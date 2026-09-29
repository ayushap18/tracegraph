"""Claude Code engine: `claude -p` on the user's Claude subscription.

A bare `claude -p` loads the full agent context (tools, MCP servers, settings, memory): about 108K tokens for a
one-word reply. These flags cut that to a few hundred: no tools unless the call needs web search, an empty MCP config,
no setting sources, no slash commands, and our own short system prompt. `--bare` is not used because it also
disables the subscription login.
"""
import json
import os
from pathlib import Path

from .base import EngineError, EngineRefusal, Reply, first_url_in
from .cli import CliEngine, Parser, run

EFFORT = {'low': 'low', 'medium': 'medium', 'high': 'high'}


class ClaudeCodeParser(Parser):
    def __init__(self):
        self.parts: list[str] = []
        self.result: dict | None = None

    def feed(self, e):
        t = e.get('type')
        if t == 'stream_event':
            ev = e.get('event') or {}
            if ev.get('type') == 'content_block_delta' and (ev.get('delta') or {}).get('type') == 'text_delta':
                text = ev['delta'].get('text', '')
                self.parts.append(text)
                return text
        elif t == 'result':
            self.result = e
        return None

    def finish(self, returncode, stderr):
        r = self.result or {}
        usage = r.get('usage') or {}
        tokens_in = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
        structured = r.get('structured_output')
        text = json.dumps(structured) if structured is not None else (r.get('result') or ''.join(self.parts)).strip()
        # is_error comes with subtype "success" for errors like "Not logged in", so check it before trusting the text.
        if r.get('is_error'):
            raise EngineError(normalize_error(text or stderr))
        reply = Reply(text, tokens_in, usage.get('output_tokens') or 0, first_url_in(text), [r])
        if r.get('stop_reason') == 'refusal':
            raise EngineRefusal(reply)
        return reply


def normalize_error(msg: str) -> str:
    m = ' '.join((msg or 'failed').split())[:200]
    if 'not logged in' in m.lower() or '/login' in m:
        return 'not logged in (run `claude` once and sign in)'
    return m


class ClaudeCodeEngine(CliEngine):
    name = 'claude-code'
    label = 'Claude Code'
    binary = 'claude'
    bin_env = 'TG_CLAUDE_BIN'
    supports_web = True
    # Images go in through the CLI's stream-json input, as base64 image blocks in the user message (the same message
    # shape the Messages API takes), so no file on disk and no Read tool is needed.
    supports_vision = True
    # Without these the CLI would bill a pay-per-token key instead of the subscription.
    drop_env = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')
    login_hint = 'run `claude` once and sign in'

    def prepare(self, workdir):
        Path(workdir, 'mcp.json').write_text('{"mcpServers": {}}')

    def command(self, *, system, prompt, effort, web, schema):
        return self.flags(system, effort, web, schema), prompt

    def persistent(self, *, system, prompt, effort, web, schema):
        # The system prompt is a flag, so each distinct one (planner, merger, each agent) gets its own warm process.
        line = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': prompt}}) + '\n'
        return self.flags(system, effort, web, schema) + ['--input-format', 'stream-json'], line

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, schema=None,
                     exec=False, images=None):
        if not images:
            return await super().stream(system=system, prompt=prompt, effort=effort, emit_delta=emit_delta,
                                        max_tokens=max_tokens, web=web, schema=schema, exec=exec)
        # A call with images starts its own process and leaves the warm pool to text calls: image calls are rare (the
        # design critic, Polish), so they are not worth a pre-started process.
        ok, why = self.available()
        if not ok:
            raise EngineError(why)
        args, line = self.with_images(system=system, prompt=prompt, effort=effort, web=web, schema=schema,
                                      images=images)
        async with self.sem:
            return await run(self.path, args, line, self.parser(), emit_delta, env=self.env(), cwd=self.cwd(),
                             timeout=self.timeout, login_hint=self.login_hint)

    def with_images(self, *, system, prompt, effort, web, schema, images):
        """(argv, one stream-json stdin line) for a prompt with PNG images before it."""
        from .anthropic_api import user_content
        message = {'role': 'user', 'content': user_content(prompt, images)}
        line = json.dumps({'type': 'user', 'message': message}) + '\n'
        return self.flags(system, effort, web, schema) + ['--input-format', 'stream-json'], line

    def flags(self, system, effort, web, schema):
        args = ['-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
                '--no-session-persistence', '--strict-mcp-config', '--mcp-config', str(Path(self.cwd(), 'mcp.json')),
                '--setting-sources', '', '--disable-slash-commands', '--system-prompt', system,
                '--effort', EFFORT.get(effort, 'medium')]
        if web:
            args += ['--tools', 'WebSearch,WebFetch', '--allowedTools', 'WebSearch', 'WebFetch']
        else:
            args += ['--tools', '']
        if schema:
            args += ['--json-schema', json.dumps(schema)]
        if os.environ.get('TG_CLAUDE_CODE_MODEL'):
            args += ['--model', os.environ['TG_CLAUDE_CODE_MODEL']]
        return args

    def parser(self):
        return ClaudeCodeParser()
