"""A stub engine for the long-document writer (tests only): it answers the outline call with a plan and each section
call with sections at the word targets it was asked for, off by up to `noise` either way."""
import json
import random
import re

from jevrouter import create as cf
from jevrouter.create.longdoc import OUTLINE_SCHEMA
from jevrouter.engines import Reply

WORDS = ('artificial intelligence is the field of computer science that studies how machines can perform tasks that '
         'normally require human intelligence such as understanding language recognising images making decisions and '
         'learning from experience early researchers believed general machine intelligence was only decades away').split()
ASK = re.compile(r'^- "(?P<h>[^"]+)" \(level (?P<lvl>\d)\): (?:about (?P<n>\d+) words|one slide)(?P<rest>.*)$', re.M)
ADD = re.compile(r'^- "(?P<h>[^"]+)": add about (?P<n>\d+) words$', re.M)
MORE_SLIDES = re.compile(r'Write (\d+) more sections')


def prose(rng: random.Random, n: int) -> list[dict]:
    """Paragraphs of about 110 words that add up to n words."""
    out = []
    while n > 0:
        k = min(n, rng.randint(90, 130))
        words = [rng.choice(WORDS) for _ in range(k)]
        words[0] = words[0].capitalize()
        out.append({'type': 'paragraph', 'text': ' '.join(words) + '.'})
        n -= k
    return out


class LongStub:
    name, label, billing, supports_web = 'stub', 'Stub', 'api', False

    def __init__(self, seed=0, noise=0.3, sections=8, headings=None, fail_sections=False):
        self.rng = random.Random(seed)
        self.noise, self.n, self.headings, self.fail_sections = noise, sections, headings, fail_sections
        self.calls = []

    def available(self):
        return True, ''

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, schema=None,
                     exec=False):
        self.calls.append({'system': system, 'prompt': prompt, 'effort': effort, 'schema': schema,
                           'max_tokens': max_tokens})
        if schema is OUTLINE_SCHEMA:
            heads = self.headings or ['What it is', 'History and origins', 'Main types and fields', 'How it works',
                                      'Uses today', 'Risks and limits', 'What comes next', 'Summary'][:self.n]
            out = {'title': 'Artificial intelligence', 'subtitle': 'What it is and how it began',
                   'sections': [{'heading': h, 'level': 1, 'words': 500, 'blocks_hint': ['paragraph']} for h in heads]}
            return Reply(json.dumps(out), 900, 300)
        if self.fail_sections:
            from jevrouter.engines import EngineError
            raise EngineError('stub down')
        sections = []
        more = MORE_SLIDES.search(prompt)
        if more:
            for i in range(int(more.group(1))):
                sections.append({'heading': f'More {i + 1}', 'level': 1, 'notes': '',
                                 'blocks': [{'type': 'bullets', 'items': ['one point', 'another point'],
                                             'ordered': False}]})
        for m in ADD.finditer(prompt):
            n = round(int(m.group('n')) * (1 + self.rng.uniform(-self.noise, self.noise)))
            sections.append({'heading': m.group('h'), 'level': 1, 'notes': '', 'blocks': prose(self.rng, n)})
        for m in ASK.finditer(prompt):
            if m.group('n'):
                n = round(int(m.group('n')) * (1 + self.rng.uniform(-self.noise, self.noise)))
                blocks = prose(self.rng, n)
            else:
                blocks = [{'type': 'bullets', 'items': ['a short point', 'another short point'], 'ordered': False}]
            rest = m.group('rest')
            if 'timeline block' in rest:
                blocks.insert(1, {'type': 'timeline', 'title': 'Milestones', 'events': [
                    {'date': str(1950 + 10 * i), 'label': f'Milestone {i + 1}'} for i in range(6)]})
            if 'tree block' in rest:
                blocks.insert(1, {'type': 'tree', 'title': 'Fields', 'nodes': [
                    {'id': 'ai', 'parent': '', 'label': 'AI'}, {'id': 'ml', 'parent': 'ai', 'label': 'Machine learning'},
                    {'id': 'dl', 'parent': 'ml', 'label': 'Deep learning'},
                    {'id': 'nlp', 'parent': 'ai', 'label': 'Language'}]})
            if 'flow block' in rest:
                blocks.insert(1, {'type': 'flow', 'title': 'Steps', 'nodes': [
                    {'id': 'a', 'label': 'Data'}, {'id': 'b', 'label': 'Train'}, {'id': 'c', 'label': 'Test'}],
                    'edges': [{'from': 'a', 'to': 'b', 'label': ''}, {'from': 'b', 'to': 'c', 'label': ''}]})
            if 'figure block' in rest:
                blocks.append({'type': 'figure', 'query': f'{m.group("h")} photo', 'caption': f'{m.group("h")}'})
            sections.append({'heading': m.group('h'), 'level': int(m.group('lvl')), 'notes': '', 'blocks': blocks})
        assert schema is cf.DOCSPEC_SCHEMA
        words = sum(len(b.get('text', '').split()) for s in sections for b in s['blocks'])
        return Reply(json.dumps({'title': '', 'subtitle': '', 'sections': sections}), 1500, round(words * 1.4))
