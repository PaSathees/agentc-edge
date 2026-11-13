from fastapi.testclient import TestClient

from agentc.app import app


def test_health_reports_ok_and_version():
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0"}
