"""TraceGraph: a planner splits each query, Jev routes every subtask, agents answer in parallel, a merger combines.

Run: .venv/bin/python server.py   then open http://localhost:8777 (PORT env var overrides)
Config comes from the environment or a .env file next to this script:
  TYPESAFE_API_KEY  required (Jev routing)
  TG_ENGINE         auto (default) | none | an engine: claude-code, codex, agy, opencode, or an API provider
                    (openai, openrouter, gemini, groq, deepseek, mistral, xai, api, or one in data/providers.json)
                    auto tries every installed CLI, then every API key that is set, moving on when one fails;
                    with none of them it runs the keyless agents only.
  TG_ENGINE_ORDER   the order auto tries engines in, e.g. codex,claude-code,opencode,openai (also settable in the UI)
  OPENAI_API_KEY    (or OPENROUTER_, GEMINI_, GROQ_, DEEPSEEK_, MISTRAL_, XAI_API_KEY) adds that API as an engine;
                    TG_API_BASE_URL + TG_API_MODEL (+ TG_API_KEY) add any OpenAI-compatible endpoint as `api`
  TG_WARM_POOL      pre-started CLI processes kept per engine so calls skip start-up (default 3, 0 turns it off)
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
    if active and active.name == 'auto':
        mode = 'Auto: ' + ' → '.join(e.label for e in active.chain())
    web.run_app(create_app(), host='127.0.0.1', port=port, print=lambda *_: print(f'TraceGraph on http://localhost:{port} · LLM engine: {mode}'))
