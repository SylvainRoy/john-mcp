import socket

import pytest


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    for var in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "_MDTODB_GEMINI_API_KEY",
        "JOHN_MARKDOWN_ROOT",
        "JOHN_CHROMA_PATH",
        "JOHN_COLLECTION",
        "JOHN_EMBEDDING",
        "JOHN_GEMINI_MODEL",
        "JOHN_GMAIL",
        "JOHN_GMAIL_CREDENTIALS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network access is not allowed in tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
