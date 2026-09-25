"""Jev Router v2: a planner splits each query, Jev routes every subtask, agents answer in parallel, a merger combines.

Run: .venv/bin/python server.py   then open http://localhost:8777 (PORT env var overrides)
Keys come from the environment or a .env file next to this script: TYPESAFE_API_KEY (required), ANTHROPIC_API_KEY (optional).
"""
import os

from aiohttp import web

from jevrouter.app import create_app
from jevrouter.config import claude_enabled, load_env

if __name__ == '__main__':
    load_env()
    if not os.environ.get('TYPESAFE_API_KEY'):
        raise SystemExit('Set TYPESAFE_API_KEY (or put it in .env)')
    port = int(os.environ.get('PORT', 8777))
    mode = 'Claude + keyless agents' if claude_enabled() else 'keyless agents (no ANTHROPIC_API_KEY)'
    web.run_app(create_app(), host='127.0.0.1', port=port, print=lambda *_: print(f'Jev Router on http://localhost:{port} with {mode}'))
