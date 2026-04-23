from fastapi.testclient import TestClient

from app.main import app


def test_metrics_endpoint_is_exposed() -> None:
    client = TestClient(app)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text or "http_request_duration" in response.text
