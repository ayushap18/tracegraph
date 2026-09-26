"""Antigravity engine: `agy -p --output-format stream-json` on the user's Google account.

Google documents launching the official agy binary as a headless child process on its cached credentials as a
supported workflow that counts against the same subscription limits as interactive use. Install with
`curl -fsSL https://antigravity.google/cli/install.sh | bash`, then run `agy` once to sign in.
Event schema (docs: antigravity.google/docs/cli/headless): `init`, `step_update` with `text_delta`, and a final
`result` carrying status, response, error and usage.
"""
import json
import os

from .base import EngineError, Reply, first_url_in
from .cli import CliEngine, Parser

EFFORT = {'low': 'low', 'medium': 'medium', 'high': 'high'}


class AgyParser(Parser):
    def __init__(self):
        self.parts: list[str] = []
        self.result: dict | None = None

    def feed(self, e):
        t = e.get('type')
        if t == 'step_update' and e.get('text_delta'):
            self.parts.append(e['text_delta'])
            return e['text_delta']
        if t == 'result':
            self.result = e
        return None

    def finish(self, returncode, stderr):
        r = self.result or {}
        usage = r.get('usage') or {}
        structured = r.get('structured_output')
        text = json.dumps(structured) if structured is not None else (r.get('response') or ''.join(self.parts)).strip()
        status = str(r.get('status') or '').upper()
        if r.get('error') or (status and status not in ('SUCCESS', 'OK', 'COMPLETED')):
            msg = r.get('error')
            msg = (msg.get('message') if isinstance(msg, dict) else msg) or status or 'failed'
            if not text or r.get('error'):
                msg = ' '.join(str(msg).split())[:200]
                raise EngineError('not logged in (run `agy` once and sign in)' if 'auth' in msg.lower() else msg)
        tokens_in = usage.get('input_tokens') or usage.get('prompt_tokens') or 0
        tokens_out = usage.get('output_tokens') or usage.get('completion_tokens') or 0
        return Reply(text, tokens_in, tokens_out, first_url_in(text), [r])


class AgyEngine(CliEngine):
    name = 'agy'
    label = 'Antigravity'
    binary = 'agy'
    bin_env = 'TG_AGY_BIN'
    supports_web = False  # its headless tool set isn't documented to include web search; research stays off
    drop_env = ('GEMINI_API_KEY', 'GOOGLE_API_KEY')
    login_hint = 'run `agy` once and sign in'

    def command(self, *, system, prompt, effort, web, schema):
        args = ['-p', f'{system}\n\n---\n\n{prompt}', '--output-format', 'stream-json',
                '--effort', EFFORT.get(effort, 'medium'), '--print-timeout', f'{int(self.timeout)}s']
        if schema:
            args += ['--json-schema', json.dumps(schema)]
        if os.environ.get('TG_AGY_MODEL'):
            args += ['--model', os.environ['TG_AGY_MODEL']]
        return args, ''

    def parser(self):
        return AgyParser()
