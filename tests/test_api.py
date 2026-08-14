"""Тесты API и слоя хранения.

Проверяют то, ради чего база вообще нужна: идемпотентность импорта,
корректность SQL-агрегатов и совпадение цифр с исходным output.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

SEED = Path(__file__).parent.parent / "seed" / "output.json"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Подмена через переменную окружения, а не через атрибут модуля:
    так её видят и db, и FastAPI-приложение — иначе тест проверял бы
    одну базу, а API работал с другой."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("DEMO_DB_PATH", str(path))
    db.init_db(path)
    return path


@pytest.fixture
def payload() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


@pytest.fixture
def client(temp_db, payload):
    db.import_run(payload, temp_db)
    return TestClient(app)


# ---------------------------------------------------------------- хранилище


def test_import_is_idempotent(temp_db, payload):
    """Главное свойство базы против файла: повторный импорт не плодит дубли."""
    run_id_1, created_1 = db.import_run(payload, temp_db)
    run_id_2, created_2 = db.import_run(payload, temp_db)

    assert created_1 is True
    assert created_2 is False, "повторный импорт создал новый прогон"
    assert run_id_1 == run_id_2
    assert len(db.list_runs(temp_db)) == 1
    assert len(db.get_items(run_id_1, path=temp_db)) == len(payload["items"])


def test_all_records_imported(temp_db, payload):
    run_id, _ = db.import_run(payload, temp_db)
    items = db.get_items(run_id, path=temp_db)

    assert len(items) == len(payload["items"])
    assert {i["request_id"] for i in items} == {i["id"] for i in payload["items"]}


def test_failed_record_keeps_diagnostics(temp_db, payload):
    """Сбойная запись должна доехать до базы с сырым ответом модели."""
    run_id, _ = db.import_run(payload, temp_db)
    failed = [i for i in db.get_items(run_id, status="failed", path=temp_db)]

    assert failed, "в сиде нет сбойной записи — демо не покажет работу валидации"
    for item in failed:
        assert item["error"]
        assert item["category"] is None
        assert item["raw_text"]


def test_stats_match_source_file(temp_db, payload):
    """Агрегаты из SQL обязаны сходиться с исходным output.json."""
    run_id, _ = db.import_run(payload, temp_db)
    stats = db.get_stats(run_id, path=temp_db)

    assert stats["counts"]["total"] == payload["meta"]["total"]
    assert stats["counts"]["ok"] == payload["meta"]["ok"]
    assert stats["counts"]["failed"] == payload["meta"]["failed"]

    src_categories: dict[str, int] = {}
    for item in payload["items"]:
        if item["status"] == "ok":
            cat = item["classification"]["category"]
            src_categories[cat] = src_categories.get(cat, 0) + 1
    assert stats["by_category"] == src_categories


def test_filters_work_in_sql(temp_db, payload):
    run_id, _ = db.import_run(payload, temp_db)

    high = db.get_items(run_id, priority="high", path=temp_db)
    assert high and all(i["priority"] == "high" for i in high)

    need = db.get_items(run_id, needs_clarification=True, path=temp_db)
    assert need and all(i["needs_clarification"] for i in need)

    found = db.get_items(run_id, search="Google Ads", path=temp_db)
    assert found, "поиск по тексту ничего не нашёл"


# ---------------------------------------------------------------- API


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_items_endpoint(client, payload):
    r = client.get("/api/items")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(payload["items"])


def test_items_filtered(client):
    r = client.get("/api/items", params={"priority": "high"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and all(i["priority"] == "high" for i in items)


def test_stats_endpoint(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert "by_category" in body["stats"]
    assert body["run"]["model"]


def test_single_item(client):
    r = client.get("/api/items/REQ-001")
    assert r.status_code == 200
    assert r.json()["request_id"] == "REQ-001"


def test_unknown_item_404(client):
    assert client.get("/api/items/REQ-999").status_code == 404


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Inbox Classifier" in r.text


def test_ukrainian_text_not_mangled(client):
    """Кодировка не должна ломаться на пути CSV → JSON → SQLite → API."""
    r = client.get("/api/items")
    text = json.dumps(r.json(), ensure_ascii=False)
    assert "Ð" not in text and "�" not in text
    assert "Привіт" in text or "привіт" in text.lower()
