"""Тесты безопасности и валидации входа.

Написаны по итогам код-ревью публичного демо. Каждый тест соответствует
конкретному найденному дефекту — они падали до исправления.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

SEED = Path(__file__).parent.parent / "seed" / "output.json"
TOKEN = "s3cret-demo-token"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "sec.db"
    monkeypatch.setenv("DEMO_DB_PATH", str(path))
    db.init_db(path)
    db.import_run(json.loads(SEED.read_text(encoding="utf-8")), path)
    return path


@pytest.fixture
def open_client(temp_db, monkeypatch):
    """Токен не настроен — доступ открыт (локальный режим)."""
    monkeypatch.delenv("DEMO_ACCESS_TOKEN", raising=False)
    return TestClient(app)


@pytest.fixture
def locked_client(temp_db, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_TOKEN", TOKEN)
    return TestClient(app)


# ---------------------------------------------------------------- токен доступа


def test_no_token_configured_means_open(open_client):
    """Без DEMO_ACCESS_TOKEN демо работает как раньше — барьера нет."""
    assert open_client.get("/api/items").status_code == 200
    assert open_client.get("/").status_code == 200


def test_api_requires_token_when_configured(locked_client):
    r = locked_client.get("/api/items")
    assert r.status_code == 401
    assert "токен" in r.json()["detail"].lower()


def test_page_requires_token_when_configured(locked_client):
    r = locked_client.get("/")
    assert r.status_code == 401
    assert "text/html" in r.headers["content-type"]
    assert "?k=" in r.text  # подсказка, как открыть


def test_valid_token_in_query_grants_access(locked_client):
    r = locked_client.get(f"/api/items?k={TOKEN}")
    assert r.status_code == 200
    assert r.json()["count"] > 0


def test_valid_token_in_header_grants_access(locked_client):
    r = locked_client.get("/api/items", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_token_from_query_is_stored_in_cookie(locked_client):
    """Токен из ссылки закрепляется в cookie, чтобы не тащить его в каждом URL."""
    r = locked_client.get(f"/?k={TOKEN}")
    assert r.status_code == 200
    assert "demo_access" in r.cookies

    follow = locked_client.get("/api/items")  # уже без ?k=
    assert follow.status_code == 200


def test_wrong_token_rejected(locked_client):
    assert locked_client.get("/api/items?k=wrong").status_code == 401
    assert locked_client.get("/api/items?k=").status_code == 401


def test_partial_token_rejected(locked_client):
    """Префикс правильного токена не должен проходить."""
    assert locked_client.get(f"/api/items?k={TOKEN[:-1]}").status_code == 401


def test_health_stays_public(locked_client):
    """Healthcheck контейнера обязан работать без токена."""
    r = locked_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["protected"] is True


# ---------------------------------------------------------------- LIKE-инъекция


def test_percent_in_search_is_escaped(open_client):
    """Дефект ревью: `search=%` возвращал ВСЕ записи — '%' работал как wildcard."""
    total = open_client.get("/api/items").json()["count"]
    r = open_client.get("/api/items", params={"search": "%"})
    assert r.json()["count"] < total, "'%' всё ещё работает как wildcard"


def test_underscore_in_search_is_escaped(open_client):
    """'_' в LIKE — это «любой один символ»."""
    r = open_client.get("/api/items", params={"search": "REQ_001"})
    ids = [i["request_id"] for i in r.json()["items"]]
    assert "REQ-001" not in ids, "'_' сработал как wildcard и подобрал REQ-001"


def test_search_still_finds_real_text(open_client):
    r = open_client.get("/api/items", params={"search": "Google Ads"})
    assert r.json()["count"] >= 1


# ---------------------------------------------------------------- SQL-инъекции


@pytest.mark.parametrize("payload", [
    "' OR 1=1 --",
    "'; DROP TABLE items;--",
    "\" OR \"\"=\"",
    "1' UNION SELECT * FROM runs--",
])
def test_sql_injection_does_not_leak_or_destroy(open_client, payload):
    r = open_client.get("/api/items", params={"search": payload})
    assert r.status_code == 200
    assert r.json()["count"] == 0, "инъекция вернула данные"

    # таблица должна остаться на месте
    assert open_client.get("/api/items").json()["count"] > 0


# ---------------------------------------------------------------- валидация


def test_invalid_category_rejected_with_422(open_client):
    """Дефект ревью: мусор в category давал 200 и пустой список — неотличимо
    от «ничего не найдено». Теперь это явная ошибка ввода."""
    r = open_client.get("/api/items", params={"category": "неіснуюча"})
    assert r.status_code == 422


def test_invalid_priority_rejected(open_client):
    assert open_client.get("/api/items", params={"priority": "urgent"}).status_code == 422


def test_valid_category_still_works(open_client):
    r = open_client.get("/api/items", params={"category": "автоматизація"})
    assert r.status_code == 200
    assert all(i["category"] == "автоматизація" for i in r.json()["items"])


def test_negative_run_id_rejected(open_client):
    assert open_client.get("/api/items", params={"run_id": -1}).status_code == 422


def test_oversized_limit_rejected(open_client):
    assert open_client.get("/api/items", params={"limit": 100000}).status_code == 422


def test_limit_and_offset_work(open_client):
    total = open_client.get("/api/items").json()["count"]
    page = open_client.get("/api/items", params={"limit": 5}).json()
    assert page["count"] == 5

    rest = open_client.get("/api/items", params={"limit": 100, "offset": 5}).json()
    assert rest["count"] == total - 5


def test_overlong_request_id_rejected(open_client):
    assert open_client.get("/api/items/" + "x" * 500).status_code == 422


# ---------------------------------------------------------------- заголовки


def test_security_headers_present(open_client):
    h = open_client.get("/").headers
    assert "Content-Security-Policy" in h
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"


def test_csp_forbids_external_sources(open_client):
    csp = open_client.get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp, "инлайн-скрипты разрешать не нужно — их нет"
