"""CLI engines against fake `claude` / `codex` / `agy` binaries that speak each tool's NDJSON format.

No logins, no network: each fake is a tiny Python script, so these tests pin the exact flags we pass, how events become
deltas, error handling, timeouts, process cleanup and the environment the child sees.
"""
import asyncio
import json
import os
import sys
import textwrap

import pytest

from jevrouter.engines import EngineError, catalog, choose, parse_json
from jevrouter.engines.agy import AgyEngine
from jevrouter.engines.claude_code import ClaudeCodeEngine
from jevrouter.engines.codex import CodexEngine

SCHEMA = {'type': 'object', 'properties': {'subtasks': {'type': 'array', 'items': {'type': 'string'}}}}


def fake(tmp_path, name, body):
    """Writes an executable fake CLI. It records argv, stdin and selected env vars to <name>.call.json first."""
    path = tmp_path / name
    path.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
        import json, os, sys, time
        argv = sys.argv[1:]
        stdin = sys.stdin.read()
        seen = {k: os.environ.get(k) for k in ('TYPESAFE_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'NO_COLOR')}
        json.dump({'argv': argv, 'stdin': stdin, 'env': seen, 'pid': os.getpid(), 'cwd': os.getcwd()},
                  open(__file__ + '.call.json', 'w'))
        out = lambda e: print(json.dumps(e), flush=True)
    ''') + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


def call(path):
    return json.loads(open(path + '.call.json').read())


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'jev-secret')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-openai')


CLAUDE_OK = '''
    out({'type': 'system', 'subtype': 'init', 'apiKeySource': 'none'})
    for t in ['Par', 'is']:
        out({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': t}}})
    out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Paris',
         'usage': {'input_tokens': 400, 'cache_read_input_tokens': 50, 'output_tokens': 2}})
'''


async def test_claude_code_streams_with_slim_flags_and_scrubbed_env(tmp_path):
    path = fake(tmp_path, 'claude', CLAUDE_OK)
    e = ClaudeCodeEngine(path)
    chunks = []
    r = await e.stream(system='Be brief.', prompt='capital of France?', effort='low', emit_delta=chunks.append)
    assert chunks == ['Par', 'is'] and r.text == 'Paris' and (r.input_tokens, r.output_tokens) == (450, 2)
    c = call(path)
    a = c['argv']
    assert a[:3] == ['-p', '--output-format', 'stream-json'] and '--include-partial-messages' in a
    assert a[a.index('--tools') + 1] == '' and a[a.index('--system-prompt') + 1] == 'Be brief.'
    assert a[a.index('--setting-sources') + 1] == '' and '--strict-mcp-config' in a and a[a.index('--effort') + 1] == 'low'
    assert c['stdin'] == 'capital of France?'
    # Our Jev key never reaches the child, and no API key can switch it off the subscription.
    assert c['env']['TYPESAFE_API_KEY'] is None and c['env']['ANTHROPIC_API_KEY'] is None and c['env']['NO_COLOR'] == '1'
    assert os.listdir(c['cwd']) == ['mcp.json']  # empty scratch dir, only our empty MCP config
    await e.aclose()
    assert not os.path.exists(c['cwd'])


async def test_claude_code_web_and_schema_flags(tmp_path):
    path = fake(tmp_path, 'claude', CLAUDE_OK)
    await ClaudeCodeEngine(path).stream(system='s', prompt='p', web=True, schema=SCHEMA)
    a = call(path)['argv']
    assert a[a.index('--tools') + 1] == 'WebSearch,WebFetch' and json.loads(a[a.index('--json-schema') + 1]) == SCHEMA


async def test_claude_code_structured_output_wins(tmp_path):
    path = fake(tmp_path, 'claude', '''
        out({'type': 'result', 'is_error': False, 'result': 'ignored', 'structured_output': {'subtasks': ['a', 'b']}, 'usage': {}})
    ''')
    r = await ClaudeCodeEngine(path).stream(system='s', prompt='p', schema=SCHEMA)
    assert parse_json(r.text) == {'subtasks': ['a', 'b']}


async def test_claude_code_not_logged_in(tmp_path):
    # Real behaviour: subtype "success" with is_error true and the message in result.
    path = fake(tmp_path, 'claude', '''
        out({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'Not logged in · Please run /login', 'usage': {}})
    ''')
    with pytest.raises(EngineError, match='not logged in'):
        await ClaudeCodeEngine(path).stream(system='s', prompt='p')


CODEX_OK = '''
    out({'type': 'thread.started', 'thread_id': 't'})
    out({'type': 'item.completed', 'item': {'id': 'i0', 'type': 'error', 'message': 'ignoring unknown config key'}})
    out({'type': 'turn.started'})
    out({'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'Paris. Source: https://en.wikipedia.org/wiki/Paris.'}})
    out({'type': 'turn.completed', 'usage': {'input_tokens': 15000, 'cached_input_tokens': 12000, 'output_tokens': 5}})
'''


async def test_codex_ignores_warning_items_and_reads_stdin_prompt(tmp_path):
    path = fake(tmp_path, 'codex', CODEX_OK)
    chunks = []
    r = await CodexEngine(path).stream(system='Be brief.', prompt='capital?', effort='high', emit_delta=chunks.append)
    assert r.text.startswith('Paris') and chunks == [r.text] and r.source == 'https://en.wikipedia.org/wiki/Paris'
    assert (r.input_tokens, r.output_tokens) == (15000, 5)
    c = call(path)
    a = c['argv']
    assert a[0] == 'exec' and '--search' not in a and a[-1] == '-' and 'read-only' in a and '--ignore-user-config' in a
    assert 'model_reasoning_effort="high"' in a and c['stdin'] == 'Be brief.\n\n---\n\ncapital?'
    assert c['env']['OPENAI_API_KEY'] is None and c['env']['TYPESAFE_API_KEY'] is None


async def test_codex_web_search_and_output_schema(tmp_path):
    path = fake(tmp_path, 'codex', CODEX_OK)
    e = CodexEngine(path)
    await e.stream(system='s', prompt='p', web=True, schema=SCHEMA)
    a = call(path)['argv']
    assert a[:2] == ['--search', 'exec'] and json.loads(open(a[a.index('--output-schema') + 1]).read()) == SCHEMA


async def test_codex_turn_failed(tmp_path):
    path = fake(tmp_path, 'codex', '''
        out({'type': 'turn.failed', 'error': {'message': '401 Unauthorized: please login'}})
    ''')
    with pytest.raises(EngineError, match='codex login'):
        await CodexEngine(path).stream(system='s', prompt='p')


async def test_agy_stream_json(tmp_path):
    path = fake(tmp_path, 'agy', '''
        out({'type': 'init', 'cwd': '.', 'tools': [], 'permission_mode': 'default'})
        for t in ['Pa', 'ris']:
            out({'type': 'step_update', 'step_type': 'response', 'state': 'running', 'text_delta': t})
        out({'type': 'result', 'status': 'SUCCESS', 'response': 'Paris', 'usage': {'input_tokens': 90, 'output_tokens': 2}})
    ''')
    chunks = []
    r = await AgyEngine(path).stream(system='Be brief.', prompt='capital?', effort='medium', emit_delta=chunks.append)
    assert chunks == ['Pa', 'ris'] and r.text == 'Paris' and (r.input_tokens, r.output_tokens) == (90, 2)
    a = call(path)['argv']
    assert a[0] == '-p' and a[1].endswith('capital?') and a[a.index('--output-format') + 1] == 'stream-json'


async def test_agy_error_result(tmp_path):
    path = fake(tmp_path, 'agy', '''
        out({'type': 'result', 'status': 'ERROR', 'error': 'authentication required', 'response': ''})
    ''')
    with pytest.raises(EngineError, match='not logged in'):
        await AgyEngine(path).stream(system='s', prompt='p')


async def test_timeout_kills_the_process_group(tmp_path):
    path = fake(tmp_path, 'claude', '''
        import subprocess
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        json.dump(child.pid, open(__file__ + '.child', 'w'))
        out({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'half'}}})
        time.sleep(60)
    ''')
    with pytest.raises(EngineError, match='timed out') as err:
        await ClaudeCodeEngine(path, timeout=1.5).stream(system='s', prompt='p')
    assert err.value.partial == 'half'
    await asyncio.sleep(0.2)
    for pid in (call(path)['pid'], json.loads(open(path + '.child').read())):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # both the CLI and anything it spawned are gone


async def test_cancelled_run_kills_the_cli(tmp_path):
    path = fake(tmp_path, 'codex', 'time.sleep(60)\n')
    task = asyncio.create_task(CodexEngine(path).stream(system='s', prompt='p'))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(call(path)['pid'], 0)


async def test_crash_without_answer(tmp_path):
    path = fake(tmp_path, 'codex', 'sys.stderr.write("boom: segfault"); sys.exit(3)\n')
    with pytest.raises(EngineError, match='exited with code 3: boom'):
        await CodexEngine(path).stream(system='s', prompt='p')


async def test_concurrency_cap(tmp_path):
    path = fake(tmp_path, 'agy', '''
        time.sleep(0.4)
        out({'type': 'result', 'status': 'SUCCESS', 'response': 'ok'})
    ''')
    e = AgyEngine(path, concurrency=1)
    t = asyncio.get_running_loop().time()
    await asyncio.gather(*(e.stream(system='s', prompt=str(i)) for i in range(3)))
    assert asyncio.get_running_loop().time() - t >= 1.2  # three calls queued one after another


async def test_missing_binary(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda name: None)
    monkeypatch.delenv('TG_CODEX_BIN', raising=False)
    e = CodexEngine()
    assert e.available() == (False, '`codex` is not installed or not on PATH') and not e.info()['available']
    with pytest.raises(EngineError, match='not installed'):
        await e.stream(system='s', prompt='p')


def test_choose_prefers_subscriptions(monkeypatch, tmp_path):
    installed = {'claude': None, 'codex': '/x/codex', 'agy': '/x/agy'}
    monkeypatch.setattr('shutil.which', lambda name: installed.get(name))
    for var in ('TG_CLAUDE_BIN', 'TG_CODEX_BIN', 'TG_AGY_BIN'):
        monkeypatch.delenv(var, raising=False)
    engines = catalog()
    monkeypatch.setenv('TG_ENGINE', 'auto')
    assert choose(engines).name == 'codex'  # claude missing, so the next subscription CLI
    assert choose(engines, 'agy').name == 'agy'
    assert choose(engines, 'claude-code').name == 'codex'  # unavailable explicit choice falls back to auto
    assert choose(engines, 'anthropic').name == 'anthropic'  # ANTHROPIC_API_KEY is set by the fixture
    assert choose(engines, 'none') is None
    installed.clear()
    monkeypatch.delenv('ANTHROPIC_API_KEY')
    assert choose(catalog()) is None  # nothing installed, no key: keyless mode


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {'a': 1}
    assert parse_json('Here you go: {"subtasks": ["x"]} done') == {'subtasks': ['x']}
