import pytest

from jevrouter import cache, evals, gate


@pytest.fixture(autouse=True)
def local_cases(tmp_path, monkeypatch):
    """Tests never read or write the real evals/cases.local.jsonl (promoted labels)."""
    path = tmp_path / 'cases.local.jsonl'
    monkeypatch.setattr(evals, 'LOCAL_CASES', path)
    monkeypatch.delenv('TG_ROUTE_EXAMPLES', raising=False)
    return path


@pytest.fixture(autouse=True)
def no_api_keys(tmp_path, monkeypatch):
    """The developer's own API keys and data/providers.json never add engines to a test's catalog."""
    from jevrouter.engines.api import BUILTIN
    for var in {p.key_env for p in BUILTIN} | {'TG_API_BASE_URL', 'TG_API_MODEL', 'TG_API_KEY'}:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('TG_PROVIDERS', str(tmp_path / 'providers.json'))


@pytest.fixture(autouse=True)
def no_meanings_lookup(monkeypatch):
    """The lone-term check (jevrouter/gate.py) asks DuckDuckGo for a term's meanings; tests never touch the network, so
    by default no term is ambiguous. Tests of that check patch gate.meanings themselves."""
    async def none(http, term):
        return None
    monkeypatch.setattr(gate, 'meanings', none)


@pytest.fixture(autouse=True)
def empty_live_cache():
    """Live-data answers (jevrouter/cache.py) are cached module-wide; every test starts without them."""
    cache.LIVE.clear()
    yield
    cache.LIVE.clear()


@pytest.fixture(autouse=True)
def no_cost_confirm(monkeypatch):
    """The cost guard (docs/PLAN-files-robust.md 5.4) is off for existing tests, so a costly-looking run still starts;
    tests of the guard turn it on with TG_COST_CONFIRM=1. Auto's lean long writer keeps its default."""
    monkeypatch.setenv('TG_COST_CONFIRM', '0')


@pytest.fixture(autouse=True)
def design_workspaces(tmp_path, monkeypatch):
    """Studio is on by default for PowerPoint and PDF, so any test that makes one lays it out in a design workspace:
    those go under the test's tmp_path, never the real data/cache/design (or the shared sandbox folder)."""
    from jevrouter.studio import workspace
    monkeypatch.setattr(workspace, 'DESIGN_DIR', tmp_path / 'design')
    monkeypatch.setattr(workspace, 'SANDBOX_DIR', tmp_path / 'design-sandbox')
