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

from enum import Enum

from fastapi import FastAPI, HTTPException, Query
from fastapi import Path as FPath
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .security import access_middleware, configured_token


class Category(str, Enum):
    """Те же значения, что в схеме пайплайна. Дублируются осознанно:
    витрина — отдельный сервис и не импортирует код классификатора."""

    AUTOMATION = "автоматизація"
    INTEGRATION = "інтеграція"
    ANALYTICS = "звіт/аналітика"
    BUG = "баг/підтримка"
    QUESTION = "питання/консультація"
    OUT_OF_SCOPE = "поза скоупом"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Status(str, Enum):
    OK = "ok"
    FAILED = "failed"

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


app.middleware("http")(access_middleware)


@app.middleware("http")
async def security_headers(request, call_next):
    """Базовый набор заголовков. CSP собран под этот фронтенд: свои скрипты
    и стили, никаких внешних источников — если в разметку что-то внедрят,
    оно не сможет ни выполниться из инлайна, ни отправить данные наружу."""
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


@app.get("/api/health")
def health() -> dict:
    """Доступен без токена — нужен Docker-healthcheck'у. Внутренних деталей
    не раскрывает: только статус и число прогонов."""
    return {"status": "ok", "runs": len(db.list_runs()), "protected": configured_token() is not None}


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
    run_id: int | None = Query(default=None, ge=1),
    category: Category | None = None,
    priority: Priority | None = None,
    status: Status | None = None,
    needs_clarification: bool | None = None,
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Значения-перечисления валидируются FastAPI: мусор в `category`
    теперь даёт 422 с понятным списком допустимых значений, а не пустой
    результат, который выглядит как «ничего не найдено»."""
    rid = _resolve_run(run_id)
    rows = db.get_items(
        rid,
        category=category.value if category else None,
        priority=priority.value if priority else None,
        status=status.value if status else None,
        needs_clarification=needs_clarification,
        search=search,
        limit=limit,
        offset=offset,
    )
    return {"run_id": rid, "count": len(rows), "limit": limit, "offset": offset, "items": rows}


@app.get("/api/items/{request_id}")
def item(request_id: str = FPath(max_length=64), run_id: int | None = Query(default=None, ge=1)) -> dict:
    rid = _resolve_run(run_id)
    rows = db.get_items(rid, request_id=request_id, limit=1)
    if not rows:
        raise HTTPException(404, f"Запит {request_id} не знайдено")
    return rows[0]


# Статика монтируется последней, чтобы не перехватывать /api/*
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
