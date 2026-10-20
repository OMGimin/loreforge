"""Automated tests never use local LangSmith credentials or emit live traces."""
import pytest


@pytest.fixture(autouse=True)
def disable_external_tracing(monkeypatch):
    # Empty values also prevent load_dotenv(override=False) from restoring keys.
    # A test may explicitly set its own dummy values after this fixture runs.
    monkeypatch.setenv('LANGSMITH_API_KEY', '')
    monkeypatch.setenv('LANGCHAIN_API_KEY', '')
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
