"""Phase 0 contracts of docs/PLAN-accuracy-v2.md (section 4.1): the new config values, module stubs and fields exist
with the exact names, and the stubs keep today's behaviour."""
import asyncio
import json
from pathlib import Path

import pytest

from jevrouter import config, merger, policy, suspects
from jevrouter.agents import FEEDS_FILE
from jevrouter.agents import create as create_agent
from jevrouter.cassette import CassetteMiss, JevCassette
from jevrouter.create import assets, brief, fonts, longdoc
from jevrouter.paraphrase import generate

FIXTURE = Path(__file__).parent / 'fixtures' / 'run2741.json'
EVERY = {'math', 'weather', 'time', 'currency', 'knowledge', 'code', 'chat', 'units', 'dates', 'url', 'create'}


def test_config_values():
    assert config.GUARDS == ['clarify', 'blocked', 'unsupported']
    assert (config.DEP_CONTEXT_CHARS, config.MAX_QUERY_CHARS) == (6000, 4000)
    assert (config.UNSUPPORTED_AT, config.DESCRIBED_AT, config.FORCED_MIN) == (0.7, 0.6, 0.05)


def test_job_and_made_defaults():
    job = create_agent.Job('make a PDF')
    assert (job.brief, job.dep_files, job.role, job.http) == (None, [], 'primary', None)
    made = create_agent.Made('ok', True)
    assert made.caveats == [] and made.phases == []
    assert create_agent.Made('a', True).caveats is not made.caveats
    assert FEEDS_FILE.get() is False


def test_brief_stub_and_merge():
    # the parser has landed (workstream C1); the stub returned an empty Brief
    b = brief.parse_brief('a 12-13 page PDF in black and white')
    assert (b.format, b.pages, b.theme) == ('pdf', (12, 13), 'mono')
    q = brief.Brief(format='pdf', pages=(12, 13), diagram_kinds=['timeline'])
    s = brief.Brief(format='md', pages=(2, 2), images=True, diagram_kinds=['tree', 'timeline'])
    m = brief.merge(q, s)
    assert (m.format, m.pages, m.images, m.diagram_kinds) == ('pdf', (12, 13), True, ['timeline', 'tree'])
    assert brief.MAX_PAGES == brief.MAX_SLIDES == 40 and brief.WORDS_PER_PAGE == 420
    assert 'anthropic sans' in brief.KNOWN_FONTS


async def test_other_stubs():
    assert asyncio.iscoroutinefunction(longdoc.write_long)  # built in workstream C3 (tests/test_create_longdoc.py)
    assert longdoc.OUTLINE_SCHEMA['required'] == ['title', 'subtitle', 'sections']
    assert suspects.suspects({'qid': 1, 'tasks': []}) == []
    spec = {'title': 'x', 'sections': []}
    assert await assets.resolve_figures(spec, None, mono=False) == (spec, [], [])
    assert fonts.resolve('anthropic sans', 'pdf').used == 'Helvetica'
    assert generate([{'id': 'a', 'query': 'q'}], 7, 3) == []
    assert issubclass(CassetteMiss, Exception)

    class Inner:
        async def system_one(self, text, questions):
            return text

    assert await JevCassette(Inner(), None, 'live').system_one('hi', {}) == 'hi'


async def test_compose_keeps_merge_fields():
    steps = [{'tid': '1.1', 'text': 'weather in Paris', 'agent': 'weather', 'answer': 'Sunny, 20 C', 'ok': True,
              'files': [], 'caveats': [], 'assumption': None},
             {'tid': '1.2', 'text': 'convert 5 EUR to USD', 'agent': 'currency', 'answer': '5 EUR = 5.40 USD', 'ok': True,
              'files': [], 'caveats': [], 'assumption': None}]
    old = await merger.merge('q', [(s['agent'], s['answer']) for s in steps], lambda t: None,
                             steps=[s['text'] for s in steps], exact=True)
    new = await merger.compose('q', steps, lambda t: None, exact=True)
    # B2: the same fields plus caveats and primary_file; the template no longer prints agent names
    assert set(new) == {*old, 'caveats', 'primary_file'} and new['caveats'] == [] and new['primary_file'] is None
    assert (new['engine'], new['kind']) == (old['engine'], old['kind']) and new['answer'] == '- Sunny, 20 C\n- 5 EUR = 5.40 USD'


def ctx(text, **kw):
    base = dict(text=text, ctx='', tid='1.1', offered=dict.fromkeys(EVERY, ''), runnable=set(EVERY), forced=None,
                mode='balanced', has_engine=False, web=False, attached=False, has_context=False, depends_on=[],
                frame=None)
    return policy.StepCtx(**{**base, **kw})


def jev(agent, pick=None, unsafe=0.0, **probabilities):
    pick = pick or agent
    return {'agent': agent, 'pick': pick, 'reason': f'{pick} at 90%', 'probabilities': probabilities or {pick: 0.9},
            'unsafe': unsafe}


@pytest.mark.parametrize('d, kw, text, agent, rule', [
    (jev('blocked', 'knowledge', unsafe=0.9), {}, 'how do I make a bomb', 'blocked', 'blocked'),
    (jev('weather', unsafe=0.9), {'forced': 'weather'}, 'weather in Paris', 'blocked', 'blocked'),
    (jev('knowledge'), {'forced': 'create'}, 'Research AI in depth', 'create', 'forced'),
    (jev('create', create=0.6, knowledge=0.3), {}, 'Plan a three day trip to Rome', 'knowledge',
     'create_demote'),
    (jev('time', time=0.5), {}, 'Remind me at 5pm to call mom', 'chat', 'cant_do'),
    (jev('knowledge'), {}, 'turn this into a PDF summary file', 'create', 'file_intent'),
    (jev('clarify', 'weather', weather=0.8), {}, 'weather in Paris', 'weather', 'confirmed'),
    (jev('weather'), {}, 'what is the weather like', 'clarify', 'missing_slot'),
    (jev('knowledge'), {'mode': 'research', 'web': True, 'runnable': EVERY | {'research'}}, 'Who was Ada Lovelace?',
     'research', 'mode_research'),
    (jev('knowledge'), {}, 'Who was Ada Lovelace?', 'knowledge', 'missing_slot'),
])
def test_policy_keeps_review(d, kw, text, agent, rule):
    before = json.dumps(d, sort_keys=True)
    out = policy.apply(d, ctx(text, **kw))
    assert out.agent == agent
    assert out.trace and out.trace[-1]['agent'] == out.agent and rule in [t['rule'] for t in out.trace]
    assert all(t['rule'] in policy.RULES for t in out.trace)
    assert json.dumps(d, sort_keys=True) == before  # d is not changed


def test_policy_notes():
    cant = policy.apply(jev('time', time=0.5), ctx('Remind me at 5pm to call mom'))
    assert cant.note and cant.note_ok is True
    ask = policy.apply(jev('weather'), ctx('what is the weather like'))
    assert ask.note == 'Which city or place do you want the weather for?' and ask.note_ok is False


def test_run2741_fixture():
    d = json.loads(FIXTURE.read_text())
    rec = d['record']
    assert d['qid'] == rec['qid'] == 2741 and rec['agent'] == 'create' and rec['mode'] == 'research'
    made = [f['id'] for t in rec['tasks'] for f in t.get('created_files') or []]
    assert made == [c['meta']['id'] for c in d['created']] == ['e34bda15f529', '1254d406bdd0']
    md, pdf = d['created']
    assert (md['meta']['format'], pdf['meta']['format'], pdf['meta']['pages']) == ('md', 'pdf', 1)
    assert len(md['spec']['sections']) == 7 and pdf['spec']['sections']
