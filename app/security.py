"""Доступ к витрине по токену.

Зачем: демо публично в интернете. Данные здесь тестовые, но сам паттерн
«выложил демо в открытый доступ и забыл» — плохая привычка. Токен стоит
дёшево и включается одной переменной окружения.

Модель доступа намеренно простая — общий секрет в ссылке (`?k=...`),
как в `afix.radai-1984.dev`. Это НЕ аутентификация пользователей: она
отвечает на вопрос «этот человек получил ссылку от владельца», а не
«кто именно этот человек». Для демо с тестовыми данными этого достаточно;
для реального инбокса понадобились бы учётные записи и роли.

Если DEMO_ACCESS_TOKEN не задан — доступ открыт (удобно локально и для
проверяющего, которому не нужен лишний барьер).
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

COOKIE_NAME = "demo_access"
TOKEN_PARAM = "k"

# Пути, доступные без токена: health нужен Docker-healthcheck'у,
# статика — чтобы страница-заглушка могла отрисоваться.
PUBLIC_PATHS = {"/api/health", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/",)


def configured_token() -> str | None:
    token = os.getenv("DEMO_ACCESS_TOKEN", "").strip()
    return token or None


def _matches(candidate: str, expected: str) -> bool:
    """Сравнение в постоянном времени: обычное `==` выходит раньше на первом
    несовпавшем символе, и по времени ответа токен можно подобрать посимвольно."""
    return hmac.compare_digest(candidate, expected)


async def access_middleware(request: Request, call_next):
    expected = configured_token()
    if expected is None:
        return await call_next(request)

    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    # Токен принимается из ссылки (?k=), из cookie (чтобы не тащить его
    # в каждом запросе фронтенда) или из заголовка (для API-клиентов).
    supplied = (
        request.query_params.get(TOKEN_PARAM)
        or request.cookies.get(COOKIE_NAME)
        or _bearer(request)
    )

    if not supplied or not _matches(supplied, expected):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Потрібен токен доступу"}, status_code=401)
        return Response(_denied_page(), status_code=401, media_type="text/html; charset=utf-8")

    response = await call_next(request)

    # Токен из ссылки закрепляем в cookie: дальше страница и её API-запросы
    # работают без него в URL, и он не утекает в Referer при переходах наружу.
    if request.query_params.get(TOKEN_PARAM):
        response.set_cookie(
            COOKIE_NAME, supplied,
            max_age=60 * 60 * 24 * 30,
            httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
        )
    return response


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _denied_page() -> str:
    return """<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Потрібен доступ</title>
<style>
 body{margin:0;height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;
      font:15px/1.6 -apple-system,"Segoe UI",Roboto,sans-serif}
 .b{text-align:center;max-width:380px;padding:32px}
 h1{font-size:19px;margin:0 0 10px}
 p{color:#8b949e;font-size:13.5px;margin:0}
 code{background:#161b22;border:1px solid #262d38;border-radius:5px;padding:2px 6px;font-size:12.5px}
</style></head>
<body><div class="b">
 <h1>Демо закрите токеном</h1>
 <p>Відкрийте посилання з параметром <code>?k=…</code>, яке надав власник.</p>
</div></body></html>"""


def require_token(request: Request) -> None:
    """Зависимость для точечной защиты, если понадобится вне middleware."""
    expected = configured_token()
    if expected is None:
        return
    supplied = request.query_params.get(TOKEN_PARAM) or request.cookies.get(COOKIE_NAME) or _bearer(request)
    if not supplied or not _matches(supplied, expected):
        raise HTTPException(401, "Потрібен токен доступу")
