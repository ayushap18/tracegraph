"""Routing that learns (docs/PLAN-learning.md): route labels, the review queue of shaky decisions, and promoting a
label to an eval case.

A label is one user judgement of one routed subtask of a saved run. Its text, the agent Jev picked, the confidence
and the margin are copied from the run record, so labels outlive the run and the client can't misreport them.
"""
import json
import os
import threading
import time
import uuid

from . import evals
from .config import (AGENTS, GUARDS, REASK_SECONDS, REPORT, RESEARCH, REVIEW_CONFIDENCE, REVIEW_MARGIN, REVIEW_SCAN,
                     RUN)
from .files import FILE_AGENTS

VERDICTS = ('right', 'wrong')
REASONS = ('low confidence', 'low margin', 'clarify', 'agent failed', 're-asked')
NOT_REVIEWED = ('eval', 'compare')  # runs made to test the router, not questions people asked
MAX_NOTE = 500
PROMOTE_LOCK = threading.Lock()


class LabelError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def margin(probs: dict) -> float:
    """Top probability minus the runner-up's (the top one alone when Jev had a single choice)."""
    top = sorted((float(p) for p in (probs or {}).values()), reverse=True)
    return round(top[0] - (top[1] if len(top) > 1 else 0.0), 4) if top else 0.0


def runner_up(probs: dict, picked: str | None) -> str | None:
    """The likeliest agent other than the one picked (for a guard, that's Jev's own top choice)."""
    return next((a for a, _ in sorted((probs or {}).items(), key=lambda kv: -kv[1]) if a != picked), None)


def task_text(t: dict) -> str:
    """The subtask as Jev routed it: the self-contained rewrite of a dependent step when there is one."""
    return t.get('input') or t.get('text') or ''


def routed_tasks(rec: dict) -> list[dict]:
    """The subtasks of a run that Jev routed (merge pseudo-tasks and subtasks that failed to route have no agent)."""
    return [t for t in rec.get('tasks') or [] if t.get('tid') and t.get('tid') != 'merge' and t.get('agent')]


def known_agents(router) -> set[str]:
    """Every agent a label can name: built-ins (engine-only ones too), file agents, custom agents and the guards."""
    return {*AGENTS, *RESEARCH, *REPORT, *RUN, *FILE_AGENTS, *(a['name'] for a in router.customs), *GUARDS}


def stored_run(router, qid) -> dict | None:
    """A saved run (finished, or in flight with its routed subtasks). Sandbox runs are never saved, so they're None."""
    if type(qid) is not int or qid in router.sandbox:
        return None
    return router.inflight.get(qid) or router.store.get_run(qid)


def make_label(router, body: dict) -> dict:
    """Validates a POST /api/labels body against the saved run and saves the label (replacing any for that subtask)."""
    qid, tid, verdict, correct, note = (body.get(k) for k in ('qid', 'tid', 'verdict', 'correct', 'note'))
    if type(qid) is not int:
        raise LabelError('qid must be a run number')
    if not isinstance(tid, str) or not tid:
        raise LabelError('tid must be a subtask id')
    if verdict not in VERDICTS:
        raise LabelError('verdict must be right or wrong')
    if note is not None and not isinstance(note, str):
        raise LabelError('note must be text')
    rec = stored_run(router, qid)
    if rec is None:
        raise LabelError(f'no saved run {qid}', 404)
    task = next((t for t in rec.get('tasks') or [] if t.get('tid') == tid), None)
    if task is None:
        raise LabelError(f'run {qid} has no subtask {tid}', 404)
    if not task.get('agent'):
        raise LabelError(f'subtask {tid} was never routed, so there is no decision to mark', 409)
    picked, probs = task['agent'], task.get('probabilities') or {}
    if verdict == 'right':
        correct = picked
    else:
        if not isinstance(correct, str) or not correct:
            raise LabelError('correct is required when the verdict is wrong: the agent that should have answered')
        if correct not in known_agents(router) and correct not in probs:
            raise LabelError(f'{correct} is not an agent')
        if correct == picked:
            raise LabelError(f'{correct} is what Jev picked; mark it right instead')
    old = router.store.label_for(qid, tid)
    label = {'id': uuid.uuid4().hex[:10], 'qid': qid, 'tid': tid, 'text': task_text(task), 'picked': picked,
             'correct': correct, 'verdict': verdict, 'confidence': round(float(task.get('confidence') or 0.0), 4),
             'margin': margin(probs), 'note': (note.strip()[:MAX_NOTE] or None) if note else None, 'at': time.time(),
             'promoted': old['promoted'] if old else None}
    label = router.store.save_label(label)
    if old and old['promoted'] and old['correct'] != correct:
        # The promoted case must follow the new answer, or the evals keep asserting the one just retracted.
        label = {**label, 'promoted': sync_promoted(router, label, old['promoted'])}
    router.refresh_labels()
    return label


def sync_promoted(router, label: dict, cid: str) -> str | None:
    """Rewrites a promoted label's case in place after its answer changed and returns the label's promoted case id.
    When the new answer can't be a case (a file agent), the stale case is removed and the label is no longer promoted."""
    if label['correct'] in FILE_AGENTS or not (label['text'] or '').strip():
        remove_case(cid)
        router.store.set_promoted(label['id'], None)
        return None
    case = case_for(label)
    write_case(case)
    if case['id'] != cid:  # the label id is kept on relabel, so this only guards against an odd legacy id
        remove_case(cid)
        router.store.set_promoted(label['id'], case['id'])
    return case['id']


# ---------- review queue ----------

def review(router, limit: int = 50, reason: str | None = None) -> dict:
    """Shaky, unlabelled subtasks from the newest REVIEW_SCAN saved runs people asked (not eval or compare runs), newest
    first: {items, scanned}."""
    runs = router.store.recent_runs(REVIEW_SCAN, NOT_REVIEWED)  # eval/compare runs have no session, so re-asked holds
    labelled = router.store.labelled()
    # Re-asked: the same chat's next turn started within REASK_SECONDS of this answer finishing.
    next_at, later = {}, {}
    for rec in runs:  # newest first, so `later` holds the next turn of each session
        sid = rec.get('session_id')
        if sid:
            if sid in later:
                next_at[rec['qid']] = later[sid]
            later[sid] = rec.get('at') or 0
    items = []
    for rec in runs:
        if rec.get('source') in NOT_REVIEWED or rec.get('status') == 'running':
            continue
        end = (rec.get('at') or 0) + (rec.get('total_ms') or 0) / 1000
        reasked = rec['qid'] in next_at and next_at[rec['qid']] - end < REASK_SECONDS
        for t in routed_tasks(rec):
            if (rec['qid'], t['tid']) in labelled:
                continue
            probs = dict(sorted((t.get('probabilities') or {}).items(), key=lambda kv: -kv[1]))
            conf, m = float(t.get('confidence') or 0.0), margin(probs)
            reasons = [r for r, hit in (('low confidence', conf < REVIEW_CONFIDENCE), ('low margin', m < REVIEW_MARGIN),
                                        ('clarify', t['agent'] == 'clarify'),
                                        ('agent failed', t.get('ok') is False and t['agent'] not in GUARDS),
                                        ('re-asked', reasked)) if hit]
            if not reasons or (reason and reason not in reasons):
                continue
            items.append({'qid': rec['qid'], 'tid': t['tid'], 'text': task_text(t), 'picked': t['agent'],
                          'confidence': round(conf, 4), 'margin': m, 'runner_up': runner_up(probs, t['agent']),
                          'probabilities': probs, 'reasons': reasons, 'at': rec.get('at')})
            if len(items) >= limit:
                return {'items': items, 'scanned': len(runs)}
    return {'items': items, 'scanned': len(runs)}


# ---------- promote ----------

def case_for(label: dict) -> dict:
    case = {'id': f"u-{label['id']}", 'query': label['text']}
    if label['correct'] in GUARDS:
        case['expect_outcome'] = label['correct']
    else:
        case['expect_agents'] = [label['correct']]
    return {**case, 'tags': ['user']}


def promote(router, lid: str) -> dict:
    """Adds the label as a case in evals/cases.local.jsonl: {case_id, created}. Idempotent: promoting again is
    created false, and rewrites the case in place if the label has changed since."""
    label = router.store.get_label(lid)
    if label is None:
        raise LabelError('no such label', 404)
    if label['correct'] in FILE_AGENTS:
        raise LabelError('a label for a file agent cannot become an eval case: evals run without attached files')
    if not (label['text'] or '').strip():
        raise LabelError('this label has no question text to test with')
    case = case_for(label)
    created = write_case(case)
    router.store.set_promoted(lid, case['id'])
    return {'case_id': case['id'], 'created': created}


def tmp_path_for(path):
    """The rewrite's temporary file: cases.local.jsonl.tmp, covered by the evals/*.local* ignore rule like the file."""
    return path.with_name(path.name + '.tmp')


def rewrite(path, lines: list[str]):
    tmp = tmp_path_for(path)
    tmp.write_text(''.join(line + '\n' for line in lines))
    os.replace(tmp, path)


def write_case(case: dict) -> bool:
    """Adds the case to evals/cases.local.jsonl, or rewrites its line in place if it's there and differs. True when
    the case is new (a committed case with the same id counts as existing)."""
    path = evals.LOCAL_CASES
    with PROMOTE_LOCK:
        lines = path.read_text().splitlines() if path.exists() else []
        at = next((i for i, line in enumerate(lines) if case_id(line) == case['id']), None)
        created = at is None and case['id'] not in {c['id'] for c in evals.load_cases(evals.CASES)}
        if created:
            path.parent.mkdir(parents=True, exist_ok=True)
            gap = path.exists() and path.stat().st_size and not path.read_bytes().endswith(b'\n')
            with open(path, 'a') as f:
                f.write(('\n' if gap else '') + json.dumps(case) + '\n')
        elif at is not None and json.loads(lines[at]) != case:
            lines[at] = json.dumps(case)
            rewrite(path, lines)
    return created


def remove_case(cid: str):
    """Drops a case from evals/cases.local.jsonl (no-op when it isn't there)."""
    path = evals.LOCAL_CASES
    with PROMOTE_LOCK:
        if not path.exists():
            return
        lines = path.read_text().splitlines()
        kept = [line for line in lines if case_id(line) != cid]
        if len(kept) != len(lines):
            rewrite(path, kept)


def case_id(line: str) -> str | None:
    try:
        c = json.loads(line)
    except json.JSONDecodeError:
        return None
    return c.get('id') if isinstance(c, dict) else None
