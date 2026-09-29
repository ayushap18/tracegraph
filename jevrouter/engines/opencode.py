"""OpenCode engine: `opencode run --format json` on whatever the user signed in to (`opencode auth login`), or on
OpenCode's free models.

OpenCode has no system-prompt flag, so instructions are prepended to the prompt, which goes in on stdin. Its agent can
edit files, run shell commands and read files anywhere, so the empty scratch directory it runs in carries an
opencode.json that puts every such tool behind "ask": a headless run cannot grant it, so the tool call fails and the
model answers without it. ("deny" would work too, but OpenCode's free tier refuses calls from a config that denies
tools.) `--format json` prints one event per line: `step_start`, `text` (a finished text part, not a delta),
`step_finish` with token counts, and `error`. TG_OPENCODE_MODEL picks the model as provider/model (`opencode models`).
"""
import json
import os
from pathlib import Path

from .base import SCHEMA_NOTE, EngineError, Reply, first_url_in
from .cli import CliEngine, Parser

LOCKED = {'permission': {tool: 'ask' for tool in ('edit', 'bash', 'shell', 'webfetch', 'external_directory')}}
LOGIN_WORDS = ('auth', 'api key', 'apikey', 'unauthorized', '401', 'credential', 'login')


class OpenCodeParser(Parser):
    def __init__(self):
        self.parts: list[str] = []
        self.tokens_in = self.tokens_out = 0
        self.failure: str | None = None

    def feed(self, e):
        t = e.get('type')
        part = e.get('part') or {}
        if t == 'text' and part.get('text'):
            self.parts.append(part['text'])
            return part['text'] if len(self.parts) == 1 else '\n\n' + part['text']
        if t == 'step_finish':
            tokens = part.get('tokens') or {}
            cache = tokens.get('cache') or {}
            self.tokens_in += (tokens.get('input') or 0) + (cache.get('read') or 0) + (cache.get('write') or 0)
            self.tokens_out += (tokens.get('output') or 0) + (tokens.get('reasoning') or 0)
        elif t == 'error':
            err = e.get('error')
            if isinstance(err, dict):
                err = (err.get('data') or {}).get('message') or err.get('message') or err.get('name')
            self.failure = str(err or 'failed')
        return None

    def finish(self, returncode, stderr):
        text = '\n\n'.join(self.parts).strip()
        if self.failure and not text:
            msg = ' '.join(self.failure.split())[:200]
            if 'free tier' not in msg.lower() and any(w in msg.lower() for w in LOGIN_WORDS):
                msg = 'not logged in (run `opencode auth login`)'
            raise EngineError(msg)
        return Reply(text, self.tokens_in, self.tokens_out, first_url_in(text), [])


class OpenCodeEngine(CliEngine):
    name = 'opencode'
    label = 'OpenCode'
    binary = 'opencode'
    bin_env = 'TG_OPENCODE_BIN'
    login_hint = 'run `opencode auth login`'

    def prepare(self, workdir):
        Path(workdir, 'opencode.json').write_text(json.dumps(LOCKED))

    def command(self, *, system, prompt, effort, web, schema):
        args = ['run', '--format', 'json']
        if os.environ.get('TG_OPENCODE_MODEL'):
            args += ['-m', os.environ['TG_OPENCODE_MODEL']]
        if schema:
            system += SCHEMA_NOTE + json.dumps(schema)
        return args, f'{system}\n\n---\n\n{prompt}'

    def parser(self):
        return OpenCodeParser()
