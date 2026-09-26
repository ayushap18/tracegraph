import pytest

from jevrouter import evals


@pytest.fixture(autouse=True)
def local_cases(tmp_path, monkeypatch):
    """Tests never read or write the real evals/cases.local.jsonl (promoted labels)."""
    path = tmp_path / 'cases.local.jsonl'
    monkeypatch.setattr(evals, 'LOCAL_CASES', path)
    monkeypatch.delenv('TG_ROUTE_EXAMPLES', raising=False)
    return path
