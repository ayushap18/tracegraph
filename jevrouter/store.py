"""SQLite persistence (stdlib sqlite3): runs, chat sessions, custom agents, uploaded files and eval results.

Every write is one small statement on a WAL database, so it runs inline rather than in a thread: that keeps the
final save of a cancelled run from depending on the event loop still being willing to schedule work.
"""
import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from .config import ROOT

DEFAULT_DB = ROOT / 'data' / 'tracegraph.db'

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(qid INTEGER PRIMARY KEY, session_id TEXT, compare_id TEXT, source TEXT, text TEXT, at REAL,
                                status TEXT, engine TEXT, total_ms INTEGER, record TEXT);
CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id);
CREATE INDEX IF NOT EXISTS runs_compare ON runs(compare_id);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS agents(name TEXT PRIMARY KEY, description TEXT, prompt TEXT, web INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, name TEXT, size INTEGER, kind TEXT, chars INTEGER, rows INTEGER,
                                 columns TEXT, created REAL);
CREATE TABLE IF NOT EXISTS evals(id TEXT PRIMARY KEY, at REAL, engine TEXT, status TEXT, done INTEGER, passed INTEGER,
                                 total INTEGER, accuracy REAL, silent_wrong INTEGER, cases TEXT);
"""


class Store:
    def __init__(self, path: str | Path = ':memory:', files_dir: str | Path | None = None):
        self.path = str(path)
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.files_dir = Path(files_dir or (Path(self.path).parent / 'files' if self.path != ':memory:'
                                            else tempfile.mkdtemp(prefix='tracegraph-files-')))
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        if self.path != ':memory:':
            self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        # A run still marked running was cut off by a restart; it will never finish now.
        for r in self.q("SELECT qid, record FROM runs WHERE status = 'running'"):
            rec = {**json.loads(r['record']), 'status': 'error', 'error': 'interrupted by a server restart'}
            self.x("UPDATE runs SET status = 'error', record = ? WHERE qid = ?", json.dumps(rec), r['qid'])
        self.x("UPDATE evals SET status = 'cancelled' WHERE status = 'running'")

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, *args):
        with self.lock:
            self.db.execute(sql, args)

    def close(self):
        self.db.close()

    # ---------- runs ----------

    def max_qid(self) -> int:
        return self.q('SELECT COALESCE(MAX(qid), 0) AS m FROM runs')[0]['m']

    def save_run(self, rec: dict):
        self.x('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)', rec['qid'], rec.get('session_id'),
               rec.get('compare_id'), rec['source'], rec['text'], rec['at'], rec.get('status'), rec.get('engine'),
               rec.get('total_ms'), json.dumps(rec))

    def get_run(self, qid: int) -> dict | None:
        rows = self.q('SELECT record FROM runs WHERE qid = ?', qid)
        return json.loads(rows[0]['record']) if rows else None

    def recent_finished(self, n: int) -> list[dict]:
        rows = self.q("SELECT record FROM runs WHERE status != 'running' ORDER BY qid DESC LIMIT ?", n)
        return [json.loads(r['record']) for r in reversed(rows)]

    def list_runs(self, limit=50, before=None, q=None, source=None, status=None, engine=None) -> list[dict]:
        where, args = [], []
        for col, val in (('source', source), ('status', status), ('engine', engine)):
            if col == 'engine' and val == 'none':
                where.append('engine IS NULL')
                continue
            if val:
                where.append(f'{col} = ?')
                args.append(val)
        if before:
            where.append('qid < ?')
            args.append(int(before))
        if q:
            qid_text = q.lstrip('#')
            if qid_text.isascii() and qid_text.isdigit() and len(qid_text) <= 18:
                where.append('(text LIKE ? OR qid = ?)')
                args.extend((f'%{q}%', int(qid_text)))
            else:
                where.append('text LIKE ?')
                args.append(f'%{q}%')
        sql = 'SELECT record FROM runs' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY qid DESC LIMIT ?'
        return [json.loads(r['record']) for r in self.q(sql, *args, int(limit))]

    def runs_where(self, col: str, value: str) -> list[dict]:
        assert col in ('session_id', 'compare_id')
        return [json.loads(r['record']) for r in self.q(f'SELECT record FROM runs WHERE {col} = ? ORDER BY qid', value)]

    def turns(self, session_id: str, before_qid: int, n: int = 3) -> list[dict]:
        """The last n finished turns of a session before this run, oldest first: [{query, answer}]."""
        rows = self.q("SELECT record FROM runs WHERE session_id = ? AND qid < ? AND status != 'running' "
                      'ORDER BY qid DESC LIMIT ?', session_id, before_qid, n)
        out = []
        for r in reversed(rows):
            rec = json.loads(r['record'])
            out.append({'query': rec['text'], 'answer': (rec.get('merged') or {}).get('answer') or rec.get('error') or ''})
        return out

    def counts(self) -> dict:
        return {t: self.q(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'] for t in ('runs', 'sessions', 'agents', 'files', 'evals')}

    # ---------- sessions ----------

    def touch_session(self, sid: str, first_query: str):
        now = time.time()
        if self.q('SELECT 1 FROM sessions WHERE id = ?', sid):
            self.x('UPDATE sessions SET updated = ? WHERE id = ?', now, sid)
        else:
            self.x('INSERT INTO sessions VALUES (?,?,?,?)', sid, first_query[:60], now, now)

    def list_sessions(self, limit=30) -> list[dict]:
        rows = self.q('SELECT s.*, (SELECT COUNT(*) FROM runs r WHERE r.session_id = s.id) AS turns FROM sessions s '
                      'ORDER BY updated DESC LIMIT ?', int(limit))
        return [dict(r) for r in rows]

    def get_session(self, sid: str) -> dict | None:
        rows = self.q('SELECT * FROM sessions WHERE id = ?', sid)
        return dict(rows[0]) if rows else None

    def delete_session(self, sid: str) -> bool:
        found = bool(self.get_session(sid))
        self.x('DELETE FROM sessions WHERE id = ?', sid)
        self.x('DELETE FROM runs WHERE session_id = ?', sid)
        return found

    # ---------- custom agents ----------

    def agents(self) -> list[dict]:
        return [{**dict(r), 'web': bool(r['web'])} for r in self.q('SELECT * FROM agents ORDER BY created')]

    def add_agent(self, a: dict):
        self.x('INSERT INTO agents VALUES (?,?,?,?,?)', a['name'], a['description'], a['prompt'], int(a['web']), time.time())

    def delete_agent(self, name: str):
        self.x('DELETE FROM agents WHERE name = ?', name)

    # ---------- files ----------

    def add_file(self, meta: dict, raw: bytes, text: str):
        (self.files_dir / meta['id']).write_bytes(raw)
        (self.files_dir / f"{meta['id']}.txt").write_text(text)
        self.x('INSERT INTO files VALUES (?,?,?,?,?,?,?,?)', meta['id'], meta['name'], meta['size'], meta['kind'],
               meta['chars'], meta.get('rows'), json.dumps(meta.get('columns')), meta['created'])

    def list_files(self, ids=None) -> list[dict]:
        rows = self.q('SELECT * FROM files ORDER BY created DESC')
        out = [file_meta(r) for r in rows]
        return out if ids is None else [f for i in ids for f in out if f['id'] == i]

    def file_text(self, fid: str) -> str:
        return (self.files_dir / f'{fid}.txt').read_text()

    def file_raw(self, fid: str) -> bytes:
        return (self.files_dir / fid).read_bytes()

    def delete_file(self, fid: str) -> bool:
        found = bool(self.q('SELECT 1 FROM files WHERE id = ?', fid))
        self.x('DELETE FROM files WHERE id = ?', fid)
        for p in (self.files_dir / fid, self.files_dir / f'{fid}.txt'):
            p.unlink(missing_ok=True)
        return found

    # ---------- evals ----------

    def save_eval(self, e: dict):
        self.x('INSERT OR REPLACE INTO evals VALUES (?,?,?,?,?,?,?,?,?,?)', e['eval_id'], e['at'], e['engine'], e['status'],
               e['done'], e['passed'], e['total'], e['accuracy'], e['silent_wrong'], json.dumps(e.get('cases', [])))

    def list_evals(self, limit=50) -> list[dict]:
        return [eval_row(r, cases=False) for r in self.q('SELECT * FROM evals ORDER BY at DESC LIMIT ?', int(limit))]

    def get_eval(self, eid: str) -> dict | None:
        rows = self.q('SELECT * FROM evals WHERE id = ?', eid)
        return eval_row(rows[0], cases=True) if rows else None


def file_meta(r) -> dict:
    m = {'id': r['id'], 'name': r['name'], 'size': r['size'], 'kind': r['kind'], 'chars': r['chars'], 'created': r['created']}
    if r['rows'] is not None:
        m.update(rows=r['rows'], columns=json.loads(r['columns']))
    return m


def eval_row(r, cases: bool) -> dict:
    e = {'eval_id': r['id'], 'at': r['at'], 'engine': r['engine'], 'status': r['status'], 'done': r['done'], 'passed': r['passed'],
         'total': r['total'], 'accuracy': r['accuracy'], 'silent_wrong': r['silent_wrong']}
    return {**e, 'cases': json.loads(r['cases'])} if cases else e
