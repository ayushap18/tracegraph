"""API-key engines: any provider that speaks the OpenAI-compatible chat completions API, billed per token.

One class covers them all; a Provider says where to send the call and which key to use. Each built-in provider whose
key is set (OPENAI_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, ...) shows up as its own engine, so Auto can fall
through from one key to the next. TG_API_BASE_URL + TG_API_MODEL (+ TG_API_KEY) add a custom endpoint named `api`
(a local Ollama or LM Studio needs no key), and a JSON file (TG_PROVIDERS, default data/providers.json) adds more:

    [{"name": "together", "label": "Together", "base_url": "https://api.together.xyz/v1",
      "key_env": "TOGETHER_API_KEY", "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "vision": false}]

A file entry with a built-in's name replaces it. TG_<NAME>_MODEL (e.g. TG_OPENAI_MODEL) overrides a provider's model.
Streaming is plain SSE over aiohttp, so no vendor SDK is needed.
"""
import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from ..config import ROOT
from .base import SCHEMA_NOTE, Engine, EngineError, EngineRefusal, Reply, first_url_in

TIMEOUT = float(os.environ.get('TG_ENGINE_TIMEOUT', 180))


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    base_url: str
    model: str
    key_env: str = ''           # the environment variable holding the key; '' for endpoints that need none
    api_key: str = ''           # a key written in the providers file itself (data/ is not committed)
    vision: bool = True         # accepts image_url parts (Studio's design critic and Polish need it)
    json_schema: bool = False   # accepts response_format json_schema; otherwise the schema is only in the prompt
    effort: bool = False        # accepts reasoning_effort
    limit: str | None = 'max_tokens'  # the output cap's parameter name, or None to send no cap
    web: dict = field(default_factory=dict)  # body fields that turn on web search, if the provider has it
    headers: dict = field(default_factory=dict)

    def key(self) -> str:
        return self.api_key or (os.environ.get(self.key_env, '') if self.key_env else '')

    def model_now(self) -> str:
        return os.environ.get(f"TG_{self.name.upper().replace('-', '_')}_MODEL") or self.model


BUILTIN = (
    # OpenAI's reasoning models take max_completion_tokens and spend part of it thinking, so no cap is sent.
    Provider('openai', 'OpenAI API', 'https://api.openai.com/v1', 'gpt-5', 'OPENAI_API_KEY', json_schema=True,
             effort=True, limit=None),
    Provider('openrouter', 'OpenRouter', 'https://openrouter.ai/api/v1', 'openrouter/auto', 'OPENROUTER_API_KEY',
             json_schema=True, web={'plugins': [{'id': 'web'}]}),
    Provider('gemini', 'Gemini API', 'https://generativelanguage.googleapis.com/v1beta/openai', 'gemini-2.5-flash',
             'GEMINI_API_KEY', json_schema=True),
    Provider('groq', 'Groq', 'https://api.groq.com/openai/v1', 'llama-3.3-70b-versatile', 'GROQ_API_KEY', vision=False),
    Provider('deepseek', 'DeepSeek', 'https://api.deepseek.com/v1', 'deepseek-chat', 'DEEPSEEK_API_KEY', vision=False),
    Provider('mistral', 'Mistral', 'https://api.mistral.ai/v1', 'mistral-large-latest', 'MISTRAL_API_KEY'),
    Provider('xai', 'xAI', 'https://api.x.ai/v1', 'grok-4', 'XAI_API_KEY'),
)
FIELDS = set(Provider.__dataclass_fields__)


def custom() -> Provider | None:
    """TG_API_BASE_URL + TG_API_MODEL: any OpenAI-compatible endpoint, with TG_API_KEY when it needs one."""
    url, model = os.environ.get('TG_API_BASE_URL', '').strip(), os.environ.get('TG_API_MODEL', '').strip()
    if not (url and model):
        return None
    key_env = 'TG_API_KEY' if os.environ.get('TG_API_KEY') else ''  # a local Ollama or LM Studio takes no key
    return Provider('api', os.environ.get('TG_API_LABEL') or 'Custom API', url, model, key_env,
                    vision=os.environ.get('TG_API_VISION', '1') != '0')


def from_file(path: Path) -> list[Provider]:
    """Providers listed in a JSON file; a missing file is none, a bad one is an error that names it."""
    if not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text())
        rows = rows.get('providers', []) if isinstance(rows, dict) else rows
        out = []
        for row in rows:
            unknown = set(row) - FIELDS
            if unknown:
                raise ValueError(f'unknown field {sorted(unknown)[0]!r}')
            row = {'label': row.get('name', ''), **row}
            out.append(Provider(**row))
        return out
    except (ValueError, TypeError) as e:
        raise ValueError(f'{path}: {e}') from None


def providers() -> list[Provider]:
    """Built-ins, then the custom endpoint, then the providers file; a later one with the same name replaces it."""
    path = Path(os.environ.get('TG_PROVIDERS') or ROOT / 'data' / 'providers.json')
    found = {p.name: p for p in (*BUILTIN, custom(), *from_file(path)) if p is not None}
    return list(found.values())


def image_parts(images) -> list[dict]:
    """PNG bytes as image_url parts with base64 data URLs."""
    return [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')}}
            for png in images or ()]


def user_content(prompt: str, images=None):
    """The user turn: the plain prompt, or the images followed by the prompt as one text part."""
    if not images:
        return prompt
    return [*image_parts(images), {'type': 'text', 'text': prompt}]


class HttpError(Exception):
    def __init__(self, status: int, message: str = ''):
        super().__init__(f'{status} {message}'.strip())
        self.status, self.message = status, message


class Http:
    """POSTs a streaming chat completion and yields each SSE chunk as a dict."""

    def __init__(self, timeout: float = TIMEOUT):
        self.timeout = timeout
        self.session = None

    async def stream(self, url: str, headers: dict, body: dict):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        async with self.session.post(url, headers=headers, json=body) as resp:
            if resp.status >= 400:
                raise HttpError(resp.status, error_message(await resp.text()))
            async for raw in resp.content:
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue  # blank separators, `event:` lines and `: keep-alive` comments
                data = line[5:].strip()
                if data == '[DONE]':
                    return
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get('error'):
                    raise HttpError(500, error_message(json.dumps(chunk)))
                yield chunk

    async def close(self):
        if self.session is not None:
            await self.session.close()
            self.session = None


def error_message(text: str) -> str:
    try:
        err = json.loads(text)
        err = err[0] if isinstance(err, list) and err else err
        err = err.get('error', err) if isinstance(err, dict) else err
        msg = err.get('message') if isinstance(err, dict) else err
    except ValueError:
        msg = text
    return ' '.join(str(msg or '').split())[:200]


class ApiEngine(Engine):
    billing = 'api'

    def __init__(self, provider: Provider, client=None):
        self.provider = provider
        self.name, self.label = provider.name, provider.label
        self.supports_vision = provider.vision
        self.supports_web = bool(provider.web)  # chat completions has no standard web search; some providers add one
        self.client = client

    def available(self):
        p = self.provider
        if self.client is not None or p.key() or not p.key_env:
            return True, ''
        return False, f'{p.key_env} is not set'

    def info(self) -> dict:
        return {**super().info(), 'model': self.provider.model_now()}

    def _client(self):
        if self.client is None:
            self.client = Http()
        return self.client

    def request(self, *, system, prompt, effort, max_tokens, schema, images, web=False) -> tuple[str, dict, dict]:
        """(url, headers, body) of one streaming call."""
        p = self.provider
        if schema:
            system = system + SCHEMA_NOTE + json.dumps(schema)
        body = {'model': p.model_now(), 'stream': True, 'stream_options': {'include_usage': True},
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': user_content(prompt, images if p.vision else None)}]}
        if p.limit:
            body[p.limit] = max_tokens
        if p.effort:
            body['reasoning_effort'] = effort if effort in ('low', 'medium', 'high') else 'medium'
        if web and p.web:
            body.update(p.web)
        if schema and p.json_schema:
            body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'answer', 'schema': schema}}
        headers = {'Content-Type': 'application/json', **p.headers}
        if p.key():
            headers['Authorization'] = f'Bearer {p.key()}'
        return p.base_url.rstrip('/') + '/chat/completions', headers, body

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, schema=None,
                     exec=False, images=None):
        ok, why = self.available()
        if not ok:
            raise EngineError(why)
        url, headers, body = self.request(system=system, prompt=prompt, effort=effort, max_tokens=max_tokens,
                                          schema=schema, images=images, web=web)
        parts, refusal, finish, usage, source = [], [], None, {}, None
        try:
            async for chunk in self._client().stream(url, headers, body):
                usage = chunk.get('usage') or usage
                for choice in chunk.get('choices') or ():
                    delta = choice.get('delta') or {}
                    source = source or cited(delta)
                    if delta.get('refusal'):
                        refusal.append(delta['refusal'])
                    if delta.get('content'):
                        parts.append(delta['content'])
                        if emit_delta:
                            emit_delta(delta['content'])
                    finish = choice.get('finish_reason') or finish
        except HttpError as e:
            raise EngineError(self.describe(e), ''.join(parts))
        except asyncio.TimeoutError:
            raise EngineError(f'timed out after {TIMEOUT:.0f}s', ''.join(parts))
        except (aiohttp.ClientError, OSError):
            raise EngineError('could not connect', ''.join(parts))
        text = ''.join(parts).strip()
        reply = Reply(text or ''.join(refusal).strip(), usage.get('prompt_tokens') or 0,
                      usage.get('completion_tokens') or 0, source or first_url_in(text), [])
        if refusal or finish == 'content_filter':
            raise EngineRefusal(reply)
        if not text:
            raise EngineError('returned no answer')
        return reply

    def describe(self, e: HttpError) -> str:
        if e.status in (401, 403):
            return f'API key was rejected (check {self.provider.key_env or "the key"})'
        if e.status == 429:
            return 'rate limited' + (f': {e.message}' if e.message else '')
        if e.status == 402:
            return 'out of credit'
        return f'API error {e.status}' + (f': {e.message}' if e.message else '')

    async def aclose(self):
        if self.client is not None and hasattr(self.client, 'close'):
            await self.client.close()


def cited(delta: dict) -> str | None:
    """The first url_citation annotation's link, which web-search providers attach to the answer."""
    for a in delta.get('annotations') or ():
        url = (a.get('url_citation') or {}).get('url') if isinstance(a, dict) else None
        if url:
            return url
    return None


def api_engines() -> dict[str, ApiEngine]:
    """An engine per provider that can be called now: its key is set, or it needs none. Unconfigured built-ins stay out
    of the picker instead of filling it with greyed-out rows."""
    engines = (ApiEngine(p) for p in providers())
    return {e.name: e for e in engines if e.available()[0]}
