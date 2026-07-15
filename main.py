"""КОМПАС — MVP платформы о сделках и компаниях.

Статика + /api/ask с двумя режимами:
- mode="base" — ответ строго по базе платформы;
- mode="web"  — ИИ дополнительно ищет в интернете (web search tool Anthropic API).
Требуется ANTHROPIC_API_KEY в переменных окружения; без ключа фронтенд работает в демо-режиме.
"""
import json
import os
import urllib.request

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="КОМПАС")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SYSTEM_BASE = """Ты — ассистент платформы «КОМПАС» о сделках и компаниях российского рынка.
Отвечай ТОЛЬКО на основе базы данных, переданной в сообщении (JSON).
Правила:
- По-русски, кратко, как аналитик для юристов и банкиров.
- Ссылки на сделки платформы: [название](#/deal/ID).
- Не выдумывай факты, суммы, консультантов. Нет данных — так и скажи.
- Данные из публичных источников и могут быть неполными; упоминай это, когда уместно.
- Никаких рейтингов качества фирм — только факты."""

SYSTEM_WEB = """Ты — ассистент платформы «КОМПАС» о сделках и компаниях российского рынка.
У тебя два источника: база платформы (JSON в сообщении) и веб-поиск.
Правила:
- Сначала проверь базу; если данных мало или вопрос шире — ищи в интернете (сайты юрфирм, финансовых консультантов, компаний, Интерфакс, Коммерсантъ, РБК, Ведомости, Forbes).
- По-русски, кратко, как аналитик для юристов и банкиров.
- Ссылки на сделки платформы: [название](#/deal/ID). Для веб-фактов ОБЯЗАТЕЛЬНО указывай источник ссылкой [название источника](URL).
- Чётко различай, что из базы «Компаса», а что найдено в сети.
- Не выдумывай факты; нет данных — так и скажи."""


class AskRequest(BaseModel):
    question: str
    context: str
    mode: str = "base"  # base | web


@app.get("/health")
def health():
    return {"status": "ok", "ai": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.post("/api/ask")
def ask(req: AskRequest):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"fallback": True})
    web = req.mode == "web"
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1400 if web else 700,
        "system": SYSTEM_WEB if web else SYSTEM_BASE,
        "messages": [
            {
                "role": "user",
                "content": f"База данных платформы (JSON):\n{req.context}\n\nВопрос пользователя: {req.question}",
            }
        ],
    }
    if web:
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90 if web else 30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return {"answer": text}
    except Exception:
        return JSONResponse({"fallback": True})


@app.get("/{full_path:path}")
def index(full_path: str):
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
