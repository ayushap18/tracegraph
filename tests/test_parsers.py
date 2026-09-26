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
