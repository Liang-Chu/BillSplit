import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-password")
    monkeypatch.setenv("DEFAULT_CURRENCY", "USD")
    monkeypatch.setenv("LOGIN_ATTEMPTS_PER_MINUTE", "100")
    from app.config import get_settings
    from app.storage.service import get_storage

    get_settings.cache_clear()
    get_storage.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()
    get_storage.cache_clear()


class Api:
    """Tiny helper that keeps a bearer token and raises on unexpected status codes."""

    def __init__(self, client: TestClient, token: str, user: dict):
        self.c, self.token, self.user = client, token, user

    @property
    def h(self):
        return {"Authorization": f"Bearer {self.token}"}

    def req(self, method, url, expect=200, **kw):
        r = self.c.request(method, "/api/v1" + url, headers=self.h, **kw)
        assert r.status_code == expect, f"{method} {url} -> {r.status_code}: {r.text}"
        return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else r

    def get(self, url, **kw):
        return self.req("GET", url, **kw)

    def post(self, url, json=None, **kw):
        return self.req("POST", url, json=json, **kw)

    def patch(self, url, json=None, **kw):
        return self.req("PATCH", url, json=json, **kw)

    def put(self, url, json=None, **kw):
        return self.req("PUT", url, json=json, **kw)

    def delete(self, url, **kw):
        return self.req("DELETE", url, **kw)


def login(client, username, password) -> Api:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    return Api(client, body["access_token"], body["user"])


def register(client, code, username, display_name=None, claim=None) -> Api:
    r = client.post(
        "/api/v1/auth/register",
        json={"join_code": code, "username": username, "password": "password123",
              "display_name": display_name or username.title(), "claim_placeholder_id": claim},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return Api(client, body["access_token"], body["user"])


@pytest.fixture
def admin(client) -> Api:
    return login(client, "admin", "admin-password")


def make_type(api: "Api", name: str, group_id: str | None = None) -> dict:
    """Create a routing (optionally pointing at a group) and a type of the same name."""
    r = api.post("/routings", {"name": name, "group_id": group_id}, expect=201)
    return api.post("/expense-types", {"name": name, "routing_id": r["id"]}, expect=201) | {"routing_id": r["id"]}


def png_bytes(color="red", size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def pdf_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(buf, "PDF")
    return buf.getvalue()
