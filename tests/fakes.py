"""Stand-ins for the Jev and Anthropic clients so tests never touch the network."""
import asyncio
from types import SimpleNamespace as NS


def choice(pick, probs):
    return NS(choice=pick, confidence=probs[pick], probabilities=probs)


class FakeJev:
    """route_for(text) -> (agent, confidence); multi is the `multi` noul (or an exception to raise); fail_on makes
    route calls whose text contains any of those substrings raise."""

    def __init__(self, route_for=None, multi=0.9, unsafe=0.02, clear=0.9, delay=0.0, fail_on=()):
        self.route_for = route_for or (lambda text: ('chat', 0.9))
        self.multi, self.unsafe, self.clear, self.delay, self.fail_on = multi, unsafe, clear, delay, fail_on
        self.calls = []

    async def system_one(self, state, qs):
        self.calls.append((state, sorted(qs)))
        await asyncio.sleep(self.delay)
        usage = NS(input_tokens=100)
        if 'multi' in qs:
            if isinstance(self.multi, Exception):
                raise self.multi
            return NS(answers={'multi': NS(noul=self.multi)}, usage=usage, model='jev-test')
        if any(f in state for f in self.fail_on):
            raise RuntimeError('jev down')
        agent, conf = self.route_for(state)
        names = list(qs['route'].criteria)
        rest = (1 - conf) / (len(names) - 1)
        probs = {n: (conf if n == agent else rest) for n in names}
        return NS(answers={'route': choice(agent, probs), 'urgency': NS(score=0.5), 'unsafe': NS(noul=self.unsafe),
                           'clear': NS(noul=self.clear)}, usage=usage, model='jev-test')


class FakeStream:
    def __init__(self, chunks, stop_reason, error):
        self.chunks, self.stop_reason, self.error = chunks, stop_reason, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for c in self.chunks:
                await asyncio.sleep(0)
                yield c
        return gen()

    async def get_final_message(self):
        return NS(stop_reason=self.stop_reason, usage=NS(input_tokens=10, output_tokens=len(self.chunks)),
                  content=[NS(type='text', text=''.join(self.chunks), citations=None)])


class FakeAnthropic:
    """Each stream() call pops the next script entry: a list of text chunks, ('refusal', chunks), or an Exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else ['ok']
        if isinstance(item, Exception):
            return FakeStream([], 'end_turn', item)
        if isinstance(item, tuple):
            return FakeStream(item[1], item[0], None)
        return FakeStream(item, 'end_turn', None)


def api_errors():
    import anthropic
    import httpx2 as httpx
    req = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
    return {
        'rate': anthropic.RateLimitError('slow down', response=httpx.Response(429, request=req), body=None),
        'status': anthropic.InternalServerError('boom', response=httpx.Response(500, request=req), body=None),
        'conn': anthropic.APIConnectionError(request=req),
    }
