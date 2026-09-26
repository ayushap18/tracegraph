"""Codex engine: `codex exec --json` on the user's ChatGPT/Codex subscription.

Codex has no system-prompt flag, so instructions are prepended to the prompt. It runs read-only in an empty scratch
directory with the user's config ignored (fewer MCP tools to load; auth still comes from CODEX_HOME). `exec --json`
does not stream text deltas, so the whole answer arrives with the agent_message item.
"""
import json
import os
from pathlib import Path

from .base import EngineError, Reply, first_url_in
from .cli import CliEngine, Parser

EFFORT = {'low': 'low', 'medium': 'medium', 'high': 'high'}


class CodexParser(Parser):
    def __init__(self):
        self.messages: list[str] = []
        self.usage: dict = {}
        self.failure: str | None = None

    def feed(self, e):
        t = e.get('type')
        if t == 'item.completed':
            item = e.get('item') or {}
            if item.get('type') == 'agent_message' and item.get('text'):
                self.messages.append(item['text'])
                return item['text'] if len(self.messages) == 1 else '\n\n' + item['text']
            # item.type == "error" is used for non-fatal warnings (e.g. ignored config keys); only turn.failed is fatal.
        elif t == 'turn.completed':
            self.usage = e.get('usage') or {}
        elif t in ('turn.failed', 'error'):
            err = e.get('error') or {}
            self.failure = (err.get('message') if isinstance(err, dict) else None) or e.get('message') or 'turn failed'
        return None

    def finish(self, returncode, stderr):
        text = '\n\n'.join(self.messages).strip()
        if self.failure and not text:
            msg = ' '.join(self.failure.split())[:200]
            raise EngineError('not logged in (run `codex login`)' if 'login' in msg.lower() or '401' in msg else msg)
        return Reply(text, self.usage.get('input_tokens') or 0, self.usage.get('output_tokens') or 0, first_url_in(text), [])


class CodexEngine(CliEngine):
    name = 'codex'
    label = 'Codex'
    binary = 'codex'
    bin_env = 'TG_CODEX_BIN'
    supports_web = True
    drop_env = ('OPENAI_API_KEY',)  # keep usage on the subscription login
    login_hint = 'run `codex login`'

    def command(self, *, system, prompt, effort, web, schema):
        args = ['--search'] if web else []
        args += ['exec', '--json', '--ephemeral', '--skip-git-repo-check', '--ignore-user-config',
                 '-s', 'read-only', '-C', self.cwd(), '-c', f'model_reasoning_effort="{EFFORT.get(effort, "medium")}"']
        if schema:
            path = Path(self.cwd(), f'schema-{abs(hash(json.dumps(schema, sort_keys=True)))}.json')
            path.write_text(json.dumps(schema))
            args += ['--output-schema', str(path)]
        if os.environ.get('TG_CODEX_MODEL'):
            args += ['-m', os.environ['TG_CODEX_MODEL']]
        args.append('-')  # prompt on stdin
        return args, f'{system}\n\n---\n\n{prompt}'

    def parser(self):
        return CodexParser()
