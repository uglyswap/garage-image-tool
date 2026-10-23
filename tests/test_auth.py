import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth as auth_module
from app.auth import COOKIE_NAME, Auth


def make_client(password="s3cret", **kw):
    app = FastAPI()
    a = Auth(password, "test-secret", **kw)
    auth_module.install(app, a)

    @app.get("/api/thing")
    def thing():
        return {"ok": True}

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return TestClient(app), a


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(auth_module, "FAILURE_DELAY_S", 0)


def test_protected_route_requires_auth():
    client, _ = make_client()
    assert client.get("/api/thing").status_code == 401


def test_whitelist_is_public():
    client, _ = make_client()
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").json() == {"auth_required": True, "authenticated": False}


def test_login_sets_session_cookie():
    client, _ = make_client()
    r = client.post("/api/auth/login", json={"password": "s3cret"})
    assert r.status_code == 204
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/thing").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_login_wrong_password():
    client, _ = make_client()
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.get("/api/thing").status_code == 401


def test_bearer_header():
    client, _ = make_client()
    assert client.get("/api/thing", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/api/thing", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_forged_and_expired_cookies_rejected():
    client, a = make_client()
    client.cookies.set(COOKIE_NAME, "9999999999." + "0" * 64)
    assert client.get("/api/thing").status_code == 401
    expired = a.make_token(now=0)
    assert a.verify_token(expired) is False
    assert a.verify_token(a.make_token()) is True
    assert a.verify_token("garbage") is False


def test_changing_password_invalidates_sessions():
    _, a = make_client()
    token = a.make_token()
    a.password = "autre"
    assert a.verify_token(token) is False


def test_rate_limit_after_five_failures():
    client, _ = make_client()
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "x"}).status_code == 401
    r = client.post("/api/auth/login", json={"password": "s3cret"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert client.get("/api/thing", headers={"Authorization": "Bearer s3cret"}).status_code == 429


def test_bad_bearer_counts_as_failure():
    client, _ = make_client()
    for _ in range(5):
        assert client.get("/api/thing", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.get("/api/thing", headers={"Authorization": "Bearer x"}).status_code == 429


def test_open_mode_lets_everything_through():
    client, a = make_client(password="")
    assert a.enabled is False
    assert client.get("/api/thing").status_code == 200
    assert client.get("/api/auth/status").json() == {"auth_required": False, "authenticated": True}


def test_logout_clears_cookie():
    client, _ = make_client()
    client.post("/api/auth/login", json={"password": "s3cret"})
    assert client.post("/api/auth/logout").status_code == 204
    client.cookies.clear()
    assert client.get("/api/thing").status_code == 401


def test_trusted_proxy_uses_last_forwarded_ip():
    client, _ = make_client(trusted_proxy=True)
    for _ in range(5):
        client.post("/api/auth/login", json={"password": "x"}, headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
    r = client.post("/api/auth/login", json={"password": "s3cret"}, headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"})
    assert r.status_code == 429  # même IP proxy-ajoutée (dernier élément)


def test_secret_persisted(tmp_path):
    f = str(tmp_path / ".secret")
    first = auth_module._load_secret("", f)
    assert first and auth_module._load_secret("", f) == first
    assert auth_module._load_secret("explicit", f) == "explicit"
