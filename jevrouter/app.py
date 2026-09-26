"""aiohttp routes: GET / (web/dist or legacy page), GET /events (SSE), POST /ask, POST /control, GET /api/config,
plus the v4 API in docs/PLAN-v4.md §1 (runs, sessions, agents, files, compare, evals, engine test)."""
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from . import evals as evals_mod
from .config import AGENTS, DIST, GUARDS, LEGACY_PAGE, REPORT, RESEARCH, RUN
from .engines import EngineError, catalog, choose
from .events import sse
from .files import FILE_AGENTS, MAX_BYTES, FileError, extract
from .pipeline import USE_ACTIVE, Router, warm_up
from .store import DEFAULT_DB, Store

ROUTER = web.AppKey('router', Router)
HTTP = web.AppKey('http', aiohttp.ClientSession)
AUTOPILOT = web.AppKey('autopilot', asyncio.Task)
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


async def ask(request):
    body, router = await read_json(request), request.app[ROUTER]
    text = str(body.get('query', '')).strip()[:500]
    if not text:
        return err('empty query')
    source = body.get('source') or 'you'
    session_id = body.get('session_id') or None
    files = body.get('files') or []
    try:
        if source not in SOURCES:
            raise Bad(f'source must be one of {", ".join(SOURCES)}')
        if session_id is not None and not (isinstance(session_id, str) and 0 < len(session_id) <= 64):
            raise Bad('session_id must be a short string')
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise Bad('files must be a list of file ids')
        known = {f['id'] for f in router.store.list_files()}
        if missing := [f for f in files if f not in known]:
            raise Bad(f'unknown file id {missing[0]!r}')
        engine = pick_engine(router, body['engine']) if body.get('engine') else USE_ACTIVE
        sandbox = body.get('sandbox_id') if source == 'sandbox' else None
        if source == 'sandbox':
            # Nothing from the sandbox is stored, so it can't use stored files or a saved chat session.
            if not (isinstance(sandbox, str) and SANDBOX_ID.match(sandbox)):
                raise Bad('sandbox_id must be 8-64 letters, digits, - or _')
            if files or session_id:
                raise Bad('sandbox runs take no files or session_id')
    except Bad as e:
        return err(str(e), e.status)
    if source == 'chat' and not session_id:
        session_id = uuid.uuid4().hex[:12]
    qid = router.submit(text, source, session_id=session_id, engine=engine, files=list(dict.fromkeys(files)), sandbox=sandbox)
    return web.json_response({'ok': True, 'qid': qid, 'session_id': session_id, **({'sandbox_id': sandbox} if sandbox else {})})


async def clear_sandbox(request):
    """Forget a sandbox: cancel its running queries and drop the turns kept for follow-up context."""
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


async def list_runs(request):
    router, q = request.app[ROUTER], request.query
    try:
        limit = max(1, min(200, int(q.get('limit', 50))))
        before = int(q['before']) if q.get('before') else None
    except ValueError:
        return err('limit and before must be integers')
    runs = router.store.list_runs(limit, before, q.get('q'), q.get('source'), q.get('status'), q.get('engine'))
    return web.json_response({'runs': [router.inflight.get(r['qid'], r) for r in runs]})


async def get_run(request):
    try:
        rec = request.app[ROUTER].get_run(int(request.match_info['qid']))
    except ValueError:
        rec = None
    return web.json_response(rec) if rec else err('no such run', 404)


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
    return web.json_response({'id': s['id'], 'title': s['title'], 'runs': router.runs_where('session_id', sid)})


async def delete_session(request):
    router, sid = request.app[ROUTER], request.match_info['id']
    for qid, rec in list(router.inflight.items()):
        if rec.get('session_id') == sid:
            router.cancel(qid)
    router.store.delete_session(sid)
    return web.json_response({'ok': True})


# ---------- agents ----------

GUARD_INFO = {'clarify': 'Asks a follow-up question when the request is unclear or Jev is not confident enough to route it',
              'blocked': 'Declines requests that Jev flags as harmful or unsafe'}
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


def validate_agent(router, body: dict) -> dict:
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
    if len(router.customs) >= MAX_CUSTOM:
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
    router.bus.emit('config', **router.config())  # Jev's route criteria changed, so every browser's agent list does too
    return web.json_response(custom_info(router, a), status=201)


async def delete_agent(request):
    router, name = request.app[ROUTER], request.match_info['name']
    if not any(a['name'] == name for a in router.customs):
        return err(f'{name} is not a custom agent')
    router.store.delete_agent(name)
    router.reload_customs()
    router.bus.emit('config', **router.config())
    return web.json_response({'ok': True})


# ---------- files ----------

async def upload_file(request):
    router = request.app[ROUTER]
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
        router.store.add_file(meta, bytes(buf), text)
        return web.json_response(meta, status=201)
    return err('no "file" field in the upload')


async def list_files(request):
    return web.json_response({'files': request.app[ROUTER].store.list_files()})


async def delete_file(request):
    if not request.app[ROUTER].store.delete_file(request.match_info['id']):
        return err('no such file', 404)
    return web.json_response({'ok': True})


# ---------- compare ----------

async def compare(request):
    body, router = await read_json(request), request.app[ROUTER]
    text = str(body.get('query', '')).strip()[:500]
    names = body.get('engines')
    try:
        if not text:
            raise Bad('empty query')
        if not isinstance(names, list) or not 2 <= len(set(map(str, names))) == len(names) <= 4:
            raise Bad('engines must list 2 to 4 different engines')
        engines = [pick_engine(router, n) for n in names]
    except Bad as e:
        return err(str(e), e.status)
    cid = uuid.uuid4().hex[:10]
    runs = [{'engine': str(n), 'qid': router.submit(text, 'compare', engine=e, compare_id=cid)} for n, e in zip(names, engines)]
    return web.json_response({'compare_id': cid, 'runs': runs})


async def get_compare(request):
    cid = request.match_info['id']
    runs = request.app[ROUTER].runs_where('compare_id', cid)
    if not runs:
        return err('no such comparison', 404)
    return web.json_response({'compare_id': cid, 'query': runs[0]['text'], 'runs': runs})


# ---------- evals ----------

async def run_eval(request):
    body, router = await read_json(request), request.app[ROUTER]
    try:
        engine = pick_engine(router, body['engine']) if body.get('engine') else router.engine
    except Bad as e:
        return err(str(e), e.status)
    return web.json_response({'eval_id': evals_mod.start(router, engine, engine.name if engine else 'none')})


async def list_evals(request):
    return web.json_response({'evals': request.app[ROUTER].store.list_evals()})


async def get_eval(request):
    e = request.app[ROUTER].store.get_eval(request.match_info['id'])
    return web.json_response(e) if e else err('no such eval', 404)


async def cancel_eval(request):
    router, eid = request.app[ROUTER], request.match_info['id']
    task = router.evals.get(eid)
    if task is not None:
        task.cancel()
        return web.json_response({'ok': True})
    return err('that eval has already finished', 409) if router.store.get_eval(eid) else err('no such eval', 404)


# ---------- engines ----------

TEST_PROMPT = 'Reply with exactly: ok'
TEST_TIMEOUT = 60.0


async def test_engine(request):
    engine = request.app[ROUTER].engines.get(request.match_info['name'])
    if engine is None:
        return err('unknown engine', 404)
    ok, why = engine.available()
    if not ok:
        return web.json_response({'ok': False, 'ms': 0, 'error': why})
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
        router.bus.emit('config', **router.config())
    if 'engine' in body:
        name = str(body['engine'] or 'none')
        if name == 'none':
            router.use_engine(None)
        else:
            engine = router.engines.get(name)
            if engine is None:
                return web.json_response({'error': f'unknown engine {name!r}'}, status=400)
            ok, why = engine.available()
            if not ok:
                return web.json_response({'error': f'{engine.label} is not available: {why}'}, status=409)
            router.use_engine(engine)
            background(warm_up(engine))
        # Agents, prices and the engine badge all change, so every browser gets the new config (older clients ignore it).
        router.bus.emit('config', **router.config())
    router.bus.emit('state', state=router.state)
    return web.json_response({**router.state, 'engine': router.engine.name if router.engine else None})


async def config(request):
    return web.json_response(request.app[ROUTER].config())


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
        hello = router.hello()
        if sid:  # a sandbox starts empty: only its own in-flight runs (after a reconnect) are replayed
            hello['history'] = [r for qid, r in sorted(router.inflight.items()) if router.sandbox.get(qid) == sid]
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
            app[ROUTER] = Router(AsyncTypeSafeClient(), app[HTTP], choose(engines), engines=engines, store=store)
            app[WARMUP] = asyncio.create_task(warm_up(app[ROUTER].engine))
        app[AUTOPILOT] = asyncio.create_task(app[ROUTER].autopilot())

    async def cleanup(app):
        app[AUTOPILOT].cancel()
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
        web.get('/', index), web.get('/events', events), web.post('/ask', ask), web.post('/control', control),
        web.get('/api/config', config),
        web.get('/api/runs', list_runs), web.get('/api/runs/{qid}', get_run), web.post('/api/runs/{qid}/cancel', cancel_run),
        web.get('/api/sessions', list_sessions), web.get('/api/sessions/{id}', get_session),
        web.delete('/api/sessions/{id}', delete_session), web.delete('/api/sandbox/{id}', clear_sandbox),
        web.get('/api/agents', list_agents), web.post('/api/agents', create_agent), web.delete('/api/agents/{name}', delete_agent),
        web.get('/api/files', list_files), web.post('/api/files', upload_file), web.delete('/api/files/{id}', delete_file),
        web.post('/api/compare', compare), web.get('/api/compare/{id}', get_compare),
        web.post('/api/evals/run', run_eval), web.get('/api/evals', list_evals), web.get('/api/evals/{id}', get_eval),
        web.post('/api/evals/{id}/cancel', cancel_eval),
        web.post('/api/engines/{name}/test', test_engine),
        web.get('/{tail:.+}', static)])
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
