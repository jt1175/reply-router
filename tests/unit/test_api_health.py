from fastapi.testclient import TestClient

from api.health import app


def test_health_returns_ok_with_required_fields():
    client = TestClient(app)
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "timestamp" in body
    assert "git_sha" in body
    # No secrets / env values leaked
    text = resp.text.lower()
    assert "api_key" not in text
    assert "secret" not in text


def test_health_is_fast():
    """Sanity: health should respond in well under 100ms."""
    import time
    client = TestClient(app)
    t0 = time.perf_counter()
    for _ in range(10):
        client.get("/v1/health")
    elapsed = (time.perf_counter() - t0) / 10
    assert elapsed < 0.1, f"health endpoint slow: {elapsed*1000:.1f}ms"
