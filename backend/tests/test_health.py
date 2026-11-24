"""The environment baseline must start without model credentials."""

from fastapi.testclient import TestClient

from interview_agent.api.app import create_app


def test_health_without_model_credentials(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "python-interview"}
