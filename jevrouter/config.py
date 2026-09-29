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
    'units': 'Converting a measurement between units of length, weight, temperature, volume, speed, data size or '
             'time (seconds, hours, days, weeks)',
    'dates': 'Calendar maths on given dates: days between two dates, the weekday of a date, a date some days before '
             'or after another, an age, or whether a year is a leap year',
    'url': 'Reading, summarizing or answering a question about a specific web page whose http or https link is in the query',
    # docs/PLAN-files.md: offered keyless too, since an earlier answer or an attached table becomes a file with no LLM.
    'create': 'Create a downloadable file: PDF, Word (DOCX), PowerPoint (PPTX), Excel (XLSX) or Markdown',
}
# Only offered to Jev when the active engine can search the web.
RESEARCH = {'research': 'Recent news, current events, or anything that needs searching the web for up-to-date information'}
# Engine-only built-ins: `report` on any engine, `run` only on an engine that can execute code in a sandbox (Codex).
REPORT = {'report': 'Long-form written reports, essays, comparisons or summaries, with sources'}
RUN = {'run': 'Write and execute code to compute or produce something'}
# Guard outcomes that are decided from Jev's other answers rather than picked by the route question. `unsupported` is
# the honest "this can't be done here" (docs/PLAN-accuracy-v2.md A3).
GUARDS = ['clarify', 'blocked', 'unsupported']
# Exact agents that parse what they need from plain text: their answers need no LLM merger and they never see context.
KEYLESS = {'math', 'weather', 'time', 'currency', 'units', 'dates'}
# Offered to Jev next to the file agents when an attached file is a table (CSV, or a JSON list of objects).
SQL_AGENT = {'sql': 'Exact answers from an attached table: filtering, counting, grouping, sorting or looking up rows'}

MIN_CONFIDENCE = 0.45
MIN_CLEAR = 0.25
# Jev said clarify, but its top pick is an exact keyless agent whose parser finds everything it needs (jevrouter/gate.py):
# that agent runs when Jev gave it at least this probability.
CONFIRM_AT = 0.6
BLOCK_AT = 0.7
MULTI_AT = 0.5
# Accuracy v2 (docs/PLAN-accuracy-v2.md). Jev's `action`, `personal` or `live` signal at or above UNSUPPORTED_AT makes a
# step unsupported (A3); `described` at or above DESCRIBED_AT means a slot is named only by a description (A5). A forced
# @agent whose best step has less than FORCED_MIN of its probability binds step 1 with a trace note (A1).
UNSUPPORTED_AT = 0.7
DESCRIBED_AT = 0.6
FORCED_MIN = 0.05
# The context an LLM step gets from its dependencies, shared across them (B1), and the longest query accepted (B4).
DEP_CONTEXT_CHARS = 6000
MAX_QUERY_CHARS = 4000
MAX_SUBTASKS = 4
HISTORY = 60
RUN_TIMEOUT = 300.0  # seconds; TG_RUN_TIMEOUT overrides
LONG_RUN_TIMEOUT = 900.0  # seconds for runs that write a long document with an engine; TG_LONG_RUN_TIMEOUT overrides

# Routing that learns (docs/PLAN-learning.md). A saved subtask is shaky, and shows in the review queue, below these.
REVIEW_CONFIDENCE = 0.6
REVIEW_MARGIN = 0.15  # top probability minus the runner-up's
REASK_SECONDS = 60.0  # the next turn of the same chat came this soon after the answer
REVIEW_SCAN = 2000  # most recent saved runs the queue looks at
# Route examples: labelled corrections fed to Jev inside each agent's criterion. TG_ROUTE_EXAMPLES=1 turns them on.
MAX_EXAMPLES = 3
MAX_NOT = 2
EXAMPLE_CHARS = 200

# Chat modes and answer styles (docs/PLAN-speed-evals-chat.md; the style instructions are in jevrouter/merger.py).
MODES = ('quick', 'balanced', 'deep', 'research')
STYLES = ('default', 'concise', 'detailed', 'bullets', 'steps', 'simple', 'table')
# Deep mode with no engine named: the strongest healthy engine, in this order. An engine that is cooling down after a
# failure, or whose recent calls succeeded less often than DEEP_MIN_OK, is passed over.
STRONGEST = ('claude-code', 'codex', 'agy', 'opencode')  # then the API-key engines, in the user's order
DEEP_MIN_OK = 0.5
GROUP_MAX = 3  # several answers: at most this many engines in one group from one ask

# Engine per step (A5). Jev's `hard` score (0 easy .. 1 hard) picks, for an LLM step on Auto or in deep/quick mode, the
# fastest healthy engine at effort low (at or below EASY_AT) or the strongest at effort high (at or above HARD_AT).
EASY_AT = 0.3
HARD_AT = 0.7
# Health-aware Auto: an engine needs this many calls before its success rate and p50 change its place; engines whose
# p50 is within SPEED_BAND times of the fastest count as equally fast, so the user's order decides between them.
HEALTH_MIN_CALLS = 3
SPEED_BAND = 2.0
# Hedged requests (TG_HEDGE=1): when an engine has shown no text by its p90 (never sooner than HEDGE_MIN seconds, or
# HEDGE_UNKNOWN before it has HEALTH_MIN_CALLS answers), the next engine starts too and the first to answer is kept.
HEDGE_MIN = 4.0
HEDGE_UNKNOWN = 10.0

# Verify step (A6): a currency answer may differ from today's reference rate by this much before it is flagged.
CURRENCY_TOLERANCE = 0.05

# URL reader egress limits (jevrouter/agents/tools.py): what a fetched page may cost.
URL_MAX_BYTES = 1_000_000
URL_TIMEOUT = 8.0
URL_MAX_REDIRECTS = 3
URL_TYPES = ('text/html', 'text/plain', 'application/xhtml+xml', 'application/json', 'text/markdown', 'text/csv',
             'application/xml', 'text/xml')
# SQL over attached tables: result rows shown, and the seconds a query may run.
SQL_MAX_ROWS = 50
SQL_TIMEOUT = 2.0

# Created files (docs/PLAN-files.md). The one spec call sees the request plus at most CREATE_CONTEXT_CHARS of context
# (earlier turns, earlier steps, attached files: a table as its columns and first CREATE_TABLE_ROWS rows), and may write
# at most CREATE_MAX_TOKENS for the format (tables come from data, so a spreadsheet needs the fewest words). Jev's
# safety check (X4) reads all of the spec's text as it will be written (numbers left out, repeated cells once), in
# pieces of CREATE_SAFETY_CHARS checked side by side, at most CREATE_SAFETY_CHUNKS of them; a file with more text than
# that is not made. A sandbox keeps at most
# CREATE_SANDBOX_FILES created files (CREATE_SANDBOX_BYTES in all) in memory; the oldest go first.
CREATE_CONTEXT_CHARS = 6000
CREATE_TABLE_ROWS = 5
CREATE_MAX_TOKENS = {'md': 3000, 'docx': 3000, 'pdf': 3000, 'pptx': 2500, 'xlsx': 1500}
CREATE_SAFETY_CHARS = 4000
CREATE_SAFETY_CHUNKS = 32
CREATE_SANDBOX_FILES = 10
CREATE_SANDBOX_BYTES = 40 * 1024 * 1024

# Dollars per million tokens.
PRICES = {'jev_in': 0.042, 'claude_in': 5.0, 'claude_out': 25.0}

# Cost preflight (docs/PLAN-files-robust.md 5.3). A run whose estimate crosses any of these asks the user to confirm
# first (/ask returns 409 without starting). TG_COST_CONFIRM=0 turns the guard off; it is read on every request.
COST_CONFIRM = True
COST_CONFIRM_TOKENS = {'agy': 120_000, 'codex': 120_000, 'claude-code': 150_000, 'opencode': 120_000,
                       'api': 60_000,        # every pay-per-token API-key engine
                       'default': 100_000}   # high end of tokens_in + tokens_out
COST_CONFIRM_CALLS = 5                   # mid model calls, planner and merge included
COST_CONFIRM_SECONDS = 180               # mid seconds
COST_DEADLINE_SHARE = 0.8                # high seconds above this share of the deadline
COST_SOURCES = ('chat', 'sandbox', 'you', 'compare')
# On Auto, a long file goes to the healthy engine with the lowest estimate (5.5). TG_LEAN_LONG_FILES=0 turns it off.
LEAN_LONG_FILES = True


def cost_confirm_on() -> bool:
    """The cost guard's switch, read on every request: TG_COST_CONFIRM overrides COST_CONFIRM."""
    return env_flag('TG_COST_CONFIRM', COST_CONFIRM)


def lean_long_files_on() -> bool:
    return env_flag('TG_LEAN_LONG_FILES', LEAN_LONG_FILES)


# Studio, the design stage (docs/PLAN-designer.md section 9). TG_STUDIO picks the formats it designs: unset or empty ->
# STUDIO_DEFAULT; '0'/'off'/'false'/'no' -> none; '1'/'on'/'true'/'yes' -> STUDIO_ON; else a comma list of formats
# ('pptx,pdf,docx'). Read on every file. On by default for slides and PDFs (the painted formats).
STUDIO_DEFAULT = 'pptx,pdf'
STUDIO_ON = ('pptx', 'pdf')
STUDIO_ROUNDS = 3                 # agent loop rounds (layout -> QA -> fix)
STUDIO_DIRECT_TOKENS = 2000       # art direction, tokens out
STUDIO_CRITIC_ROUNDS = 2
STUDIO_CRITIC_TOKENS = 6000       # critic, tokens in + out per file
STUDIO_FREEFORM_PAGES = 2
STUDIO_FREEFORM_TOKENS = 2000     # tokens out per freeform page
STUDIO_TIME_BUDGET = 60.0         # seconds the design stage may take before it paints what it has
# The integration's own limits (builder I): the create agent stops waiting for the design stage after the time budget
# plus STUDIO_GRACE seconds and falls back to the standard layout; the font API's input limits (docs/PLAN-designer.md
# 9.9); what one Polish critic call is priced at before the cost guard sees it (a contact sheet of 6 pages is one
# image, about STUDIO_SHEET_TOKENS tokens in).
STUDIO_GRACE = 30.0
FONT_QUERY_CHARS = 64
FONT_LIMIT_MAX = 50
FONT_PREVIEW_CHARS = 60
STUDIO_SHEET_PAGES = 6
STUDIO_SHEET_TOKENS = 1_600
STUDIO_CRITIC_PROMPT_TOKENS = 1_500   # system prompt, schema and the design report
STUDIO_CRITIC_OUT = 600               # the edits JSON


def studio_formats() -> frozenset:
    """The formats Studio designs now (TG_STUDIO, see above)."""
    raw = os.environ.get('TG_STUDIO')
    v = (STUDIO_DEFAULT if raw is None or not raw.strip() else raw).strip().lower()
    if not v or v in ('0', 'off', 'false', 'no'):
        return frozenset()
    if v in ('1', 'on', 'true', 'yes'):
        return frozenset(STUDIO_ON)
    return frozenset(p.strip() for p in v.split(',') if p.strip() in ('pdf', 'docx', 'pptx', 'xlsx', 'md'))

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
    'Convert 5 km to miles', 'How many days between 1 March 2025 and 4 July 2025?', 'What weekday is 25 December 2026?',
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
