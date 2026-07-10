"""Реестр — MVP платформы о сделках и компаниях.

Статика + /api/ask: если в переменных окружения задан ANTHROPIC_API_KEY,
вопросы ассистенту уходят в Anthropic API; иначе фронтенд работает в демо-режиме.
"""
import json
import os
import urllib.request

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Реестр")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SYSTEM_PROMPT = """Ты — ассистент платформы «Реестр» о сделках и компаниях российского рынка.
Отвечай ТОЛЬКО на основе базы данных, переданной в сообщении пользователя (JSON).
Правила:
- Отвечай по-русски, кратко и по делу, как аналитик для юристов и банкиров.
- Ссылайся на сделки в формате [название](#/deal/ID) — это внутренние ссылки платформы.
- Никогда не выдумывай факты, суммы или консультантов, которых нет в базе. Если данных нет — так и скажи.
- Данные собраны из публичных источников и могут быть неполными; упоминай это, когда уместно (особенно про консультантов и суммы).
- Никаких рейтингов и оценочных суждений о качестве фирм: только факты «в базе N известных сделок»."""


class AskRequest(BaseModel):
    question: str
    context: str  # компактный JSON базы, передаётся с фронтенда


@app.get("/health")
def health():
    return {"status": "ok", "ai": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.post("/api/ask")
def ask(req: AskRequest):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"fallback": True})
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 700,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": f"База данных платформы (JSON):\n{req.context}\n\nВопрос пользователя: {req.question}",
            }
        ],
    }
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
        with urllib.request.urlopen(request, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(block.get("text", "") for block in data.get("content", []))
        return {"answer": text}
    except Exception:
        return JSONResponse({"fallback": True})


@app.get("/{full_path:path}")
def index(full_path: str):
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
