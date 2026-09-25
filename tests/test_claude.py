from types import SimpleNamespace as NS

import pytest

from jevrouter import agents
from jevrouter.agents.claude import first_url
from jevrouter.merger import merge
from tests.fakes import FakeAnthropic, api_errors


def collector():
    chunks = []
    return chunks, chunks.append


async def test_streaming_code_agent():
    claude = FakeAnthropic(['Use ', '`reversed()`', ' or slicing.'])
    chunks, emit = collector()
    r = await agents.build(None, claude)['code']('reverse a list in python', emit)
    assert chunks == ['Use ', '`reversed()`', ' or slicing.']
    assert r.ok and r.engine == 'claude' and r.answer == 'Use `reversed()` or slicing.' and (r.claude_in, r.claude_out) == (10, 3)
    kw = claude.calls[0]
    assert kw['model'] == 'claude-opus-5' and kw['output_config'] == {'effort': 'medium'} and kw['thinking'] == {'type': 'adaptive'}
    assert kw['betas'] == ['server-side-fallback-2026-07-01'] and kw['fallbacks'] == 'default' and 'tools' not in kw
    assert kw['messages'] == [{'role': 'user', 'content': 'reverse a list in python'}]


async def test_chat_uses_low_effort_and_knowledge_survives_missing_grounding():
    claude = FakeAnthropic(['Hi!'], ['Ada Lovelace was a mathematician.'])
    reg = agents.build(None, claude)
    assert (await reg['chat']('hey', lambda t: None)).answer == 'Hi!'
    assert claude.calls[0]['output_config']['effort'] == 'low'
    r = await reg['knowledge']('Who was Ada Lovelace?', lambda t: None)  # http=None: DDG lookup fails, Claude still answers
    assert r.ok and r.engine == 'claude' and claude.calls[1]['messages'][0]['content'] == 'Who was Ada Lovelace?'


async def test_refusal_is_a_failed_result():
    chunks, emit = collector()
    r = await agents.build(None, FakeAnthropic(('refusal', ['I'])))['code']('something', emit)
    assert not r.ok and r.engine == 'claude' and 'declined' in r.answer and r.claude_in == 10


@pytest.mark.parametrize('kind', ['rate', 'status', 'conn'])
async def test_api_error_falls_back_to_keyless(kind):
    chunks, emit = collector()
    r = await agents.build(None, FakeAnthropic(api_errors()[kind]))['chat']('thanks!', emit)
    assert r.ok and r.engine == 'keyless' and r.answer == "You're welcome!"
    assert chunks[-1] == "You're welcome!" and 'keyless fallback' in chunks[0]


async def test_research_without_fallback_fails_cleanly():
    r = await agents.build(None, FakeAnthropic(api_errors()['rate']))['research']('news today', lambda t: None)
    assert not r.ok and 'rate limited' in r.answer


def test_keyless_registry_has_no_research():
    assert 'research' not in agents.build(None) and 'research' in agents.build(None, FakeAnthropic())


def test_first_url():
    content = [NS(type='server_tool_use'), NS(type='web_search_tool_result', content=[NS(url='https://a.example')]),
               NS(type='text', text='x', citations=[NS(url='https://b.example')])]
    assert first_url(content) == 'https://a.example'
    assert first_url([NS(type='text', text='x', citations=[NS(url='https://b.example')])]) == 'https://b.example'
    assert first_url([NS(type='web_search_tool_result', content=NS(error_code='too_many_requests'))]) is None


RESULTS = [('weather', 'Paris 18°C'), ('currency', '100 EUR = 9000 INR')]


async def test_merger_modes():
    chunks, emit = collector()
    m = await merge('q', RESULTS[:1], emit)
    assert m['engine'] == 'single' and m['answer'] == 'Paris 18°C' and not chunks

    m = await merge('q', RESULTS, emit)
    assert m['engine'] == 'concat' and m['answer'] == '**weather**: Paris 18°C\n\n**currency**: 100 EUR = 9000 INR'
    assert chunks == [m['answer']]

    claude = FakeAnthropic(['Paris is 18°C; ', '100 EUR is 9000 INR.'])
    chunks, emit = collector()
    m = await merge('q', RESULTS, emit, claude)
    assert m['engine'] == 'claude' and m['answer'] == 'Paris is 18°C; 100 EUR is 9000 INR.' and len(chunks) == 2
    assert claude.calls[0]['output_config']['effort'] == 'low' and '[currency agent]' in claude.calls[0]['messages'][0]['content']


@pytest.mark.parametrize('item', [api_errors()['status'], ('refusal', []), []])
async def test_merger_falls_back_to_concat(item):
    m = await merge('q', RESULTS, lambda t: None, FakeAnthropic(item))
    assert m['engine'] == 'concat'


async def test_merger_marks_partial_claude_text_before_concat():
    from tests.fakes import FakeStream

    class MidStreamFail(FakeStream):
        @property
        def text_stream(self):
            async def gen():
                yield 'Paris is'
                raise api_errors()['conn']
            return gen()

    claude = FakeAnthropic()
    claude.beta.messages.stream = lambda **kw: MidStreamFail([], 'end_turn', None)
    chunks, emit = collector()
    m = await merge('q', RESULTS, emit, claude)
    assert m['engine'] == 'concat' and chunks[0] == 'Paris is' and 'concatenated' in chunks[1] and chunks[2] == m['answer']

    chunks, emit = collector()  # nothing streamed: no marker
    await merge('q', RESULTS, emit, FakeAnthropic(api_errors()['status']))
    assert len(chunks) == 1 and chunks[0].startswith('**weather**')
