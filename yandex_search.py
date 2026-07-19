"""Клиент Yandex Search API v2 для «КОМПАС».

Паттерн перенесён из TruePost (фаза 1.5):
- синхронный вызов /v2/web/search тем же Api-Key, что и у LLM;
- разбор XML-выдачи;
- пустой результат и ошибка сервиса различаются (SearchError vs []);
- сортировка по свежести (modtime, если есть).

Откат одной переменной: YANDEX_SEARCH_ENABLED=0.
"""
import base64
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("kompas.search")

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
DEFAULT_TIMEOUT = 15.0
MAX_RESULTS = 8


class SearchError(Exception):
    """Ошибка сервиса поиска (сеть, 403/5xx, битый ответ). НЕ пустая выдача."""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    modtime: datetime | None = None

    def as_prompt_line(self) -> str:
        date = f" ({self.modtime.date().isoformat()})" if self.modtime else ""
        return f"- {self.title}{date} — {self.snippet}\n  Источник: {self.url}"


@dataclass
class SearchConfig:
    api_key: str
    folder_id: str
    enabled: bool = True
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "SearchConfig":
        return cls(
            api_key=os.environ.get("YANDEX_API_KEY", ""),
            folder_id=os.environ.get("YANDEX_FOLDER_ID", ""),
            enabled=os.environ.get("YANDEX_SEARCH_ENABLED", "1") not in ("0", "false", "False"),
            timeout=float(os.environ.get("YANDEX_SEARCH_TIMEOUT", DEFAULT_TIMEOUT)),
        )

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.folder_id)


def _parse_modtime(raw: str | None) -> datetime | None:
    """modtime в выдаче: '20260715T120000' либо unix-время."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        if re.fullmatch(r"\d{8}T\d{6}", raw):
            return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def parse_search_xml(xml_bytes: bytes) -> list[SearchResult]:
    """Разбор XML-ответа Search API. Битый XML -> SearchError."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise SearchError(f"невалидный XML от Search API: {e}") from e

    err = root.find(".//error")
    if err is not None:
        raise SearchError(f"Search API вернул ошибку: {_text(err) or err.get('code', '?')}")

    results: list[SearchResult] = []
    for doc in root.iter("doc"):
        url = _text(doc.find("url"))
        title = _text(doc.find("title"))
        if not url or not title:
            continue
        passages = [_text(p) for p in doc.iter("passage")]
        snippet = " ".join(p for p in passages if p) or _text(doc.find("headline"))
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet[:500],
                modtime=_parse_modtime(_text(doc.find("modtime")) or None),
            )
        )

    # Свежее — выше; записи без даты в конце, исходный порядок сохраняется.
    results.sort(key=lambda r: (r.modtime is None, -(r.modtime.timestamp() if r.modtime else 0)))
    return results[:MAX_RESULTS]


def yandex_search(
    query: str,
    config: SearchConfig | None = None,
    client: httpx.Client | None = None,
) -> list[SearchResult]:
    """Синхронный поиск. [] — честная пустая выдача; SearchError — сбой сервиса."""
    cfg = config or SearchConfig.from_env()
    if not cfg.usable:
        raise SearchError("поиск выключен или не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID")

    body = {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
        "folderId": cfg.folder_id,
        "responseFormat": "FORMAT_XML",
    }
    headers = {"Authorization": f"Api-Key {cfg.api_key}"}

    try:
        if client is not None:
            resp = client.post(SEARCH_URL, json=body, headers=headers, timeout=cfg.timeout)
        else:
            resp = httpx.post(SEARCH_URL, json=body, headers=headers, timeout=cfg.timeout)
    except httpx.HTTPError as e:
        raise SearchError(f"сеть/таймаут при запросе Search API: {e}") from e

    if resp.status_code != 200:
        raise SearchError(f"Search API HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        raw = resp.json().get("rawData", "")
        xml_bytes = base64.b64decode(raw)
    except Exception as e:  # noqa: BLE001 — любой сбой декодирования это сбой сервиса
        raise SearchError(f"не удалось декодировать rawData: {e}") from e

    results = parse_search_xml(xml_bytes)
    logger.info("yandex_search дал %d результатов для «%s»", len(results), query[:80])
    return results


def build_search_block(results: list[SearchResult]) -> str:
    """Блок для промпта — тот же формат, что в TruePost."""
    if not results:
        return ""
    lines = "\n".join(r.as_prompt_line() for r in results)
    return (
        "СВЕЖАЯ ВЫДАЧА ПОИСКА (Яндекс) — используй только эти факты, не выдумывай.\n"
        "Источники не связаны между собой: выбери релевантные вопросу, "
        "не пересказывай все подряд.\n"
        f"{lines}"
    )
