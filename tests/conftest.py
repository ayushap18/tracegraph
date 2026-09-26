import pytest

from jevrouter import evals, gate


@pytest.fixture(autouse=True)
def local_cases(tmp_path, monkeypatch):
    """Tests never read or write the real evals/cases.local.jsonl (promoted labels)."""
    path = tmp_path / 'cases.local.jsonl'
    monkeypatch.setattr(evals, 'LOCAL_CASES', path)
    monkeypatch.delenv('TG_ROUTE_EXAMPLES', raising=False)
    return path


@pytest.fixture(autouse=True)
def no_meanings_lookup(monkeypatch):
    """The lone-term check (jevrouter/gate.py) asks DuckDuckGo for a term's meanings; tests never touch the network, so
    by default no term is ambiguous. Tests of that check patch gate.meanings themselves."""
    async def none(http, term):
        return None
    monkeypatch.setattr(gate, 'meanings', none)
