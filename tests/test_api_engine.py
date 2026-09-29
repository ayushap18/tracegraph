"""API-key engines (jevrouter/engines/api.py): providers, the request each call sends, and SSE streaming against a
local aiohttp server that speaks the OpenAI-compatible chat completions format."""
import json

import pytest
from aiohttp import web

from jevrouter.engines import EngineError, EngineRefusal, catalog
from jevrouter.engines.api import BUILTIN, ApiEngine, Provider, api_engines, providers
from tests.fakes import FakeLLM, api_errors, eng

SCHEMA = {'type': 'object', 'properties': {'subtasks': {'type': 'array', 'items': {'type': 'string'}}}}


def sse(*chunks) -> bytes:
    lines = [': keep-alive\n\n'] + [f'data: {json.dumps(c)}\n\n' for c in chunks] + ['data: [DONE]\n\n']
    return ''.join(lines).encode()


@pytest.fixture
async def server():
    """A local OpenAI-compatible endpoint. Set `reply` to (status, body bytes); every request lands in `seen`."""
    state = {'reply': (200, sse()), 'seen': []}

    async def completions(request):
        state['seen'].append({'headers': dict(request.headers), 'body': await request.json()})
        status, body = state['reply']
        return web.Response(status=status, body=body,
                            content_type='text/event-stream' if status == 200 else 'application/json')
    app = web.Application()
    app.router.add_post('/v1/chat/completions', completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state['url'] = f'http://127.0.0.1:{port}/v1'
    yield state
    await runner.cleanup()


def provider(url, **kw):
    return Provider('local', 'Local', url, 'test-model', 'LOCAL_KEY', **kw)


async def test_streams_sse_with_bearer_key_usage_and_citation(server, monkeypatch):
    monkeypatch.setenv('LOCAL_KEY', 'sk-local')
    server['reply'] = (200, sse(
        {'choices': [{'delta': {'role': 'assistant', 'content': ''}}]},
        {'choices': [{'delta': {'content': 'Par'}}]},
        {'choices': [{'delta': {'content': 'is', 'annotations': [
            {'type': 'url_citation', 'url_citation': {'url': 'https://example.org/paris'}}]}}]},
        {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
        {'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 2}}))
    e = ApiEngine(provider(server['url'], web={'plugins': [{'id': 'web'}]}))
    chunks = []
    r = await e.stream(system='Be brief.', prompt='capital?', emit_delta=chunks.append, web=True, max_tokens=300)
    assert r.text == 'Paris' and chunks == ['Par', 'is'] and (r.input_tokens, r.output_tokens) == (12, 2)
    assert r.source == 'https://example.org/paris'
    seen = server['seen'][0]
    assert seen['headers']['Authorization'] == 'Bearer sk-local'
    body = seen['body']
    assert body['model'] == 'test-model' and body['stream'] is True and body['max_tokens'] == 300
    assert body['plugins'] == [{'id': 'web'}] and body['stream_options'] == {'include_usage': True}
    assert body['messages'] == [{'role': 'system', 'content': 'Be brief.'}, {'role': 'user', 'content': 'capital?'}]
    await e.aclose()


@pytest.mark.parametrize('status, why', [(401, 'API key was rejected \\(check LOCAL_KEY\\)'), (429, 'rate limited: slow'),
                                         (402, 'out of credit'), (500, 'API error 500: slow')])
async def test_http_errors(server, monkeypatch, status, why):
    monkeypatch.setenv('LOCAL_KEY', 'sk-local')
    server['reply'] = (status, json.dumps({'error': {'message': 'slow'}}).encode())
    e = ApiEngine(provider(server['url']))
    with pytest.raises(EngineError, match=why):
        await e.stream(system='s', prompt='p')
    await e.aclose()


async def test_connection_refused_and_empty_answer(server, monkeypatch):
    monkeypatch.setenv('LOCAL_KEY', 'sk-local')
    e = ApiEngine(provider('http://127.0.0.1:9/v1'))
    with pytest.raises(EngineError, match='could not connect'):
        await e.stream(system='s', prompt='p')
    await e.aclose()
    e = ApiEngine(provider(server['url']))  # the default reply streams nothing
    with pytest.raises(EngineError, match='returned no answer'):
        await e.stream(system='s', prompt='p')
    await e.aclose()


async def test_schema_effort_and_images_follow_the_provider():
    fake = FakeLLM(['{}'], ['{}'], ['x'])
    strict = ApiEngine(Provider('a', 'A', 'https://a.test/v1', 'm', json_schema=True, effort=True, limit=None), client=fake)
    await strict.stream(system='plan', prompt='p', schema=SCHEMA, effort='low', images=[b'png'])
    body = fake.calls[0]
    assert body['response_format']['json_schema']['schema'] == SCHEMA and body['reasoning_effort'] == 'low'
    assert 'max_tokens' not in body and body['messages'][1]['content'][0]['type'] == 'image_url'
    plain = ApiEngine(Provider('b', 'B', 'https://b.test/v1/', 'm', vision=False), client=fake)
    await plain.stream(system='plan', prompt='p', schema=SCHEMA, images=[b'png'])
    body = fake.calls[1]
    assert 'response_format' not in body and 'reasoning_effort' not in body and body['max_tokens'] == 2048
    assert json.dumps(SCHEMA) in body['messages'][0]['content'] and body['messages'][1]['content'] == 'p'
    assert body['_url'] == 'https://b.test/v1/chat/completions' and 'Authorization' not in body['_headers']
    assert not plain.supports_vision and not plain.supports_web


async def test_refusal_and_mid_stream_failure_keep_what_was_said():
    with pytest.raises(EngineRefusal):
        await eng(FakeLLM(('refusal', ['No.']))).stream(system='s', prompt='p')
    chunks = []
    with pytest.raises(EngineError, match='could not connect') as err:
        await eng(FakeLLM(['half ', api_errors()['conn']])).stream(system='s', prompt='p', emit_delta=chunks.append)
    assert err.value.partial == 'half ' and chunks == ['half ']


def test_only_providers_with_a_key_become_engines(monkeypatch):
    assert api_engines() == {}  # conftest clears every key
    monkeypatch.setenv('GROQ_API_KEY', 'gsk')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk')
    monkeypatch.setenv('TG_OPENAI_MODEL', 'gpt-custom')
    engines = api_engines()
    assert list(engines) == ['openai', 'groq'] and engines['openai'].info()['model'] == 'gpt-custom'
    assert engines['openai'].billing == 'api' and not engines['groq'].supports_vision
    assert {p.name for p in BUILTIN} >= {'openai', 'openrouter', 'gemini', 'groq', 'deepseek', 'mistral', 'xai'}
    assert 'anthropic' not in {p.name for p in BUILTIN}
    names = list(catalog())
    assert names[:5] == ['auto', 'claude-code', 'codex', 'agy', 'opencode'] and names[5:] == ['openai', 'groq']


def test_custom_endpoint_and_providers_file(tmp_path, monkeypatch):
    monkeypatch.setenv('TG_API_BASE_URL', 'http://localhost:11434/v1')
    monkeypatch.setenv('TG_API_MODEL', 'llama3.2')
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps([
        {'name': 'together', 'base_url': 'https://api.together.xyz/v1', 'key_env': 'TOGETHER_API_KEY', 'model': 't'},
        {'name': 'openai', 'label': 'Work OpenAI', 'base_url': 'https://proxy.test/v1', 'model': 'gpt-x',
         'api_key': 'sk-file', 'headers': {'X-Team': 'jev'}},
    ]))
    monkeypatch.setenv('TG_PROVIDERS', str(path))
    found = {p.name: p for p in providers()}
    assert found['api'].base_url == 'http://localhost:11434/v1' and found['api'].key() == ''
    assert found['openai'].label == 'Work OpenAI' and found['openai'].key() == 'sk-file'  # the file replaces the built-in
    assert found['together'].label == 'together'
    engines = api_engines()
    assert set(engines) == {'api', 'openai'}  # a local endpoint needs no key; together's key is not set
    url, headers, _ = engines['openai'].request(system='s', prompt='p', effort='medium', max_tokens=5, schema=None,
                                                images=None)
    assert url == 'https://proxy.test/v1/chat/completions' and headers['X-Team'] == 'jev'
    assert headers['Authorization'] == 'Bearer sk-file'
    path.write_text(json.dumps([{'name': 'x', 'base_url': 'u', 'model': 'm', 'colour': 'red'}]))
    with pytest.raises(ValueError, match="providers.json: unknown field 'colour'"):
        providers()
