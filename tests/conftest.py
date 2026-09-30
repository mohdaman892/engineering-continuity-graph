import pytest

from eng_graph.state import reset_registry


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.setenv("ECG_EMBEDDING_PROVIDER", "testing")
    monkeypatch.delenv("ECG_DB_PATH", raising=False)
    monkeypatch.delenv("ECG_AUTO_INJECT", raising=False)
    monkeypatch.delenv("ECG_AUTO_INJECT_MAX_TOKENS", raising=False)
    reset_registry()
    yield
    reset_registry()
