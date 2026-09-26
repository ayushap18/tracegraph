"""The engine contract: anything that can turn (system, prompt) into streamed text.

Planner, merger and the LLM agents only talk to this interface, so an Anthropic API key, a Claude Code subscription,
a Codex (ChatGPT) subscription or an Antigravity (Google) subscription are interchangeable backends.
"""
import json
import os
import re
from dataclasses import dataclass, field


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    source: str | None = None  # first cited URL, when the engine searched the web
    raw: list = field(default_factory=list)  # engine-specific final content, for debugging
    engine: str | None = None  # the backend that actually answered, when Auto picked one


# Failures that don't fix themselves in seconds: an engine that hit one is skipped for COOLDOWN seconds.
COOLDOWN = float(os.environ.get('TG_ENGINE_COOLDOWN', 600))
LASTING = re.compile(r'usage limit|rate limit|quota|not logged in|login|not installed|credit|upgrade', re.I)


class EngineError(Exception):
    """The engine could not produce an answer (not installed, not logged in, timed out, crashed, rate limited)."""

    def __init__(self, why: str, partial: str = ''):
        super().__init__(why)
        self.why = why
        self.partial = partial


class EngineRefusal(Exception):
    def __init__(self, reply: Reply):
        super().__init__('refusal')
        self.reply = reply


class Engine:
    name = 'engine'
    label = 'Engine'
    billing = 'api'  # 'api' (pay per token) or 'subscription' (counts against a plan, $0 per call here)
    supports_web = False  # can run a web-search tool, which is what the research agent needs
    supports_exec = False  # can write and run code in its own sandbox, which is what the run agent needs

    def available(self) -> tuple[bool, str]:
        """(usable, why not). Cheap: never spends a model call."""
        return True, ''

    def info(self) -> dict:
        ok, why = self.available()
        return {'name': self.name, 'label': self.label, 'billing': self.billing, 'web': self.supports_web,
                'exec': self.supports_exec, 'available': ok, 'why': why}

    async def stream(self, *, system: str, prompt: str, effort: str = 'medium', emit_delta=None,
                     max_tokens: int = 2048, web: bool = False, schema: dict | None = None, exec: bool = False) -> Reply:
        """exec=True lets the engine write and run code in a sandbox; engines without supports_exec ignore it."""
        raise NotImplementedError

    async def prewarm(self, *, system='', effort='medium', web=False, schema=None):
        """Gets ready for calls with these settings before the first one arrives. Only CLI engines need to."""

    async def aclose(self):
        pass


FENCE = re.compile(r'^```(?:json)?\s*|\s*```$')


def parse_json(text: str):
    """Structured output from CLI engines arrives as text; tolerate a Markdown fence or leading prose."""
    t = FENCE.sub('', text.strip())
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start = min((i for i in (t.find('{'), t.find('[')) if i >= 0), default=-1)
        if start < 0:
            raise
        return json.JSONDecoder().raw_decode(t[start:])[0]


URL = re.compile(r'https?://[^\s)\]>"\']+')


def first_url_in(text: str) -> str | None:
    m = URL.search(text or '')
    return m.group(0).rstrip('.,;') if m else None
