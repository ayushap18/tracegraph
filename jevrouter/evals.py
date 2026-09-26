"""Evals: run evals/cases.jsonl (plus the git-ignored evals/cases.local.jsonl of promoted labels) through the real
pipeline and score each final run record.

A case passes when every expectation holds: each `expect_agents` entry was chosen for some subtask, the outcome
(clarify / blocked / answer) matches, and the final answer matches `must_match` and not `must_not_match`
(case-insensitive regexes). `silent_wrong` counts failures where every subtask still reported ok=true: the answers a
user would trust without a warning.

CLI: .venv/bin/python -m jevrouter.evals [--engine NAME|none] [--examples on|off] [--save-baseline]
It prints a table and exits 1 if a case that passes in evals/baseline.json (for the same engine) now fails.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid

from .config import ROOT

CASES = ROOT / 'evals' / 'cases.jsonl'
LOCAL_CASES = ROOT / 'evals' / 'cases.local.jsonl'  # labels promoted to cases; personal, so git-ignored
BASELINE = ROOT / 'evals' / 'baseline.json'
CONCURRENCY = 2


def load_cases(path=None) -> list[dict]:
    """The committed cases, then the local ones. The local file is optional and a bad line in it is skipped. When an id
    appears twice the first one wins, so a local case can never replace a committed one."""
    if path is not None:
        return [json.loads(line) for line in open(path) if line.strip()]
    cases, seen = [], set()
    for c in [*(json.loads(line) for line in open(CASES) if line.strip()), *local_cases()]:
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
            out.append({**c, 'tags': c.get('tags') if isinstance(c.get('tags'), list) else []})
    return out


def outcome(rec: dict) -> str:
    agents = [t.get('agent') for t in rec.get('tasks', [])]
    if 'blocked' in agents:
        return 'blocked'
    if 'clarify' in agents:
        return 'clarify'
    return 'answer' if any(t.get('ok') for t in rec.get('tasks', [])) else 'error'


def score(case: dict, rec: dict) -> dict:
    """One scored case, in the shape GET /api/evals/{id} returns."""
    tasks = rec.get('tasks', [])
    agents = [t['agent'] for t in tasks if t.get('agent')]
    answer = (rec.get('merged') or {}).get('answer') or rec.get('error') or ''
    reasons = []
    if rec.get('status') != 'done':
        reasons.append(f"run ended {rec.get('status')}")
    for a in case.get('expect_agents') or []:
        if a not in agents:
            reasons.append(f'expected agent {a}, got {", ".join(agents) or "none"}')
    if case.get('expect_outcome') and outcome(rec) != case['expect_outcome']:
        reasons.append(f"expected outcome {case['expect_outcome']}, got {outcome(rec)}")
    if case.get('must_match') and not re.search(case['must_match'], answer, re.I):
        reasons.append(f"answer does not match /{case['must_match']}/")
    if case.get('must_not_match') and re.search(case['must_not_match'], answer, re.I):
        reasons.append(f"answer matches forbidden /{case['must_not_match']}/")
    return {'id': case['id'], 'query': case['query'], 'tags': case.get('tags', []), 'pass': not reasons,
            'reasons': reasons, 'agents': agents, 'answer': answer, 'ms': rec.get('total_ms'), 'qid': rec.get('qid'),
            'expect_agents': case.get('expect_agents'), 'expect_outcome': case.get('expect_outcome'),
            'all_ok': bool(tasks) and all(t.get('ok') for t in tasks),
            'jev_tokens': (rec.get('tokens') or {}).get('jev_in', 0)}


def summary(e: dict) -> dict:
    cases = e['cases']
    passed = sum(c['pass'] for c in cases)
    return {**e, 'done': len(cases), 'passed': passed, 'accuracy': round(passed / e['total'], 4) if e['total'] else 0.0,
            'silent_wrong': sum(1 for c in cases if not c['pass'] and c['all_ok'])}


def start(router, engine, engine_name: str | None, cases: list[dict] | None = None, examples: bool | None = None) -> str:
    """Starts an eval in the background and returns its id. engine: an Engine or None (keyless). examples forces route
    examples on or off for this eval's runs only; None uses the global switch as it is now."""
    eid = uuid.uuid4().hex[:10]
    task = asyncio.create_task(run_eval(router, eid, engine, engine_name, cases or load_cases(), examples))
    router.evals[eid] = task
    task.add_done_callback(lambda t: router.evals.pop(eid, None))
    return eid


async def run_eval(router, eid: str, engine, engine_name, cases: list[dict], examples: bool | None = None) -> dict:
    examples = router.route_examples if examples is None else bool(examples)  # fixed for the whole eval
    e = {'eval_id': eid, 'at': time.time(), 'engine': engine_name, 'status': 'running', 'total': len(cases), 'cases': [],
         'examples': examples}
    router.store.save_eval(summary(e))
    sem = asyncio.Semaphore(CONCURRENCY)
    qids: set[int] = set()

    async def one(case):
        async with sem:  # cases start in file order, two at a time
            qid = router.submit(case['query'], 'eval', engine=engine, examples=examples, extras={'holdout': case['query']})
            qids.add(qid)
            await router.running[qid]
            qids.discard(qid)
            e['cases'].append(score(case, router.get_run(qid)))
            s = summary(e)
            router.store.save_eval(s)
            router.bus.emit('eval_progress', eval_id=eid, done=s['done'], total=s['total'], passed=s['passed'])

    try:
        await asyncio.gather(*(one(c) for c in cases))
        e['status'] = 'done'
    except asyncio.CancelledError:
        for qid in list(qids):
            router.cancel(qid)
        e['status'] = 'cancelled'
    except Exception:
        e['status'] = 'error'
    order = {c['id']: i for i, c in enumerate(cases)}
    e['cases'].sort(key=lambda c: order.get(c['id'], 0))
    s = summary(e)
    router.store.save_eval(s)
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


async def cli(engine_name: str | None, save: bool, examples: bool | None = None) -> int:
    import aiohttp
    from typesafe_sdk import AsyncTypeSafeClient

    from .config import load_env
    from .engines import catalog, choose
    from .pipeline import Router
    from .store import DEFAULT_DB, Store, read_labels

    load_env()
    engines = catalog()
    engine = None if engine_name == 'none' else choose(engines, engine_name) if engine_name else choose(engines)
    if engine_name not in (None, 'none') and (engine is None or engine.name != engine_name):
        print(f'engine {engine_name!r} is not available', file=sys.stderr)
        return 2
    async with aiohttp.ClientSession() as http:
        router = Router(AsyncTypeSafeClient(), http, engine, engines=engines, store=Store())  # in-memory: CLI evals don't touch the app DB
        examples = router.route_examples if examples is None else examples
        if examples:  # route examples come from the app's labels, read without opening its DB for writing
            router.labels = read_labels(os.environ.get('TG_DB') or DEFAULT_DB)
        name = engine.name if engine else 'none'
        result = await run_eval(router, 'cli', engine, name, load_cases(), examples)
    for c in result['cases']:
        print(f"{'PASS' if c['pass'] else 'FAIL'}  {c['id']:<14} {c['ms'] or 0:>6}ms  {','.join(c['agents']):<22} "
              f"{c['query'][:48]:<48} {'; '.join(c['reasons'])[:90]}")
    tokens = side(result)['mean_jev_tokens']
    print(f"\n{name}: {result['passed']}/{result['total']} passed ({result['accuracy']:.0%}), silent_wrong {result['silent_wrong']}, "
          f"examples {'on' if result['examples'] else 'off'}, mean Jev tokens {tokens:g}")
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    if save:
        baseline[name] = {c['id']: c['pass'] for c in result['cases']}
        BASELINE.write_text(json.dumps(baseline, indent=1, sort_keys=True) + '\n')
        print(f'baseline for {name} saved to {BASELINE}')
        return 0
    bad = regressions(result, baseline)
    if bad:
        print(f"regressions against baseline: {', '.join(bad)}")
    return 1 if bad else 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Run the TraceGraph eval cases through the real pipeline.')
    ap.add_argument('--engine', help='engine name, or none for keyless (default: TG_ENGINE / auto)')
    ap.add_argument('--examples', choices=('on', 'off'), help='route examples from your labels on or off (default: TG_ROUTE_EXAMPLES)')
    ap.add_argument('--save-baseline', action='store_true', help='record this run as the baseline for its engine')
    args = ap.parse_args()
    sys.exit(asyncio.run(cli(args.engine, args.save_baseline, None if args.examples is None else args.examples == 'on')))
