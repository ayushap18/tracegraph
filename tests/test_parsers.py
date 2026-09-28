import pytest

from jevrouter.agents.tools import agent_chat, agent_math, clarify, find_place, knowledge_term, parse_currency, solve_math

KNOWN = {'USD', 'EUR', 'GBP', 'INR', 'JPY', 'CHF', 'CNY'}


@pytest.mark.parametrize('q,value', [
    ("What's 18% of 2450?", 441), ('18% of 2450', 441), ('(45 * 12) / 7 + 3^2', 45 * 12 / 7 + 9),
    ('square root of 1764', 42), ('12 times 3 plus 4', 40), ('1,000 divided by 8', 125), ('2 to the power of 10', 1024),
])
def test_solve_math(q, value):
    assert solve_math(q)[1] == pytest.approx(value)


def test_math_int_formatting_and_rejects():
    assert solve_math('square root of 1764')[1] == 42 and isinstance(solve_math('square root of 1764')[1], int)
    assert solve_math('hello there') is None
    with pytest.raises(ValueError):
        solve_math('2 ^ 1000')


@pytest.mark.parametrize('q', ['(((9**99)**99)**99)**99', '((10**100)**100)**100', '(10**100)**100', '1' * 400,
                               '(10**200) * (10**200)'])
def test_math_rejects_huge_results_fast(q):
    import time
    t = time.perf_counter()
    with pytest.raises(ValueError, match='too large'):
        solve_math(q)
    assert time.perf_counter() - t < 0.1


async def test_agent_math_answer():
    r = await agent_math('18% of 2450')
    assert r.ok and r.answer.endswith('= 441')
    assert not (await agent_math('what?')).ok


@pytest.mark.parametrize('q,expected', [
    ('How many euros is 100 pounds?', (100.0, 'GBP', 'EUR')),  # v1 regression: amount's currency is the source
    ('Convert 250 USD to INR', (250.0, 'USD', 'INR')),
    ('convert 100 EUR to INR', (100.0, 'EUR', 'INR')),
    ('JPY to EUR rate', (1.0, 'JPY', 'EUR')),
    ('100 dollars in rupees', (100.0, 'USD', 'INR')),
])
def test_parse_currency(q, expected):
    assert parse_currency(q, KNOWN) == expected


@pytest.mark.parametrize('q,expected', [
    ("What's 20% of 1500 USD in INR?", (300.0, 'USD', 'INR')),
    ('Convert 15 percent of $200 to EUR', (30.0, 'USD', 'EUR')),
    ("What's 7% of €33 in GBP?", (2.31, 'EUR', 'GBP')),
])
def test_a_share_of_an_amount_is_the_amount_converted(q, expected):
    # the percentage is not the amount: "20% of 1500 USD" converts 300 USD, and the parser uses every number it saw
    from jevrouter import gate
    assert parse_currency(q, KNOWN) == expected and gate.parse_for('currency', q).full


def test_parse_currency_errors():
    assert isinstance(parse_currency('convert 100 USD', KNOWN), str)
    assert isinstance(parse_currency('100 USD to dollars', KNOWN), str)


@pytest.mark.parametrize('q,place', [
    ('Will it rain in Mumbai tomorrow?', 'Mumbai'), ("What's the weather in Tokyo right now?", 'Tokyo'),
    ('Is it cold in Oslo today?', 'Oslo'), ('What time is it in New York?', 'New York'), ('weather in Paris', 'Paris'),
    ('Paris weather please, Tell me London', 'London'), ('how is the weather', None),
])
def test_find_place(q, place):
    assert find_place(q) == place


@pytest.mark.parametrize('q,term', [
    ('Who was Ada Lovelace?', 'Ada Lovelace'),  # v1 regression
    ('What is a black hole?', 'black hole'), ('Tell me about the Eiffel Tower', 'Eiffel Tower'),
    ('explain photosynthesis', 'photosynthesis'), ('Quantum computing', 'Quantum computing'),
])
def test_knowledge_term(q, term):
    assert knowledge_term(q) == term


async def test_chat_and_clarify():
    assert (await agent_chat('thanks!')).answer == "You're welcome!"
    r = clarify([('math', 0.3), ('chat', 0.2)])
    # plain-English options, never internal agent names (eval s11 caught "the currency agent or the math agent")
    assert not r.ok and 'a calculation' in r.answer and 'just a chat' in r.answer and 'agent' not in r.answer


def test_currency_ignores_sentence_punctuation():
    # LLM planners end subtasks with a period; "JPY." must still be a currency code.
    from jevrouter.agents.tools import parse_currency
    known = {'USD', 'JPY', 'EUR', 'INR'}
    assert parse_currency('Convert 20 USD to JPY.', known) == (20.0, 'USD', 'JPY')
    assert parse_currency('Convert 12.5 EUR to INR.', known) == (12.5, 'EUR', 'INR')


@pytest.mark.parametrize('q, want', [("what's €75 in £", (75.0, 'EUR', 'GBP')), ('$20 to ₹', (20.0, 'USD', 'INR')),
                                     ('A$50 in USD', (50.0, 'AUD', 'USD')), ('how much is 1.5k $ in EUR', (1500.0, 'USD', 'EUR'))])
def test_currency_signs(q, want):
    from jevrouter.agents.tools import ECB_CODES, parse_currency
    assert parse_currency(q, ECB_CODES) == want


@pytest.mark.parametrize('q, place', [('Berlin weather', 'Berlin'), ('New York weather tomorrow', 'New York'),
                                      ('Tokyo time now', 'Tokyo'), ('What weather', None), ('Current weather', None)])
def test_leading_place_names(q, place):
    from jevrouter.agents.tools import find_place
    assert find_place(q) == place


@pytest.mark.parametrize('q, found', [
    ('Weather in Paris, Texas', ('Paris', 'Texas')), ('sorry, I meant Paris in Texas', ('Paris', 'Texas')),
    ("What's the weather in Sydney, Australia?", ('Sydney', 'Australia')),
    ('Weather in New York', None), ('Weather in Paris', None), ('Weather in Trinidad and Tobago', None),
])
def test_a_place_qualified_by_its_region(q, found):
    from jevrouter.agents.tools import qualified_place
    assert qualified_place(q) == found


async def test_weather_for_a_qualified_place_is_looked_up_inside_its_region(monkeypatch):
    from jevrouter.agents import tools
    asked = []

    async def fake(http, ttl, url, **params):
        asked.append(params)
        if 'geocoding' in url:
            return {'results': [{'name': 'Paris', 'country': 'France', 'admin1': 'Ile-de-France', 'latitude': 48.9,
                                 'longitude': 2.3},
                                {'name': 'Paris', 'country': 'United States', 'admin1': 'Texas', 'latitude': 33.7,
                                 'longitude': -95.6}][:params['count']]}
        return {'current': {'temperature_2m': 30, 'weather_code': 0, 'wind_speed_10m': 5},
                'daily': {'precipitation_probability_max': [0, 0], 'weather_code': [0, 0],
                          'temperature_2m_min': [20, 20], 'temperature_2m_max': [31, 31]}}

    monkeypatch.setattr(tools, 'cached_json', fake)
    r = await tools.agent_weather('Weather in Paris, Texas', None)
    assert r.ok and r.answer.startswith('Paris, Texas, United States: now 30°C') and asked[0]['name'] == 'Paris'
    r = await tools.agent_weather('Weather in Paris', None)
    assert r.answer.startswith('Paris, France: now')


@pytest.mark.parametrize('q,expected', [
    ('Convert 100 Canadian dollars to EUR', (100.0, 'CAD', 'EUR')),  # never USD
    ('100 Australian dollars in yen', (100.0, 'AUD', 'JPY')),
    ('Convert 100 Hong Kong dollars to GBP', (100.0, 'HKD', 'GBP')),
    ('How much is 100 British pounds in US dollars', (100.0, 'GBP', 'USD')),
    ('Convert 50 USD to Mexican pesos', (50.0, 'USD', 'MXN')),
])
def test_a_nationality_names_its_own_currency(q, expected):
    from jevrouter.agents.tools import ECB_CODES
    assert parse_currency(q, ECB_CODES) == expected


@pytest.mark.parametrize('q,unknown,unsupported', [
    ('Convert 100 USD to Wakandan dollars', 'Wakandan dollars', 'Wakandan dollars'),
    ('what is 100 USD in WKD', 'WKD', 'WKD'),
    ('Convert 20 Fijian dollars to EUR', None, 'Fijian dollars'),  # real, but no rates here: never read as USD
    ('50 Egyptian pounds to USD', None, 'EGP'),
    ('Convert 100 USD to INR', None, None), ('What time is it in UTC?', None, None),
])
def test_a_currency_that_is_not_real_or_not_covered(q, unknown, unsupported):
    from jevrouter.agents.tools import ECB_CODES, unknown_currency, unsupported_currency
    assert unknown_currency(q) == unknown and unsupported_currency(q, ECB_CODES) == unsupported


async def test_an_unknown_currency_gets_an_honest_answer_not_a_question(monkeypatch):
    from jevrouter import gate
    from jevrouter.agents import tools

    async def fake(http, ttl, url, **params):
        return {c: c for c in tools.ECB_CODES}

    monkeypatch.setattr(tools, 'cached_json', fake)
    r = await tools.agent_currency('Convert 100 USD to Wakandan dollars', None)
    assert not r.ok and r.answer.startswith("I don't recognise Wakandan dollars as a real currency")
    assert gate.question('currency', 'Convert 100 USD to Wakandan dollars') is None
