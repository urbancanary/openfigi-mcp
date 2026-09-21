"""POST /enrich is a write path: it must refuse anonymous callers (backlog 1487)."""

import os

os.environ.setdefault("AUTH_MCP_URL", "https://auth.invalid")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402


def _client(monkeypatch, key="k" * 32, fail=False):
    def fake_get_key(name):
        assert name == "OPENFIGI_ENRICH_KEY"
        if fail:
            raise RuntimeError("auth-mcp unreachable")
        return key

    monkeypatch.setattr(server, "_get_key", fake_get_key)
    monkeypatch.setattr(server, "_enrich_key_cache", {"value": None, "tried_at": None})
    ran = []
    monkeypatch.setattr(server, "_get_openfigi_key", lambda: ran.append(1) or (_ for _ in ()).throw(SystemExit("reached work")))
    return TestClient(server.app, raise_server_exceptions=False), ran


def test_enrich_without_key_is_401_and_does_no_work(monkeypatch):
    client, ran = _client(monkeypatch)
    assert client.post("/enrich", json={"dry_run": True}).status_code == 401
    assert client.post("/enrich", json={}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert not ran


def test_enrich_fails_closed_when_its_credential_cannot_load(monkeypatch):
    client, ran = _client(monkeypatch, fail=True)
    assert client.post("/enrich", json={}, headers={"X-API-Key": "k" * 32}).status_code == 503
    assert not ran


def test_enrich_with_the_key_reaches_the_work(monkeypatch):
    client, ran = _client(monkeypatch)
    client.post("/enrich", json={"dry_run": True}, headers={"X-API-Key": "k" * 32})
    assert ran == [1]


def test_enrich_is_not_offered_cross_origin(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.options("/enrich", headers={"Origin": "https://evil.example",
                                           "Access-Control-Request-Method": "POST"})
    assert r.status_code == 400 or "access-control-allow-origin" not in r.headers
