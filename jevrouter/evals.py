"""Evals: run evals/cases.jsonl (plus the git-ignored evals/cases.local.jsonl of promoted labels) through the real
pipeline and score each final run record.

A case passes when every expectation holds: each `expect_agents` entry was chosen for some subtask, the outcome
(clarify / blocked / answer) matches, and the final answer matches `must_match` and not `must_not_match`
(case-insensitive regexes). `silent_wrong` counts failures where every subtask still reported ok=true: the answers a
user would trust without a warning.

Optional case keys (docs/PLAN-speed-evals-chat.md, "Eval cases"): `split` (dev or holdout), `turns` (a multi-turn
conversation run in one session, each turn with its own expectations), `files` (fixtures under evals/fixtures/,
uploaded through the same store path as a user's upload and attached to every turn), `judge` + `judge_min` (a rubric
scored by an LLM judge, see jevrouter/judge.py; a judge that fails fails the case, "not judged"), `max_ms` +
`strict_ms` (a latency budget; strict only on keyless runs, the budgets are keyless ones) and `paraphrases` (extra
phrasings, each run as its own attempt with the same expectations). An eval can repeat every attempt (1-5 times); a
case passes only when all its attempts pass, and is flaky when the repeats of one phrasing disagree (a paraphrase that
always fails is a plain failure, not flakiness).

CLI: .venv/bin/python -m jevrouter.evals [--engine NAME|none] [--examples on|off] [--split dev|holdout|all]
     [--tags a,b] [--repeat N] [--judge NAME|auto] [--matrix] [--json] [--save-baseline]
It prints a table and exits 1 if a case that passes in evals/baseline.json (for the same engine) now fails.
"""
import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
import uuid
import weakref

from . import judge as judge_mod
from .config import ROOT

CASES = ROOT / 'evals' / 'cases.jsonl'
LOCAL_CASES = ROOT / 'evals' / 'cases.local.jsonl'  # labels promoted to cases; personal, so git-ignored
FIXTURES = ROOT / 'evals' / 'fixtures'
BASELINE = ROOT / 'evals' / 'baseline.json'
CONCURRENCY = 2
MAX_REPEAT = 5
SPLITS = ('dev', 'holdout', 'all')
OUTCOMES = ('clarify', 'blocked', 'answer')
EXPECT_KEYS = ('expect_agents', 'expect_outcome', 'must_match', 'must_not_match', 'expect_file')
FILE_FORMATS = ('pdf', 'docx', 'pptx', 'xlsx', 'md')  # jevrouter/create/spec.py FORMATS
FILE_KEYS = {'format', 'contains', 'min_pages', 'max_pages', 'slides_min', 'sheets', 'charts_min', 'rules_ok'}
# X2: the only functions a created spreadsheet may compute with; anything else is an injected formula.
SAFE_FORMULA = re.compile(r'^=(?:[A-Z]+\d+|[A-Z]+\d+:[A-Z]+\d+|(?:SUM|AVERAGE|MIN|MAX|COUNT|ROUND)\(|[\s\d.,+\-*/()])+$', re.I)
TURN_KEYS = {'query', *EXPECT_KEYS}
CASE_KEYS = {'id', 'query', 'tags', 'split', 'turns', 'files', 'judge', 'judge_min', 'max_ms', 'strict_ms', 'paraphrases',
             'note', *EXPECT_KEYS}
CASE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
# Summary fields beyond the evals table's own columns, kept as JSON in its `extra` column.
EXTRA_KEYS = ('split', 'repeat', 'judge', 'tags', 'by_tag', 'p50_ms', 'p95_ms', 'flaky', 'judge_mean', 'judge_errors')


class CaseError(ValueError):
    pass


# ---------- cases ----------

def check_expectations(d: dict, where: str) -> list[str]:
    errors = []
    agents = d.get('expect_agents')
    if agents is not None and not (isinstance(agents, list) and agents and all(isinstance(a, str) and a for a in agents)):
        errors.append(f'{where}expect_agents must be a non-empty list of agent names')
    if 'expect_outcome' in d and d['expect_outcome'] not in OUTCOMES:
        errors.append(f'{where}expect_outcome must be one of {", ".join(OUTCOMES)}')
    for k in ('must_match', 'must_not_match'):
        if k in d:
            try:
                re.compile(d[k])
            except (re.error, TypeError) as e:
                errors.append(f'{where}{k} is not a valid regex ({e})')
    if 'expect_file' in d:
        errors += check_file_expectation(d['expect_file'], where)
    return errors


def check_file_expectation(f, where: str) -> list[str]:
    """expect_file is false (no file may be created) or {format, contains?, min_pages?, max_pages?, slides_min?,
    sheets?, charts_min?, rules_ok?}."""
    if f is False:
        return []
    if not isinstance(f, dict):
        return [f'{where}expect_file must be an object or false']
    errors = []
    if unknown := sorted(set(f) - FILE_KEYS):
        errors.append(f'{where}expect_file has unknown key {unknown[0]!r}')
    if f.get('format') not in FILE_FORMATS:
        errors.append(f'{where}expect_file format must be one of {", ".join(FILE_FORMATS)}')
    for k in ('contains', 'sheets'):
        v = f.get(k, [])
        if not (isinstance(v, list) and all(isinstance(x, str) and x for x in v)):
            errors.append(f'{where}expect_file {k} must be a list of regexes')
            continue
        for x in v:
            try:
                re.compile(x)
            except re.error as e:
                errors.append(f'{where}expect_file {k} has a bad regex {x!r} ({e})')
    for k in ('min_pages', 'max_pages', 'slides_min', 'charts_min'):
        if k in f and not (type(f[k]) is int and f[k] >= 0):
            errors.append(f'{where}expect_file {k} must be a whole number')
    if 'rules_ok' in f and not isinstance(f['rules_ok'], bool):
        errors.append(f'{where}expect_file rules_ok must be true or false')
    return errors


def validate_case(c, strict: bool = False) -> list[str]:
    """Everything wrong with one case, as plain sentences; [] when it's valid. strict (used for the committed suite's
    own test) also wants every case to expect something, so no case passes by merely finishing."""
    if not isinstance(c, dict):
        return ['a case must be a JSON object']
    errors = []
    if not (isinstance(c.get('id'), str) and CASE_ID.match(c['id'])):
        errors.append('id must be 1-64 letters, digits, _ . or -')
    if unknown := sorted(set(c) - CASE_KEYS):
        errors.append(f'unknown key {unknown[0]!r}')
    if not (isinstance(c.get('tags', []), list) and all(isinstance(t, str) and t for t in c.get('tags', []))):
        errors.append('tags must be a list of strings')
    if c.get('split', 'dev') not in ('dev', 'holdout'):
        errors.append('split must be dev or holdout')
    turns = c.get('turns')
    if turns is not None:
        if not (isinstance(turns, list) and len(turns) >= 2 and all(isinstance(t, dict) for t in turns)):
            errors.append('turns must be a list of at least two objects')
        else:
            for i, t in enumerate(turns, 1):
                if unknown := sorted(set(t) - TURN_KEYS):
                    errors.append(f'turn {i}: unknown key {unknown[0]!r}')
                if not (isinstance(t.get('query'), str) and t['query'].strip()):
                    errors.append(f'turn {i}: query must be a non-empty string')
                errors += check_expectations(t, f'turn {i}: ')
            if strict and not any(k in t for t in turns for k in EXPECT_KEYS) and 'judge' not in c:
                errors.append('at least one turn needs an expectation')
        if any(k in c for k in EXPECT_KEYS):
            errors.append('a multi-turn case keeps its expectations on its turns')
        if 'paraphrases' in c:
            errors.append('a multi-turn case cannot have paraphrases')
        if 'query' in c and not (isinstance(c['query'], str) and c['query'].strip()):
            errors.append('query must be a non-empty string')
    else:
        if not (isinstance(c.get('query'), str) and c['query'].strip()):
            errors.append('query must be a non-empty string')
        errors += check_expectations(c, '')
        if strict and not any(k in c for k in (*EXPECT_KEYS, 'judge')):
            errors.append('a case needs at least one expectation (expect_agents, expect_outcome, must_match, '
                          'must_not_match or judge)')
    files = c.get('files')
    if files is not None:
        if not (isinstance(files, list) and files and all(isinstance(f, str) for f in files)):
            errors.append('files must be a non-empty list of fixture names')
        else:
            for f in files:
                if '/' in f or '\\' in f or f.startswith('.') or not (FIXTURES / f).is_file():
                    errors.append(f'no fixture evals/fixtures/{f}')
    if 'judge' in c and not (isinstance(c['judge'], str) and c['judge'].strip()):
        errors.append('judge must be the rubric text')
    if 'judge_min' in c:
        if 'judge' not in c:
            errors.append('judge_min needs a judge rubric')
        elif not (isinstance(c['judge_min'], (int, float)) and not isinstance(c['judge_min'], bool) and 1 <= c['judge_min'] <= 5):
            errors.append('judge_min must be a number from 1 to 5')
    if 'max_ms' in c and not (type(c['max_ms']) is int and c['max_ms'] > 0):
        errors.append('max_ms must be a positive whole number of milliseconds')
    if 'strict_ms' in c and (not isinstance(c['strict_ms'], bool) or 'max_ms' not in c):
        errors.append('strict_ms must be true or false and needs max_ms')
    p = c.get('paraphrases')
    if p is not None and not (isinstance(p, list) and p and all(isinstance(x, str) and x.strip() for x in p)
                              and len(set(p)) == len(p) and c.get('query') not in p):
        errors.append('paraphrases must be a list of distinct non-empty strings, other than the query')
    return errors


def read_cases(path) -> list[dict]:
    """A cases file, every line validated. Raises CaseError naming the first bad line and what's wrong with it."""
    out, seen = [], set()
    for n, line in enumerate(open(path), 1):
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError as e:
            raise CaseError(f'{path.name if hasattr(path, "name") else path} line {n}: not JSON ({e.msg})')
        errors = validate_case(c)
        if not errors and c['id'] in seen:
            errors = [f'duplicate id {c["id"]!r}']
        if errors:
            cid = c.get('id') if isinstance(c, dict) else None
            raise CaseError(f'{getattr(path, "name", path)} line {n}{f" ({cid})" if cid else ""}: {"; ".join(errors)}')
        seen.add(c['id'])
        out.append(c)
    return out


def load_cases(path=None) -> list[dict]:
    """The committed cases, then the local ones. The local file is optional and a bad line in it is skipped. When an id
    appears twice the first one wins, so a local case can never replace a committed one."""
    if path is not None:
        return read_cases(path)
    cases, seen = [], set()
    for c in [*read_cases(CASES), *local_cases()]:
        if c['id'] not in seen:
            seen.add(c['id'])
            cases.append(c)
    return cases


def local_cases() -> list[dict]:
    if not LOCAL_CASES.exists():
        return []
    out = []
    for line in LOCAL_CASES.read_text().splitlines():
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(c, dict) and isinstance(c.get('id'), str) and isinstance(c.get('query'), str) and c['query'].strip():
            c = {**c, 'tags': c.get('tags') if isinstance(c.get('tags'), list) else []}
            if not validate_case(c):
                out.append(c)
    return out


def select(cases: list[dict], split: str = 'all', tags=None) -> list[dict]:
    """Cases in `split` (dev, holdout or all) that carry any of `tags` (None or empty: every tag)."""
    wanted = set(tags or ())
    return [c for c in cases if (split == 'all' or c.get('split', 'dev') == split)
            and (not wanted or wanted & set(c.get('tags', [])))]


def case_query(c: dict) -> str:
    return c.get('query') or c['turns'][0]['query']


def case_kind(c: dict) -> str:
    return 'multi_turn' if c.get('turns') else 'file' if c.get('files') else 'judge' if c.get('judge') else 'single'


# ---------- scoring ----------

def outcome(rec: dict) -> str:
    agents = [t.get('agent') for t in rec.get('tasks', [])]
    if 'blocked' in agents:
        return 'blocked'
    if 'clarify' in agents:
        return 'clarify'
    return 'answer' if any(t.get('ok') for t in rec.get('tasks', [])) else 'error'


def answer_of(rec: dict) -> str:
    return (rec.get('merged') or {}).get('answer') or rec.get('error') or ''


def check(expect: dict, rec: dict) -> list[str]:
    """Why a run record misses these expectations ([] when it meets them)."""
    agents = [t['agent'] for t in rec.get('tasks', []) if t.get('agent')]
    answer = answer_of(rec)
    reasons = []
    if rec.get('status') != 'done':
        reasons.append(f"run ended {rec.get('status')}")
    for a in expect.get('expect_agents') or []:
        if a not in agents:
            reasons.append(f'expected agent {a}, got {", ".join(agents) or "none"}')
    if expect.get('expect_outcome') and outcome(rec) != expect['expect_outcome']:
        reasons.append(f"expected outcome {expect['expect_outcome']}, got {outcome(rec)}")
    if expect.get('must_match') and not re.search(expect['must_match'], answer, re.I):
        reasons.append(f"answer does not match /{expect['must_match']}/")
    if expect.get('must_not_match') and re.search(expect['must_not_match'], answer, re.I):
        reasons.append(f"answer matches forbidden /{expect['must_not_match']}/")
    return reasons


def score(case: dict, rec: dict) -> dict:
    """One scored single-run case, in the shape GET /api/evals/{id} returns."""
    tasks = rec.get('tasks', [])
    reasons = check(case, rec)
    return {'id': case['id'], 'query': case_query(case), 'tags': case.get('tags', []), 'pass': not reasons,
            'reasons': reasons, 'agents': [t['agent'] for t in tasks if t.get('agent')], 'answer': answer_of(rec),
            'ms': rec.get('total_ms'), 'qid': rec.get('qid'),
            'expect_agents': case.get('expect_agents'), 'expect_outcome': case.get('expect_outcome'),
            'all_ok': bool(tasks) and all(t.get('ok') for t in tasks),
            'jev_tokens': (rec.get('tokens') or {}).get('jev_in', 0)}


# ---------- created files (docs/PLAN-files.md, "Evals") ----------

def created_files(rec: dict) -> list[dict]:
    """Every CreatedFile the run's steps made, in step order."""
    return [f for t in rec.get('tasks', []) for f in (t.get('created_files') or []) if isinstance(f, dict)]


def read_created(store, fid: str) -> bytes:
    """A created file's bytes from the store, by id only (X5): the store's own reader when it has one, else the plan's
    data/created/<id> next to the uploads."""
    for name in ('created_raw', 'created_bytes', 'get_created_bytes'):
        if callable(getattr(store, name, None)):
            return getattr(store, name)(fid)
    if not CASE_ID.match(fid):
        raise ValueError(f'bad created file id {fid!r}')
    base = getattr(store, 'created_dir', None) or store.files_dir.parent / 'created'
    from pathlib import Path
    return (Path(base) / fid).read_bytes()


def reopen(fmt: str, data: bytes) -> dict:
    """A created file reopened with its own library (V1): its text, and its pages, slides, sheets and charts. Raises
    when the file does not open."""
    import io
    buf = io.BytesIO(data)
    out = {'text': '', 'pages': None, 'slides': None, 'sheets': [], 'charts': 0, 'formulas': []}
    if fmt == 'md':
        out['text'] = data.decode('utf-8')
    elif fmt == 'pdf':
        from pypdf import PdfReader
        r = PdfReader(buf)
        out['pages'] = len(r.pages)
        title = (r.metadata or {}).get('/Title') or ''
        out['text'] = '\n'.join([str(title), *((p.extract_text() or '') for p in r.pages)])
    elif fmt == 'docx':
        import docx
        d = docx.Document(buf)
        parts = [d.core_properties.title or '', *(p.text for p in d.paragraphs)]
        parts += [c.text for t in d.tables for row in t.rows for c in row.cells]
        out['text'] = '\n'.join(parts)
    elif fmt == 'pptx':
        from pptx import Presentation
        prs = Presentation(buf)
        out['slides'] = len(prs.slides)
        parts = []
        for slide in prs.slides:
            for sh in slide.shapes:
                if sh.has_text_frame:
                    parts.append(sh.text_frame.text)
                if getattr(sh, 'has_table', False) and sh.has_table:
                    parts += [c.text for row in sh.table.rows for c in row.cells]
                if getattr(sh, 'has_chart', False) and sh.has_chart:
                    out['charts'] += 1
                    ch = sh.chart
                    if ch.has_title and ch.chart_title.has_text_frame:
                        parts.append(ch.chart_title.text_frame.text)
                    parts += [str(c) for c in ch.plots[0].categories] if len(ch.plots) else []
            if slide.has_notes_slide:
                parts.append(slide.notes_slide.notes_text_frame.text)
        out['text'] = '\n'.join(parts)
        # X2 in slides too: each chart's data is an embedded workbook, where an "=..." label would be a live formula
        from .create.rules import _embedded_formulas
        out['formulas'] += ['=' + f for f in _embedded_formulas(data)]
    elif fmt == 'xlsx':
        import openpyxl
        wb = openpyxl.load_workbook(buf)  # formulas as written, so an injected one shows up
        out['sheets'] = wb.sheetnames
        parts = []
        for ws in wb.worksheets:
            out['charts'] += len(getattr(ws, '_charts', []))
            parts.append(ws.title)
            for row in ws.iter_rows():
                for c in row:
                    if c.value is None:
                        continue
                    if c.data_type == 'f':
                        out['formulas'].append(str(c.value))
                    parts.append(str(c.value))
        out['text'] = '\n'.join(parts)
    else:
        raise ValueError(f'unknown format {fmt!r}')
    return out


def check_file(expect, rec: dict, read) -> list[str]:
    """Why the files a run created miss expect_file ([] when they meet it). read(fid) returns a file's bytes. The first
    file in the expected format is scored; expect_file false means no file may be created."""
    files = created_files(rec)
    if expect is False:
        return [f"expected no file, got {', '.join(f.get('name', '?') for f in files)}"] if files else []
    fmt = expect['format']
    f = next((f for f in files if f.get('format') == fmt), None)
    if f is None:
        got = ', '.join(f.get('name') or f.get('format', '?') for f in files) or 'none'
        return [f'expected a {fmt} file, got {got}']
    name = f.get('name') or f.get('id')
    try:
        got = reopen(fmt, read(f['id']))
    except Exception as e:
        return [f'{name} does not reopen ({str(e)[:120]})']
    reasons = []
    for pat in expect.get('contains', []):
        if not re.search(pat, got['text'], re.I):
            reasons.append(f'{name} does not contain /{pat}/')
    pages = got['pages'] if got['pages'] is not None else f.get('pages')
    if 'min_pages' in expect and (pages or 0) < expect['min_pages']:
        reasons.append(f"{name} has {pages} pages, fewer than {expect['min_pages']}")
    if 'max_pages' in expect and pages is not None and pages > expect['max_pages']:
        reasons.append(f"{name} has {pages} pages, more than {expect['max_pages']}")
    if 'slides_min' in expect and (got['slides'] or 0) < expect['slides_min']:
        reasons.append(f"{name} has {got['slides'] or 0} slides, fewer than {expect['slides_min']}")
    for pat in expect.get('sheets', []):
        if not any(re.search(pat, s, re.I) for s in got['sheets']):
            reasons.append(f"{name} has no sheet like /{pat}/ (sheets: {', '.join(got['sheets']) or 'none'})")
    if 'charts_min' in expect and got['charts'] < expect['charts_min']:
        reasons.append(f"{name} has {got['charts']} charts, fewer than {expect['charts_min']}")
    # X2 always: a formula outside the allow-list means a cell's text was written as a live formula
    if bad := [x for x in got['formulas'] if not SAFE_FORMULA.match(x)]:
        reasons.append(f'{name} has an unsafe formula {bad[0][:40]!r}')
    if expect.get('rules_ok'):
        failed = [r.get('id', '?') for r in f.get('rules') or []
                  if not r.get('ok') and r.get('severity') in ('block', 'warn')]
        if failed:
            reasons.append(f"{name} failed rules {', '.join(failed)}")
    return reasons


def turn_result(t: dict, rec: dict, read=None) -> dict:
    s = score({'id': '', **t}, rec)
    if 'expect_file' in t:
        if read is None:
            s['reasons'].append('created files cannot be checked here')
        else:
            s['reasons'] += check_file(t['expect_file'], rec, read)
        s['pass'] = not s['reasons']
    out = {k: s[k] for k in ('query', 'pass', 'reasons', 'answer', 'agents', 'ms', 'qid', 'all_ok', 'jev_tokens')}
    # the engines that wrote this answer (steps and merge), so the judge can be one that didn't
    out['engines'] = sorted({e for e in [*(t.get('engine') for t in rec.get('tasks', [])),
                                         (rec.get('merged') or {}).get('engine')] if e and e != 'keyless'})
    return out


def attempt_label(n: int, phrasing: int, repeat: int, phrasings: int) -> str:
    parts = ([f'run {n + 1}'] if repeat > 1 else []) + ([f'paraphrase {phrasing}'] if phrasing else
                                                          ['original'] if phrasings > 1 else [])
    return ', '.join(parts)


def combine(case: dict, attempts: list[dict]) -> dict:
    """One case's result from its attempts. The shown answer, agents and turns are the first failing attempt's (or the
    first attempt's when all passed); the case passes only when every attempt passed."""
    shown = next((a for a in attempts if not a['pass']), attempts[0])
    turns = shown['turns']
    reasons = []
    for a in attempts:
        for r in a['reasons']:
            r = f"{a['label']}: {r}" if a['label'] else r
            if r not in reasons:
                reasons.append(r)
    out = {'id': case['id'], 'query': case_query(case), 'tags': case.get('tags', []), 'pass': all(a['pass'] for a in attempts),
           'reasons': reasons, 'agents': [a for t in turns for a in t['agents']], 'answer': turns[-1]['answer'],
           'ms': sum(t['ms'] or 0 for t in turns), 'qid': turns[-1]['qid'],
           'expect_agents': case.get('expect_agents'), 'expect_outcome': case.get('expect_outcome'),
           'all_ok': all(t['all_ok'] for t in turns), 'jev_tokens': sum(t['jev_tokens'] or 0 for t in turns),
           'kind': case_kind(case), 'split': case.get('split', 'dev'),
           'judge': shown['judge'] if shown['judge'] else next((a['judge'] for a in attempts if a['judge']), None),
           'max_ms': case.get('max_ms'), 'over_budget': any(a['over_budget'] for a in attempts),
           # flaky: the same text passed on one run and failed on another (paraphrases are compared to themselves)
           'flaky': any(len({a['pass'] for a in attempts if a.get('phrasing', 0) == i}) > 1
                        for i in {a.get('phrasing', 0) for a in attempts})}
    if err := next((a['judge_error'] for a in attempts if a.get('judge_error')), None):
        out['judge_error'] = err
    if case.get('turns'):
        keep = ('query', 'pass', 'reasons', 'answer', 'agents', 'ms', 'qid')
        out['turns'] = [{k: t[k] for k in keep} for t in turns]
    if sum(1 for a in attempts if a.get('phrasing', 0) == 0) > 1:  # repeat > 1
        out['attempts'] = [a['pass'] for a in attempts]
    return out


def percentile(values: list, p: float) -> int | None:
    """Nearest-rank percentile of the numbers in `values` (None when there are none)."""
    v = sorted(x for x in values if isinstance(x, (int, float)))
    return round(v[max(0, math.ceil(p / 100 * len(v)) - 1)]) if v else None


def summary(e: dict) -> dict:
    """The stored and returned form of an eval: its fields plus the counts. Private working keys (_ms) are dropped."""
    cases = e['cases']
    passed = sum(c['pass'] for c in cases)
    by_tag: dict[str, dict] = {}
    for c in cases:
        for t in c.get('tags', []):
            s = by_tag.setdefault(t, {'passed': 0, 'total': 0})
            s['total'] += 1
            s['passed'] += bool(c['pass'])
    ms = e.get('_ms') or [t['ms'] for c in cases for t in (c.get('turns') or [c])]
    judged = [c['judge']['mean'] for c in cases if c.get('judge')]
    return {**{k: v for k, v in e.items() if not k.startswith('_')}, 'done': len(cases), 'passed': passed,
            'accuracy': round(passed / e['total'], 4) if e['total'] else 0.0,
            'silent_wrong': sum(1 for c in cases if not c['pass'] and c['all_ok'] and not c.get('judge_error')),
            'by_tag': dict(sorted(by_tag.items())), 'p50_ms': percentile(ms, 50), 'p95_ms': percentile(ms, 95),
            'flaky': sum(1 for c in cases if c.get('flaky')),
            'judge_mean': round(sum(judged) / len(judged), 2) if judged else None,
            'judge_errors': [f"{c['id']}: {c['judge_error']}" for c in cases if c.get('judge_error')]}


# ---------- storage (the evals table's `extra` column, added here on first use) ----------

_migrated: 'weakref.WeakSet' = weakref.WeakSet()


def migrate(store):
    """Adds the `extra` JSON column to an older database's evals table. Safe to call again."""
    if store in _migrated:
        return
    if 'extra' not in {r['name'] for r in store.q('PRAGMA table_info(evals)')}:
        store.x('ALTER TABLE evals ADD COLUMN extra TEXT')
    _migrated.add(store)


def save(store, s: dict):
    migrate(store)
    store.save_eval(s)
    store.x('UPDATE evals SET extra = ? WHERE id = ?', json.dumps({k: s[k] for k in EXTRA_KEYS if k in s}), s['eval_id'])


def extras_of(store, ids: list[str]) -> dict[str, dict]:
    migrate(store)
    if not ids:
        return {}
    rows = store.q(f'SELECT id, extra FROM evals WHERE id IN ({",".join("?" * len(ids))})', *ids)
    out = {}
    for r in rows:
        try:
            d = json.loads(r['extra']) if r['extra'] else {}
        except ValueError:
            d = {}
        out[r['id']] = d if isinstance(d, dict) else {}
    return out


def get(store, eid: str) -> dict | None:
    """A stored eval with its cases and extra fields (older evals simply have none)."""
    e = store.get_eval(eid)
    return {**e, **extras_of(store, [eid]).get(eid, {})} if e else None


def listed(store, limit: int = 50) -> list[dict]:
    evals = store.list_evals(limit)
    extra = extras_of(store, [e['eval_id'] for e in evals])
    return [{**e, **extra.get(e['eval_id'], {})} for e in evals]


# ---------- running ----------

def start(router, engine, engine_name: str | None, cases: list[dict] | None = None, examples: bool | None = None, *,
          split: str = 'all', tags=None, repeat: int = 1, judge=None) -> str:
    """Starts an eval in the background and returns its id. engine: an Engine or None (keyless). examples forces route
    examples on or off for this eval's runs only; None uses the global switch as it is now. split and tags pick the
    cases; repeat runs every attempt that many times; judge is the Engine that scores rubric cases (None: not judged)."""
    eid = uuid.uuid4().hex[:10]
    picked = select(load_cases() if cases is None else cases, split, tags)
    task = asyncio.create_task(run_eval(router, eid, engine, engine_name, picked, examples, split=split, tags=tags,
                                        repeat=repeat, judge=judge))
    router.evals[eid] = task
    task.add_done_callback(lambda t: router.evals.pop(eid, None))
    return eid


class Fixtures:
    """Fixture files uploaded once per eval through the store, like a user's upload, and deleted when it ends."""

    def __init__(self, store):
        self.store, self.ids, self.lock = store, {}, asyncio.Lock()

    async def ids_for(self, names) -> list[str]:
        from .files import extract
        async with self.lock:
            for name in names or ():
                if name not in self.ids:
                    raw = (FIXTURES / name).read_bytes()
                    meta, text = await asyncio.to_thread(extract, name, raw)
                    self.store.add_file(meta, raw, text)
                    self.ids[name] = meta['id']
        return [self.ids[n] for n in names or ()]

    def remove(self):
        for fid in self.ids.values():
            self.store.delete_file(fid)


async def run_attempt(router, ctx: dict, case: dict, text: str, label: str) -> dict:
    """One attempt at a case: every turn in order (one session when there are several), then the judge and budget."""
    turns = case.get('turns') or [{'query': text, **{k: case[k] for k in EXPECT_KEYS if k in case}}]
    session_id = f'eval-{ctx["eid"]}-{uuid.uuid4().hex[:8]}' if len(turns) > 1 else None
    files = await ctx['fixtures'].ids_for(case.get('files'))
    results = []
    try:
        for t in turns:
            qid = router.submit(t['query'], 'eval', session_id=session_id, engine=ctx['engine'], files=files,
                                examples=ctx['examples'], extras={'holdout': t['query']})
            ctx['qids'].add(qid)
            await router.running[qid]
            ctx['qids'].discard(qid)
            rec = router.get_run(qid)
            ctx['ms'].append(rec.get('total_ms'))
            if 'expect_file' in t:  # reopening a file is blocking work
                results.append(await asyncio.to_thread(turn_result, t, rec, lambda fid: read_created(router.store, fid)))
            else:
                results.append(turn_result(t, rec))
    finally:
        if session_id:  # the runs stay (eval detail links to them); the chat list must not fill with eval sessions
            router.store.x('DELETE FROM sessions WHERE id = ?', session_id)
    reasons = [f'turn {i}: {r}' if len(turns) > 1 else r for i, t in enumerate(results, 1) for r in t['reasons']]
    over = [t['ms'] for t in results if case.get('max_ms') and (t['ms'] or 0) > case['max_ms']]
    if over and case.get('strict_ms') and ctx['engine'] is None:  # the budgets are keyless ones (the plan's "< 2 s keyless")
        reasons.append(f"took {max(over)} ms, over the {case['max_ms']} ms budget")
    score_ = judge_error = None
    if case.get('judge') and ctx['judge'] is not None:
        question = '\n'.join(f'User: {t["query"]}' for t in turns)
        answered = {e for t in results for e in t['engines']}
        try:
            score_ = await judge_mod.grade(judge_mod.avoiding(ctx['judge'], answered, router.engines), question,
                                           results[-1]['answer'], case['judge'])
        except judge_mod.JudgeError as e:
            # the rubric could not be checked, so the case can't pass on its regexes alone
            judge_error = str(e)[:200]
            reasons.append(f'not judged: {judge_error}')
        if score_ and score_['mean'] < case.get('judge_min', judge_mod.JUDGE_MIN):
            reasons.append(f"judge mean {score_['mean']:g} is below {case.get('judge_min', judge_mod.JUDGE_MIN):g}"
                           + (f" ({score_['note']})" if score_['note'] else ''))
    return {'label': label, 'pass': not reasons, 'reasons': reasons, 'turns': results, 'judge': score_,
            'judge_error': judge_error, 'over_budget': bool(over)}


async def run_case(router, ctx: dict, case: dict) -> dict:
    texts = [case_query(case), *case.get('paraphrases', [])] if not case.get('turns') else [case_query(case)]
    attempts = []
    for n in range(ctx['repeat']):
        for i, text in enumerate(texts):
            label = attempt_label(n, i, ctx['repeat'], len(texts))
            try:
                attempts.append({**await run_attempt(router, ctx, case, text, label), 'phrasing': i})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a broken fixture or harness bug fails this case, not the whole eval
                attempts.append({'label': label, 'pass': False, 'reasons': [f'eval harness error: {str(e)[:160]}'],
                                 'turns': [{'query': text, 'pass': False, 'reasons': [], 'answer': '', 'agents': [],
                                            'ms': None, 'qid': None, 'all_ok': False, 'jev_tokens': 0, 'engines': []}],
                                 'judge': None, 'over_budget': False, 'phrasing': i})
    return combine(case, attempts)


async def run_eval(router, eid: str, engine, engine_name, cases: list[dict], examples: bool | None = None, *,
                   split: str = 'all', tags=None, repeat: int = 1, judge=None) -> dict:
    examples = router.route_examples if examples is None else bool(examples)  # fixed for the whole eval
    repeat = max(1, min(MAX_REPEAT, int(repeat)))
    e = {'eval_id': eid, 'at': time.time(), 'engine': engine_name, 'status': 'running', 'total': len(cases), 'cases': [],
         'examples': examples, 'split': split, 'repeat': repeat, 'judge': judge.name if judge else None,
         'tags': list(tags) if tags else None, '_ms': []}
    save(router.store, summary(e))
    sem = asyncio.Semaphore(CONCURRENCY)
    ctx = {'eid': eid, 'engine': engine, 'examples': examples, 'repeat': repeat, 'judge': judge, 'qids': set(),
           'ms': e['_ms'], 'fixtures': Fixtures(router.store)}

    async def one(case):
        async with sem:  # cases start in file order, two at a time
            e['cases'].append(await run_case(router, ctx, case))
            s = summary(e)
            save(router.store, s)
            router.bus.emit('eval_progress', eval_id=eid, done=s['done'], total=s['total'], passed=s['passed'])

    try:
        await asyncio.gather(*(one(c) for c in cases))
        e['status'] = 'done'
    except asyncio.CancelledError:
        for qid in list(ctx['qids']):
            router.cancel(qid)
        e['status'] = 'cancelled'
    except Exception:
        e['status'] = 'error'
    finally:
        ctx['fixtures'].remove()
    order = {c['id']: i for i, c in enumerate(cases)}
    e['cases'].sort(key=lambda c: order.get(c['id'], 0))
    s = summary(e)
    save(router.store, s)
    router.bus.emit('eval_done', eval_id=eid, passed=s['passed'], total=s['total'], accuracy=s['accuracy'], status=e['status'])
    return s


def side(e: dict) -> dict:
    tokens = [c['jev_tokens'] for c in e['cases'] if isinstance(c.get('jev_tokens'), (int, float))]
    return {'eval_id': e['eval_id'], 'engine': e['engine'], 'examples': bool(e.get('examples')), 'accuracy': e['accuracy'],
            'passed': e['passed'], 'total': e['total'], 'silent_wrong': e['silent_wrong'],
            'mean_jev_tokens': round(sum(tokens) / len(tokens), 1) if tokens else 0.0}


def compare(a: dict, b: dict) -> dict:
    """Two evals side by side (GET /api/evals/compare): each side's summary and every case in either, a's order first.
    a_pass / b_pass is None where that eval didn't score the case."""
    pa, pb = ({c['id']: c for c in e['cases']} for e in (a, b))
    ids = list(dict.fromkeys([*pa, *pb]))
    return {'a': side(a), 'b': side(b),
            'cases': [{'id': i, 'query': (pa.get(i) or pb[i])['query'], 'a_pass': pa[i]['pass'] if i in pa else None,
                       'b_pass': pb[i]['pass'] if i in pb else None} for i in ids]}


def regressions(result: dict, baseline: dict) -> list[str]:
    before = baseline.get(result['engine'] or 'none') or {}
    return [c['id'] for c in result['cases'] if before.get(c['id']) and not c['pass']]


# ---------- CLI ----------

def print_table(result: dict):
    for c in result['cases']:
        marks = ''.join(m for m, on in ((' flaky', c.get('flaky')), (' slow', c.get('over_budget'))) if on)
        j = f" judge {c['judge']['mean']:g}" if c.get('judge') else ''
        print(f"{'PASS' if c['pass'] else 'FAIL'}  {c['id']:<22} {c['ms'] or 0:>6}ms  {','.join(c['agents'])[:22]:<22} "
              f"{c['query'][:44]:<44}{marks}{j}  {'; '.join(c['reasons'])[:110]}")
    print('\nby split:')  # dev and holdout are reported separately (B3), so a fix tuned on dev shows up honestly
    for split in ('dev', 'holdout'):
        cs = [c for c in result['cases'] if c.get('split', 'dev') == split]
        if cs:
            n = sum(c['pass'] for c in cs)
            print(f"  {split:<16} {n:>3}/{len(cs):<3} {n / len(cs):>5.0%}")
    print('by tag:')
    for tag, t in result['by_tag'].items():
        print(f"  {tag:<16} {t['passed']:>3}/{t['total']:<3} {t['passed'] / t['total']:>5.0%}")


def headline(name: str, r: dict) -> str:
    j = f", judge mean {r['judge_mean']:g} ({r['judge']})" if r.get('judge_mean') is not None else ''
    return (f"{name}: {r['passed']}/{r['total']} passed ({r['accuracy']:.0%}), silent_wrong {r['silent_wrong']}, "
            f"flaky {r['flaky']}, p50 {r['p50_ms']}ms, p95 {r['p95_ms']}ms, split {r['split']}, repeat {r['repeat']}, "
            f"examples {'on' if r['examples'] else 'off'}, mean Jev tokens {side(r)['mean_jev_tokens']:g}{j}")


def matrix_table(results: list[dict]) -> str:
    """Engines as columns: the headline numbers, then accuracy per tag."""
    names = [r['engine'] for r in results]
    rows = [('passed', [f"{r['passed']}/{r['total']}" for r in results]),
            ('accuracy', [f"{r['accuracy']:.0%}" for r in results]),
            ('silent_wrong', [str(r['silent_wrong']) for r in results]),
            ('flaky', [str(r['flaky']) for r in results]),
            ('p50 ms', [str(r['p50_ms']) for r in results]),
            ('p95 ms', [str(r['p95_ms']) for r in results]),
            ('judge mean', ['-' if r.get('judge_mean') is None else f"{r['judge_mean']:g}" for r in results])]
    for tag in sorted({t for r in results for t in r['by_tag']}):
        rows.append((f'tag {tag}', [f"{r['by_tag'][tag]['passed']}/{r['by_tag'][tag]['total']}" if tag in r['by_tag']
                                    else '-' for r in results]))
    w = max(len(k) for k, _ in rows) + 2
    cw = [max(len(n), *(len(v[i]) for _, v in rows)) + 2 for i, n in enumerate(names)]
    lines = [''.ljust(w) + ''.join(n.rjust(cw[i]) for i, n in enumerate(names))]
    lines += [k.ljust(w) + ''.join(v[i].rjust(cw[i]) for i in range(len(names))) for k, v in rows]
    return '\n'.join(lines)


async def cli(engine_name: str | None, save: bool, examples: bool | None = None, *, split: str = 'all',
              tags=None, repeat: int = 1, judge: str | None = None, matrix: bool = False, as_json: bool = False) -> int:
    import aiohttp
    from typesafe_sdk import AsyncTypeSafeClient

    from .config import load_env
    from .engines import catalog, choose
    from .pipeline import Router
    from .store import DEFAULT_DB, Store, read_labels

    load_env()
    engines = catalog()
    if matrix:
        names = ['none', *(n for n, e in engines.items() if n != 'auto' and e.available()[0])]
        print(f"--matrix runs {len(names)} engines ({', '.join(names)}). Every engine other than none spends your "
              'subscription or API quota: about one to three calls per case.', file=sys.stderr)
    else:
        engine = None if engine_name == 'none' else choose(engines, engine_name) if engine_name else choose(engines)
        if engine_name not in (None, 'none') and (engine is None or engine.name != engine_name):
            print(f'engine {engine_name!r} is not available', file=sys.stderr)
            return 2
        names = [engine.name if engine else 'none']
    try:
        every = load_cases()
        cases = select(every, split, tags)
    except CaseError as e:
        print(f'bad eval case: {e}', file=sys.stderr)
        return 2
    if not cases:
        print('no cases match that split and those tags', file=sys.stderr)
        return 2
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    results, code = [], 0
    async with aiohttp.ClientSession() as http:
        for name in names:
            engine = None if name == 'none' else engines[name]
            try:
                judge_engine, note = judge_mod.pick(engines, judge, engine)
            except judge_mod.JudgeError as e:
                print(str(e), file=sys.stderr)
                return 2
            if judge and note:
                print(note, file=sys.stderr)
            router = Router(AsyncTypeSafeClient(), http, engine, engines=engines, store=Store())  # in-memory: CLI evals don't touch the app DB
            ex = router.route_examples if examples is None else examples
            if ex:  # route examples come from the app's labels, read without opening its DB for writing
                router.labels = read_labels(os.environ.get('TG_DB') or DEFAULT_DB)
            result = await run_eval(router, 'cli', engine, name, cases, ex, split=split, tags=tags, repeat=repeat,
                                    judge=judge_engine)
            results.append(result)
            if not as_json:
                print_table(result)
                print('\n' + headline(name, result))
                rubric = sum(1 for c in cases if c.get('judge'))
                if rubric and judge_engine is None:
                    print(f'{rubric} rubric cases were not judged: {note}; pass --judge NAME or --judge auto to score them')
                for err in result.get('judge_errors', []):
                    print(f'judge error: {err}')
            if save:
                # Merged: a --split/--tags run updates its own cases and keeps the others' (cases since removed drop out).
                known = {c['id'] for c in every}
                kept = {i: ok for i, ok in (baseline.get(name) or {}).items() if i in known}
                baseline[name] = {**kept, **{c['id']: c['pass'] for c in result['cases']}}
                BASELINE.write_text(json.dumps(baseline, indent=1, sort_keys=True) + '\n')
                if not as_json:
                    print(f'baseline for {name} saved to {BASELINE}')
                continue
            bad = regressions(result, baseline)
            if bad:
                code = 1
                if not as_json:
                    print(f"regressions against baseline: {', '.join(bad)}")
    if as_json:
        print(json.dumps(results if matrix else results[0], indent=1))
    elif matrix:
        print('\n' + matrix_table(results))
    return code


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog='python -m jevrouter.evals', description='Run the TraceGraph eval cases through the real pipeline.')
    ap.add_argument('--engine', help='engine name, or none for keyless (default: TG_ENGINE / auto)')
    ap.add_argument('--examples', choices=('on', 'off'), help='route examples from your labels on or off (default: TG_ROUTE_EXAMPLES)')
    ap.add_argument('--split', choices=SPLITS, default='all', help='dev, holdout or all cases (default: all)')
    ap.add_argument('--tags', type=lambda s: [t.strip() for t in s.split(',') if t.strip()],
                    help='only cases with any of these comma-separated tags')
    ap.add_argument('--repeat', type=int, default=1, choices=range(1, MAX_REPEAT + 1), metavar='1-5',
                    help='attempts per case; a case is flaky when they disagree (default 1)')
    ap.add_argument('--judge', help='engine that scores rubric cases (a name, or auto for any engine other than the one under test)')
    ap.add_argument('--matrix', action='store_true', help='run keyless and then every available engine, and print a table (spends quota)')
    ap.add_argument('--json', action='store_true', dest='as_json', help='print the result as JSON instead of a table')
    ap.add_argument('--save-baseline', action='store_true', help='record this run as the baseline for its engine')
    args = ap.parse_args(argv)
    if args.matrix and args.engine:
        ap.error('--matrix runs every engine; leave out --engine')
    return args


def main(argv=None) -> int:
    a = parse_args(argv)
    return asyncio.run(cli(a.engine, a.save_baseline, None if a.examples is None else a.examples == 'on', split=a.split,
                           tags=a.tags, repeat=a.repeat, judge=a.judge, matrix=a.matrix, as_json=a.as_json))


if __name__ == '__main__':
    sys.exit(main())
