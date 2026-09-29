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
    e.warm = 0  # the cold path: flags and prompt exactly as a one-shot `claude -p` gets them
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
    e = AgyEngine(path)
    e.warm = 0
    r = await e.stream(system='Be brief.', prompt='capital?', effort='medium', emit_delta=chunks.append)
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
    auto = choose(engines)
    assert auto.name == 'auto' and auto.lead().name == 'codex'  # claude missing, so the next subscription CLI
    assert [e.name for e in auto.chain()] == ['codex', 'agy', 'anthropic']  # the API key is the last resort
    assert choose(engines, 'agy').name == 'agy'
    assert choose(engines, 'claude-code').name == 'auto'  # unavailable explicit choice falls back to auto
    assert choose(engines, 'anthropic').name == 'anthropic'  # ANTHROPIC_API_KEY is set by the fixture
    assert choose(engines, 'none') is None
    installed.clear()
    monkeypatch.delenv('ANTHROPIC_API_KEY')
    assert choose(catalog()) is None  # nothing installed, no key: keyless mode


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {'a': 1}
    assert parse_json('Here you go: {"subtasks": ["x"]} done') == {'subtasks': ['x']}


async def test_agy_real_nested_event_shape(tmp_path):
    # Captured from agy 1.2.11: events are {"event": name, name: {...}}, not the flat shape in the docs.
    path = fake(tmp_path, 'agy', '''
        out({'event': 'init', 'conversation_id': 'c', 'init': {'cwd': '.', 'tools': []}})
        out({'event': 'step_update', 'step_update': {'step_index': 0, 'state': 'DONE', 'step_type': 'user_input'}})
        out({'event': 'step_update', 'step_update': {'step_index': 1, 'state': 'ACTIVE', 'step_type': 'agent_response', 'text_delta': 'pong'}})
        out({'event': 'step_update', 'step_update': {'step_index': 1, 'state': 'DONE', 'step_type': 'agent_response', 'text_delta': '\\n'}})
        out({'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'pong\\n', 'num_turns': 1,
             'usage': {'input_tokens': 12241, 'output_tokens': 30}}})
    ''')
    chunks = []
    r = await AgyEngine(path).stream(system='s', prompt='p', emit_delta=chunks.append)
    assert r.text == 'pong' and chunks == ['pong', '\n'] and (r.input_tokens, r.output_tokens) == (12241, 30)


# ---------- warm starts ----------

WARM_AGY = '''
    msg = json.loads(stdin)
    assert msg['event'] == 'user'
    out({'event': 'result', 'result': {'status': 'SUCCESS', 'response': f"{os.getpid()}:{msg['message']['content']}"}})
'''


async def test_warm_process_is_started_before_the_next_call(tmp_path):
    path = fake(tmp_path, 'agy', WARM_AGY)
    e = AgyEngine(path)
    first = await e.stream(system='s', prompt='one', effort='low')
    await asyncio.gather(*e.refills)
    assert len(e.idle) == 1  # a replacement is waiting for the next call with the same flags
    waiting = e.idle[0][2].pid
    second = await e.stream(system='s', prompt='two', effort='low')
    assert second.text == f'{waiting}:s\n\n---\n\ntwo' and first.text.split(':')[0] != str(waiting)
    a = call(path)['argv']
    assert '--input-format' in a and a[-1] == '-p=' and '--print-timeout' not in a
    await asyncio.gather(*e.refills)
    await e.aclose()
    assert e.idle == []


async def test_warm_pool_is_keyed_by_flags_and_capped(tmp_path):
    path = fake(tmp_path, 'agy', WARM_AGY)
    e = AgyEngine(path)
    e.warm = 2
    for effort in ('low', 'medium', 'high'):
        await e.prewarm(effort=effort)
    assert len(e.idle) == 2 and [k[k.index('--effort') + 1] for k, _, _ in e.idle] == ['medium', 'high']
    await e.prewarm(effort='high')  # already warm: no second process
    assert len(e.idle) == 2
    waiting = next(p.pid for k, _, p in e.idle if 'high' in k)
    r = await e.stream(system='s', prompt='p', effort='high')
    assert r.text.startswith(f'{waiting}:')  # the idle process answered
    await asyncio.gather(*e.refills)
    await e.aclose()


async def test_claude_code_warm_call_sends_the_prompt_as_a_stream_json_line(tmp_path):
    path = fake(tmp_path, 'claude', '''
        msg = json.loads(stdin)
        out({'type': 'result', 'is_error': False, 'result': msg['message']['content'], 'usage': {}})
    ''')
    e = ClaudeCodeEngine(path)
    r = await e.stream(system='Be brief.', prompt='capital?')
    a = call(path)['argv']
    assert r.text == 'capital?' and a[a.index('--input-format') + 1] == 'stream-json' and a[a.index('--system-prompt') + 1] == 'Be brief.'
    await e.aclose()


# ---------- auto fallback ----------

class Stub:
    def __init__(self, name, fail=None, web=False, exec=False):
        self.name, self.label, self.billing, self.fail = name, name.title(), 'subscription', fail
        self.supports_web, self.supports_exec, self.calls = web, exec, 0

    def available(self):
        return True, ''

    async def stream(self, **kw):
        self.calls += 1
        if self.fail:
            if kw.get('emit_delta'):
                kw['emit_delta']('half')
            raise EngineError(self.fail, 'half')
        from jevrouter.engines import Reply
        return Reply(f'{self.name} ok')

    async def prewarm(self, **kw):
        pass


async def test_auto_falls_through_and_cools_down_a_used_up_plan():
    from jevrouter.engines import AutoEngine
    claude, codex, api = Stub('claude-code'), Stub('codex', fail="You've hit your usage limit"), Stub('anthropic')
    auto = AutoEngine({'codex': codex, 'claude-code': claude, 'anthropic': api}, order=['codex', 'claude-code', 'anthropic'])
    chunks = []
    r = await auto.stream(system='s', prompt='p', emit_delta=chunks.append)
    assert r.text == 'claude-code ok' and r.engine == 'claude-code' and 'switching to Claude-Code' in ''.join(chunks)
    await auto.stream(system='s', prompt='p')
    assert codex.calls == 1  # skipped while cooling down, not retried on every call
    assert 'usage limit' in auto.info()['cooling']['codex'] and auto.lead().name == 'claude-code'
    auto.set_order(['anthropic'])
    assert auto.order == ['anthropic', 'codex', 'claude-code'] and auto.info()['cooling'] == {}


async def test_auto_picks_an_engine_that_can_do_the_call():
    from jevrouter.engines import AutoEngine
    agy, codex = Stub('agy'), Stub('codex', web=True, exec=True)
    auto = AutoEngine({'agy': agy, 'codex': codex})
    assert (await auto.stream(system='s', prompt='p')).engine == 'agy'
    assert (await auto.stream(system='s', prompt='p', web=True)).engine == 'codex'
    assert auto.supports_web and auto.supports_exec


async def test_auto_reports_every_failure():
    from jevrouter.engines import AutoEngine
    auto = AutoEngine({'a': Stub('a', fail='timed out after 180s'), 'b': Stub('b', fail='not logged in')})
    with pytest.raises(EngineError, match='A: timed out after 180s; B: not logged in'):
        await auto.stream(system='s', prompt='p')


async def test_auto_warms_the_engine_that_takes_over():
    from jevrouter.engines import AutoEngine
    warmed = []
    codex, agy = Stub('codex', fail='usage limit'), Stub('agy')
    agy.prewarm = lambda **kw: asyncio.sleep(0, warmed.append(kw))
    auto = AutoEngine({'codex': codex, 'agy': agy})
    await auto.prewarm(system='plan', effort='low')  # codex leads and has nothing to warm
    assert warmed == []
    await auto.stream(system='s', prompt='p')
    await asyncio.gather(*auto.tasks)
    assert warmed == [{'system': 'plan', 'effort': 'low'}]


class SeeingStub(Stub):
    supports_vision = True

    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        self.images = []

    async def stream(self, **kw):
        self.images.append(kw.get('images'))
        return await super().stream(**kw)


async def test_auto_vision_follows_a_healthy_engine_that_can_see_and_routes_images_to_it():
    """Studio's critic and Polish through Auto: supports_vision is true only while the chain has a healthy engine that
    reads images, and a call with images goes to it even when a blind engine leads (or is preferred first)."""
    import time
    from jevrouter.engines import AutoEngine
    from jevrouter.engines.auto import Steered
    from jevrouter.studio import critic
    agy, claude = Stub('agy'), SeeingStub('claude-code')
    auto = AutoEngine({'agy': agy, 'claude-code': claude}, order=['agy', 'claude-code'])
    assert auto.lead() is agy and auto.supports_vision and critic.can_see(auto) and auto.seer() is claude
    r = await auto.stream(system='s', prompt='p', images=[b'png'])
    assert r.engine == 'claude-code' and claude.images == [[b'png']] and agy.calls == 0
    r = await Steered(auto, 'agy').stream(system='s', prompt='p', images=[b'png'])
    assert r.engine == 'claude-code' and agy.calls == 0
    assert (await auto.stream(system='s', prompt='p')).engine == 'agy' and claude.images[-1] == [b'png']  # text: lead
    # the only engine that can see is cooling down after a failure: Auto can't see now
    auto.cooling['claude-code'] = (time.monotonic() + 60, 'usage limit')
    assert not auto.supports_vision and not critic.can_see(auto)
    auto.cooling.clear()
    assert auto.supports_vision
    # no engine that can see at all: no vision, and an image call fails instead of reaching a blind engine
    blind = AutoEngine({'agy': Stub('agy'), 'codex': Stub('codex')})
    assert not blind.supports_vision and blind.seer() is None
    with pytest.raises(EngineError, match='read images'):
        await blind.stream(system='s', prompt='p', images=[b'png'])
    assert blind.engines['agy'].calls == blind.engines['codex'].calls == 0


async def test_the_critic_through_auto_reaches_the_seeing_engine():
    from jevrouter.engines import AutoEngine, Reply
    from jevrouter.studio import critic

    class Critic(SeeingStub):
        async def stream(self, **kw):
            self.images.append(kw.get('images'))
            return Reply('{"edits": []}', 900, 20)
    agy, claude = Stub('agy'), Critic('claude-code')
    auto = AutoEngine({'agy': agy, 'claude-code': claude}, order=['agy', 'claude-code'])
    from jevrouter.studio.plan import DesignPlan
    plan = DesignPlan(file_id='f', format='pptx', preset='minimal', system={}, direction={}, pages=[])
    got = await critic.critique(plan, {'checks': []}, [b'sheet'], auto)
    assert claude.images == [[b'sheet']] and agy.calls == 0 and got.llm_in == 900
