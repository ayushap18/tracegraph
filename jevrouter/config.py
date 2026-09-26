import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'web' / 'dist'
LEGACY_PAGE = ROOT / 'index.html'

# The route choice Jev makes. Descriptions are what Jev reads, so they say when each agent applies.
AGENTS = {
    'math': 'Arithmetic, percentages, or evaluating a numeric expression',
    'weather': 'Current weather or a forecast for a place',
    'time': 'The current local time or date in a city or timezone',
    'currency': 'Converting an amount of money between currencies, or an exchange rate',
    'knowledge': 'A factual question about a person, place, event, thing, or concept',
    'code': 'Programming, software errors, or how to do something in code',
    'chat': 'Greetings, small talk, or questions about the assistant itself',
}
# Only offered to Jev when the active engine can search the web.
RESEARCH = {'research': 'Recent news, current events, or anything that needs searching the web for up-to-date information'}
# Guard outcomes that are decided from Jev's other answers rather than picked by the route question.
GUARDS = ['clarify', 'blocked']
KEYLESS = {'math', 'weather', 'time', 'currency'}

MIN_CONFIDENCE = 0.45
MIN_CLEAR = 0.25
BLOCK_AT = 0.7
MULTI_AT = 0.5
MAX_SUBTASKS = 4
HISTORY = 60

# Dollars per million tokens.
PRICES = {'jev_in': 0.042, 'claude_in': 5.0, 'claude_out': 25.0}

SAMPLES = [
    "What's 18% of 2450?", '(45 * 12) / 7 + 3^2', 'square root of 1764',
    'Will it rain in Mumbai tomorrow?', "What's the weather in Tokyo right now?", 'Is it cold in Oslo today?',
    'What time is it in New York?', "What's the date in Sydney right now?",
    'Convert 250 USD to INR', 'How many euros is 100 pounds?', 'JPY to EUR rate',
    'Who was Ada Lovelace?', 'What is a black hole?', 'Tell me about the Eiffel Tower',
    'How do I reverse a list in Python?', 'TypeError: undefined is not a function in JavaScript',
    'git undo last commit', 'How to center a div with CSS',
    'hey, how are you?', 'What can you do?', 'thanks, that helped!',
    'hmm', 'the thing from before',
    'weather in Paris and convert 100 EUR to INR',
    "What time is it in Tokyo and what's 15% of 380?",
    'Who was Alan Turing; then convert 50 GBP to USD',
]


def load_env(path: Path = ROOT / '.env'):
    if path.exists():
        for line in path.read_text().splitlines():
            k, _, v = line.partition('=')
            if k.strip() and not k.startswith('#'):
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))
