"""КОМПАС — MVP платформы о сделках и компаниях.

Статика + /api/ask с двумя режимами:
- mode="base" — ответ строго по базе платформы;
- mode="web"  — перед вызовом модели выполняется поиск Яндекса
  (yandex_search.py), выдача подкладывается в промпт. Если поиск
  упал или пуст — тихая деградация в режим base (с логом, без
  ошибки для пользователя).

LLM: DeepSeek 4 Flash через Yandex AI Studio Responses API.
Требуются YANDEX_API_KEY и YANDEX_FOLDER_ID; без них фронтенд
работает в демо-режиме (fallback=true).
"""
import logging
import os
import re

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from yandex_search import SearchConfig, SearchError, SearchResult, build_search_block, yandex_search

_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(https?://[^)]+\)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("kompas")

app = FastAPI(title="КОМПАС")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

RESPONSES_URL = "https://ai.api.cloud.yandex.net/v1/responses"
LLM_TIMEOUT = 60.0
LLM_RETRIES = 2  # повторов сверх первой попытки
THINKING_BUDGET = 8000  # DeepSeek: thinking включён всегда, отключить через Yandex нельзя

# Общий keep-alive клиент (урок TruePost: не создавать новый на каждый вызов).
_http = httpx.Client(
    timeout=LLM_TIMEOUT,
    transport=httpx.HTTPTransport(retries=2),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

SYSTEM_BASE = """Ты — ассистент платформы «КОМПАС» о сделках и компаниях российского рынка.
Отвечай ТОЛЬКО на основе базы данных, переданной в сообщении (JSON).
Правила:
- По-русски, кратко, как аналитик для юристов и банкиров.
- Ссылки на сделки платформы: [название](#/deal/ID).
- Разрешённое форматирование: ссылки [текст](адрес) и выделение **жирным**. Запрещены: заголовки #, списки с - или *, таблицы, код в ```.
- Никаких вступительных фраз («Отличный вопрос», «Конечно») — сразу ответ по существу.
- Не выдумывай факты, суммы, консультантов. Нет данных — так и скажи.
- Данные из публичных источников и могут быть неполными; упоминай это, когда уместно.
- Никаких рейтингов качества фирм — только факты."""

SYSTEM_WEB = """Ты — ассистент платформы «КОМПАС» о сделках и компаниях российского рынка.
У тебя два источника: база платформы (JSON в сообщении) и блок «СВЕЖАЯ ВЫДАЧА ПОИСКА (Яндекс)».
Правила:
- Сначала проверь базу; факты из интернета бери ТОЛЬКО из блока выдачи поиска. Не выдумывай ничего сверх этих двух источников.
- По-русски, кратко, как аналитик для юристов и банкиров.
- Ссылки на сделки платформы: [название](#/deal/ID). Для фактов из выдачи ОБЯЗАТЕЛЬНО указывай источник ссылкой [название источника](URL) — URL бери из строки «Источник:».
- Формат обязателен для КАЖДОГО факта из выдачи, без исключений. Пример: «Роснефть выкупила «Саянскхимпласт» за 30,3 млрд ₽ [Интерфакс](https://www.interfax.ru/...).» Факт из выдачи без ссылки сразу после него — ошибка, так делать нельзя.
- Чётко различай, что из базы «Компаса», а что найдено в сети.
- Разрешённое форматирование: ссылки [текст](адрес) и выделение **жирным**. Запрещены: заголовки #, списки с - или *, таблицы, код в ```.
- Никаких вступительных фраз — сразу ответ по существу.
- Нет данных ни в базе, ни в выдаче — так и скажи."""


class AskRequest(BaseModel):
    question: str
    context: str
    mode: str = "base"  # base | web


def _yandex_ready() -> bool:
    return bool(os.environ.get("YANDEX_API_KEY")) and bool(os.environ.get("YANDEX_FOLDER_ID"))


@app.get("/health")
def health():
    return {"status": "ok", "ai": _yandex_ready()}


def _extract_text(data: dict) -> str:
    """Достаём текст из Responses API, отбрасывая reasoning-блоки DeepSeek."""
    parts: list[str] = []
    for item in data.get("output", []):
        if item.get("type") == "reasoning":
            continue
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") == "output_text":
                    parts.append(block.get("text", ""))
    if not parts and isinstance(data.get("output_text"), str):
        parts.append(data["output_text"])
    return "".join(parts).strip()


def call_llm(system: str, user: str, max_tokens: int) -> str:
    """Вызов Yandex AI Studio Responses API с ретраями. Пустой ответ/сбой -> RuntimeError."""
    api_key = os.environ.get("YANDEX_API_KEY", "")
    folder_id = os.environ.get("YANDEX_FOLDER_ID", "")
    model = os.environ.get("YANDEX_MODEL", "deepseek-v4-flash/latest")
    payload = {
        "model": f"gpt://{folder_id}/{model}",
        "instructions": system,
        "input": user,
        "temperature": float(os.environ.get("LLM_TEMPERATURE", "0.7")),
        "max_output_tokens": max_tokens + THINKING_BUDGET,
    }
    headers = {"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"}

    last_err: Exception | None = None
    for attempt in range(1 + LLM_RETRIES):
        try:
            resp = _http.post(RESPONSES_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"Responses API HTTP {resp.status_code}: {resp.text[:300]}")
            text = _extract_text(resp.json())
            if text:
                return text
            raise RuntimeError("Responses API вернул пустой текст")
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            last_err = e
            logger.warning("LLM attempt %d/%d failed: %s", attempt + 1, 1 + LLM_RETRIES, e)
    raise RuntimeError(f"LLM недоступен после {1 + LLM_RETRIES} попыток: {last_err}")


def _sources_footer(results: list[SearchResult]) -> str:
    """Список источников как markdown-ссылки — гарантия цитирования, даже если модель
    проигнорировала инструкцию про ссылки в тексте (DeepSeek это делает не всегда)."""
    lines = "\n".join(f"[{r.title}]({r.url})" for r in results[:5])
    return f"\n\nИсточники:\n{lines}"


@app.post("/api/ask")
def ask(req: AskRequest):
    if not _yandex_ready():
        return JSONResponse({"fallback": True})

    web = req.mode == "web"
    system = SYSTEM_BASE
    search_block = ""
    results: list = []

    if web:
        try:
            results = yandex_search(req.question, config=SearchConfig.from_env(), client=_http)
            if results:
                search_block = build_search_block(results)
                system = SYSTEM_WEB
            else:
                logger.info("web-режим: пустая выдача, деградация в base")
        except SearchError as e:
            logger.warning("web-режим: поиск упал (%s), деградация в base", e)

    user_msg = f"База данных платформы (JSON):\n{req.context}\n\n"
    if search_block:
        user_msg += f"{search_block}\n\n"
    user_msg += f"Вопрос пользователя: {req.question}"

    try:
        text = call_llm(system, user_msg, max_tokens=1400 if search_block else 700)
        if search_block and not _MD_LINK_RE.search(text):
            logger.info("web-режим: модель не дала ссылки сама, подставляю источники")
            text += _sources_footer(results)
        return {"answer": text}
    except RuntimeError as e:
        logger.error("ask() failed: %s", e)
        return JSONResponse({"fallback": True})


@app.get("/{full_path:path}")
def index(full_path: str):
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
