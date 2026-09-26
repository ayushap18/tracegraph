"""aiohttp routes: GET / (web/dist or legacy page), GET /events (SSE), POST /ask, POST /control, GET /api/config."""
import asyncio
import json
from pathlib import Path

import aiohttp
from aiohttp import web

from .config import DIST, LEGACY_PAGE
from .engines import catalog, choose
from .events import sse
from .pipeline import Router

ROUTER = web.AppKey('router', Router)
HTTP = web.AppKey('http', aiohttp.ClientSession)
AUTOPILOT = web.AppKey('autopilot', asyncio.Task)


async def read_json(request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({'error': 'invalid JSON'}), content_type='application/json')
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({'error': 'expected an object'}), content_type='application/json')
    return body


async def ask(request):
    text = str((await read_json(request)).get('query', '')).strip()[:500]
    if not text:
        return web.json_response({'error': 'empty query'}, status=400)
    return web.json_response({'ok': True, 'qid': request.app[ROUTER].submit(text, 'you')})


async def control(request):
    body, router = await read_json(request), request.app[ROUTER]
    try:
        if 'autopilot' in body:
            router.state['autopilot'] = bool(body['autopilot'])
        if 'interval' in body:
            router.state['interval'] = max(1.0, min(15.0, float(body['interval'])))
    except (TypeError, ValueError):
        return web.json_response({'error': 'interval must be a number'}, status=400)
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
        # Agents, prices and the engine badge all change, so every browser gets the new config (older clients ignore it).
        router.bus.emit('config', **router.config())
    router.bus.emit('state', state=router.state)
    return web.json_response({**router.state, 'engine': router.engine.name if router.engine else None})


async def config(request):
    return web.json_response(request.app[ROUTER].config())


async def events(request):
    router = request.app[ROUTER]
    resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    await resp.prepare(request)
    q = router.bus.subscribe()
    try:
        await resp.write(sse(router.hello()))
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


async def index(request):
    return web.FileResponse(dist_file('index.html') or LEGACY_PAGE)


async def static(request):
    f = dist_file(request.match_info['tail'])
    if f:
        return web.FileResponse(f)
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
            app[ROUTER] = Router(AsyncTypeSafeClient(), app[HTTP], choose(engines), engines=engines)
        app[AUTOPILOT] = asyncio.create_task(app[ROUTER].autopilot())

    async def cleanup(app):
        app[AUTOPILOT].cancel()
        router = app[ROUTER]
        for t in list(router.tasks):
            t.cancel()
        await app[HTTP].close()
        for c in (router.jev, *router.engines.values()):
            close = getattr(c, 'aclose', None) or getattr(c, 'close', None)
            if close:
                try:
                    await close()
                except Exception:
                    pass

    app.add_routes([web.get('/', index), web.get('/events', events), web.post('/ask', ask), web.post('/control', control),
                    web.get('/api/config', config), web.get('/{tail:.+}', static)])
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
