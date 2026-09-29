"""aiohttp routes: GET / (web/dist or legacy page), GET /events (SSE), POST /ask, POST /control, GET /api/config,
plus the v4 API in docs/PLAN-v4.md §1 (runs, sessions, agents, files, compare, evals, engine test), the learning API in
docs/PLAN-learning.md (labels, review queue, route examples, eval compare, engine health) and created files in
docs/PLAN-files.md (list, download, preview, convert, the ruleset), and from docs/PLAN-accuracy-v2.md the query limit
(B4), suspect runs and promoting a run to an eval case (D6) and routing-only evals (D3)."""
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

from . import create as create_mod
from . import estimate as estimate_mod
from . import evals as evals_mod
from . import judge as judge_mod
from . import labels as labels_mod
from .config import (AGENTS, COST_SOURCES, FONT_LIMIT_MAX, FONT_PREVIEW_CHARS, FONT_QUERY_CHARS, PRICES, DIST, GROUP_MAX,
                     GUARDS, LEGACY_PAGE, MAX_QUERY_CHARS, MODES, REPORT, RESEARCH, RUN, STUDIO_CRITIC_OUT,
                     STUDIO_CRITIC_PROMPT_TOKENS, STUDIO_GRACE, STUDIO_SHEET_PAGES, STUDIO_SHEET_TOKENS,
                     STUDIO_TIME_BUDGET, STYLES, cost_confirm_on)
from .engines import EngineError, catalog, choose
from .engines.health import pct, unblock
from .agents import create as maker
from .events import sse
from .jev import examples_for
from .files import FILE_AGENTS, MAX_BYTES, FileError, extract
from .pipeline import SANDBOX_QID0, USE_ACTIVE, Router, checkpoint_info, public_run, table_files, warm_up
from .sandbox import MAX_FILES as MAX_SANDBOX_FILES, SandboxError
from .store import CREATED_ID, DEFAULT_DB, Store

ROUTER = web.AppKey('router', Router)
HTTP = web.AppKey('http', aiohttp.ClientSession)
AUTOPILOT = web.AppKey('autopilot', asyncio.Task)
SWEEPER = web.AppKey('sweeper', asyncio.Task)
WARMUP = web.AppKey('warmup', asyncio.Task)
WARMING: set[asyncio.Task] = set()


async def read_json(request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({'error': 'invalid JSON'}), content_type='application/json')
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({'error': 'expected an object'}), content_type='application/json')
    return body


def err(message: str, status: int = 400):
    return web.json_response({'error': message}, status=status)


class Bad(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def pick_engine(router, name):
    """An engine override by name: 'none' is keyless; unknown is 400, known but unusable is 409."""
    name = str(name)
    if name == 'none':
        return None
    engine = router.engines.get(name)
    if engine is None:
        raise Bad(f'unknown engine {name!r}')
    ok, why = engine.available()
    if not ok:
        raise Bad(f'{engine.label} is not available: {why}', 409)
    return engine


SOURCES = ('you', 'chat', 'compare', 'eval', 'sandbox')
SANDBOX_ID = re.compile(r'^[A-Za-z0-9_-]{8,64}$')


def too_long(text: str):
    """B4: a query over MAX_QUERY_CHARS is refused whole, never cut short (its last constraints would be lost)."""
    if len(text) > MAX_QUERY_CHARS:
        return web.json_response({'error': f'Your message is {len(text):,} characters; the limit is {MAX_QUERY_CHARS:,}.',
                                  'limit': MAX_QUERY_CHARS}, status=400)
    return None


def body_font() -> str | None:
    """The family of TRACEGRAPH_BODY_FONT (a .ttf/.otf file), or None when it isn't set to a file."""
    path = os.environ.get('TRACEGRAPH_BODY_FONT')
    if not path or not Path(path).is_file():
        return None
    try:
        from .create import fonts
        family = getattr(fonts, 'family_of', None)
        if callable(family):
            return family(path) or Path(path).stem
    except Exception:
        pass
    return Path(path).stem


def with_limits(d: dict) -> dict:
    """Config and hello carry the query limit and the document font; the router's own values win when it has them."""
    return {'limits': {'query_chars': MAX_QUERY_CHARS}, 'fonts': {'body': body_font()}, **d}


def emit_config(router):
    router.bus.emit('config', **with_limits(router.config()))


def validate_ask(router, body: dict, text: str) -> dict:
    """Everything /ask and /api/estimate check before a run could start (raises Bad): the source, session, files, chat
    options, engine or engines, and a sandbox run's own fields. Starts nothing."""
    source = body.get('source') or 'you'
    session_id = body.get('session_id') or None
    files = body.get('files') or []
    if source not in SOURCES:
        raise Bad(f'source must be one of {", ".join(SOURCES)}')
    if session_id is not None and not (isinstance(session_id, str) and 0 < len(session_id) <= 64):
        raise Bad('session_id must be a short string')
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise Bad('files must be a list of file ids')
    if source != 'sandbox':
        known = {f['id'] for f in router.store.list_files()}
        if missing := [f for f in files if f not in known]:
            raise Bad(f'unknown file id {missing[0]!r}')
    if 'confirm_cost' in body and not isinstance(body['confirm_cost'], bool):
        raise Bad('confirm_cost must be true or false')
    chat = chat_options(body)
    out = {'source': source, 'session_id': session_id, 'chat': chat, 'files': list(dict.fromkeys(files)),
           'sandbox': None, 'extras': None}
    if body.get('retry_of') is not None:
        out['retry'] = True
        return out
    engine = pick_engine(router, body['engine']) if body.get('engine') else USE_ACTIVE
    if source == 'sandbox':
        sandbox = body.get('sandbox_id')
        # Nothing from the sandbox is stored, so it can't use a saved chat session (or stored files).
        if not (isinstance(sandbox, str) and SANDBOX_ID.match(sandbox)):
            raise Bad('sandbox_id must be 8-64 letters, digits, - or _')
        if session_id:
            raise Bad('sandbox runs take no session_id')
        if body.get('engines') is not None:
            raise Bad('sandbox runs take one engine')
        engine, out['extras'] = sandbox_extras(router, sandbox, body, engine, out['files'])
        out['sandbox'] = sandbox
    if body.get('engines') is not None:
        if body.get('engine'):
            raise Bad('send engine or engines, not both')
        engines = pick_engines(router, body['engines'])
        runs = [mode_engine(router, chat, e) for e in engines]
    else:
        runs = [mode_engine(router, chat, engine)]
    extras = out['extras']
    attached = [meta for meta, _ in extras['files']] if extras else router.store.list_files(out['files']) if out['files'] else []
    for e in runs:
        check_agent(router, chat, e, attached, text)
    out.update(runs=runs, attached=attached)
    return out


async def ask(request):
    body, router = await read_json(request), request.app[ROUTER]
    text = str(body.get('query', '')).strip()
    if not text:
        return err('empty query')
    if (resp := too_long(text)) is not None:
        return resp
    try:
        v = validate_ask(router, body, text)
        if v.get('retry'):
            return retry(router, body, v['chat'])
        source, session_id, chat, files = v['source'], v['session_id'], v['chat'], v['files']
        sandbox, extras, runs = v['sandbox'], v['extras'], v['runs']
        ests = run_estimates(router, text, v)
    except Bad as e:
        return err(str(e), e.status)
    if (resp := cost_guard(body, source, estimate_mod.combine(ests) if ests else None)) is not None:
        return resp
    if source == 'chat' and not session_id:
        session_id = uuid.uuid4().hex[:12]
    if len(runs) == 1:
        extras = with_estimate(extras, ests[0] if ests else None)
        qid = router.submit(text, source, session_id=session_id, engine=runs[0], files=files, sandbox=sandbox,
                            extras=extras, **chat)
        return web.json_response({'ok': True, 'qid': qid, 'session_id': session_id,
                                  **({'sandbox_id': sandbox} if sandbox else {})})
    # Several answers: one run per engine in a new group; the first is the chosen answer until the user picks another.
    gid, qids = uuid.uuid4().hex[:10], []
    for i, e in enumerate(runs):
        qids.append(router.submit(text, source, session_id=session_id, engine=e, files=files, group_id=gid,
                                  chosen=i == 0, context_before=qids[0] if qids else None,
                                  extras=with_estimate(None, ests[i] if ests else None), **chat))
    return web.json_response({'ok': True, 'qid': qids[0], 'qids': qids, 'group_id': gid, 'session_id': session_id})


# ---------- cost preflight (docs/PLAN-files-robust.md section 5) ----------


def engine_views(router, chat: dict, engine):
    """(the engine most calls go to, the engine a web search goes to, the healthy alternatives) as EngineViews. A pinned
    engine is itself; Auto gives the head of its call chain and of its web chain; deep mode with none named gives the
    strongest healthy engine; keyless gives (None, None, [])."""
    if engine is USE_ACTIVE:
        engine = router.deep_engine() if chat.get('mode') == 'deep' else router.engine
    if engine is None:
        return None, None, []
    if hasattr(engine, 'chain'):
        lead, webs = engine.chain(), engine.chain(web=True)
        main = lead[0] if lead else None
        web_e = next((e for e in webs if e.supports_web), None)
    else:
        main, web_e = engine, engine if engine.supports_web else None
    alts = [router.engine_view(e) for e in router.backends() if e.available()[0]]
    return router.engine_view(main), router.engine_view(web_e), alts


def chat_state(router, session_id: str | None, sandbox: str | None, remember: bool = True) -> tuple[bool, dict | None]:
    """(the chat has an earlier answer, the chat's newest created file) for the estimate's zero-token paths."""
    from .agents import create as ca
    if sandbox:
        mem = router.sandboxes.peek(sandbox) if remember else None
        recs = [t['record'] for t in (mem.thread if mem else [])][-ca.LOOKBACK:]
    elif session_id:
        recs = router.store.session_records(session_id, 10 ** 15, ca.LOOKBACK)
    else:
        recs = []
    done = [r for r in recs if ca.answered(r)]
    has_answer = any(not ca.only_created(r) and (r.get('merged') or {}).get('answer') for r in done)
    last = next((f for r in reversed(done) for t in reversed(r.get('tasks') or [])
                 for f in reversed(t.get('created_files') or [])), None)
    return has_answer, last


def draft_for(router, text: str, v: dict) -> estimate_mod.Draft:
    """The estimate's view of a request: its text, mode, @agent, attached files (with the start of their text, so a
    design file is recognised) and what the chat already holds."""
    extras = v.get('extras') or {}
    if v.get('sandbox'):
        texts = {meta['id']: t for meta, t in extras.get('files', ())}
    else:
        texts = {}
        for f in v.get('attached') or []:
            try:
                texts[f['id']] = router.store.file_text(f['id'])
            except Exception:
                texts[f['id']] = ''
    files = [{**f, 'text': (texts.get(f['id']) or '')[:4000]} for f in v.get('attached') or []]
    has_answer, last = chat_state(router, v.get('session_id'), v.get('sandbox'), extras.get('remember', True))
    return estimate_mod.Draft(text, v['chat']['mode'], v['chat']['agent'], files, has_answer, last, v['source'])


def estimate_for(router, draft: estimate_mod.Draft, chat: dict, engine, query: str) -> estimate_mod.Estimate:
    main, web_e, alts = engine_views(router, chat, engine)
    run_engine = (router.deep_engine() if chat.get('mode') == 'deep' else router.engine) if engine is USE_ACTIVE else engine
    # the real price table: router.config()['prices'] is zeroed for display when the active engine is a subscription,
    # which would price a pay-per-token run on another engine at $0
    return estimate_mod.estimate(draft, main, web_e, deadline_s=router.deadline(query, run_engine), alternatives=alts,
                                 prices=PRICES, lean=lean_chain(router, run_engine))


def lean_chain(router, run_engine) -> list | None:
    """On Auto a long file goes to the cheapest healthy backend (Router.lean_engine): Auto's chain as EngineViews, so
    the estimate prices the file there too; None for a pinned engine or with lean long files off."""
    if run_engine is not None and getattr(run_engine, 'name', None) == 'auto' and hasattr(run_engine, 'chain'):
        from .config import lean_long_files_on
        if lean_long_files_on():
            return [router.engine_view(e) for e in run_engine.chain()]
    return None


def run_estimates(router, text: str, v: dict) -> list:
    """One estimate per run the ask would start (never for evals), or [] when it cannot be made: a bug in pricing
    must never stop a run."""
    if v['source'] == 'eval':
        return []
    try:
        draft = draft_for(router, text, v)
        return [estimate_for(router, draft, v['chat'], e, text) for e in v['runs']]
    except Exception:
        return []


def cost_guard(body: dict, source: str, est) -> web.Response | None:
    """5.4: a costly run is not started until the user confirms it (409 NeedsConfirmation). Keyless runs and evals are
    never asked; TG_COST_CONFIRM=0 turns the guard off."""
    if est is None or not cost_confirm_on() or source not in COST_SOURCES or body.get('confirm_cost') is True:
        return None
    if not est.needs_confirmation:
        return None
    n = est.calls
    return web.json_response({'error': f'This run needs about {n} model call{"" if n == 1 else "s"}. Confirm to go '
                                       f'ahead.', 'needs_confirmation': True, 'estimate': est.to_dict()}, status=409)


def with_estimate(extras: dict | None, est) -> dict | None:
    if est is None:
        return extras
    return {**(extras or {}), 'estimate': est.to_dict()}


async def estimate_run(request):
    """POST /api/estimate (EstimateBody): what the draft's model calls would cost. Validates like /ask; never submits
    a run or calls an engine."""
    body, router = await read_json(request), request.app[ROUTER]
    text = str(body.get('query', '')).strip()
    if not text:
        return err('empty query')
    if (resp := too_long(text)) is not None:
        return resp
    try:
        for key in ('retry_of', 'replaces', 'draft_agent'):
            if body.get(key) is not None:
                raise Bad(f'{key} is not part of an estimate')
        base = {k: val for k, val in body.items() if k not in ('remember', 'confirm_cost')}
        if base.get('source') == 'sandbox' and base.get('engines') is not None:
            # a sandbox side-by-side sends one run per engine: price each as that run, then add them up
            if base.get('engine'):
                raise Bad('send engine or engines, not both')
            names = base.pop('engines')
            pick_engines(router, names)
            ests = []
            for n in names:
                v = validate_ask(router, {**base, 'engine': n}, text)
                draft = draft_for(router, text, v)
                ests += [estimate_for(router, draft, v['chat'], e, text) for e in v['runs']]
        else:
            v = validate_ask(router, base, text)
            draft = draft_for(router, text, v)
            ests = [estimate_for(router, draft, v['chat'], e, text) for e in v['runs']]
    except Bad as e:
        return err(str(e), e.status)
    return web.json_response(estimate_mod.combine(ests).to_dict())


def run_for_resume(router, qid: int, sandbox: str | None) -> dict | None:
    if sandbox:
        mem = router.sandboxes.peek(sandbox)
        held = next((t['record'] for t in (mem.thread if mem else []) if t['qid'] == qid), None)
        if held is None and router.sandbox.get(qid) == sandbox:
            held = router.inflight.get(qid)
        return held
    if qid in router.sandbox or qid >= SANDBOX_QID0:
        return None
    return router.get_run(qid)


async def resume_created(request):
    """POST /api/created/resume (ResumeBody): a new run that writes only the missing parts of a partial, failed or
    timed-out file step, from its checkpoint. Priced and guarded like /ask; 202 ResumeResponse."""
    body, router = await read_json(request), request.app[ROUTER]
    try:
        qid, tid, sid = body.get('qid'), body.get('tid'), body.get('sandbox_id')
        if type(qid) is not int:
            raise Bad('qid must be a run number')
        if not isinstance(tid, str) or not tid or len(tid) > 40:
            raise Bad('tid must be a step id')
        if sid is not None and not (isinstance(sid, str) and SANDBOX_ID.match(sid)):
            raise Bad('sandbox_id must be 8-64 letters, digits, - or _')
        if 'confirm_cost' in body and not isinstance(body['confirm_cost'], bool):
            raise Bad('confirm_cost must be true or false')
        rec = run_for_resume(router, qid, sid)
        state = ((rec or {}).get('checkpoints_state') or {}).get(tid)
        if not isinstance(state, dict):
            raise Bad('no checkpoint for that file step', 404)
        if rec.get('status') == 'running':
            raise Bad('That run is still going. Resume it once it has finished.', 409)
        if (taken := resumed_by(router, state, sid)) is not None:
            raise Bad(taken, 409)
        if not checkpoint_info(state, qid, tid).get('resumable'):
            raise Bad('That file is already complete.', 409)
        mode = rec.get('mode') if rec.get('mode') in MODES and rec.get('mode') != 'research' else 'balanced'
        chat = {'mode': mode, 'style': rec.get('style') if rec.get('style') in STYLES else 'default', 'agent': 'create'}
        engine = pick_engine(router, body['engine']) if body.get('engine') else USE_ACTIVE
        main, _, _ = engine_views(router, chat, engine)
        run_engine = (router.deep_engine() if mode == 'deep' else router.engine) if engine is USE_ACTIVE else engine
        lean = lean_chain(router, run_engine) if state.get('kind') != 'single' else None
        est = estimate_mod.for_resume(state, main, deadline_s=router.deadline(rec['text'], run_engine), lean=lean,
                                      prices=PRICES)
    except Bad as e:
        return err(str(e), e.status)
    source = 'sandbox' if sid else rec.get('source') if rec.get('source') in SOURCES else 'chat'
    if (resp := cost_guard(body, source, est)) is not None:
        return resp
    extras = {'resume': {'qid': qid, 'tid': tid, 'state': {k: v for k, v in state.items() if k != 'resumed_by'}},
              'estimate': est.to_dict()}
    if sid:
        extras.update(files=[], remember=True)
    if (taken := resumed_by(router, state, sid)) is not None:  # a second click while this one was being priced
        return err(taken, 409)
    new = router.submit(rec['text'], source, session_id=None if sid else rec.get('session_id'), engine=engine,
                        files=[] if sid else rec.get('files') or [], sandbox=sid, extras=extras, **chat)
    router.mark_resumed(rec, tid, new, sid)
    return web.json_response({'ok': True, 'qid': new, 'session_id': None if sid else rec.get('session_id'),
                              'estimate': est.to_dict()}, status=202)


def resumed_by(router, state: dict, sid: str | None) -> str | None:
    """Why a checkpoint cannot be resumed again, or None: a resume run for it is still going, or one already made its
    file. A resume that ended with no file frees the checkpoint again."""
    by = state.get('resumed_by') if isinstance(state, dict) else None
    if not isinstance(by, dict) or type(by.get('qid')) is not int:
        return None
    n = by['qid']
    if by.get('file_id'):
        return f'That file was already resumed as run #{n}.'
    if n in router.inflight:
        return f'This file is already being resumed as run #{n}.'
    done = run_for_resume(router, n, sid)
    if isinstance(done, dict) and any(f.get('id') for t in done.get('tasks') or [] for f in t.get('created_files') or []):
        return f'That file was already resumed as run #{n}.'
    return None


async def run_checkpoints(request):
    """GET /api/runs/{qid}/checkpoints[?sandbox_id=]: the CheckpointInfo summaries of a run's file steps."""
    router = request.app[ROUTER]
    try:
        qid = int(request.match_info['qid'])
    except ValueError:
        return err('bad qid', 404)
    sid = request.query.get('sandbox_id') or None
    if sid is not None and not SANDBOX_ID.match(sid):
        return err('bad sandbox id')
    rec = run_for_resume(router, qid, sid)
    if rec is None:
        return err('no such run', 404)
    return web.json_response({'checkpoints': (public_run(rec) or {}).get('checkpoints') or []})


def chat_options(body: dict) -> dict:
    """The run's chat variety options (docs/PLAN-speed-evals-chat.md), validated: mode, style and a forced agent."""
    mode, style, agent = body.get('mode') or 'balanced', body.get('style') or 'default', body.get('agent') or None
    if mode not in MODES:
        raise Bad(f'mode must be one of {", ".join(MODES)}')
    if style not in STYLES:
        raise Bad(f'style must be one of {", ".join(STYLES)}')
    if agent is not None:
        if not isinstance(agent, str):
            raise Bad('agent must be an agent name')
        agent = agent.strip().lstrip('@')
    return {'mode': mode, 'style': style, 'agent': agent}


def pick_engines(router, names) -> list:
    if not isinstance(names, list) or not 2 <= len(names) <= GROUP_MAX or len(set(map(str, names))) != len(names):
        raise Bad(f'engines must list 2 to {GROUP_MAX} different engines')
    return [pick_engine(router, n) for n in names]


def mode_engine(router, chat: dict, engine):
    """The engine a run in this mode uses: always the one the user selected or named. Deep with none named:
    USE_ACTIVE (the run resolves it: the selected engine, or the strongest healthy one when Auto is selected).
    Research: the selected engine even without web search (the answer then says so); 400 only when keyless."""
    if chat['mode'] == 'research':
        # The selected engine, strictly: research never switches engines behind the user's back. One without web
        # search still runs, answering from its own knowledge, and the answer says so (pipeline merge).
        e = router.engine if engine is USE_ACTIVE else engine
        if e is None:
            raise Bad('Research mode needs an LLM engine; keyless mode can only answer with the built-in agents.')
        return e
    return engine


def check_agent(router, chat: dict, engine, attached: list[dict], text: str):
    """@agent must be an agent this run could route to (guards are not agents you can pick): file agents need a file
    attached, @sql a table among them (as the run itself decides, see table_files)."""
    agent = chat['agent']
    if agent is None:
        return
    if engine is USE_ACTIVE:
        engine = router.deep_engine() if chat['mode'] == 'deep' else router.engine
    offered = router.offered(engine, with_files=bool(attached), tables=bool(table_files(attached, engine, text)))
    if agent not in offered:
        raise Bad(f'@{agent} is not an agent you can pick here')


def retry(router, body: dict, chat: dict):
    """Another answer to an earlier run's question, on `engine` (the active one if none), added to that run's group."""
    qid = body['retry_of']
    if type(qid) is not int:
        raise Bad('retry_of must be a qid')
    if body.get('engines') is not None or body.get('source') == 'sandbox':
        raise Bad('retry_of takes one engine and no sandbox')
    rec = router.get_run(qid)
    if rec is None or qid in router.sandbox or qid >= SANDBOX_QID0:
        raise Bad(f'no run {qid}', 404)
    engine = mode_engine(router, chat, pick_engine(router, body['engine']) if body.get('engine') else USE_ACTIVE)
    attached = router.store.list_files(rec['files']) if rec.get('files') else []
    check_agent(router, chat, engine, attached, rec['text'])
    ests = run_estimates(router, rec['text'], {'source': rec['source'], 'session_id': rec.get('session_id'),
                                               'chat': chat, 'attached': attached, 'runs': [engine], 'sandbox': None})
    if (resp := cost_guard(body, body.get('source') or rec['source'], ests[0] if ests else None)) is not None:
        return resp
    gid, first = router.join_group(qid)
    new = router.submit(rec['text'], rec['source'], session_id=rec.get('session_id'), engine=engine,
                        files=rec.get('files') or [], group_id=gid, chosen=False, context_before=first,
                        extras=with_estimate(None, ests[0] if ests else None), **chat)
    return web.json_response({'ok': True, 'qid': new, 'qids': [new], 'group_id': gid, 'session_id': rec.get('session_id')})


def sandbox_extras(router, sid: str, body: dict, engine, files: list[str]):
    """Validates a sandbox run's own fields (files, draft_agent, replaces, remember). Returns (engine, extras)."""
    mem = router.sandboxes.peek(sid)
    held = mem.files if mem else {}
    if missing := [f for f in files if f not in held]:
        raise Bad(f'unknown sandbox file id {missing[0]!r}')
    remember = body.get('remember', True)
    if not isinstance(remember, bool):
        raise Bad('remember must be true or false')
    replaces = body.get('replaces')
    if replaces is not None:
        if type(replaces) is not int:
            raise Bad('replaces must be a qid')
        if not mem or replaces not in mem.qids():
            raise Bad(f'{replaces} is not a finished turn in this sandbox')
    draft = body.get('draft_agent')
    if draft is not None:
        if not isinstance(draft, dict):
            raise Bad('draft_agent must be an object')
        draft = validate_agent(router, draft, draft=True)
        # Resolve the engine now, so the run can't start keyless after the check if the active engine changes.
        if engine is USE_ACTIVE:
            engine = router.engine
        if engine is None:
            raise Bad('the draft agent needs an LLM engine', 409)
    extras = {'files': [held[f] for f in files], 'remember': remember}
    if replaces is not None:
        extras['replaces'] = replaces
    if draft is not None:
        extras['draft'] = draft
    return engine, extras


def sandbox_id(request) -> str:
    sid = request.match_info['id']
    if not SANDBOX_ID.match(sid):
        raise Bad('bad sandbox id')
    return sid


async def keep_sandbox(request):
    """Save sandbox turns as a new normal chat, on request. The sandbox itself is unchanged."""
    router = request.app[ROUTER]
    try:
        sid = sandbox_id(request)
        body = await read_json(request) if request.can_read_body else {}
        qids = body.get('qids')
        if qids is not None and not (isinstance(qids, list) and all(type(q) is int for q in qids)):
            raise Bad('qids must be a list of qids')
        session_id, new = router.keep_sandbox(sid, None if qids is None else list(dict.fromkeys(qids)))
    except (Bad, SandboxError) as e:
        return err(str(e), e.status)
    return web.json_response({'session_id': session_id, 'qids': new})


async def upload_sandbox_file(request):
    """A temporary attachment: extracted and kept in the sandbox's memory only, never written to disk."""
    router = request.app[ROUTER]
    try:
        sid = sandbox_id(request)
    except Bad as e:
        return err(str(e), e.status)
    mem = router.sandboxes.get(sid, create=True)
    try:
        if len(mem.files) >= MAX_SANDBOX_FILES:  # refuse before reading the upload
            raise SandboxError(f'at most {MAX_SANDBOX_FILES} files per sandbox')
        got = await read_upload(request)
        if isinstance(got, web.Response):
            return got
        meta, _ = got
        mem.add_file(*got)
    except SandboxError as e:
        return err(str(e), e.status)
    return web.json_response(meta, status=201)


async def delete_sandbox_file(request):
    router = request.app[ROUTER]
    try:
        sid = sandbox_id(request)
    except Bad as e:
        return err(str(e), e.status)
    mem = router.sandboxes.get(sid)
    if mem is None or mem.files.pop(request.match_info['fid'], None) is None:
        return err('no such file', 404)
    return web.json_response({'ok': True})


async def clear_sandbox(request):
    """Forget a sandbox: cancel its running queries and drop its thread and files."""
    sid = request.match_info['id']
    if not SANDBOX_ID.match(sid):
        return err('bad sandbox id')
    return web.json_response({'ok': True, 'cancelled': request.app[ROUTER].clear_sandbox(sid)})


# ---------- runs ----------

async def cancel_run(request):
    try:
        qid = int(request.match_info['qid'])
    except ValueError:
        return err('bad qid', 404)
    result = request.app[ROUTER].cancel(qid)
    if result == 'unknown':
        return err(f'no run {qid}', 404)
    if result == 'finished':
        return err(f'run {qid} has already finished', 409)
    return web.json_response({'ok': True})


SUSPECT_SCAN = 5000  # most saved runs one ?suspect=1 page looks through


async def list_runs(request):
    """GET /api/runs: newest first. ?suspect=1 keeps only runs with suspects (D6), paging through older runs until the
    page is full."""
    router, q = request.app[ROUTER], request.query
    try:
        limit = max(1, min(200, int(q.get('limit', 50))))
        before = int(q['before']) if q.get('before') else None
    except ValueError:
        return err('limit and before must be integers')
    if q.get('suspect') not in (None, '', '0', 'false'):
        runs, scanned = [], 0
        while len(runs) < limit and scanned < SUSPECT_SCAN:
            batch = router.store.list_runs(200, before, q.get('q'), q.get('source'), q.get('status'), q.get('engine'))
            if not batch:
                break
            scanned += len(batch)
            runs += [r for r in batch if evals_mod.suspects_of(r)]
            before = batch[-1]['qid']
        runs = runs[:limit]
    else:
        runs = router.store.list_runs(limit, before, q.get('q'), q.get('source'), q.get('status'), q.get('engine'))
    # a run saved before suspects were stored gets them computed now, so the Suspect column and filter can show it
    runs = [r if isinstance(r.get('suspects'), list) else {**r, 'suspects': evals_mod.suspects_of(r)} for r in runs]
    return web.json_response({'runs': [public_run(router.inflight.get(r['qid'], r)) for r in runs]})


async def promote_run(request):
    """POST /api/runs/{qid}/promote: the run as a draft case in evals/cases.local.jsonl (PromoteRunResponse)."""
    try:
        qid = int(request.match_info['qid'])
    except ValueError:
        return err('bad qid', 404)
    try:
        return web.json_response(evals_mod.promote_run(request.app[ROUTER], qid))
    except evals_mod.PromoteError as e:
        return err(str(e), e.status)
    except evals_mod.CaseError as e:
        return err(f'bad eval case: {e}', 500)


async def get_run(request):
    try:
        rec = request.app[ROUTER].get_run(int(request.match_info['qid']))
    except ValueError:
        rec = None
    return web.json_response(public_run(rec)) if rec else err('no such run', 404)


async def choose_run(request):
    """Several answers: this run becomes its group's chosen answer (the one follow-ups build on), its siblings not."""
    try:
        qid = int(request.match_info['qid'])
    except ValueError:
        return err('bad qid', 404)
    status, gid = request.app[ROUTER].choose(qid)
    if status == 'unknown':
        return err(f'no run {qid}', 404)
    if status == 'no group':
        return err(f'run {qid} is not one of several answers', 409)
    return web.json_response({'ok': True, 'group_id': gid, 'chosen': qid})


STAGES = (('plan', 'plan_ms'), ('route', 'route_ms'), ('agents', 'agents_ms'), ('merge', 'merge_ms'),
          ('first_token', 'first_token_ms'))
MAX_TIMED = 1000


async def timings_summary(request):
    """GET /api/timings?engine=&limit=: p50/p90 per stage over the newest finished runs that stored timings."""
    q = request.query
    try:
        limit = int_param(q, 'limit', 200, 1, MAX_TIMED)
    except Bad as e:
        return err(str(e), e.status)
    engine = q.get('engine') or None
    runs = request.app[ROUTER].store.timed_runs(engine, limit)
    stages = []
    for stage, key in (*STAGES, ('total', None)):
        vals = sorted(v for r in runs if (v := r.get('total_ms') if key is None else (r.get('timings') or {}).get(key))
                      is not None)
        stages.append({'stage': stage, 'p50': pct(vals, 0.5), 'p90': pct(vals, 0.9), 'n': len(vals)})
    return web.json_response({'engine': engine, 'runs': len(runs), 'stages': stages})


# ---------- sessions ----------

async def list_sessions(request):
    try:
        limit = max(1, min(200, int(request.query.get('limit', 30))))
    except ValueError:
        return err('limit must be an integer')
    return web.json_response({'sessions': request.app[ROUTER].store.list_sessions(limit)})


async def get_session(request):
    router, sid = request.app[ROUTER], request.match_info['id']
    s = router.store.get_session(sid)
    if not s:
        return err('no such session', 404)
    return web.json_response({'id': s['id'], 'title': s['title'],
                              'runs': [public_run(r) for r in router.runs_where('session_id', sid)]})


async def delete_session(request):
    router, sid = request.app[ROUTER], request.match_info['id']
    for qid, rec in list(router.inflight.items()):
        if rec.get('session_id') == sid:
            router.cancel(qid)
    router.store.delete_session(sid)
    return web.json_response({'ok': True})


# ---------- agents ----------

GUARD_INFO = {'clarify': 'Asks a follow-up question when the request is unclear or Jev is not confident enough to route it',
              'blocked': 'Declines requests that Jev flags as harmful or unsafe',
              'unsupported': 'Says honestly when a request needs something this app cannot do, such as live data or '
                             'acting in the world'}
NAME = re.compile(r'^[a-z][a-z0-9_-]{1,23}$')
MAX_CUSTOM = 12


def agent_list(router) -> list[dict]:
    e = router.engine
    needs = {'research': bool(e and e.supports_web), 'report': e is not None, 'run': bool(getattr(e, 'supports_exec', False))}
    out = [{'name': n, 'description': d, 'kind': 'builtin', 'engine_required': n in needs, 'available': needs.get(n, True)}
           for n, d in {**AGENTS, **RESEARCH, **REPORT, **RUN, **FILE_AGENTS}.items()]
    out += [{'name': g, 'description': GUARD_INFO[g], 'kind': 'guard', 'engine_required': False, 'available': True} for g in GUARDS]
    out += [custom_info(router, a) for a in router.customs]
    return out


def custom_info(router, a: dict) -> dict:
    return {'name': a['name'], 'description': a['description'], 'kind': 'custom', 'engine_required': True,
            'available': router.engine is not None, 'prompt': a['prompt'], 'web': a['web']}


def validate_agent(router, body: dict, draft: bool = False) -> dict:
    """A custom agent from a request body. A sandbox `draft` is checked the same way but is never stored, so it
    doesn't count against MAX_CUSTOM."""
    name, desc, prompt = (body.get(k) for k in ('name', 'description', 'prompt'))
    if not isinstance(name, str) or not NAME.match(name):
        raise Bad('name must be 2-24 characters: lowercase letters, digits, - or _, starting with a letter')
    if name in {**AGENTS, **RESEARCH, **REPORT, **RUN, **FILE_AGENTS} or name in GUARDS:
        raise Bad(f'{name} is a built-in agent name')
    if any(a['name'] == name for a in router.customs):
        raise Bad(f'a custom agent named {name} already exists')
    if not isinstance(desc, str) or not 10 <= len(desc.strip()) <= 200:
        raise Bad('description must be 10-200 characters')
    if not isinstance(prompt, str) or not 10 <= len(prompt.strip()) <= 4000:
        raise Bad('prompt must be 10-4000 characters')
    if not draft and len(router.customs) >= MAX_CUSTOM:
        raise Bad(f'at most {MAX_CUSTOM} custom agents')
    return {'name': name, 'description': desc.strip(), 'prompt': prompt.strip(), 'web': bool(body.get('web', False))}


async def list_agents(request):
    return web.json_response({'agents': agent_list(request.app[ROUTER])})


async def create_agent(request):
    body, router = await read_json(request), request.app[ROUTER]
    try:
        a = validate_agent(router, body)
    except Bad as e:
        return err(str(e), e.status)
    router.store.add_agent(a)
    router.reload_customs()
    emit_config(router)  # Jev's route criteria changed, so every browser's agent list does too
    return web.json_response(custom_info(router, a), status=201)


async def delete_agent(request):
    router, name = request.app[ROUTER], request.match_info['name']
    if not any(a['name'] == name for a in router.customs):
        return err(f'{name} is not a custom agent')
    router.store.delete_agent(name)
    router.reload_customs()
    emit_config(router)
    return web.json_response({'ok': True})


# ---------- files ----------

async def read_upload(request, keep_raw: bool = False):
    """The multipart "file" field, extracted: (meta, text), or (meta, text, raw) with keep_raw; an error response
    when the upload is missing, too large or unreadable."""
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError, KeyError):
        return err('expected multipart/form-data with a "file" field')
    while (field := await reader.next()) is not None:
        if field.name != 'file':
            continue
        name = os.path.basename(field.filename or 'upload.txt')[:120]
        buf = bytearray()
        while chunk := await field.read_chunk(65536):
            buf += chunk
            if len(buf) > MAX_BYTES:
                return err('file is larger than 10 MB', 413)
        try:
            meta, text = await asyncio.to_thread(extract, name, bytes(buf))  # PDF parsing is CPU-bound
        except FileError as e:
            return err(str(e))
        return (meta, text, bytes(buf)) if keep_raw else (meta, text)
    return err('no "file" field in the upload')


async def upload_file(request):
    got = await read_upload(request, keep_raw=True)
    if isinstance(got, web.Response):
        return got
    meta, text, raw = got
    request.app[ROUTER].store.add_file(meta, raw, text)
    return web.json_response(meta, status=201)


async def list_files(request):
    return web.json_response({'files': request.app[ROUTER].store.list_files()})


async def delete_file(request):
    if not request.app[ROUTER].store.delete_file(request.match_info['id']):
        return err('no such file', 404)
    return web.json_response({'ok': True})


# ---------- created files (docs/PLAN-files.md) ----------

MAX_CREATED_LIST = 200


def disposition(name: str) -> str:
    """Content-Disposition for a download (RFC 6266): a plain ASCII quoted filename, plus filename* for the exact name.
    Names are ours (F7: lowercase words and hyphens), but nothing that could close the quotes or a header gets through."""
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', '_', name)[:120].lstrip('.') or 'download'
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name, safe="")}'


def file_response(meta: dict, data: bytes) -> web.Response:
    return web.Response(body=data, headers={
        'Content-Type': create_mod.MIME[meta['format']], 'Content-Disposition': disposition(meta['name']),
        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store',
        'Content-Security-Policy': "default-src 'none'; sandbox"})


def created_id(request) -> str:
    fid = request.match_info['fid']
    if not CREATED_ID.match(fid):
        raise Bad('no such file', 404)
    return fid


def stored_created(request) -> tuple[Store, dict]:
    store = request.app[ROUTER].store
    fid = created_id(request)
    meta = store.get_created(fid)
    if meta is None:
        raise Bad('no such file', 404)
    return store, meta


async def list_created(request):
    q = request.query
    try:
        limit = int_param(q, 'limit', 50, 1, MAX_CREATED_LIST)
        before = float(q['before']) if q.get('before') else None
    except (Bad, ValueError):
        return err('limit and before must be numbers')
    return web.json_response({'files': request.app[ROUTER].store.list_created(limit, before)})


async def get_created(request):
    try:
        _, meta = stored_created(request)
    except Bad as e:
        return err(str(e), e.status)
    return web.json_response(meta)


async def delete_created(request):
    try:
        store, meta = stored_created(request)
    except Bad as e:
        return err(str(e), e.status)
    store.delete_created(meta['id'])
    return web.json_response({'ok': True})


async def download_created(request):
    try:
        store, meta = stored_created(request)
        data = store.created_raw(meta['id'])
    except Bad as e:
        return err(str(e), e.status)
    except OSError:
        return err('the file\'s content is missing', 404)
    return file_response(meta, data)


async def preview_of(meta: dict, data: bytes):
    try:
        return web.json_response(await asyncio.to_thread(create_mod.preview, meta['format'], data))
    except Exception as e:
        return err(f'no preview: {str(e)[:160]}', 422)


async def preview_created(request):
    try:
        store, meta = stored_created(request)
        data = store.created_raw(meta['id'])
    except Bad as e:
        return err(str(e), e.status)
    except OSError:
        return err('the file\'s content is missing', 404)
    return await preview_of(meta, data)


async def convert_created(request):
    """A new file in another format from the stored spec: no LLM (0 tokens, rule L5), source convert, from_id."""
    body = await read_json(request)
    fmt = body.get('format')
    try:
        if fmt not in create_mod.FORMATS:
            raise Bad(f'format must be one of {", ".join(create_mod.FORMATS)}')
        store, meta = stored_created(request)
        if meta['format'] == fmt:
            raise Bad(f'{meta["name"]} is already {fmt}', 409)
        spec = store.created_spec(meta['id'])
        # with TG_STUDIO on for the format the design stage lays it out keyless (0 tokens), seeded from the source
        # file's workspace (docs/PLAN-designer.md 9.10); off, this is build() exactly
        new, spec, data, _ = await maker.build_designed(spec, fmt, source='convert', tokens=0, from_id=meta['id'],
                                                        extra=maker.carried(meta), seed_from=meta['id'])
    except Bad as e:
        return err(str(e), e.status)
    except create_mod.SpecError as e:
        return err(f'Rule {e.rule_id} blocked it: {e.message}', 422)
    except Exception as e:  # a stored spec the renderer can't lay out is an honest 422, never a bare 500
        return err(f'The {fmt} file could not be made ({type(e).__name__}: {str(e)[:160]})', 422)
    store.add_created(new, spec, data)
    return web.json_response(new, status=201)


async def list_rules(request):
    return web.json_response({'rules': create_mod.RULES})


def sandbox_created(request) -> tuple[dict, dict, bytes]:
    """A file a sandbox run made, from that sandbox's memory only (nothing is on disk)."""
    sid = sandbox_id(request)
    fid = created_id(request)
    mem = request.app[ROUTER].sandboxes.peek(sid)
    held = mem.created.get(fid) if mem else None
    if held is None:
        raise Bad('no such file', 404)
    return held


async def download_sandbox_created(request):
    try:
        meta, _, data = sandbox_created(request)
    except Bad as e:
        return err(str(e), e.status)
    return file_response(meta, data)


async def preview_sandbox_created(request):
    try:
        meta, _, data = sandbox_created(request)
    except Bad as e:
        return err(str(e), e.status)
    return await preview_of(meta, data)


# ---------- Studio: presets, fonts, thumbnails, the design report, restyle and polish (docs/PLAN-designer.md 9.9) ----------
# Every /api/created/{fid}/... route here also exists as /api/sandbox/{id}/created/{fid}/... for sandbox files, whose
# design workspace lives in the sandbox's temp dir. Failures inside Studio are honest statuses, never a bare 500.

RESTYLE_KEYS = ('preset', 'fonts', 'dark', 'template', 'layouts', 'print')
FONT_ROLES = ('display', 'heading', 'body')
NOT_DESIGNED = 'This file was made without the design stage, so it has no thumbnails.'
NO_VISION = ("Polish needs an engine that can read images (Claude Code or the Anthropic API); {what} can't, so the "
             "design was not changed.")


def png_response(data: bytes, cache: str = 'private, max-age=300') -> web.Response:
    return web.Response(body=data, headers={'Content-Type': 'image/png', 'X-Content-Type-Options': 'nosniff',
                                            'Cache-Control': cache})


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's IHDR chunk; (0, 0) when it isn't one."""
    if len(data) >= 24 and data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR':
        return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    return 0, 0


async def design_presets(request):
    """GET /api/design/presets: every preset (with its thumbnail URL) and every student template."""
    from .studio import presets

    def listing():
        return ([p.to_dict() for p in presets.list_presets()], [t.to_dict() for t in presets.list_templates()])
    try:
        ps, ts = await asyncio.to_thread(listing)
    except Exception as e:
        return err(f'The design presets could not be listed ({type(e).__name__}).', 503)
    return web.json_response({'presets': ps, 'templates': ts})


async def design_preset_thumb(request):
    """GET /api/design/presets/{id}/thumb: a sample cover slide in the preset (PNG)."""
    from .studio import presets, thumbs
    pid = request.match_info['id']
    if pid not in presets.PRESETS:
        return err('no such preset', 404)
    try:
        data = await asyncio.to_thread(thumbs.preset_thumb, pid)
    except Exception as e:
        return err(f'The preview of {pid} could not be drawn ({type(e).__name__}).', 503)
    return png_response(data, 'public, max-age=86400')


async def fonts_search(request):
    """GET /api/fonts/search?q=&limit=20: open-licensed families only (an empty q lists the curated ones)."""
    from .studio import fonts
    q = request.query.get('q', '')
    if len(q) > FONT_QUERY_CHARS:
        return err(f'q is at most {FONT_QUERY_CHARS} characters')
    raw = request.query.get('limit', '')
    try:
        limit = int(raw) if raw.strip() else 20
    except ValueError:
        return err('limit must be a whole number')
    if not 1 <= limit <= FONT_LIMIT_MAX:
        return err(f'limit must be 1 to {FONT_LIMIT_MAX}')
    try:
        found, offline = await fonts.search(q.strip(), request.app[HTTP], limit)
    except Exception:  # search never raises by contract; if it does, it is offline with nothing found
        found, offline = [], True
    out = [f.to_dict() if hasattr(f, 'to_dict') else dict(f) for f in found or []]
    out = [f for f in out if f.get('licence') in fonts.LICENCES_OK][:limit]  # never a font that isn't open
    return web.json_response({'fonts': out, 'offline': bool(offline)})


async def font_preview(request):
    """GET /api/fonts/preview?family=&text=: a PNG sample of a cached or installed open family."""
    from .studio import fonts
    family = request.query.get('family', '').strip()
    text = request.query.get('text')
    if not family or len(family) > FONT_QUERY_CHARS:
        return err(f'family must be 1 to {FONT_QUERY_CHARS} characters')
    if text is not None and len(text) > FONT_PREVIEW_CHARS:
        return err(f'text is at most {FONT_PREVIEW_CHARS} characters')
    try:
        data = await asyncio.to_thread(fonts.preview_png, family, text) if text else \
            await asyncio.to_thread(fonts.preview_png, family)
    except Exception:
        data = None
    if not data:
        return err(f'{family} is not cached or installed here, or is not openly licensed.', 404)
    return png_response(data, 'public, max-age=86400')


def held_created(request) -> tuple[dict, dict | None, str | None]:
    """(CreatedFile, its stored spec or None, sandbox id or None) for a stored file or, on a /api/sandbox/{id}/ route,
    a sandbox file. Raises Bad 404 when there is no such file."""
    if 'id' in request.match_info:
        meta, spec, _ = sandbox_created(request)
        return meta, spec, request.match_info['id']
    store, meta = stored_created(request)
    return meta, store.created_spec(meta['id']), None


def created_base(meta: dict, sid: str | None) -> str:
    return f'/api/sandbox/{sid}/created/{meta["id"]}' if sid else f'/api/created/{meta["id"]}'


def designed(meta: dict) -> bool:
    return bool((meta.get('design') or {}).get('studio'))


def open_ws(fid: str, sid: str | None):
    from .studio import workspace
    return workspace.open_workspace(fid, sandbox=sid)


def load_design(fid: str, sid: str | None, *, plan=True, report=False, thumbs=False) -> dict:
    """What a file's workspace holds, read in a thread: {ws, plan, report, thumbs}. Missing pieces are None/[]."""
    ws = open_ws(fid, sid)
    out = {'ws': ws, 'plan': None, 'report': None, 'thumbs': []}
    if not ws.exists():
        return out
    if plan:
        out['plan'] = ws.load_plan()
    if report:
        out['report'] = ws.load_report()
    if thumbs:
        out['thumbs'] = list(ws.thumbs())
    return out


def plan_summary(plan) -> dict | None:
    """DesignPlanSummary (web/src/protocol.ts): the plan without its boxes."""
    if plan is None:
        return None
    import dataclasses
    return {'preset': plan.preset, 'format': plan.format,
            'pages': [{'index': p.index, 'layout': p.layout, 'variant': p.variant, 'freeform': p.freeform,
                       'section': p.section} for p in plan.pages],
            'fonts': [dataclasses.asdict(f) for f in plan.fonts], 'assets': len(plan.assets), 'score': plan.score,
            'rounds': plan.rounds, 'stop': plan.stop, 'tokens': dict(plan.tokens or {})}


async def created_thumbs(request):
    """GET .../created/{fid}/thumbs: {thumbs: Thumb[], reason?}; [] and a reason for a file made without Studio."""
    try:
        meta, _, sid = held_created(request)
    except Bad as e:
        return err(str(e), e.status)
    if not designed(meta):
        return web.json_response({'thumbs': [], 'reason': NOT_DESIGNED})
    try:
        got = await asyncio.to_thread(load_design, meta['id'], sid, thumbs=True)
    except Exception:
        return web.json_response({'thumbs': [], 'reason': 'The design workspace of this file could not be read.'})
    if not got['thumbs']:
        return web.json_response({'thumbs': [], 'reason': 'No thumbnails are stored for this file (design workspaces '
                                                          'are kept for 14 days).'})
    pages = got['plan'].pages if got['plan'] is not None else []
    base, out = created_base(meta, sid), []
    for i, path in enumerate(got['thumbs']):
        try:
            with open(path, 'rb') as fh:
                w, h = png_size(fh.read(24))
        except OSError:
            continue
        out.append({'page': i + 1, 'url': f'{base}/thumbs/{i + 1}.png', 'w': w, 'h': h,
                    'layout': pages[i].layout if i < len(pages) else 'freeform'})
    return web.json_response({'thumbs': out})


async def created_thumb(request):
    """GET .../created/{fid}/thumbs/{n}.png: one thumbnail, n 1-based."""
    try:
        meta, _, sid = held_created(request)
        n = int(request.match_info['n'])
        if not designed(meta) or n < 1:
            raise Bad('no such thumbnail', 404)
        got = await asyncio.to_thread(load_design, meta['id'], sid, plan=False, thumbs=True)
        if n > len(got['thumbs']):
            raise Bad('no such thumbnail', 404)
        data = await asyncio.to_thread(Path(got['thumbs'][n - 1]).read_bytes)
    except Bad as e:
        return err(str(e), e.status)
    except Exception:
        return err('no such thumbnail', 404)
    return png_response(data)


async def created_design(request):
    """GET .../created/{fid}/design: {report: DesignReport | null, plan: DesignPlanSummary | null}."""
    try:
        meta, _, sid = held_created(request)
    except Bad as e:
        return err(str(e), e.status)
    if not designed(meta):
        return web.json_response({'report': None, 'plan': None})
    try:
        got = await asyncio.to_thread(load_design, meta['id'], sid, report=True)
    except Exception:
        return web.json_response({'report': None, 'plan': None})
    return web.json_response({'report': got['report'], 'plan': plan_summary(got['plan'])})


def restyle_options(body: dict, fmt: str, plan=None):
    """RestyleBody -> studio RestyleOptions (raises Bad 400): known presets, templates and layouts for the format, open
    fonts only, and at least one change."""
    from .studio import fonts as studio_fonts
    from .studio.library import PAGE_TEMPLATES, SLIDE_LAYOUTS
    from .studio.plan import RestyleOptions
    from .studio.presets import PRESETS, TEMPLATES
    if unknown := [k for k in body if k not in RESTYLE_KEYS]:
        raise Bad(f'unknown field {unknown[0][:40]!r}; send preset, fonts, dark, template, layouts or print')
    given = {k: v for k, v in body.items() if v is not None and v != {} and v is not False}
    if not given:
        raise Bad('Say what to change: a preset, fonts, dark, a template, page layouts or the print version.')
    preset, template, dark = body.get('preset'), body.get('template'), body.get('dark')
    if preset is not None and preset not in PRESETS:
        raise Bad(f'unknown preset {str(preset)[:40]!r}; use one of {", ".join(PRESETS)}')
    if template is not None and template not in TEMPLATES:
        raise Bad(f'unknown template {str(template)[:40]!r}; use one of {", ".join(TEMPLATES)}')
    if dark is not None and not isinstance(dark, bool):
        raise Bad('dark must be true or false')
    if body.get('print') is not None and not isinstance(body.get('print'), bool):
        raise Bad('print must be true or false')
    fonts = body.get('fonts')
    if fonts is not None:
        if not isinstance(fonts, dict) or any(k not in FONT_ROLES for k in fonts):
            raise Bad('fonts must map display, heading or body to a font family')
        for role, fam in fonts.items():
            if not isinstance(fam, str) or not fam.strip() or len(fam) > FONT_QUERY_CHARS:
                raise Bad(f'the {role} font must be a family name of 1 to {FONT_QUERY_CHARS} characters')
            try:
                alt = studio_fonts.alternative(fam.strip())
            except Exception:
                alt = None
            if alt is not None:
                raise Bad(f"{fam.strip()} isn't openly licensed, so it can't be used; {alt[0]} is the closest open "
                          f"match.")
        fonts = {k: v.strip() for k, v in fonts.items()} or None
    layouts = body.get('layouts')
    if layouts is not None:
        allowed = SLIDE_LAYOUTS if fmt == 'pptx' else PAGE_TEMPLATES
        if not isinstance(layouts, dict):
            raise Bad('layouts must map page numbers (0-based, as text) to layout ids')
        pages = len(plan.pages) if plan is not None else None
        out = {}
        for k, v in layouts.items():
            if not (isinstance(k, str) and k.isdigit() and len(k) <= 3):
                raise Bad(f'layout page {str(k)[:12]!r} must be a 0-based page number')
            if pages is not None and int(k) >= pages:
                raise Bad(f'page {int(k) + 1} is not in this file ({pages} pages)')
            if v not in allowed:
                raise Bad(f'unknown layout {str(v)[:40]!r} for a {fmt} file')
            out[int(k)] = v
        layouts = out or None
    return RestyleOptions(preset=preset, fonts=fonts, dark=dark, template=template, layouts=layouts,
                          print_version=bool(body.get('print')))


def keep_created(router, sid: str | None, meta: dict, spec: dict, data: bytes) -> dict:
    """Store a file an endpoint made: in the store, or in its sandbox's memory only."""
    if sid:
        meta = {**meta, 'sandbox': sid}
        mem = router.sandboxes.get(sid)
        if mem is None:
            raise Bad('no such file', 404)
        mem.add_created(meta, spec, data)
    else:
        router.store.add_created(meta, spec, data)
    return meta


def drop_ws(fid: str | None, sid: str | None):
    if not fid:
        return
    try:
        open_ws(fid, sid).delete()
    except Exception:
        pass


async def restyle_created(request):
    """POST .../created/{fid}/restyle (RestyleBody): the stored content re-laid out and re-painted with another preset,
    fonts, dark or light, template or per-page layouts, at 0 tokens; a new CreatedFile (source convert, from_id)."""
    from .studio import agent as studio_agent
    from .studio.plan import PAINTED
    router = request.app[ROUTER]
    body = await read_json(request)
    tmp, new_id, sid = None, uuid.uuid4().hex[:12], None
    try:
        meta, spec, sid = held_created(request)
        fmt = meta['format']
        if spec is None:
            raise Bad('This file has no stored content to restyle.', 409)
        got = await asyncio.to_thread(load_design, meta['id'], sid) if designed(meta) else {'plan': None, 'ws': None}
        opts = restyle_options(body, fmt, got['plan'])
        if fmt not in PAINTED:
            raise Bad('Restyle lays out PowerPoint and PDF files. Convert this file to one of them first (0 tokens).')
        norm, _ = await asyncio.to_thread(create_mod.normalize, spec, fmt)
        src = got['ws']
        if got['plan'] is None:
            # made without the design stage: design it first (keyless, 0 tokens), then restyle that design
            from .studio.presets import template as template_of
            tmp = uuid.uuid4().hex[:12]
            preset = opts.preset or (getattr(template_of(opts.template), 'preset', None) if opts.template else None)
            await asyncio.wait_for(studio_agent.design(norm, fmt, file_id=tmp, request='', engine=None, http=None,
                                                       mode='quick', sandbox=sid, preset=preset),
                                   timeout=STUDIO_TIME_BUDGET + STUDIO_GRACE)
            src = open_ws(tmp, sid)
        result = await asyncio.to_thread(studio_agent.restyle, norm, fmt, src, opts, file_id=new_id)
        if getattr(result, 'painted_bytes', None) is None:
            raise RuntimeError('nothing was painted')
        new, spec, data = await asyncio.to_thread(maker.build, spec, fmt, source='convert', tokens=0,
                                                  from_id=meta['id'], extra=maker.carried(meta), studio=result,
                                                  file_id=new_id)
        new = keep_created(router, sid, new, spec, data)
    except Bad as e:
        drop_ws(new_id, sid)
        return err(str(e), e.status)
    except create_mod.SpecError as e:
        drop_ws(new_id, sid)
        return web.json_response({'error': f'Rule {e.rule_id} blocked it: {e.message}', 'rule': e.rule_id}, status=422)
    except Exception as e:  # the design stage failed: an honest 422, and the original file is untouched
        drop_ws(new_id, sid)
        why = 'it took too long' if isinstance(e, asyncio.TimeoutError) else f'{type(e).__name__}: {str(e)[:160]}'
        return web.json_response({'error': f'The file could not be restyled ({why}).', 'rule': 'V1'}, status=422)
    finally:
        drop_ws(tmp, sid)
    return web.json_response(new)


def polish_engine(router, name):
    """The engine a Polish uses: the named one or the active one. Auto stays Auto while its chain has a healthy
    engine that can read images (it routes the image call there); otherwise the first available one that can.
    Raises Bad: 400 unknown or unable to read images (keyless included), 503 unavailable."""
    from .studio.critic import can_see
    if name is not None and not isinstance(name, str):
        raise Bad('engine must be an engine name')
    if name == 'none':
        raise Bad(NO_VISION.format(what='keyless mode'))
    engine = router.engine if name is None else router.engines.get(name)
    if name is not None and engine is None:
        raise Bad(f'unknown engine {name[:40]!r}')
    if engine is None:
        raise Bad(NO_VISION.format(what='keyless mode'))
    if hasattr(engine, 'chain') and not can_see(engine):
        seeing = [e for e in engine.chain() if can_see(e)]
        if not seeing:
            raise Bad(NO_VISION.format(what='none of the engines Auto can use'))
        engine = next((e for e in seeing if e.available()[0]), seeing[0])
    elif not can_see(engine):
        raise Bad(NO_VISION.format(what=getattr(engine, 'label', engine.name)))
    ok, why = engine.available()
    if not ok:
        raise Bad(f'{engine.label} is not available: {why}', 503)
    return engine


def polish_estimate(router, engine, plan, meta: dict):
    """What one critic call costs: the contact sheets (6 pages each) and the design report in, the edits out."""
    import math
    seer = engine.seer() if callable(getattr(engine, 'seer', None)) else None
    view = router.engine_view(seer or engine)   # on Auto, the backend the image call goes to
    sheets = max(1, math.ceil(len(plan.pages or []) / STUDIO_SHEET_PAGES))
    calls = [estimate_mod.price('critic', view, prompt=STUDIO_CRITIC_PROMPT_TOKENS, ctx=sheets * STUDIO_SHEET_TOKENS,
                                base_out=STUDIO_CRITIC_OUT)]
    return estimate_mod.finish(calls, None, view, router.deadline(meta.get('title') or '', engine), [], False, PRICES,
                               what=meta['format'])


async def polish_created(request):
    """POST .../created/{fid}/polish (PolishBody): one critic round on the stored design with a vision engine, edits
    applied by code and QA re-run; priced and guarded like /ask (409 needs_confirmation). A new CreatedFile."""
    from .engines import EngineRefusal
    from .studio import agent as studio_agent
    router = request.app[ROUTER]
    body = await read_json(request)
    new_id, sid = uuid.uuid4().hex[:12], None
    try:
        meta, spec, sid = held_created(request)
        if unknown := [k for k in body if k not in ('engine', 'confirm_cost')]:
            raise Bad(f'unknown field {unknown[0][:40]!r}; send engine or confirm_cost')
        if 'confirm_cost' in body and not isinstance(body['confirm_cost'], bool):
            raise Bad('confirm_cost must be true or false')
        if spec is None:
            raise Bad('This file has no stored content to polish.', 409)
        got = await asyncio.to_thread(load_design, meta['id'], sid) if designed(meta) else {'plan': None}
        if got['plan'] is None:
            raise Bad('This file has no design plan to polish. Restyle it first to lay it out (0 tokens).', 409)
        engine = polish_engine(router, body.get('engine'))
        est = polish_estimate(router, engine, got['plan'], meta)
    except Bad as e:
        return err(str(e), e.status)
    if (resp := cost_guard(body, 'sandbox' if sid else 'chat', est)) is not None:
        return resp
    fmt = meta['format']
    try:
        norm, _ = await asyncio.to_thread(create_mod.normalize, spec, fmt)
        budget = STUDIO_TIME_BUDGET + STUDIO_GRACE
        result = await asyncio.wait_for(studio_agent.polish(norm, fmt, got['ws'], engine, file_id=new_id,
                                                            deadline=time.monotonic() + STUDIO_TIME_BUDGET),
                                        timeout=budget)
        if getattr(result, 'painted_bytes', None) is None:
            raise RuntimeError('nothing was painted')
        used = maker.studio_usage(result)
        new, spec, data = await asyncio.to_thread(maker.build, spec, fmt, source='convert',
                                                  tokens=used['llm_in'] + used['llm_out'], from_id=meta['id'],
                                                  extra=maker.carried(meta), studio=result, file_id=new_id)
        new = keep_created(router, sid, new, spec, data)
    except ValueError as e:
        drop_ws(new_id, sid)
        if str(e) == 'no-vision':
            return err(NO_VISION.format(what=getattr(engine, 'label', 'this engine')))
        if isinstance(e, create_mod.SpecError):
            return web.json_response({'error': f'Rule {e.rule_id} blocked it: {e.message}', 'rule': e.rule_id},
                                     status=422)
        return web.json_response({'error': f'The design could not be polished ({str(e)[:160]}).', 'rule': 'V1'},
                                 status=422)
    except LookupError:
        drop_ws(new_id, sid)
        return err('This file has no design plan to polish. Restyle it first to lay it out (0 tokens).', 409)
    except (EngineError, EngineRefusal) as e:
        drop_ws(new_id, sid)
        return err(f'{engine.label} could not polish the design ({str(getattr(e, "why", "") or e)[:160]}).', 503)
    except Bad as e:
        drop_ws(new_id, sid)
        return err(str(e), e.status)
    except Exception as e:
        drop_ws(new_id, sid)
        why = 'it took too long' if isinstance(e, asyncio.TimeoutError) else f'{type(e).__name__}: {str(e)[:160]}'
        return web.json_response({'error': f'The design could not be polished ({why}).', 'rule': 'V1'}, status=422)
    if not sid and (used['llm_in'] or used['llm_out']):
        router.usage(None, {'claude_in': used['llm_in'], 'claude_out': used['llm_out']})
    return web.json_response(new)


def prune_design_caches():
    """Startup: the font cache and design workspaces back under their caps and TTL (docs/PLAN-designer.md 3.3, 3.6)."""
    for mod, fn in (('fonts', 'prune'), ('workspace', 'prune')):
        try:
            module = __import__(f'jevrouter.studio.{mod}', fromlist=[fn])
            getattr(module, fn)()
        except Exception:
            pass


# ---------- compare ----------

async def compare(request):
    body, router = await read_json(request), request.app[ROUTER]
    text = str(body.get('query', '')).strip()
    names = body.get('engines')
    if (resp := too_long(text)) is not None:
        return resp
    try:
        if not text:
            raise Bad('empty query')
        if not isinstance(names, list) or not 2 <= len(set(map(str, names))) == len(names) <= 4:
            raise Bad('engines must list 2 to 4 different engines')
        engines = [pick_engine(router, n) for n in names]
        if 'confirm_cost' in body and not isinstance(body['confirm_cost'], bool):
            raise Bad('confirm_cost must be true or false')
    except Bad as e:
        return err(str(e), e.status)
    # 5.4: a costly comparison (a long file on up to 4 engines) is priced and confirmed like /ask
    ests = []
    try:
        chat = {'mode': 'balanced', 'style': 'default', 'agent': None}
        draft = estimate_mod.Draft(text, 'balanced', None, [], False, None, 'compare')
        ests = [estimate_for(router, draft, chat, e, text) for e in engines]
    except Exception:
        ests = []  # a bug in pricing never stops a run
    if (resp := cost_guard(body, 'compare', estimate_mod.combine(ests) if ests else None)) is not None:
        return resp
    cid = uuid.uuid4().hex[:10]
    runs = [{'engine': str(n), 'qid': router.submit(text, 'compare', engine=e, compare_id=cid,
                                                     extras=with_estimate(None, ests[i] if ests else None))}
            for i, (n, e) in enumerate(zip(names, engines))]
    return web.json_response({'compare_id': cid, 'runs': runs})


async def get_compare(request):
    cid = request.match_info['id']
    runs = request.app[ROUTER].runs_where('compare_id', cid)
    if not runs:
        return err('no such comparison', 404)
    return web.json_response({'compare_id': cid, 'query': runs[0]['text'], 'runs': [public_run(r) for r in runs]})


# ---------- evals ----------

async def run_eval(request):
    """RunEvalBody: engine, examples, split (dev/holdout/all), tags (any of), repeat (1-5), judge (engine name, auto or
    null), mode (full, or route: only routing, no agent runs) and jev (route mode: live, or replay from the committed
    cassette; recording happens only from the CLI). A judge that is the engine under test is swapped for another
    available engine when there is one; auto takes the first healthy engine in STRONGEST order."""
    body, router = await read_json(request), request.app[ROUTER]
    examples, split, tags = body.get('examples'), body.get('split', 'all'), body.get('tags')
    repeat, judge = body.get('repeat', 1), body.get('judge')
    mode, jev = body.get('mode') or 'full', body.get('jev') or 'live'
    try:
        if mode not in evals_mod.EVAL_MODES:
            raise Bad(f'mode must be one of {", ".join(evals_mod.EVAL_MODES)}')
        if jev not in ('live', 'replay'):
            raise Bad('jev must be live or replay (a cassette is recorded from the CLI)')
        if jev == 'replay' and mode != 'route':
            raise Bad('jev replay needs mode route')
        if jev == 'replay' and not evals_mod.CASSETTE.exists():
            raise Bad('no Jev cassette has been recorded', 409)
        if mode == 'route' and judge is not None:
            raise Bad('route mode has no answers to judge')
        engine = pick_engine(router, body['engine']) if body.get('engine') else router.engine
        if examples is not None and not isinstance(examples, bool):
            raise Bad('examples must be true or false')
        if split not in evals_mod.SPLITS:
            raise Bad('split must be dev, holdout or all')
        if tags is not None and not (isinstance(tags, list) and len(tags) <= 50 and all(isinstance(t, str) and t for t in tags)):
            raise Bad('tags must be a list of tag names')
        if type(repeat) is not int or not 1 <= repeat <= evals_mod.MAX_REPEAT:
            raise Bad(f'repeat must be a whole number from 1 to {evals_mod.MAX_REPEAT}')
        if judge is not None and not (isinstance(judge, str) and (judge == 'auto' or judge in router.engines)):
            raise Bad(f'unknown judge engine {judge!r}')
        try:
            judge_engine, _ = judge_mod.pick(router.engines, judge, engine, healthy=router.healthy)
        except judge_mod.JudgeError as e:
            raise Bad(str(e), 409)
        try:
            cases = evals_mod.select(evals_mod.load_cases(), split, tags)
        except evals_mod.CaseError as e:
            raise Bad(f'bad eval case: {e}', 500)
        if not cases:
            raise Bad('no cases match that split and those tags')
    except Bad as e:
        return err(str(e), e.status)
    if mode == 'route':
        engine = None
    eid = evals_mod.start(router, engine, engine.name if engine else 'none', cases, examples, split=split, tags=tags or None,
                          repeat=repeat, judge=judge_engine, mode=mode, jev=jev)
    return web.json_response({'eval_id': eid})


async def compare_evals(request):
    store, q = request.app[ROUTER].store, request.query
    if not q.get('a') or not q.get('b'):
        return err('a and b must be eval ids')
    a, b = evals_mod.get(store, q['a']), evals_mod.get(store, q['b'])
    if a is None or b is None:
        return err(f"no such eval {q['a'] if a is None else q['b']}", 404)
    return web.json_response(evals_mod.compare(a, b))


async def list_evals(request):
    return web.json_response({'evals': evals_mod.listed(request.app[ROUTER].store)})


async def get_eval(request):
    e = evals_mod.get(request.app[ROUTER].store, request.match_info['id'])
    return web.json_response(e) if e else err('no such eval', 404)


async def cancel_eval(request):
    router, eid = request.app[ROUTER], request.match_info['id']
    task = router.evals.get(eid)
    if task is not None:
        task.cancel()
        return web.json_response({'ok': True})
    return err('that eval has already finished', 409) if router.store.get_eval(eid) else err('no such eval', 404)


# ---------- labels, review and route examples (docs/PLAN-learning.md) ----------

MAX_LABELS = 500


def int_param(q, key: str, default=None, lo: int = 1, hi: int | None = None):
    if q.get(key) in (None, ''):
        return default
    try:
        v = int(q[key])
    except ValueError:
        raise Bad(f'{key} must be an integer')
    return max(lo, min(hi, v)) if hi else v


async def create_label(request):
    body, router = await read_json(request), request.app[ROUTER]
    try:
        label = labels_mod.make_label(router, body)
    except labels_mod.LabelError as e:
        return err(str(e), e.status)
    return web.json_response(label)


async def list_labels(request):
    router, q = request.app[ROUTER], request.query
    try:
        limit = int_param(q, 'limit', 100, 1, MAX_LABELS)
        qid = int_param(q, 'qid', lo=0)
        if q.get('verdict') and q['verdict'] not in labels_mod.VERDICTS:
            raise Bad('verdict must be right or wrong')
    except Bad as e:
        return err(str(e), e.status)
    labels = router.store.list_labels(q.get('agent') or None, q.get('verdict') or None, qid, limit)
    return web.json_response({'labels': labels})


async def delete_label(request):
    router = request.app[ROUTER]
    if not router.store.delete_label(request.match_info['id']):
        return err('no such label', 404)
    router.refresh_labels()  # a deleted label stops steering routing at once
    return web.json_response({'ok': True})


async def promote_label(request):
    try:
        return web.json_response(labels_mod.promote(request.app[ROUTER], request.match_info['id']))
    except labels_mod.LabelError as e:
        return err(str(e), e.status)


async def review(request):
    q = request.query
    try:
        limit = int_param(q, 'limit', 50, 1, MAX_LABELS)
        reason = q.get('reason') or None
        if reason is not None and reason not in labels_mod.REASONS:
            raise Bad(f'reason must be one of {", ".join(labels_mod.REASONS)}')
    except Bad as e:
        return err(str(e), e.status)
    return web.json_response(await asyncio.to_thread(labels_mod.review, request.app[ROUTER], limit, reason))


async def agent_examples(request):
    """Exactly what Jev would see now: each offered agent's examples and not-list (only agents that have some)."""
    router = request.app[ROUTER]
    offered = {**router.agents, **FILE_AGENTS}
    ex = examples_for(offered, router.labels)
    return web.json_response({'enabled': router.route_examples,
                              'agents': [{'agent': a, **ex[a]} for a in offered if a in ex]})


# ---------- engines ----------

async def engine_health(request):
    router = request.app[ROUTER]
    return web.json_response({'engines': router.health.snapshot(router.engines)})


TEST_PROMPT = 'Reply with exactly: ok'
TEST_TIMEOUT = 60.0


async def test_engine(request):
    engine = request.app[ROUTER].engines.get(request.match_info['name'])
    if engine is None:
        return err('unknown engine', 404)
    ok, why = engine.available()
    if not ok:
        return web.json_response({'ok': False, 'ms': 0, 'error': why})
    unblock(engine)  # a test is how you check a refilled quota or a fresh login: really ask the engine
    t0 = time.perf_counter()
    ms = lambda: round((time.perf_counter() - t0) * 1000)
    try:
        r = await asyncio.wait_for(engine.stream(system=TEST_PROMPT, prompt=TEST_PROMPT, effort='low', max_tokens=64), TEST_TIMEOUT)
    except TimeoutError:
        return web.json_response({'ok': False, 'ms': ms(), 'error': f'timed out after {TEST_TIMEOUT:.0f}s'})
    except EngineError as e:
        return web.json_response({'ok': False, 'ms': ms(), 'error': e.why})
    except Exception as e:
        return web.json_response({'ok': False, 'ms': ms(), 'error': f'{type(e).__name__}: {str(e)[:200]}'})
    return web.json_response({'ok': bool(r.text), 'ms': ms(), 'text': r.text[:200]})


def background(coro):
    task = asyncio.create_task(coro)
    WARMING.add(task)
    task.add_done_callback(WARMING.discard)


async def control(request):
    body, router = await read_json(request), request.app[ROUTER]
    try:
        if 'autopilot' in body:
            router.state['autopilot'] = bool(body['autopilot'])
        if 'interval' in body:
            router.state['interval'] = max(1.0, min(15.0, float(body['interval'])))
    except (TypeError, ValueError):
        return web.json_response({'error': 'interval must be a number'}, status=400)
    if 'route_examples' in body:
        if not isinstance(body['route_examples'], bool):
            return web.json_response({'error': 'route_examples must be true or false'}, status=400)
        router.set_route_examples(body['route_examples'])  # also clears the route cache
        emit_config(router)
    if 'engine_order' in body:
        auto = router.engines.get('auto')
        if auto is None or not isinstance(body['engine_order'], list):
            return web.json_response({'error': 'engine_order must be a list of engine names'}, status=400)
        try:
            auto.set_order(body['engine_order'])
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)
        if router.engine is auto:
            background(warm_up(auto))
        emit_config(router)
    if 'engine' in body:
        name = str(body['engine'] or 'none')
        if name == 'none':
            router.use_engine(None)
            router.store.set_setting('engine', 'none')
        else:
            engine = router.engines.get(name)
            if engine is None:
                return web.json_response({'error': f'unknown engine {name!r}'}, status=400)
            ok, why = engine.available()
            if not ok:
                return web.json_response({'error': f'{engine.label} is not available: {why}'}, status=409)
            unblock(engine)
            router.use_engine(engine)
            router.store.set_setting('engine', engine.name)  # remembered across restarts
            background(warm_up(engine))
        # Agents, prices and the engine badge all change, so every browser gets the new config (older clients ignore it).
        emit_config(router)
    router.bus.emit('state', state=router.state)
    return web.json_response({**router.state, 'engine': router.engine.name if router.engine else None})


async def config(request):
    return web.json_response(with_limits(request.app[ROUTER].config()))


async def events(request):
    """The SSE stream. The main stream never carries sandbox runs; `?sandbox=<id>` streams only that sandbox's runs
    (plus run-independent events such as config and state), and its hello carries no history."""
    router = request.app[ROUTER]
    sid = request.query.get('sandbox')
    if sid is not None and not SANDBOX_ID.match(sid):
        return err('bad sandbox id')
    if sid:
        def accept(e):
            qid = e.get('qid')
            return qid is None or router.sandbox.get(qid) == sid
    else:
        def accept(e):
            return e.get('qid') not in router.sandbox
    resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    await resp.prepare(request)
    q = router.bus.subscribe(accept)
    try:
        hello = with_limits(router.hello())
        if sid:  # a sandbox starts empty: only its own in-flight runs (after a reconnect) are replayed
            hello['history'] = [public_run(r) for qid, r in sorted(router.inflight.items()) if router.sandbox.get(qid) == sid]
        await resp.write(sse(hello))
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), 15)
                if msg is None:  # fell too far behind: close so the browser reconnects and gets a fresh hello
                    break
                await resp.write(sse(msg))
            except asyncio.TimeoutError:
                await resp.write(b': ping\n\n')  # keeps idle connections from being closed by proxies
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        router.bus.unsubscribe(q)
    return resp


def dist_file(rel: str) -> Path | None:
    """A file inside web/dist, checked at request time so a build made after startup is picked up."""
    root = DIST.resolve()
    f = (root / rel).resolve()
    return f if f.is_file() and f.is_relative_to(root) else None


# index.html must be revalidated so a new build is picked up; hashed /assets files never change and can be cached.
NO_CACHE = {'Cache-Control': 'no-cache'}
IMMUTABLE = {'Cache-Control': 'public, max-age=31536000, immutable'}


async def index(request):
    return web.FileResponse(dist_file('index.html') or LEGACY_PAGE, headers=NO_CACHE)


async def static(request):
    tail = request.match_info['tail']
    f = dist_file(tail)
    if f:
        return web.FileResponse(f, headers=IMMUTABLE if tail.startswith('assets/') else NO_CACHE)
    raise web.HTTPNotFound()


def create_app(router_factory=None) -> web.Application:
    """router_factory(http) -> Router; the default builds the real Jev client and picks an LLM engine (TG_ENGINE)."""
    app = web.Application()

    async def startup(app):
        app[HTTP] = aiohttp.ClientSession()
        if router_factory:
            app[ROUTER] = router_factory(app[HTTP])
        else:
            from typesafe_sdk import AsyncTypeSafeClient
            engines = catalog()
            store = Store(os.environ.get('TG_DB') or DEFAULT_DB)
            # The engine picked in the app survives a restart; TG_ENGINE is the default until one is picked.
            app[ROUTER] = Router(AsyncTypeSafeClient(), app[HTTP], choose(engines, store.get_setting('engine')),
                                 engines=engines, store=store)
            app[WARMUP] = asyncio.create_task(warm_up(app[ROUTER].engine))
            # the font cache and design workspaces back under their caps (never for a test's router factory)
            background(asyncio.to_thread(prune_design_caches))
        app[AUTOPILOT] = asyncio.create_task(app[ROUTER].autopilot())
        app[SWEEPER] = asyncio.create_task(app[ROUTER].sweeper())

    async def cleanup(app):
        app[AUTOPILOT].cancel()
        app[SWEEPER].cancel()
        if WARMUP in app:
            app[WARMUP].cancel()
        router = app[ROUTER]
        # Cancel through the same path as a user cancel, and wait, so each run is saved with its final status.
        pending = [*router.evals.values(), *router.tasks]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5)
        await app[HTTP].close()
        for c in (router.jev, *router.engines.values()):
            close = getattr(c, 'aclose', None) or getattr(c, 'close', None)
            if close:
                try:
                    await close()
                except Exception:
                    pass
        router.store.close()

    app.add_routes([
        web.get('/', index), web.get('/events', events), web.post('/ask', ask), web.post('/api/ask', ask),
        web.post('/api/estimate', estimate_run), web.post('/api/created/resume', resume_created),
        web.get('/api/runs/{qid}/checkpoints', run_checkpoints),
        web.post('/control', control),
        web.get('/api/config', config),
        web.get('/api/runs', list_runs), web.get('/api/runs/{qid}', get_run), web.post('/api/runs/{qid}/cancel', cancel_run),
        web.post('/api/runs/{qid}/choose', choose_run), web.post('/api/runs/{qid}/promote', promote_run),
        web.get('/api/timings', timings_summary),
        web.get('/api/sessions', list_sessions), web.get('/api/sessions/{id}', get_session),
        web.delete('/api/sessions/{id}', delete_session), web.delete('/api/sandbox/{id}', clear_sandbox),
        web.post('/api/sandbox/{id}/files', upload_sandbox_file), web.delete('/api/sandbox/{id}/files/{fid}', delete_sandbox_file),
        web.post('/api/sandbox/{id}/keep', keep_sandbox),
        web.get('/api/agents', list_agents), web.post('/api/agents', create_agent), web.delete('/api/agents/{name}', delete_agent),
        web.get('/api/files', list_files), web.post('/api/files', upload_file), web.delete('/api/files/{id}', delete_file),
        web.get('/api/created', list_created), web.get('/api/created/{fid}', get_created),
        web.delete('/api/created/{fid}', delete_created), web.get('/api/created/{fid}/download', download_created),
        web.get('/api/created/{fid}/preview', preview_created), web.post('/api/created/{fid}/convert', convert_created),
        web.get('/api/rules', list_rules),
        web.get('/api/sandbox/{id}/created/{fid}/download', download_sandbox_created),
        web.get('/api/sandbox/{id}/created/{fid}/preview', preview_sandbox_created),
        # Studio (docs/PLAN-designer.md 9.9)
        web.get('/api/design/presets', design_presets), web.get('/api/design/presets/{id}/thumb', design_preset_thumb),
        web.get('/api/fonts/search', fonts_search), web.get('/api/fonts/preview', font_preview),
        web.get('/api/created/{fid}/thumbs', created_thumbs), web.get(r'/api/created/{fid}/thumbs/{n:\d+}.png', created_thumb),
        web.get('/api/created/{fid}/design', created_design), web.post('/api/created/{fid}/restyle', restyle_created),
        web.post('/api/created/{fid}/polish', polish_created),
        web.get('/api/sandbox/{id}/created/{fid}/thumbs', created_thumbs),
        web.get(r'/api/sandbox/{id}/created/{fid}/thumbs/{n:\d+}.png', created_thumb),
        web.get('/api/sandbox/{id}/created/{fid}/design', created_design),
        web.post('/api/sandbox/{id}/created/{fid}/restyle', restyle_created),
        web.post('/api/sandbox/{id}/created/{fid}/polish', polish_created),
        web.post('/api/compare', compare), web.get('/api/compare/{id}', get_compare),
        web.post('/api/evals/run', run_eval), web.get('/api/evals', list_evals),
        web.get('/api/evals/compare', compare_evals), web.get('/api/evals/{id}', get_eval),
        web.post('/api/evals/{id}/cancel', cancel_eval),
        web.get('/api/labels', list_labels), web.post('/api/labels', create_label), web.delete('/api/labels/{id}', delete_label),
        web.post('/api/labels/{id}/promote', promote_label), web.get('/api/review', review),
        web.get('/api/agents/examples', agent_examples),
        web.get('/api/engines/health', engine_health), web.post('/api/engines/{name}/test', test_engine),
        web.get('/{tail:.+}', static)])
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
