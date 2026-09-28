"""Routing correctness (docs/PLAN-accuracy-v2.md A1-A7) and the pipeline wiring for B, C and D: the decision policy and
its trace, the @agent binding, the "unsupported" outcome, keyless follow-ups and parser contracts, the clarify policy,
route-only runs, dependency context, and what the create step and the merger are handed."""
import json
import uuid
from collections import OrderedDict
from types import SimpleNamespace as NS

import pytest

from jevrouter import policy
from jevrouter.agents import FEEDS_FILE, AgentResult
from jevrouter.agents import create as create_agent
from jevrouter.config import MAX_QUERY_CHARS
from jevrouter.pipeline import ROUTE_CHARS, Router, allot, cut, pinned_plan
from tests.fakes import FakeJev, ScriptEngine
from tests.test_pipeline import check_fields

QUERY_2741 = ('pdf on the ai what is ai how ai begins using the multiple diagrams also add the images from the web sources '
              'build 12-13 page of pdf properly using the black white text also anthropic sans font also keep the sizing '
              'properly and all images are to be sourced from the best sources')
EVERY = {'math', 'weather', 'time', 'currency', 'knowledge', 'code', 'chat', 'units', 'dates', 'url', 'create'}
LLM = EVERY | {'research', 'report'}


class SignalJev(FakeJev):
    """FakeJev whose route call also answers the A3 signals: signals_for(text) -> {live, action, personal, described}."""

    def __init__(self, signals_for=None, **kw):
        super().__init__(**kw)
        self.signals_for = signals_for or (lambda text: {})

    async def system_one(self, state, qs):
        r = await super().system_one(state, qs)
        if 'route' in qs:
            assert {'live', 'action', 'personal', 'described'} <= set(qs)  # one call: the signals ride on the route
            sig = self.signals_for(state)
            for name in ('live', 'action', 'personal', 'described'):
                r.answers[name] = NS(noul=sig.get(name, 0.0))
        return r


def ctx(text, **kw):
    base = dict(text=text, ctx='', tid='1.1', offered=dict.fromkeys(EVERY, ''), runnable=set(EVERY), forced=None,
                mode='balanced', has_engine=False, web=False, attached=False, has_context=False, depends_on=[],
                frame=None)
    return policy.StepCtx(**{**base, **kw})


def engine_ctx(text, web=False, **kw):
    runnable = LLM if web else LLM - {'research'}
    return ctx(text, has_engine=True, web=web, runnable=runnable, offered=dict.fromkeys(runnable, ''), **kw)


def jev(agent, pick=None, unsafe=0.0, signals=None, **probabilities):
    pick = pick or agent
    d = {'agent': agent, 'pick': pick, 'reason': f'{pick} at 90%', 'probabilities': probabilities or {pick: 0.9},
         'unsafe': unsafe}
    if signals:
        d['signals'] = signals
    return d


def rules(decision):
    return [t['rule'] for t in decision.trace]


# ---------- A2: the policy table ----------

@pytest.mark.parametrize('d, s, agent, rule', [
    (jev('weather', unsafe=0.95), ctx('weather in Paris', forced='weather'), 'blocked', 'blocked'),
    (jev('clarify', 'weather', weather=0.8), ctx('Weather in Madrid next year'), 'unsupported', 'unsupported'),
    (jev('weather'), ctx('What will the weather be in Paris on 1 January 2090?'), 'unsupported', 'unsupported'),
    (jev('create', create=0.9), ctx('Make me a presentation'), 'clarify', 'missing_slot'),
    (jev('knowledge', signals={'action': 0.9}), ctx('Get my car washed at noon'), 'unsupported', 'unsupported'),
    (jev('knowledge', signals={'personal': 0.9}), ctx('What did I have for breakfast today?'), 'unsupported',
     'unsupported'),
    (jev('knowledge', signals={'live': 0.9}), ctx("What's the stock price of Apple?"), 'unsupported', 'unsupported'),
    (jev('knowledge', signals={'live': 0.9}), engine_ctx("What's the stock price of Apple?", web=True), 'research',
     'mode_research'),
    (jev('knowledge', signals={'live': 0.9}), ctx("What was Apple's stock price in 2010?"), 'knowledge', 'missing_slot'),
    (jev('knowledge', signals={'action': 0.9}), ctx('How do I book a flight cheaply?'), 'knowledge', 'missing_slot'),
    (jev('weather', signals={'live': 0.9}), ctx('weather in Paris'), 'weather', 'missing_slot'),
    (jev('knowledge', signals={'live': 0.9}), ctx('Who will win the 2060 US presidential election?'), 'unsupported',
     'unsupported'),
    (jev('code', code=0.65, chat=0.2), engine_ctx('Should I learn Python or JavaScript first?'), 'chat', 'advice'),
    (jev('code', code=0.65, knowledge=0.2), ctx('Should I learn Python or JavaScript first?'), 'knowledge', 'advice'),
    (jev('knowledge'), engine_ctx('What is the IIT Bombay cutoff rank this year?', web=True), 'research',
     'time_sensitive'),
    (jev('clarify', 'knowledge', knowledge=0.95), engine_ctx('the thing about black holes'), 'knowledge',
     'clarify_policy'),
    (jev('clarify', 'knowledge', knowledge=0.95), ctx('the thing about black holes'), 'clarify', 'missing_slot'),
    (jev('currency'), ctx('Convert 100 USD to EUR and GBP'), 'clarify', 'keyless_contract'),
    (jev('weather'), ctx("Weather in William Shakespeare's birthplace"), 'unsupported', 'keyless_contract'),
    (jev('weather'), engine_ctx("Weather in William Shakespeare's birthplace"), 'knowledge', 'keyless_contract'),
    (jev('time'), engine_ctx('When it is 3pm in London what time is it in Tokyo'), 'knowledge', 'keyless_contract'),
    (jev('report'), engine_ctx('A report on bees', web=True, mode='research'), 'research', 'mode_research'),
    (jev('clarify', 'document', document=0.41, create=0.4), ctx('and as markdown please', has_context=True), 'create',
     'refers_back_file'),
    (jev('clarify', 'document', document=0.8, create=0.1), ctx('and as markdown please', has_context=True), 'clarify',
     'missing_slot'),
    (jev('currency'), ctx('Which country hosted the 2016 Summer Olympics? Convert 100 EUR into its currency.'),
     'unsupported', 'keyless_contract'),
])
def test_policy_rules(d, s, agent, rule):
    before = json.dumps(d, sort_keys=True)
    out = policy.apply(d, s)
    assert out.agent == agent and rule in rules(out), out
    assert out.trace[-1]['agent'] == out.agent
    assert json.dumps(d, sort_keys=True) == before


def test_policy_notes_are_plain_and_honest():
    live = policy.apply(jev('knowledge', signals={'live': 0.9}), ctx("What's the stock price of Apple?"))
    assert live.note_ok and "don't have live data" in live.note
    private = policy.apply(jev('weather', signals={'personal': 0.9}), ctx('Is it cold where I am right now?'))
    assert private.agent == 'unsupported' and "can't see your" in private.note and 'location' in private.note
    future = policy.apply(jev('clarify', 'weather', weather=0.8), ctx('Weather in Madrid next year'))
    assert 'forecasts only reach about 16 days' in future.note
    topic = policy.apply(jev('create', create=0.9), ctx('Make me a presentation'))
    assert topic.note == 'What should the presentation be about?' and not topic.note_ok
    hedge = policy.apply(jev('knowledge'), ctx('What is the IIT Bombay cutoff rank this year?'))
    assert hedge.agent == 'knowledge' and hedge.caveats == ['Figures like these change every year; check the official '
                                                            'source.']
    assume = policy.apply(jev('clarify', 'knowledge', knowledge=0.95), engine_ctx('the thing about black holes'))
    assert assume.assumption == 'Assuming you mean facts about something about "the thing about black holes".'
    for d in (live, private, future, topic):
        assert '—' not in d.note and '–' not in d.note and 'agent' not in d.note


def test_forced_steps_keep_the_at_agent_but_not_past_honesty():
    assert policy.apply(jev('knowledge'), ctx('Research AI in depth', forced='create')).agent == 'create'
    out = policy.apply(jev('weather'), ctx('Weather in Madrid next year', forced='weather'))
    assert out.agent == 'unsupported'  # an @agent never makes up a forecast
    out = policy.apply(jev('chat'), ctx('Remind me at 5pm to call mom', forced='chat'))
    assert out.agent == 'chat' and out.note_ok and "can't set reminders" in out.note


def test_follow_up_sticks_with_the_previous_agent():
    frame = {'agent': 'weather', 'slots': {'city': 'Paris'}}
    d = jev('chat', chat=0.45, weather=0.35)
    out = policy.apply(d, ctx('and in Rome?', frame=frame))
    assert out.agent == 'weather' and 'frame' in rules(out)
    far = policy.apply(jev('chat', chat=0.9, weather=0.05), ctx('and in Rome?', frame=frame))
    assert far.agent == 'chat'


# ---------- A3, A5: jev.py signals; the keyless parser contracts ----------

async def test_route_one_returns_the_signals_from_the_same_call():
    from jevrouter.jev import route_one
    j = SignalJev(signals_for=lambda t: {'live': 0.912, 'action': 0.1})
    d = await route_one(j, 'price of gold now', {'knowledge': 'facts', 'chat': 'talk'})
    assert d['signals'] == {'live': 0.91, 'action': 0.1, 'personal': 0.0, 'described': 0.0} and len(j.calls) == 1
    d = await route_one(FakeJev(), 'hi', {'chat': 'talk', 'math': 'sums'})  # a Jev that gives none: no signals
    assert d['signals'] == {}


@pytest.mark.parametrize('q, value', [('Divide 240 by 8', 30), ('12 percent of 850', 102), ('Multiply 13 by 11', 143),
                                      ('square root 2025', 45), ('sqrt 144', 12), ('add 120 and 380', 500),
                                      ('subtract 5 from 20', 15), ('3 to the power of 4', 81)])
def test_solve_math_word_operators(q, value):
    from jevrouter.agents.tools import solve_math
    assert solve_math(q)[1] == value


async def test_math_never_answers_with_a_number_it_left_out():
    from jevrouter.agents.tools import agent_math, solve_math
    assert solve_math('Divide 240 by 8 and add the tip of 5') is None and solve_math('hello') is None
    r = await agent_math('Divide 240 by 8 and add the tip of 5')
    assert not r.ok and '240' in r.answer and '5' in r.answer and r.answer.endswith('.')


def test_currency_direction_and_described_currencies():
    from jevrouter import gate
    from jevrouter.agents.tools import CAPITAL_OF, COUNTRY_ROWS, CURRENCY_OF, ECB_CODES, ISO_CODES, parse_currency
    assert parse_currency('how many yen is 200 British pounds', ECB_CODES) == (200.0, 'GBP', 'JPY')
    assert parse_currency('200 pounds in yen', ECB_CODES) == (200.0, 'GBP', 'JPY')
    assert len(COUNTRY_ROWS) >= 190 and all(c in ISO_CODES for _, _, c in COUNTRY_ROWS)
    assert (CAPITAL_OF['switzerland'], CURRENCY_OF['brazil'], CAPITAL_OF['uk']) == ('Bern', 'BRL', 'London')
    assert gate.resolve_described('Convert 100 USD to the currency of Brazil') == 'Convert 100 USD to BRL'
    assert gate.resolve_described('weather in the capital of Switzerland') == 'weather in Bern'
    assert gate.resolve_described("Weather in William Shakespeare's birthplace") is None
    assert gate.question('currency', 'Convert 100 USD to the currency of Wakanda') is None  # described, never asked


@pytest.mark.parametrize('q, resolved', [
    ('Convert 250 GBP into the currency used in the capital of Switzerland', 'Convert 250 GBP into CHF'),
    ('Convert 100 USD to the currency used in Tokyo', 'Convert 100 USD to JPY'),  # a capital names its country
    ('Convert 100 USD to the currency used in Springfield', None),  # no country to look up: an engine must
])
def test_a_currency_named_by_the_place_it_is_used_in(q, resolved):
    from jevrouter import gate
    assert gate.resolve_described(q) == resolved


def test_parse_for_reports_what_the_parser_cannot_use():
    from jevrouter import gate
    p = gate.parse_for('weather', 'Weather in Madrid next year')
    assert p.slots == {'city': 'Madrid'} and p.unused == ['future date'] and not p.full
    assert not gate.confirmed('weather', 'Weather in Madrid next year') and gate.confirmed('weather', 'weather in Madrid')
    assert gate.parse_for('weather', 'Weather in Vienna and Prague').unused == ['second place']
    assert gate.parse_for('currency', 'Convert 100 USD to EUR and GBP').unused == ['second currency']
    assert gate.parse_for('time', 'What time is it in UTC+5:30?').full
    assert gate.parse_for('time', 'what time is it in New Delhi, India').full
    assert gate.parse_for('math', 'Divide 240 by 8').full and gate.parse_for('knowledge', 'x') is None


async def test_local_time_is_never_the_answer_for_a_named_place(monkeypatch):
    from jevrouter.agents import tools
    seen = []

    async def geocode(http, place):
        seen.append(place)
        return {'name': 'Sydney', 'country': 'Australia', 'timezone': 'Australia/Sydney'} if place == 'sydney' else None
    monkeypatch.setattr(tools, 'geocode', geocode)
    r = await tools.agent_time('local time in sydney australia', None)
    assert r.ok and r.answer.startswith('Sydney, Australia: ') and seen == ['sydney australia', 'sydney']
    r = await tools.agent_time('what time is it in Atlantis Prime', None)
    assert not r.ok and 'Your local time' not in r.answer and 'Atlantis Prime' in r.answer


async def test_keyless_dead_ends_say_what_would_answer():
    from jevrouter.agents import tools

    async def nothing(*a, **k):
        return {}
    for agent in (tools.agent_knowledge, tools.agent_code):
        orig = tools.get_json, tools.cached_json
        tools.get_json = tools.cached_json = nothing
        try:
            r = await agent('Should I learn Rust or Go first?', None)
        finally:
            tools.get_json, tools.cached_json = orig
        assert not r.ok and r.answer == ("I couldn't find a reference answer for that. A question like this needs an "
                                         'LLM engine; choose one in Settings.')


# ---------- A7: the planner ----------

@pytest.mark.parametrize('q, parts', [
    ('Weather in Vienna and Prague', ['Weather in Vienna', 'Weather in Prague']),
    ('Convert 100 USD to EUR and GBP', ['Convert 100 USD to EUR', 'Convert 100 USD to GBP']),
    ('salt and pepper', ['salt and pepper']), ('Weather in Bosnia and Herzegovina', ['Weather in Bosnia and Herzegovina']),
    ('Add 120 and 380 dollars, then convert the result to EUR', ['Add 120 and 380 dollars', 'convert the result to EUR']),
])
def test_coordination_splits(q, parts):
    from jevrouter.planner import candidate_split
    assert candidate_split(q) == parts


async def test_a_step_that_uses_the_result_waits_for_it_and_gets_its_number():
    from jevrouter.planner import plan
    p = await plan('Add 120 and 380 dollars, then convert the result to EUR', FakeJev(multi=0.9))
    assert p['subtasks'] == ['Add 120 and 380 dollars', 'convert the result to EUR'] and p['deps'] == [[], [0]]
    seen = []

    async def math(text, emit):
        return AgentResult('(120+380) = 500', True)

    async def currency(text, emit):
        seen.append(text)
        return AgentResult('500.00 USD = 430.00 EUR', True)
    route = lambda t: ('math', 0.9) if t.startswith('Add') else ('currency', 0.9)
    r = Router(FakeJev(route_for=route, multi=0.9), registry={'math': math, 'currency': currency})
    await r.handle('Add 120 and 380 dollars, then convert the result to EUR', 'you')
    assert seen == ['convert 500 USD to EUR']


def test_parse_steps_never_leaks_step_numbers():
    import re
    from jevrouter.planner import parse_steps
    texts, _ = parse_steps([{'text': 'Add 120 and 380', 'depends_on': []},
                            {'text': 'Convert the result from subtask 0 to EUR', 'depends_on': [0]},
                            {'text': 'Use step 2 to find the time', 'depends_on': [1]}])
    assert not any(re.search(r'\bsubtask \d|\bstep \d', t) for t in texts)
    assert texts[1] == 'Convert the previous answer to EUR'


async def test_resolve_step_keeps_the_step_when_the_rewrite_pastes_an_answer():
    from jevrouter.planner import resolve_step
    answer = 'Sydney is ahead of London by eleven hours in the southern summer and nine in winter.'
    e = ScriptEngine(rewrites={'there': 'What time is it in Sydney, which is ahead of London by eleven hours in the '
                                        'southern summer and nine in winter?'})
    text, _, _ = await resolve_step(e, 'What time is it there?\n\nContext: ' + answer, step='What time is it there?',
                                    upstream=[answer])
    assert text == 'What time is it there?'
    e = ScriptEngine(rewrites={'there': 'What time is it in Sydney?'})
    assert (await resolve_step(e, 'What time is it there? ctx', step='What time is it there?', upstream=[answer]))[0] == \
        'What time is it in Sydney?'


async def test_planner_is_told_about_the_at_agent_and_the_web():
    from jevrouter.planner import NO_WEB, plan
    e = ScriptEngine(plan={'subtasks': [{'text': 'x', 'depends_on': []}]})
    await plan('research AI in depth and make a 12 page PDF about it', FakeJev(), e, forced='create', web=False,
               mode='deep')
    call = e.calls[0]
    assert call['system'].endswith(NO_WEB) and 'The user picked @create. Plan exactly one step for it' in call['prompt']
    assert 'restate every format, length, page or slide' in call['system']
    e.calls.clear()
    await plan('research AI in depth and make a 12 page PDF about it', FakeJev(), e, web=True, mode='deep')
    assert not e.calls[0]['system'].endswith(NO_WEB) and '@' not in e.calls[0]['prompt']


# ---------- A1: @agent binds one step ----------

def fake_make(jobs: list):
    async def make(job, engine=None, jev=None, mode='balanced'):
        jobs.append(job)
        meta = {'id': uuid.uuid4().hex[:12], 'name': 'x.pdf', 'format': 'pdf', 'size': 10, 'created': 1.0, 'pages': 12,
                'source': 'llm', 'rules': []}
        return create_agent.Made('Created **x.pdf**, 12 pages', True, 'claude-code', 5, 3, 0, meta,
                                 {'title': 'X', 'sections': []}, b'%PDF', caveats=['asked for 12-13 pages, made 12'])
    return make


async def run_events(router, query, **kw):
    events = []
    router.bus.taps.append(events.append)
    await router.handle(query, 'you', **kw)
    router.bus.taps.remove(events.append)
    return events


async def test_2741_forced_create_binds_the_file_step_only(monkeypatch):
    jobs = []
    monkeypatch.setattr(create_agent, 'make', fake_make(jobs))
    engine = ScriptEngine(plan={'subtasks': [{'text': 'Research X in depth', 'depends_on': []},
                                             {'text': 'Create a 12 page PDF on X from it', 'depends_on': [0]}]})
    feeds = []
    from jevrouter import agents as agent_registry
    real_build = agent_registry.build

    def build(http, eng=None, customs=(), prefer_keyless=False):
        reg = real_build(http, eng, customs, prefer_keyless)
        inner = reg['research']

        async def research(text, emit):
            feeds.append(FEEDS_FILE.get())
            return await inner(text, emit)
        return {**reg, 'research': research}
    monkeypatch.setattr(agent_registry, 'build', build)
    from jevrouter import pipeline
    composed, real_compose = [], pipeline.compose

    async def compose(query, steps, *a, **kw):
        composed.append(steps)
        return await real_compose(query, steps, *a, **kw)
    monkeypatch.setattr(pipeline, 'compose', compose)
    route = lambda t: ('knowledge', 0.9) if t.startswith('Research') else ('create', 0.9)
    router = Router(FakeJev(route_for=route), engine=engine)
    query = QUERY_2741
    events = await run_events(router, query, agent='create', mode='research')
    check_fields(events)
    tasks = router.history[-1]['tasks']
    assert tasks[0]['agent'] == 'research' and 'forced' not in tasks[0] and 'bound' not in tasks[0]
    assert tasks[1]['agent'] == 'create' and tasks[1]['forced'] is True and tasks[1]['bound'] is True
    assert [f['format'] for t in tasks for f in t.get('created_files') or []] == ['pdf']
    assert [t['rule'] for t in tasks[0]['trace']][-1] == 'mode_research'
    (job,) = jobs
    assert job.role == 'primary' and job.http is None and job.request == 'Create a 12 page PDF on X from it'
    assert job.deps[0][0] == 'Research X in depth' and job.deps[0][1].startswith('claude-code: ')
    assert tasks[1]['caveats'] == ['asked for 12-13 pages, made 12']
    # the primary step's brief reads the whole query (C1): whatever the parser finds there, the query's wins
    from jevrouter.create.brief import merge, parse_brief
    assert job.brief == merge(parse_brief(query), parse_brief('Create a 12 page PDF on X from it'))
    assert feeds == [bool(job.brief.pages and job.brief.pages[1] >= 3 or job.brief.images or job.brief.diagrams)]
    assert FEEDS_FILE.get() is False  # reset after the step
    # the merger is told which answer went into the long file, so the answer leads with the file, not the notes
    assert [st['feeds_file'] for st in composed[0]] == [feeds[0], False]


async def test_forced_create_on_a_plan_with_no_file_step_makes_the_whole_query(monkeypatch):
    jobs = []
    monkeypatch.setattr(create_agent, 'make', fake_make(jobs))
    engine = ScriptEngine(plan={'subtasks': [{'text': 'Research X', 'depends_on': []},
                                             {'text': 'Summarize the findings', 'depends_on': [0]}]})
    router = Router(FakeJev(route_for=lambda t: ('knowledge', 0.9)), engine=engine)
    await run_events(router, 'research X and summarize it as a report', agent='create')
    tasks = router.history[-1]['tasks']
    assert [t['agent'] for t in tasks] == ['knowledge', 'create'] and tasks[1]['bound']
    assert jobs[0].request == 'research X and summarize it as a report'


async def test_forced_agent_binds_the_step_jev_gives_it_most(monkeypatch):
    def route(t):
        return ('weather', 0.9) if 'weather' in t else ('currency', 0.9)
    jev = FakeJev(route_for=route, multi=0.9)

    async def weather(text, emit):
        return AgentResult('Paris: 18°C', True)

    async def currency(text, emit):
        return AgentResult('100.00 EUR = 9,000.00 INR', True)
    router = Router(jev, registry={'weather': weather, 'currency': currency})
    events = await run_events(router, 'convert 100 EUR to INR and weather in Paris', agent='weather')
    check_fields(events)
    tasks = router.history[-1]['tasks']
    assert [t['agent'] for t in tasks] == ['currency', 'weather']
    assert tasks[1]['bound'] and tasks[1]['forced'] and 'forced' not in tasks[0] and 'bound' not in tasks[0]
    assert tasks[0]['reason'] == 'currency at 90%' and tasks[1]['reason'] == 'you picked @weather'
    # each step was routed once: the binding's routes are reused, and the bound step needs no safety call of its own
    routes = [c for c in jev.calls if 'route' in c[1]]
    assert sorted(c[0] for c in routes) == ['convert 100 EUR to INR', 'weather in Paris']
    assert not [c for c in jev.calls if c[1] == ['unsafe']]


async def test_forced_agent_that_fits_no_step_binds_the_first():
    jev = FakeJev(route_for=lambda t: ('chat', 0.99), multi=0.9)

    async def answer(text, emit):
        return AgentResult('ok', True)
    router = Router(jev, registry={'chat': answer, 'weather': answer, 'currency': answer, 'math': answer})
    await run_events(router, 'weather in Paris and convert 100 EUR to INR', agent='math')
    tasks = router.history[-1]['tasks']
    assert tasks[0]['bound'] and tasks[0]['agent'] == 'math' and 'bound' not in tasks[1]
    assert tasks[0]['trace'][0] == {'rule': 'forced', 'agent': 'math', 'why': 'forced agent fits no step well'}


async def test_single_step_forced_create_is_forced_as_before(monkeypatch):
    jobs = []
    monkeypatch.setattr(create_agent, 'make', fake_make(jobs))
    jev = FakeJev(route_for=lambda t: ('chat', 0.9))
    router = Router(jev, engine=ScriptEngine(plan={'subtasks': [{'text': 'make a PDF about X', 'depends_on': []}]}))
    await run_events(router, 'make a PDF about X', agent='create')
    (task,) = router.history[-1]['tasks']
    assert task['agent'] == 'create' and task['forced'] and task['bound'] and jobs[0].request == 'make a PDF about X'
    assert not [c for c in jev.calls if 'route' in c[1]]


# ---------- D3: route-only runs ----------

class Boom:
    """An engine or HTTP session that fails the test if anything calls it."""
    name, label, supports_web, supports_exec, billing = 'boom', 'Boom', True, False, 'api'

    def available(self):
        return True, ''

    def info(self):
        return {'name': self.name}

    async def stream(self, **kw):
        raise AssertionError('route mode called the engine')

    async def prewarm(self, **kw):
        raise AssertionError('route mode warmed the engine')

    def get(self, *a, **kw):
        raise AssertionError('route mode made an HTTP call')


async def test_route_mode_runs_no_agent_merger_engine_or_http(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError('route mode ran an agent')
    monkeypatch.setattr(create_agent, 'make', boom)
    jev = FakeJev(route_for=lambda t: ('knowledge', 0.9) if 'Research' in t else ('create', 0.9))
    router = Router(FakeJev(), http=Boom(), engine=Boom(), registry={'knowledge': boom, 'research': boom, 'create': boom})
    pinned = {'subtasks': ['Research X in depth', 'Create a 12 page PDF on X from it'], 'deps': [[], [0]]}
    qid = router.submit('pdf on X', 'eval', extras={'dry_run': 'route', 'jev': jev, 'plan': pinned}, agent='create',
                        mode='research')
    await router.running[qid]
    rec = router.store.get_run(qid)
    assert rec['status'] == 'done' and rec['dry_run'] == 'route' and rec['merged'] is None
    assert rec['plan']['planner'] == 'pinned' and [t['text'] for t in rec['tasks']] == pinned['subtasks']
    t0, t1 = rec['tasks']
    assert t0['agent'] == 'research' and t0['trace'] and 'hard' in t0 and 'answer' not in t0
    assert t1['agent'] == 'create' and t1['forced'] and t1['bound'] and 'answer' not in t1
    assert [c[0] for c in jev.calls if 'route' in c[1]] == ['Research X in depth']  # the pinned plan: no planner call
    assert not router.jev.calls  # the run's own Jev is never asked


async def test_route_mode_plans_without_the_engine_and_gives_dependents_no_context():
    jev = FakeJev(route_for=lambda t: ('currency', 0.9) if 'EUR' in t else ('time', 0.9), multi=0.9)
    router = Router(FakeJev(), engine=Boom())
    events = await run_events(router, 'Convert 50 EUR to INR and then what time is it there', extras={'dry_run': 'route',
                                                                                                       'jev': jev})
    assert not [e for e in events if e['type'] in ('answered', 'merged', 'delta')]
    rec = router.history[-1]
    assert rec['plan']['planner'] == 'heuristic' and rec['tasks'][1]['depends_on'] == [rec['tasks'][0]['tid']]
    seen = [c[0] for c in jev.calls if 'route' in c[1]]
    assert seen[1].startswith('what time is it in New Delhi, India') and '(not run: route mode)' in seen[1]


def test_pinned_plan_is_checked_like_a_planner_plan():
    p = pinned_plan({'subtasks': ['a', ' ', 'b', 'c'], 'deps': [[], [], [0, 5], [2]]})
    assert p['subtasks'] == ['a', 'b', 'c'] and p['deps'] == [[], [0], [1]] and p['planner'] == 'pinned'
    with pytest.raises(ValueError):
        pinned_plan({'subtasks': []})


# ---------- A4: keyless follow-ups ----------

async def session_run(router, query, sid='s'):
    events = await run_events(router, query, session_id=sid)
    return events, router.history[-1]


def keyless_router():
    seen = []

    def agent(name):
        async def run(text, emit):
            seen.append((name, text))
            answers = {'currency': f'{text} = 1', 'weather': 'Lisbon: 20°C', 'time': 'Lisbon: 9:00', 'math': '(13*11) = 143'}
            return AgentResult(answers[name], True)
        return run

    def route(t):
        for word, a in (('Convert', 'currency'), ('make it', 'chat'), ('Weather', 'weather'), ('time', 'time'),
                        ('raining', 'weather'), ('Multiply', 'math')):
            if word in t:
                return a, 0.9
        return 'chat', 0.3
    router = Router(FakeJev(route_for=route), registry={n: agent(n) for n in ('currency', 'weather', 'time', 'math')})
    return router, seen


async def test_keyless_follow_ups_carry_the_previous_frame():
    router, seen = keyless_router()
    await session_run(router, 'Convert 50 EUR to USD')
    events, rec = await session_run(router, 'make it 500')
    assert rec['tasks'][0]['agent'] == 'currency' and seen[-1] == ('currency', 'Convert 500 EUR to USD')
    assert rec['tasks'][0]['frame_used']['agent'] == 'currency' and rec['tasks'][0]['trace'][0]['rule'] == 'frame'
    assert rec['tasks'][0]['frame'] == {'agent': 'currency', 'slots': {'amount': 500.0, 'from': 'EUR', 'to': 'USD'}}
    await session_run(router, 'Weather in Lisbon', 'w')
    events, rec = await session_run(router, 'And what time is it there?', 'w')
    assert seen[-1] == ('time', 'What time is it in Lisbon?')
    await session_run(router, 'Multiply 13 by 11', 'm')
    events, rec = await session_run(router, 'Switching topics: is it raining in Oslo?', 'm')
    assert seen[-1] == ('weather', 'Switching topics: is it raining in Oslo?') and 'frame_used' not in rec['tasks'][0]


async def test_a_question_back_keeps_what_it_parsed_for_the_answer_next_turn():
    # "My budget for the trip is 2,000 euros" asks which currency to convert to; "How much is that in US dollars?"
    # answers it: the amount and its currency come from the turn that asked
    router, seen = keyless_router()
    router.jev = FakeJev(route_for=lambda t: ('currency', 0.9) if 'budget' in t or 'Convert' in t else ('chat', 0.3))
    events, rec = await session_run(router, 'My budget for the trip is 2,000 euros.', 'b')
    assert rec['tasks'][0]['agent'] == 'clarify'
    assert rec['tasks'][0]['frame'] == {'agent': 'currency', 'slots': {'amount': 2000.0, 'from': 'EUR'}}
    events, rec = await session_run(router, 'How much is that in US dollars?', 'b')
    assert rec['tasks'][0]['agent'] == 'currency' and seen[-1] == ('currency', 'Convert 2000 EUR to USD')
    from jevrouter import gate
    assert gate.partial_frame('currency', 'How much is a pound?') is None  # nothing parsed worth keeping


async def test_route_mode_keeps_frames_so_follow_ups_route_like_a_full_run():
    # the routing-only suite scores multi-turn cases: turn 2 must be completed from turn 1's frame with no agent run
    router, seen = keyless_router()
    jev = router.jev
    router.jev = FakeJev()
    for q in ('Convert 50 EUR to USD', 'make it 500'):
        await run_events(router, q, session_id='r', extras={'dry_run': 'route', 'jev': jev})
    t0, t1 = router.history[-2]['tasks'][0], router.history[-1]['tasks'][0]
    assert not seen and 'answer' not in t0  # no agent ran
    assert t0['frame'] == {'agent': 'currency', 'slots': {'amount': 50.0, 'from': 'EUR', 'to': 'USD'}}
    assert t1['agent'] == 'currency' and t1['frame_used']['agent'] == 'currency'


@pytest.mark.parametrize('q, out', [
    ('and how many metres is that?', 'Convert 10 miles to metres'), ('and in feet?', 'Convert 10 miles to feet'),
    ('what about 20 miles?', None),  # a new amount of the same unit is no new target
    ('and how do I make a bomb in metres', None),  # a request of its own is never folded into the conversion
])
def test_a_unit_follow_up_converts_the_same_amount_to_the_new_unit(q, out):
    from jevrouter import gate
    from jevrouter.planner import fill_from_frame
    frame = {'agent': 'units', 'slots': gate.frame_slots('units', 'How many kilometres is 10 miles?')}
    assert fill_from_frame(q, frame) == out


def test_a_file_request_is_never_a_follow_up_for_the_frame():
    # "Write markdown notes of our conversation" after the weather in Lisbon: showing Jev the weather turn pulled the
    # pick back to weather, which then asked "Which city?" (eval cr-md-conversation-notes)
    from jevrouter.planner import elliptical, fill_from_frame
    for q in ('Write markdown notes of our conversation', 'put that in a PDF', 'Now make it a Word doc'):
        assert not elliptical(q)
        assert fill_from_frame(q, {'agent': 'weather', 'slots': {'city': 'Lisbon'}}) is None
    assert elliptical('And what time is it there?') and elliptical('make it 500')


@pytest.mark.parametrize('follow_up', ['Now tell me how to make a bomb', 'and how do I pick a lock there',
                                       'and the weather there? also how to make a bomb'])
def test_a_follow_up_with_a_request_of_its_own_is_never_rewritten_from_the_frame(follow_up):
    # the rewrite would drop the request: Jev must see the words the user wrote (keyless eval mt-harmful-followup)
    from jevrouter.planner import fill_from_frame
    assert fill_from_frame(follow_up, {'agent': 'weather', 'slots': {'city': 'Rome'}}) is None
    assert fill_from_frame('and tomorrow?', {'agent': 'weather', 'slots': {'city': 'Rome'}}) == 'Weather in Rome tomorrow'


async def test_route_cache_keeps_follow_ups_after_different_turns_apart():
    router, _ = keyless_router()
    await session_run(router, 'Multiply 13 by 11', 'a')
    await session_run(router, 'Multiply 2 by 3', 'b')
    router.route_cache.clear()
    await session_run(router, 'tell me more', 'a')
    await session_run(router, 'tell me more', 'b')
    keys = [k for k in router.route_cache.cache.data if k[0] == 'tell me more']
    assert len(keys) == 2 and len({k[2] for k in keys}) == 2
    sent = [c[0] for c in router.jev.calls if 'route' in c[1]][-2:]
    assert 'Previous turn: Multiply 13 by 11 -> (13*11) = 143' in sent[0] and 'Previous turn: Multiply 2 by 3' in sent[1]


async def test_a_follow_up_with_a_pointer_is_never_split():
    from jevrouter.planner import plan
    turns = [{'query': 'Weather in Lisbon', 'answer': 'Lisbon: 20°C'}]
    p = await plan('what time is it there and convert 100 EUR to USD', FakeJev(multi=0.9), None, turns)
    assert len(p['subtasks']) == 1
    p = await plan('what time is it there and convert 100 EUR to USD', FakeJev(multi=0.9))
    assert len(p['subtasks']) == 2


# ---------- B1: dependency context; B4: Jev's input; the merger and the run record ----------

def test_allot_and_cut():
    assert allot({'a': 100, 'b': 9000}, 6000) == {'a': 100, 'b': 5900}
    assert allot({'a': 5000, 'b': 5000}, 6000) == {'a': 3000, 'b': 3000}
    text = 'a' * 30 + '\n' + 'b' * 30
    assert cut(text, 40) == 'a' * 30 + '\n[truncated 31 chars]'
    assert cut('x' * 100, 40) == 'x' * 40 + '\n[truncated 60 chars]'
    assert cut('short', 40) == 'short'


async def test_a_long_dependency_reaches_llm_steps_and_never_keyless_ones():
    long = ('fact line\n' * 500)[:5000]
    seen = {}

    async def knowledge(text, emit):
        return AgentResult(long, True, engine='claude-code')

    def recorder(name):
        async def run(text, emit):
            seen[name] = text
            return AgentResult('ok', True, engine='claude-code' if name == 'report' else 'keyless')
        return run
    engine = ScriptEngine(plan={'subtasks': [{'text': 'Tell me about X', 'depends_on': []},
                                             {'text': 'Write a report from it', 'depends_on': [0]},
                                             {'text': 'Convert 5 EUR to USD using it', 'depends_on': [0]}]})
    route = lambda t: (('report', 0.9) if t.startswith('Write') else ('currency', 0.9) if t.startswith('Convert')
                       else ('knowledge', 0.9))
    router = Router(FakeJev(route_for=route), engine=engine,
                    registry={'knowledge': knowledge, 'report': recorder('report'), 'currency': recorder('currency')})
    await router.handle('please tell me everything you know about X in detail, then write a long report from what you '
                        'found, and then also convert 5 EUR to USD using the numbers in it', 'you')  # long: an LLM plan
    ctx = seen['report'].split('Context from earlier steps:', 1)[1]
    assert len(ctx) >= 4000 and ctx.count('fact line') >= 400 and '[truncated' not in ctx
    assert 'Context from earlier steps' not in seen['currency'] and 'fact line' not in seen['currency']
    sent = [c[0] for c in router.jev.calls if 'route' in c[1]]
    assert max(len(t) for t in sent) <= ROUTE_CHARS  # Jev reads at most 2,000 characters
    task = router.history[-1]['tasks'][1]
    assert 'first 2,000 of' in task['trace'][-1]['why'] and task['trace'][-1]['agent'] == 'report'


async def test_run_hands_compose_its_steps_and_stores_caveats_and_suspects(monkeypatch):
    from jevrouter import pipeline, suspects
    got = {}

    async def compose(query, steps, emit_delta, engine=None, style='default', exact=False):
        got['steps'] = steps
        return {'answer': 'joined', 'engine': 'concat', 'claude_in': 0, 'claude_out': 0, 'kind': 'template',
                'caveats': ['Figures like these change every year; check the official source.'], 'primary_file': None}
    monkeypatch.setattr(pipeline, 'compose', compose)
    monkeypatch.setattr(suspects, 'suspects', lambda rec, specs=None: [{'code': 'unfulfilled', 'note': f"{len(rec['tasks'])} steps"}])

    async def knowledge(text, emit):
        return AgentResult('It is rank 67.', True)

    async def weather(text, emit):
        return AgentResult('Paris: 18°C', True)
    route = lambda t: ('weather', 0.9) if 'weather' in t else ('knowledge', 0.9)
    router = Router(FakeJev(route_for=route, multi=0.9), registry={'knowledge': knowledge, 'weather': weather})
    events = await run_events(router, 'What is the IIT Bombay cutoff rank this year and weather in Paris')
    check_fields(events)
    s0, s1 = got['steps']
    assert set(s0) == {'tid', 'text', 'agent', 'answer', 'ok', 'files', 'caveats', 'assumption', 'feeds_file', 'probabilities',
                       'asked'}
    assert s0['feeds_file'] is False and s1['feeds_file'] is False
    assert max(s1['probabilities'], key=s1['probabilities'].get) == 'weather'
    assert s0['caveats'] == ['Figures like these change every year; check the official source.'] and s1['caveats'] == []
    rec = router.history[-1]
    assert rec['merged'] == {'answer': 'joined', 'engine': 'concat',
                             'caveats': ['Figures like these change every year; check the official source.']}
    assert events[-2]['caveats'] == rec['merged']['caveats']
    assert rec['suspects'] == [{'code': 'unfulfilled', 'note': '2 steps'}]
    assert router.store.get_run(rec['qid'])['suspects'] == rec['suspects']


async def test_policy_trace_can_be_hidden(monkeypatch):
    router, _ = keyless_router()
    monkeypatch.setenv('TG_POLICY_TRACE', '0')
    events = await run_events(router, 'Convert 50 EUR to USD')
    assert 'trace' not in next(e for e in events if e['type'] == 'routed')


def test_config_reports_the_query_limit_and_body_font(monkeypatch):
    monkeypatch.delenv('TRACEGRAPH_BODY_FONT', raising=False)
    router = Router(FakeJev())
    assert router.config()['limits'] == {'query_chars': MAX_QUERY_CHARS} and router.config()['fonts'] == {'body': None}
    assert router.hello()['limits'] == {'query_chars': 4000}
    monkeypatch.setenv('TRACEGRAPH_BODY_FONT', '/fonts/DejaVuSans.ttf')
    assert router.config()['fonts']['body']


async def test_unsupported_is_an_honest_answer_in_the_run():
    jev = SignalJev(signals_for=lambda t: {'live': 0.95}, route_for=lambda t: ('knowledge', 0.99))
    router = Router(jev, registry={})
    events = await run_events(router, "What's the stock price of Apple?")
    check_fields(events)
    a = next(e for e in events if e['type'] == 'answered')
    assert a['agent'] == 'unsupported' and a['ok'] and "don't have live data" in a['answer']
    assert router.stats['by_agent']['unsupported'] == 1


async def test_described_place_is_looked_up_first_with_an_engine():
    engine = ScriptEngine(plan={'subtasks': [{'text': "Weather in William Shakespeare's birthplace", 'depends_on': []}]},
                          rewrites={'birthplace': 'Weather in Stratford-upon-Avon'})
    seen = []

    async def weather(text, emit):
        seen.append(text)
        return AgentResult('Stratford-upon-Avon: 14°C', True)

    async def knowledge(text, emit):
        return AgentResult('Stratford-upon-Avon', True, engine='claude-code')
    route = lambda t: ('knowledge', 0.9) if t.startswith('What is') else ('weather', 0.9)
    router = Router(FakeJev(route_for=route), engine=engine, registry={'weather': weather, 'knowledge': knowledge})
    await router.handle("What's the weather in William Shakespeare's birthplace, please?", 'you')
    tasks = router.history[-1]['tasks']
    assert [t['agent'] for t in tasks] == ['knowledge', 'weather'] and tasks[1]['depends_on'] == [tasks[0]['tid']]
    assert tasks[0]['text'] == "What is William Shakespeare's birthplace? Give only the place name."
    assert seen == ['Weather in Stratford-upon-Avon']


async def test_speculative_and_bound_routes_count_their_tokens():
    jev = FakeJev(route_for=lambda t: ('weather', 0.9) if 'weather' in t else ('currency', 0.9), multi=0.9)

    async def answer(text, emit):
        return AgentResult('ok', True)
    router = Router(jev, registry={'weather': answer, 'currency': answer})
    await run_events(router, 'convert 100 EUR to INR and weather in Paris', agent='weather')
    assert router.history[-1]['tokens']['jev_in'] == 100 * len(jev.calls)


async def test_at_weather_binds_step_one_and_step_two_goes_through_jev():
    jev = FakeJev(route_for=lambda t: ('weather', 0.9) if 'weather' in t else ('currency', 0.9), multi=0.9)

    async def answer(text, emit):
        return AgentResult('ok', True)
    router = Router(jev, registry={'weather': answer, 'currency': answer})
    await run_events(router, 'weather in Paris and convert 100 EUR to INR', agent='weather')
    t1, t2 = router.history[-1]['tasks']
    assert (t1['agent'], t1.get('bound'), t1.get('forced')) == ('weather', True, True)
    assert (t2['agent'], t2.get('bound'), t2.get('forced')) == ('currency', None, None)
    assert t2['probabilities']['currency'] == 0.9  # routed by Jev, not forced


@pytest.mark.parametrize('text, d', [
    ('asdfghjkl', jev('clarify', 'chat', chat=0.3, knowledge=0.2)),
    ("What's the rate?", jev('clarify', 'currency', currency=0.5)),
    ('How much is a pound?', jev('currency', currency=0.8)),
])
def test_vague_requests_still_clarify(text, d):
    for s in (ctx(text), engine_ctx(text)):
        assert policy.apply(d, s).agent == 'clarify'


async def test_mercury_still_asks_which_meaning(monkeypatch):
    from jevrouter import gate

    async def meanings(http, term):
        return ['Mercury (planet)', 'Mercury (element)']
    monkeypatch.setattr(gate, 'meanings', meanings)

    async def knowledge(text, emit):
        return AgentResult('a planet', True)
    router = Router(FakeJev(route_for=lambda t: ('knowledge', 0.9)), http=object(), registry={'knowledge': knowledge})
    events = await run_events(router, 'Mercury')
    check_fields(events)
    task = router.history[-1]['tasks'][0]
    assert task['agent'] == 'clarify' and task['trace'][-1]['rule'] == 'ambiguous_term'


def test_create_clarify_asks_for_the_topic():
    from jevrouter import gate
    assert gate.clarify_text({'create': 0.9, 'chat': 0.1}, 'Make me a presentation') == \
        'What should the presentation be about?'
    assert gate.clarify_text({'create': 0.9, 'chat': 0.1}, 'make a PDF') == 'What should the PDF be about?'
    assert 'about?' not in gate.clarify_text({'create': 0.9, 'chat': 0.1}, 'make slides about solar power')


async def test_a_dependency_file_and_attachment_reach_the_steps_after_it(monkeypatch):
    jobs = []
    monkeypatch.setattr(create_agent, 'make', fake_make(jobs))
    engine = ScriptEngine(plan={'subtasks': [{'text': 'Make a Markdown file about solar power', 'depends_on': []},
                                             {'text': 'Turn it into a PDF', 'depends_on': [0]}]})
    router = Router(FakeJev(route_for=lambda t: ('create', 0.9)), engine=engine)
    await router.handle('please make a markdown file about solar power with a lot of detail in it, and then after that '
                        'turn it into a PDF for me as well', 'you')
    first, second = jobs
    assert first.role == 'working' and second.role == 'primary' and first.dep_files == []
    (meta, spec), = second.dep_files
    assert meta['id'] == router.history[-1]['tasks'][0]['created_files'][0]['id'] and spec == {'title': 'X',
                                                                                              'sections': []}


async def test_a_step_after_a_document_answer_sees_the_attached_passage():
    from jevrouter.store import Store
    store = Store()
    text = 'Track one is about robots.\n\nTrack two is about oceans.'
    meta = {'id': uuid.uuid4().hex[:12], 'name': 'notes.txt', 'size': 1, 'kind': 'text', 'chars': len(text),
            'created': 1.0}
    store.add_file(meta, b'x', text)
    seen = {}

    async def document(text, emit):
        return AgentResult('There are two tracks.', True, 'notes.txt', 'claude-code')

    async def knowledge(text, emit):
        seen['k'] = text
        return AgentResult('ok', True, engine='claude-code')
    engine = ScriptEngine(plan={'subtasks': [{'text': 'How many tracks are in the file?', 'depends_on': []},
                                             {'text': 'Explain the oceans track', 'depends_on': [0]}]})
    route = lambda t: ('document', 0.9) if t.startswith('How many') else ('knowledge', 0.9)
    router = Router(FakeJev(route_for=route), engine=engine, store=store,
                    registry={'document': document, 'knowledge': knowledge})
    await router.handle('how many tracks are in the file, and then explain the oceans track to me in some detail '
                        'please, using what the file says about it', 'you', files=[meta['id']])
    assert 'There are two tracks.' in seen['k'] and 'Track two is about oceans.' in seen['k']


# ---------- verification round 2: keyless honesty fixes ----------

def test_a_made_up_currency_is_answered_honestly_not_asked_about():
    # Jev finds it unclear, but the currency parser knows the currency isn't real: its honest answer beats a question
    for q in ('Convert 100 USD to Wakandan dollars', 'what is 100 USD in WKD', 'convert $100 into Wakandan currency'):
        out = policy.apply(jev('clarify', pick='currency', currency=0.8, knowledge=0.2), ctx(q))
        assert out.agent == 'currency' and out.trace[-2]['rule'] == 'confirmed', q
    # a real pair Jev is unsure of still goes through the usual rules, and a vague one still asks
    assert policy.apply(jev('clarify', pick='currency', currency=0.8), ctx("What's the rate?")).agent == 'clarify'


def test_a_file_request_pointing_back_at_nothing_asks_for_the_topic():
    from jevrouter import gate
    ask = gate.clarify_text({'create': 0.6, 'knowledge': 0.4}, 'Put that in a PDF', earlier=False)
    assert ask == "There's nothing earlier in this chat to put in a PDF. What should the PDF be about?"
    # with an earlier turn "that" has something to mean, so no topic question
    assert 'nothing earlier' not in gate.clarify_text({'create': 0.6, 'knowledge': 0.4}, 'Put that in a PDF')
    # a confident create pick runs: the create agent itself says there is no earlier answer to use
    assert policy.apply(jev('create', create=0.9), ctx('Put that in a PDF')).agent == 'create'


async def test_put_that_in_a_pdf_with_no_earlier_turn_says_there_is_nothing():
    router = Router(FakeJev(route_for=lambda t: ('create', 0.6), clear=0.1))  # Jev finds it unclear
    events = []
    router.bus.taps.append(events.append)
    await router.handle('Put that in a PDF', 'you')
    a = next(e for e in events if e['type'] == 'answered')
    assert a['agent'] == 'clarify' and 'nothing earlier in this chat' in a['answer'] and 'be about?' in a['answer']


@pytest.mark.parametrize('q, value', [('what number multiplied by itself gives 2025?', 45),
                                      ('what number times itself is 144', 12), ('7 multiplied by itself', 49)])
def test_solve_math_reads_itself(q, value):
    from jevrouter.agents.tools import solve_math
    assert solve_math(q)[1] == value


def test_solve_math_never_drops_an_operation_word():
    from jevrouter.agents.tools import solve_math
    assert solve_math('what x multiplied by y gives 2025?') is None  # "*" left out: 2025 would be confidently wrong
    assert solve_math('x * y = 12') is None
    assert solve_math('What is 15% of 80 for a T-shirt?')[1] == 12  # a hyphen in a word is not an operation


@pytest.mark.parametrize('q, parts', [
    ('vienna weather, prague weather', ['vienna weather', 'prague weather']),
    ('Paris weather, London time', ['Paris weather', 'London time']),
    ('London weather, Paris weather, and Rome weather', ['London weather', 'Paris weather', 'Rome weather']),
    ('weather in Paris, Texas', ['weather in Paris, Texas']),  # "Texas" names no request of its own
    ('time in Paris, France', ['time in Paris, France']),
])
def test_a_comma_list_of_requests_splits(q, parts):
    from jevrouter.planner import candidate_split
    assert candidate_split(q) == parts


@pytest.mark.parametrize('q, place', [('vienna weather', 'vienna'), ('weather prague?', 'prague'),
                                      ('new york weather', 'new york'), ('Berlin weather', 'Berlin'),
                                      ('nice weather', None), ('weather today', None), ('weather forecast', None),
                                      ('screen time', None)])
def test_a_terse_lowercase_place_names_the_weather_place(q, place):
    from jevrouter.agents.tools import find_place
    assert find_place(q) == place


async def test_a_hanging_duckduckgo_is_given_up_on_and_skipped(monkeypatch):
    import asyncio
    from jevrouter.agents import tools
    calls = []

    async def fake(http, ttl, url, **params):
        calls.append(url)
        if 'duckduckgo' in url:
            await asyncio.sleep(60)  # throttled: never answers
        return {}

    async def wiki(http, term):
        return f'{term} is a thing.', 'https://en.wikipedia.org/wiki/X'

    monkeypatch.setattr(tools, 'cached_json', fake)
    monkeypatch.setattr(tools, 'wiki_abstract', wiki)
    monkeypatch.setattr(tools, 'DDG_TIMEOUT', 0.05)
    monkeypatch.setattr(tools, '_ddg_down_until', 0.0)
    assert (await tools.ddg_abstract(None, 'Who was Ada Lovelace?'))[1] == 'Ada Lovelace is a thing.'
    assert (await tools.ddg_abstract(None, 'Who was Alan Turing?'))[1] == 'Alan Turing is a thing.'
    assert sum('duckduckgo' in u for u in calls) == 1  # the second lookup skips it


# ---------- keyless knowledge: the article's lines on what was asked ----------

PARACETAMOL_LEAD = ('Paracetamol, or acetaminophen, is an analgesic and antipyretic agent used to treat fever and mild to '
                    'moderate pain. It is sold under brand names including Tylenol and Panadol.')
PARACETAMOL_TEXT = ('Paracetamol is a painkiller.\n\n== Medical uses ==\nIt treats fever in children.\n'
                    'At a recommended maximum daily dose for an adult of three to four grams, paracetamol is safe.\n'
                    'A single dose should not exceed 1000 mg.\n\n== References ==\n'
                    'Smith J. The maximum daily dose of paracetamol for an adult. Lancet.')


def wikipedia(article: str | Exception, calls: list):
    async def fake(http, url, **params):
        calls.append(params.get('prop') or url)
        if 'duckduckgo' in url:
            return {}
        if params.get('list') == 'search':
            return {'query': {'search': [{'title': 'Paracetamol'}]}}
        if params.get('prop') == 'extracts':
            if isinstance(article, Exception):
                raise article
            return {'query': {'pages': {'1': {'title': 'Paracetamol', 'extract': article}}}}
        return {'type': 'standard', 'extract': PARACETAMOL_LEAD,
                'content_urls': {'desktop': {'page': 'https://en.wikipedia.org/wiki/Paracetamol'}}}
    return fake


async def test_keyless_knowledge_quotes_the_article_line_on_what_was_asked(monkeypatch):
    from jevrouter import cache
    from jevrouter.agents import tools
    calls = []
    monkeypatch.setattr(tools, 'get_json', wikipedia(PARACETAMOL_TEXT, calls))
    monkeypatch.setattr(tools, '_ddg_down_until', 0.0)
    monkeypatch.setattr(cache.LIVE, 'data', OrderedDict())
    r = await tools.agent_knowledge("What's the maximum daily dose of paracetamol for an adult?", None)
    assert r.ok and r.answer.startswith('At a recommended maximum daily dose for an adult of three to four grams')
    assert PARACETAMOL_LEAD in r.answer and 'Lancet' not in r.answer  # the lead follows; references are never quoted
    assert 'extracts' in calls and r.source == 'https://en.wikipedia.org/wiki/Paracetamol'


async def test_keyless_knowledge_about_the_subject_itself_reads_only_the_lead(monkeypatch):
    from jevrouter import cache
    from jevrouter.agents import tools
    calls = []
    monkeypatch.setattr(tools, 'get_json', wikipedia(PARACETAMOL_TEXT, calls))
    monkeypatch.setattr(tools, '_ddg_down_until', 0.0)
    monkeypatch.setattr(cache.LIVE, 'data', OrderedDict())
    for q in ('What is paracetamol?', 'When was paracetamol invented?'):  # the subject; one word is too weak
        r = await tools.agent_knowledge(q, None)
        assert r.ok and r.answer == PARACETAMOL_LEAD
    assert 'extracts' not in calls


async def test_keyless_knowledge_falls_back_to_the_lead_when_the_article_fails(monkeypatch):
    from jevrouter import cache
    from jevrouter.agents import tools
    monkeypatch.setattr(tools, 'get_json', wikipedia(OSError('unreachable'), []))
    monkeypatch.setattr(tools, '_ddg_down_until', 0.0)
    monkeypatch.setattr(cache.LIVE, 'data', OrderedDict())
    r = await tools.agent_knowledge("What's the maximum daily dose of paracetamol for an adult?", None)
    assert r.ok and r.answer == PARACETAMOL_LEAD
    monkeypatch.setattr(tools, 'get_json', wikipedia('Nothing here names the dose asked about.', []))
    monkeypatch.setattr(cache.LIVE, 'data', OrderedDict())
    r = await tools.agent_knowledge("What's the maximum daily dose of paracetamol for an adult?", None)
    assert r.ok and r.answer == PARACETAMOL_LEAD


@pytest.mark.parametrize('a, b, same', [('dose', 'doses', True), ('adult', 'adults', True), ('bomb', 'bombings', True),
                                        ('dose', 'overdose', False), ('cat', 'cats', False), ('max', 'maximum', False)])
def test_same_word_takes_an_ending_but_not_a_prefix(a, b, same):
    from jevrouter.agents.tools import same_word
    assert same_word(a, b) is same


@pytest.mark.parametrize('titles, term, pick', [
    (['Mount Everest in 2018', 'Mount Everest', 'Tallest mountain'], 'How tall is Mount Everest', 'Mount Everest'),
    (['Median lethal dose', 'Caffeine', 'Stimulant'], "What's the lethal dose of caffeine", 'Caffeine'),
    (['Atomic bombings of Hiroshima and Nagasaki', 'Hiroshima (book)'], 'Why was the atomic bomb dropped on Hiroshima',
     'Atomic bombings of Hiroshima and Nagasaki'),  # the top hit stands: a qualified title names a narrower sense
    (['QUIC', 'User Datagram Protocol'], 'difference between TCP and UDP', 'QUIC'),  # nothing named: the top hit
    (['Pierre Curie', 'Marie Curie'], 'What is Marie Curie known for', 'Marie Curie'),
])
def test_wikipedia_search_prefers_the_hit_the_question_names(titles, term, pick):
    from jevrouter.agents.tools import named_hit
    assert named_hit(titles, term) == pick
