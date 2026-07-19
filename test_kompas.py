"""Тесты «КОМПАС»: yandex_search + /api/ask. Запуск: pytest test_kompas.py -v"""
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import yandex_search as ys

# ---------- yandex_search ----------

XML_OK = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch><response><results><grouping>
<group><doc>
  <url>https://old.example.ru/a</url><title>Старая новость</title>
  <modtime>20260101T000000</modtime>
  <passage>Старый сниппет</passage>
</doc></group>
<group><doc>
  <url>https://fresh.example.ru/b</url><title>Свежая сделка</title>
  <modtime>20260718T120000</modtime>
  <passage>Компания А купила компанию Б</passage>
</doc></group>
<group><doc>
  <url>https://nodate.example.ru/c</url><title>Без даты</title>
  <passage>Сниппет без modtime</passage>
</doc></group>
</grouping></results></response></yandexsearch>"""

XML_EMPTY = """<?xml version="1.0"?><yandexsearch><response><results/></response></yandexsearch>"""
XML_ERROR = """<?xml version="1.0"?><yandexsearch><response><error code="15">Nothing found for query</error></response></yandexsearch>"""

CFG = ys.SearchConfig(api_key="k", folder_id="f")


def _transport(status=200, xml=XML_OK):
    def handler(request):
        return httpx.Response(status, json={"rawData": base64.b64encode(xml.encode()).decode()})
    return httpx.MockTransport(handler)


def test_parse_ok_sorted_by_freshness():
    r = ys.parse_search_xml(XML_OK.encode())
    assert [x.title for x in r] == ["Свежая сделка", "Старая новость", "Без даты"]
    assert r[0].url == "https://fresh.example.ru/b"
    assert r[0].modtime == datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    assert "Источник: https://fresh.example.ru/b" in r[0].as_prompt_line()


def test_parse_empty_is_empty_list_not_error():
    assert ys.parse_search_xml(XML_EMPTY.encode()) == []


def test_parse_service_error_raises():
    with pytest.raises(ys.SearchError):
        ys.parse_search_xml(XML_ERROR.encode())


def test_search_http_error_raises():
    with httpx.Client(transport=_transport(status=403)) as c:
        with pytest.raises(ys.SearchError):
            ys.yandex_search("q", config=CFG, client=c)


def test_search_disabled_raises():
    cfg = ys.SearchConfig(api_key="k", folder_id="f", enabled=False)
    with pytest.raises(ys.SearchError):
        ys.yandex_search("q", config=cfg)


def test_search_bad_base64_raises():
    def handler(request):
        return httpx.Response(200, json={"rawData": "%%%not-base64%%%"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ys.SearchError):
            ys.yandex_search("q", config=CFG, client=c)


def test_build_search_block():
    block = ys.build_search_block(ys.parse_search_xml(XML_OK.encode()))
    assert block.startswith("СВЕЖАЯ ВЫДАЧА ПОИСКА (Яндекс)")
    assert "не выдумывай" in block
    assert block.count("Источник:") == 3
    assert ys.build_search_block([]) == ""


# ---------- /api/ask ----------

RESPONSES_OK = {
    "output": [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "думаю..."}]},
        {"type": "message", "content": [{"type": "output_text", "text": "Ответ ассистента"}]},
    ]
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("YANDEX_API_KEY", "k")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "f")
    return TestClient(main.app)


def test_health_ai_flag(monkeypatch):
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    assert TestClient(main.app).get("/health").json() == {"status": "ok", "ai": False}


def test_ask_no_keys_fallback(monkeypatch):
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    r = TestClient(main.app).post("/api/ask", json={"question": "q", "context": "{}"})
    assert r.json() == {"fallback": True}


def test_ask_base_mode(client, monkeypatch):
    captured = {}

    def fake_llm(system, user, max_tokens):
        captured.update(system=system, user=user, max_tokens=max_tokens)
        return "Ответ ассистента"

    monkeypatch.setattr(main, "call_llm", fake_llm)
    r = client.post("/api/ask", json={"question": "Кто купил X?", "context": "{}", "mode": "base"})
    assert r.json() == {"answer": "Ответ ассистента"}
    assert captured["system"] == main.SYSTEM_BASE
    assert "СВЕЖАЯ ВЫДАЧА" not in captured["user"]
    assert captured["max_tokens"] == 700


def test_ask_web_mode_with_results(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main, "yandex_search",
        lambda q, config=None, client=None: ys.parse_search_xml(XML_OK.encode()),
    )

    def fake_llm(system, user, max_tokens):
        captured.update(system=system, user=user, max_tokens=max_tokens)
        return "Ответ с фактами"

    monkeypatch.setattr(main, "call_llm", fake_llm)
    r = client.post("/api/ask", json={"question": "q", "context": "{}", "mode": "web"})
    assert r.json() == {"answer": "Ответ с фактами"}
    assert captured["system"] == main.SYSTEM_WEB
    assert "СВЕЖАЯ ВЫДАЧА ПОИСКА (Яндекс)" in captured["user"]
    assert captured["max_tokens"] == 1400


def test_ask_web_mode_search_fails_degrades_to_base(client, monkeypatch):
    captured = {}

    def broken_search(q, config=None, client=None):
        raise ys.SearchError("403")

    monkeypatch.setattr(main, "yandex_search", broken_search)
    monkeypatch.setattr(main, "call_llm", lambda s, u, max_tokens: captured.update(system=s) or "Ответ по базе")
    r = client.post("/api/ask", json={"question": "q", "context": "{}", "mode": "web"})
    assert r.json() == {"answer": "Ответ по базе"}  # пользователь не видит ошибку
    assert captured["system"] == main.SYSTEM_BASE


def test_ask_web_mode_empty_results_degrades_to_base(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "yandex_search", lambda q, config=None, client=None: [])
    monkeypatch.setattr(main, "call_llm", lambda s, u, max_tokens: captured.update(system=s) or "Ответ по базе")
    r = client.post("/api/ask", json={"question": "q", "context": "{}", "mode": "web"})
    assert r.json() == {"answer": "Ответ по базе"}
    assert captured["system"] == main.SYSTEM_BASE


def test_ask_llm_dead_returns_fallback(client, monkeypatch):
    def dead(s, u, max_tokens):
        raise RuntimeError("LLM недоступен")
    monkeypatch.setattr(main, "call_llm", dead)
    r = client.post("/api/ask", json={"question": "q", "context": "{}", "mode": "base"})
    assert r.json() == {"fallback": True}


# ---------- call_llm / _extract_text ----------

def test_extract_text_filters_reasoning():
    assert main._extract_text(RESPONSES_OK) == "Ответ ассистента"


def test_call_llm_thinking_budget_and_retry(monkeypatch):
    monkeypatch.setenv("YANDEX_API_KEY", "k")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "f")
    calls = {"n": 0, "payload": None}

    def handler(request):
        calls["n"] += 1
        calls["payload"] = json.loads(request.content)
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=RESPONSES_OK)

    monkeypatch.setattr(main, "_http", httpx.Client(transport=httpx.MockTransport(handler)))
    text = main.call_llm("sys", "user", max_tokens=700)
    assert text == "Ответ ассистента"
    assert calls["n"] == 2  # первый упал, второй прошёл
    p = calls["payload"]
    assert p["model"] == "gpt://f/deepseek-v4-flash/latest"
    assert p["max_output_tokens"] == 700 + main.THINKING_BUDGET
    assert p["instructions"] == "sys"
