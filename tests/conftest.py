import pytest


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "_MDTODB_GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
