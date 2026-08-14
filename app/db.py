"""Слой хранения — SQLite.

Почему SQLite, а не Postgres: демо читает данные и почти не пишет, работает
в одном контейнере, объём — сотни записей. Postgres здесь добавил бы второй
контейнер, сеть, пароли и бэкапы, не решив ни одной существующей проблемы.
Простое перед сложным; схема совместима, миграция при росте — смена URL.

Ключевое свойство против «просто читать output.json»: идемпотентность.
Повторный импорт того же прогона не плодит дубли, история прогонов сохраняется,
и по каждому запросу видно, как менялась классификация между запусками.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def db_path() -> Path:
    """Путь к базе читается при каждом обращении, а не фиксируется при импорте.

    Так тест может подменить его через переменную окружения, и подмена
    подействует на все модули — иначе FastAPI работал бы с одной базой,
    а тест проверял другую.
    """
    return Path(os.getenv("DEMO_DB_PATH", "data/demo.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key       TEXT NOT NULL UNIQUE,   -- дедупликация импорта
    generated_at  TEXT NOT NULL,
    model         TEXT NOT NULL,
    temperature   REAL NOT NULL,
    prompt_version TEXT NOT NULL,
    total         INTEGER NOT NULL,
    ok            INTEGER NOT NULL,
    failed        INTEGER NOT NULL,
    imported_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    request_id    TEXT NOT NULL,
    channel       TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    raw_text      TEXT NOT NULL,
    status        TEXT NOT NULL,
    category      TEXT,
    target_department TEXT,
    priority      TEXT,
    short_summary TEXT,
    requested_actions TEXT,          -- JSON-массив
    needs_clarification INTEGER,
    clarification_questions TEXT,    -- JSON-массив
    confidence    REAL,
    language      TEXT,
    is_actionable INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 1,
    possible_duplicate_of TEXT,
    error         TEXT,
    raw_llm_output TEXT,
    UNIQUE (run_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_items_run      ON items(run_id);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(run_id, category);
CREATE INDEX IF NOT EXISTS idx_items_priority ON items(run_id, priority);
CREATE INDEX IF NOT EXISTS idx_items_status   ON items(run_id, status);
"""


@contextmanager
def connect(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(path) if path is not None else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | str | None = None) -> None:
    path = path if path is not None else db_path()
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def import_run(payload: dict, path: Path | str | None = None) -> tuple[int, bool]:
    """Импортирует output.json. Возвращает (run_id, был_ли_он_новым).

    Идемпотентность: run_key собран из метаданных прогона. Повторный импорт
    того же файла вернёт существующий id и ничего не продублирует.
    """
    path = path if path is not None else db_path()
    meta = payload["meta"]
    run_key = f"{meta['generated_at']}|{meta['model']}|{meta['prompt_version']}|{meta['total']}"

    with connect(path) as conn:
        existing = conn.execute("SELECT id FROM runs WHERE run_key = ?", (run_key,)).fetchone()
        if existing:
            return existing["id"], False

        cur = conn.execute(
            """INSERT INTO runs
               (run_key, generated_at, model, temperature, prompt_version, total, ok, failed, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run_key, meta["generated_at"], meta["model"], meta["temperature"],
                meta["prompt_version"], meta["total"], meta["ok"], meta["failed"],
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        run_id = cur.lastrowid

        for item in payload["items"]:
            c = item.get("classification") or {}
            conn.execute(
                """INSERT INTO items
                   (run_id, request_id, channel, timestamp, raw_text, status,
                    category, target_department, priority, short_summary,
                    requested_actions, needs_clarification, clarification_questions,
                    confidence, language, is_actionable, attempts,
                    possible_duplicate_of, error, raw_llm_output)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, item["id"], item["channel"], item["timestamp"],
                    item["raw_text"], item["status"],
                    c.get("category"), c.get("target_department"), c.get("priority"),
                    c.get("short_summary"),
                    json.dumps(c.get("requested_actions", []), ensure_ascii=False),
                    int(c["needs_clarification"]) if "needs_clarification" in c else None,
                    json.dumps(c.get("clarification_questions", []), ensure_ascii=False),
                    c.get("confidence"), c.get("language"),
                    int(c["is_actionable"]) if "is_actionable" in c else None,
                    item.get("attempts", 1), item.get("possible_duplicate_of"),
                    item.get("error"), item.get("raw_llm_output"),
                ),
            )
        return run_id, True


def latest_run_id(path: Path | str | None = None) -> int | None:
    path = path if path is not None else db_path()
    with connect(path) as conn:
        row = conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return row["id"] if row else None


def get_run(run_id: int, path: Path | str | None = None) -> dict | None:
    path = path if path is not None else db_path()
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs(path: Path | str | None = None) -> list[dict]:
    path = path if path is not None else db_path()
    with connect(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC")]


def get_items(
    run_id: int,
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    needs_clarification: bool | None = None,
    search: str | None = None,
    path: Path | str | None = None,
) -> list[dict]:
    """Фильтрация делается в SQL, а не в Python: на демо разница незаметна,
    но это то, ради чего база вообще существует."""
    path = path if path is not None else db_path()
    sql = "SELECT * FROM items WHERE run_id = ?"
    params: list = [run_id]

    if category:
        sql += " AND category = ?"
        params.append(category)
    if priority:
        sql += " AND priority = ?"
        params.append(priority)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if needs_clarification is not None:
        sql += " AND needs_clarification = ?"
        params.append(int(needs_clarification))
    if search:
        sql += " AND (raw_text LIKE ? OR short_summary LIKE ? OR request_id LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like]

    sql += " ORDER BY id"

    with connect(path) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]

    for r in rows:
        r["requested_actions"] = json.loads(r["requested_actions"] or "[]")
        r["clarification_questions"] = json.loads(r["clarification_questions"] or "[]")
        r["needs_clarification"] = bool(r["needs_clarification"]) if r["needs_clarification"] is not None else None
        r["is_actionable"] = bool(r["is_actionable"]) if r["is_actionable"] is not None else None
    return rows


def get_stats(run_id: int, path: Path | str | None = None) -> dict:
    """Агрегаты считает SQL — те же цифры, что в report.md."""
    path = path if path is not None else db_path()
    with connect(path) as conn:
        def group(field: str) -> dict[str, int]:
            rows = conn.execute(
                f"SELECT {field} AS k, COUNT(*) AS n FROM items "
                f"WHERE run_id = ? AND status = 'ok' GROUP BY {field} ORDER BY n DESC",
                (run_id,),
            )
            return {(r["k"] if r["k"] is not None else "не визначено"): r["n"] for r in rows}

        channels = conn.execute(
            "SELECT channel AS k, COUNT(*) AS n FROM items WHERE run_id = ? GROUP BY channel ORDER BY k",
            (run_id,),
        )
        counts = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(status = 'ok') AS ok,
                 SUM(status = 'failed') AS failed,
                 SUM(needs_clarification = 1) AS need_clarification,
                 SUM(is_actionable = 0) AS not_actionable,
                 SUM(possible_duplicate_of IS NOT NULL) AS duplicates,
                 SUM(confidence < 0.6) AS low_confidence
               FROM items WHERE run_id = ?""",
            (run_id,),
        ).fetchone()

        return {
            "counts": {k: (v or 0) for k, v in dict(counts).items()},
            "by_category": group("category"),
            "by_priority": group("priority"),
            "by_department": group("target_department"),
            "by_channel": {r["k"]: r["n"] for r in channels},
        }
