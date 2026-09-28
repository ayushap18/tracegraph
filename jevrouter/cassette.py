"""Recorded Jev answers for the routing-only suite (docs/PLAN-accuracy-v2.md D3).

JevCassette wraps anything with `system_one(text, questions)` (the Jev client, or a fake). Each call is keyed by
sha256(text + the sorted question names with their type, instructions and criteria), so the same text asked the same
questions always has the same key, whatever order the questions were built in.

- replay: answers come from the cassette only. A miss raises CassetteMiss (the eval scores that case `unrecorded`).
- record: a hit replays; a miss asks the inner client and appends the answer to the cassette.
- live: every call goes to the inner client and nothing is stored.

The cassette is evals/cassettes/jev.jsonl, append-only, one JSON line per key:
{key, text, answers: {name: {choice?, probabilities?, confidence?, noul?, score?}}, model, input_tokens}.
A later line for the same key wins, so a re-record never needs the old line removed.
"""
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace as NS

MODES = ('replay', 'record', 'live')
ANSWER_FIELDS = ('choice', 'probabilities', 'confidence', 'noul', 'score')


class CassetteMiss(KeyError):
    """Replay mode met a (text, questions) pair the cassette has no answer for."""

    def __init__(self, key: str, text: str):
        super().__init__(key)
        self.key, self.text = key, text

    def __str__(self):
        return f'no recorded Jev answer for {self.text[:80]!r} (key {self.key[:12]})'


def question_spec(q) -> dict:
    """What a question asks, as plain data: its type, instructions and criteria."""
    if isinstance(q, dict):
        return {k: q.get(k) for k in ('type', 'instructions', 'criteria')}
    return {'type': getattr(q, 'type', type(q).__name__.lower()), 'instructions': getattr(q, 'instructions', None),
            'criteria': getattr(q, 'criteria', None)}


def key_for(text: str, questions: dict) -> str:
    spec = json.dumps({name: question_spec(questions[name]) for name in sorted(questions)}, sort_keys=True,
                      ensure_ascii=False, default=str)
    return hashlib.sha256((str(text) + spec).encode('utf-8')).hexdigest()


def answer_data(a) -> dict:
    """One answer object as stored: only the fields the router reads."""
    out = {}
    for f in ANSWER_FIELDS:
        v = a.get(f) if isinstance(a, dict) else getattr(a, f, None)
        if v is None:
            continue
        if f == 'probabilities':
            v = {str(k): round(float(p), 6) for k, p in dict(v).items()}
        elif f in ('confidence', 'noul', 'score'):
            v = round(float(v), 6)
        out[f] = v
    return out


def reply_of(entry: dict):
    """The stored answer as the object system_one returns: answers (a dict), usage.input_tokens and model."""
    answers = {name: NS(**{f: a.get(f) for f in ANSWER_FIELDS}) for name, a in (entry.get('answers') or {}).items()}
    return NS(answers=answers, usage=NS(input_tokens=int(entry.get('input_tokens') or 0)), model=entry.get('model') or '')


class JevCassette:
    """See the module docstring. `path` may be None (an in-memory cassette, for tests)."""

    def __init__(self, inner, path=None, mode: str = 'live'):
        if mode not in MODES:
            raise ValueError(f'mode must be one of {", ".join(MODES)}')
        self.inner, self.path, self.mode = inner, Path(path) if path else None, mode
        self.entries: dict[str, dict] = {}
        self.misses: list[str] = []      # texts replay could not answer, in order
        self.recorded = 0                # new entries written by this instance
        self.hits = 0
        self.lock = threading.Lock()
        if self.path is not None and self.path.exists():
            self.load()

    def load(self):
        for line in self.path.read_text(encoding='utf-8').splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from an interrupted recording
            if isinstance(e, dict) and isinstance(e.get('key'), str):
                self.entries[e['key']] = e

    def __len__(self):
        return len(self.entries)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def view(self) -> 'CassetteView':
        """A handle for one eval run that shares this cassette but keeps its own misses, so a miss is charged to the
        case that met it even when cases run side by side."""
        return CassetteView(self)

    def write(self, entry: dict):
        with self.lock:
            self.entries[entry['key']] = entry
            self.recorded += 1
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n')

    async def system_one(self, text, questions, _misses=None):
        if self.mode == 'live':
            return await self.inner.system_one(text, questions)
        key = key_for(text, questions)
        entry = self.entries.get(key)
        if entry is not None:
            self.hits += 1
            return reply_of(entry)
        if self.mode == 'replay':
            self.misses.append(text)
            if _misses is not None:
                _misses.append(text)
            raise CassetteMiss(key, str(text))
        if self.inner is None:
            raise CassetteMiss(key, str(text))
        r = await self.inner.system_one(text, questions)
        answers = r.answers if isinstance(r.answers, dict) else dict(r.answers)
        self.write({'key': key, 'text': str(text), 'answers': {n: answer_data(a) for n, a in answers.items()},
                    'model': getattr(r, 'model', '') or '',
                    'input_tokens': int(getattr(getattr(r, 'usage', None), 'input_tokens', 0) or 0)})
        return reply_of(self.entries[key])


class CassetteView:
    """JevCassette.view(): the same answers, this run's own misses."""

    def __init__(self, cassette: JevCassette):
        self.cassette, self.misses = cassette, []

    def __getattr__(self, name):
        return getattr(self.cassette, name)

    async def system_one(self, text, questions):
        return await self.cassette.system_one(text, questions, _misses=self.misses)
