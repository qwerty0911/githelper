from fastapi import testclient

from app import main


def test_health_route_returns_ok():
    response = testclient.TestClient(main.app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
