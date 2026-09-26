"""TraceGraph: a planner splits each query, Jev routes every subtask, agents answer in parallel, a merger combines.

Run: .venv/bin/python server.py   then open http://localhost:8777 (PORT env var overrides)
Config comes from the environment or a .env file next to this script:
  TYPESAFE_API_KEY  required (Jev routing)
  TG_ENGINE         auto (default) | claude-code | codex | agy | anthropic | none
                    auto uses the first installed subscription CLI, then ANTHROPIC_API_KEY, else keyless agents only.
  TG_DB             SQLite file for runs, sessions, agents, files and evals (default data/tracegraph.db)
  TG_RUN_TIMEOUT    per-run deadline in seconds (default 300)
Evals: .venv/bin/python -m jevrouter.evals [--engine NAME|none] [--save-baseline]
"""
import os

from aiohttp import web

from jevrouter.app import create_app
from jevrouter.config import load_env
from jevrouter.engines import catalog, choose

if __name__ == '__main__':
    load_env()
    if not os.environ.get('TYPESAFE_API_KEY'):
        raise SystemExit('Set TYPESAFE_API_KEY (or put it in .env)')
    port = int(os.environ.get('PORT', 8777))
    engines = catalog()
    for e in engines.values():
        ok, why = e.available()
        print(f"  {'✓' if ok else '·'} {e.label:<14} {'available' if ok else why}")
    active = choose(engines)
    mode = f'{active.label} ({active.billing})' if active else 'keyless agents only'
    web.run_app(create_app(), host='127.0.0.1', port=port, print=lambda *_: print(f'TraceGraph on http://localhost:{port} · LLM engine: {mode}'))
