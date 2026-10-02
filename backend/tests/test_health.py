"""Health endpoint tests."""

from fastapi.testclient import TestClient

from app import __version__
from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["env"] in {"local", "ci", "production"}


def test_openapi_schema_is_generated() -> None:
    schema = client.get("/openapi.json").json()

    assert schema["info"]["version"] == __version__
    assert "/health" in schema["paths"]
