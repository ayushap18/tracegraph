import pytest

from jevrouter import agents
from jevrouter.engines.api import cited
from jevrouter.merger import merge
from tests.fakes import FakeLLM, api_errors, eng


def collector():
    chunks = []
    return chunks, chunks.append


async def test_streaming_code_agent():
    claude = FakeLLM(['Use ', '`reversed()`', ' or slicing.'])
    chunks, emit = collector()
    r = await agents.build(None, eng(claude))['code']('reverse a list in python', emit)
    assert chunks == ['Use ', '`reversed()`', ' or slicing.']
    assert r.ok and r.engine == 'api' and r.answer == 'Use `reversed()` or slicing.' and (r.claude_in, r.claude_out) == (10, 3)
    kw = claude.calls[0]
    assert kw['_url'] == 'https://llm.test/v1/chat/completions' and kw['model'] == 'test-model' and kw['stream'] is True
    assert kw['reasoning_effort'] == 'medium' and 'plugins' not in kw and 'response_format' not in kw
    assert kw['messages'][0]['role'] == 'system' and kw['messages'][1] == {'role': 'user', 'content': 'reverse a list in python'}


async def test_chat_uses_low_effort_and_knowledge_survives_missing_grounding():
    claude = FakeLLM(['Hi!'], ['Ada Lovelace was a mathematician.'])
    reg = agents.build(None, eng(claude))
    assert (await reg['chat']('hey', lambda t: None)).answer == 'Hi!'
    assert claude.calls[0]['reasoning_effort'] == 'low'
    r = await reg['knowledge']('Who was Ada Lovelace?', lambda t: None)  # http=None: DDG lookup fails, Claude still answers
    assert r.ok and r.engine == 'api' and claude.calls[1]['messages'][1]['content'] == 'Who was Ada Lovelace?'


async def test_refusal_is_a_failed_result():
    chunks, emit = collector()
    r = await agents.build(None, eng(FakeLLM(('refusal', ['I']))))['code']('something', emit)
    assert not r.ok and r.engine == 'api' and 'declined' in r.answer and r.claude_in == 10


@pytest.mark.parametrize('kind', ['rate', 'status', 'conn'])
async def test_api_error_falls_back_to_keyless(kind):
    chunks, emit = collector()
    r = await agents.build(None, eng(FakeLLM(api_errors()[kind])))['chat']('thanks!', emit)
    assert r.ok and r.engine == 'keyless' and r.answer == "You're welcome!"
    assert chunks[-1] == "You're welcome!" and 'keyless fallback' in chunks[0]


async def test_research_without_fallback_fails_cleanly():
    r = await agents.build(None, eng(FakeLLM(api_errors()['rate'])))['research']('news today', lambda t: None)
    assert not r.ok and 'rate limited' in r.answer


def test_keyless_registry_has_no_research():
    assert 'research' not in agents.build(None) and 'research' in agents.build(None, eng(FakeLLM()))


def test_cited():
    delta = {'content': 'x', 'annotations': [{'type': 'file'}, {'type': 'url_citation', 'url_citation': {'url': 'https://a.example'}}]}
    assert cited(delta) == 'https://a.example' and cited({'content': 'x'}) is None and cited({'annotations': ['junk']}) is None


RESULTS = [('weather', 'Paris 18°C'), ('currency', '100 EUR = 9000 INR')]


async def test_merger_modes():
    chunks, emit = collector()
    m = await merge('q', RESULTS[:1], emit)
    assert m['engine'] == 'single' and m['answer'] == 'Paris 18°C' and not chunks

    m = await merge('q', RESULTS, emit)
    assert m['engine'] == 'concat' and m['answer'] == '**weather**: Paris 18°C\n\n**currency**: 100 EUR = 9000 INR'
    assert chunks == [m['answer']]

    claude = FakeLLM(['Paris is 18°C; ', '100 EUR is 9000 INR.'])
    chunks, emit = collector()
    m = await merge('q', RESULTS, emit, eng(claude))
    assert m['engine'] == 'api' and m['answer'] == 'Paris is 18°C; 100 EUR is 9000 INR.' and len(chunks) == 2
    assert claude.calls[0]['reasoning_effort'] == 'low' and '[currency agent]' in claude.calls[0]['messages'][1]['content']


@pytest.mark.parametrize('item', [api_errors()['status'], ('refusal', []), []])
async def test_merger_falls_back_to_concat(item):
    m = await merge('q', RESULTS, lambda t: None, eng(FakeLLM(item)))
    assert m['engine'] == 'concat'


async def test_merger_marks_partial_claude_text_before_concat():
    claude = FakeLLM(['Paris is', api_errors()['conn']])
    chunks, emit = collector()
    m = await merge('q', RESULTS, emit, eng(claude))
    assert m['engine'] == 'concat' and chunks[0] == 'Paris is' and 'concatenated' in chunks[1] and chunks[2] == m['answer']

    chunks, emit = collector()  # nothing streamed: no marker
    await merge('q', RESULTS, emit, eng(FakeLLM(api_errors()['status'])))
    assert len(chunks) == 1 and chunks[0].startswith('**weather**')


# ---------- research that feeds a file (docs/PLAN-accuracy-v2.md B5) ----------

class Recorder:
    name, label, supports_web = 'claude-code', 'Claude Code', True

    def __init__(self, text='notes'):
        self.text, self.calls = text, []

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, **kw):
        from jevrouter.engines import Reply
        self.calls.append({'system': system, 'prompt': prompt, 'max_tokens': max_tokens, 'web': web})
        return Reply(self.text, 5, 3)


@pytest.mark.parametrize('agent,web', [('research', True), ('knowledge', False), ('report', False)])
async def test_notes_prompts_when_a_step_feeds_a_file(agent, web):
    from jevrouter.agents import llm
    engine = Recorder()
    run = agents.build(None, engine)[agent]
    token = agents.FEEDS_FILE.set(True)
    try:
        r = await run('Research the history of AI', lambda t: None)
    finally:
        agents.FEEDS_FILE.reset(token)
    call = engine.calls[0]
    assert r.ok and call['max_tokens'] == 4096 and call['web'] is web
    assert call['system'] == (llm.NOTES_RESEARCH if agent == 'research' else llm.NOTES_KNOWLEDGE)
    assert '600-1,200 words' in call['system'] and 'IMAGE: <search query> | <caption>' in call['system']
    if not web:
        assert 'from your own knowledge' in call['system'] and 'Do not use tools.' in call['system']
    # without the flag the agent writes its usual short answer
    engine.calls.clear()
    await run('Research the history of AI', lambda t: None)
    assert '600-1,200 words' not in engine.calls[0]['system']


def test_image_ideas_are_read_from_notes():
    from jevrouter.agents.llm import image_ideas
    notes = ('## Origins\n- 1956: Dartmouth workshop\nIMAGE: Alan Turing portrait | Alan Turing in 1951\n'
             '- `IMAGE: ENIAC computer | ENIAC in 1946`\nIMAGE: no caption here\n')
    assert image_ideas(notes) == [('Alan Turing portrait', 'Alan Turing in 1951'), ('ENIAC computer', 'ENIAC in 1946')]
    assert len(image_ideas('\n'.join(f'IMAGE: q{i} | c{i}' for i in range(12)))) == 8
