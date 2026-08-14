"""FastAPI-бэкенд демо-витрины.

Отдаёт статику и JSON API поверх SQLite. Пайплайн классификации здесь НЕ живёт —
демо потребляет его результат. Это осознанная граница: основной репозиторий
остаётся самостоятельным CLI, а витрина читает то, что он произвёл.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db

WEB_DIR = Path(__file__).parent.parent / "web"
SEED_FILE = Path(os.getenv("SEED_FILE", "seed/output.json"))

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Схема + импорт сида. Идемпотентно: перезапуск не плодит дубли."""
    db.init_db()
    if SEED_FILE.exists() and db.latest_run_id() is None:
        payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        run_id, created = db.import_run(payload)
        print(f"[startup] сид импортирован: run_id={run_id}, новый={created}")
    else:
        print(f"[startup] прогонов в базе: {len(db.list_runs())}")
    yield


app = FastAPI(
    title="Netpeak Inbox Classifier — demo",
    description="Вітрина результатів LLM-класифікації вхідних запитів",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


def _resolve_run(run_id: int | None) -> int:
    rid = run_id or db.latest_run_id()
    if rid is None:
        raise HTTPException(404, "В базе нет ни одного прогона")
    if db.get_run(rid) is None:
        raise HTTPException(404, f"Прогон {rid} не найден")
    return rid


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "runs": len(db.list_runs())}


@app.get("/api/runs")
def runs() -> list[dict]:
    """История прогонов — то, чего файл на диске не даёт."""
    return db.list_runs()


@app.get("/api/stats")
def stats(run_id: int | None = None) -> dict:
    rid = _resolve_run(run_id)
    return {"run": db.get_run(rid), "stats": db.get_stats(rid)}


@app.get("/api/items")
def items(
    run_id: int | None = None,
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    needs_clarification: bool | None = None,
    search: str | None = Query(default=None, max_length=200),
) -> dict:
    rid = _resolve_run(run_id)
    rows = db.get_items(
        rid,
        category=category,
        priority=priority,
        status=status,
        needs_clarification=needs_clarification,
        search=search,
    )
    return {"run_id": rid, "count": len(rows), "items": rows}


@app.get("/api/items/{request_id}")
def item(request_id: str, run_id: int | None = None) -> dict:
    rid = _resolve_run(run_id)
    rows = [r for r in db.get_items(rid) if r["request_id"] == request_id]
    if not rows:
        raise HTTPException(404, f"Запит {request_id} не знайдено")
    return rows[0]


# Статика монтируется последней, чтобы не перехватывать /api/*
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
