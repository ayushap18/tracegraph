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
# Engine-only built-ins: `report` on any engine, `run` only on an engine that can execute code in a sandbox (Codex).
REPORT = {'report': 'Long-form written reports, essays, comparisons or summaries, with sources'}
RUN = {'run': 'Write and execute code to compute or produce something'}
# Guard outcomes that are decided from Jev's other answers rather than picked by the route question.
GUARDS = ['clarify', 'blocked']
KEYLESS = {'math', 'weather', 'time', 'currency'}

MIN_CONFIDENCE = 0.45
MIN_CLEAR = 0.25
# Jev said clarify, but its top pick is an exact keyless agent whose parser finds everything it needs (jevrouter/gate.py):
# that agent runs when Jev gave it at least this probability.
CONFIRM_AT = 0.6
BLOCK_AT = 0.7
MULTI_AT = 0.5
MAX_SUBTASKS = 4
HISTORY = 60
RUN_TIMEOUT = 300.0  # seconds; TG_RUN_TIMEOUT overrides

# Routing that learns (docs/PLAN-learning.md). A saved subtask is shaky, and shows in the review queue, below these.
REVIEW_CONFIDENCE = 0.6
REVIEW_MARGIN = 0.15  # top probability minus the runner-up's
REASK_SECONDS = 60.0  # the next turn of the same chat came this soon after the answer
REVIEW_SCAN = 2000  # most recent saved runs the queue looks at
# Route examples: labelled corrections fed to Jev inside each agent's criterion. TG_ROUTE_EXAMPLES=1 turns them on.
MAX_EXAMPLES = 3
MAX_NOT = 2
EXAMPLE_CHARS = 200

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


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None or not v.strip() else v.strip().lower() in ('1', 'true', 'yes', 'on')


def load_env(path: Path = ROOT / '.env'):
    if path.exists():
        for line in path.read_text().splitlines():
            k, _, v = line.partition('=')
            if k.strip() and not k.startswith('#'):
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))
