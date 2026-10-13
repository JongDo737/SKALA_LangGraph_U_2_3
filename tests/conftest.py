"""모든 테스트는 실제 DART·검색·LLM 대신 tests/fake_services.py 의 가짜 응답기를 쓴다."""
import pytest

from tests import fake_services


@pytest.fixture(autouse=True)
def _fake_services(monkeypatch):
    fake_services.use("fixture")
    monkeypatch.setattr("orchestrator.common.get_services", lambda: fake_services.ACTIVE)
    yield
