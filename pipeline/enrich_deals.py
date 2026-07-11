#!/usr/bin/env python3
"""Этап 2 пайплайна «Реестра»: дедупликация и обогащение сырых записей сделок.

Вход — сырые записи из collect_deals.py (сайты фирм и/или Telegram-каналы), где одна и та же
сделка может встречаться много раз, а канал часто пишет только «сделка была» без деталей.

Что делает:
  1. Дедуп: схлопывает записи по нормализованному ключу «стороны + дата ± WINDOW_DAYS».
  2. Приоритизация: «тонкие» записи (нет суммы/консультанта) обогащаются в первую очередь —
     именно у них наибольший потенциальный прирост ценности от веб-поиска.
  3. Обогащение: для каждой сделки — запрос к Claude с включённым web_search; модель ищет
     более подробные источники (сайты юрфирм, финансовых консультантов, СМИ) и возвращает
     объединённую карточку с массивом источников sources: [{outlet, url}, ...].
  4. Сохраняет ПОСЛЕ КАЖДОЙ записи в --out, плюс отдельно ведёт --state с прогрессом,
     чтобы можно было прерваться и продолжить с --resume.

Запуск:
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 enrich_deals.py --in collected_telegram.json,collected_deals.json --dedupe-only
      # шаг 1 отдельно: посмотреть, сколько реально уникальных сделок после дедупа (бесплатно)

  python3 enrich_deals.py --in collected_telegram.json,collected_deals.json --limit 50
      # обогатить первые 50 приоритетных (самых «тонких») записей — тестовый прогон

  python3 enrich_deals.py --in collected_telegram.json,collected_deals.json --resume
      # продолжить с того места, где остановились (по --state)
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
WINDOW_DAYS = 30
DELAY_SEC = 1.0

ENRICH_SYSTEM = """Ты обогащаешь запись о сделке дополнительными деталями через веб-поиск.
Тебе дан черновик записи (JSON) — обычно скудный: только факт «сделка была», без сумм/консультантов.
Твоя задача — найти более подробные источники: сайты юридических и финансовых консультантов,
деловые СМИ (Коммерсантъ, РБК, Ведомости, Forbes, Интерфакс) — и вернуть ОБОГАЩЁННУЮ запись.

Верни СТРОГО JSON, без пояснений и markdown-ограждений:
{
  "title": "уточнённое краткое название сделки",
  "date": "YYYY-MM-DD (уточни, если черновик неточен)",
  "industry": "одна отрасль из: Нефть и газ, Уголь, ГМК и добыча, Энергетика, Химия и удобрения, Агро, Пищепром и напитки, Ритейл, E-commerce, Потребительские товары, ИТ и интернет, Телеком, Банки, Страхование, Инвестиции и рынок ЦБ, Транспорт и логистика, Порты и инфраструктура, Автопром, Недвижимость, Строительство, Фарма и медицина, Медиа, Машиностроение",
  "sum": "сумма сделки, если нашлась, иначе null",
  "legal_advisors": ["названия юрфирм, если нашлись, с указанием чью сторону вели"],
  "financial_advisors": ["названия фин./инвест. консультантов, если нашлись"],
  "extra_details": "2-4 предложения дополнительных деталей: мультипликаторы, структура, согласования, цель сделки — ТОЛЬКО то, что подтверждено найденными источниками",
  "sources": [{"outlet": "название источника", "url": "точный URL"}],
  "enrichment_confidence": "high | medium | low | none"
}

ЖЕЛЕЗНЫЕ ПРАВИЛА:
- Не выдумывай ничего. Если поиск не дал ничего нового сверх черновика — верни
  "enrichment_confidence": "none" и пустые/null поля, но не изобретай факты.
- Каждый факт в extra_details должен быть прослеживаем к конкретному URL в sources.
- Если источники расходятся в цифрах — укажи оба варианта с атрибуцией, не выбирай один произвольно."""


def norm_date(d: str) -> str:
    d = (d or "").strip()
    if len(d) == 4:
        return f"{d}-06-15"
    if len(d) == 7:
        return f"{d}-15"
    return (d[:10] or "1970-01-01")


def date_bucket(d: str, window: int = WINDOW_DAYS) -> int:
    """Номер «окна» в днях от эпохи — записи в одном окне считаются потенциальным дублем."""
    import datetime
    try:
        dt = datetime.date.fromisoformat(norm_date(d))
    except ValueError:
        dt = datetime.date(1970, 1, 1)
    epoch = datetime.date(1970, 1, 1)
    return (dt - epoch).days // window


def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[«»\"'`,.\-–—()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


STOPWORDS = {"ооо", "ао", "пао", "зао", "group", "групп", "компания", "холдинг", "и", "в", "на", "с", "по", "за"}


def key_tokens(title: str) -> set:
    words = [w for w in norm_text(title).split() if len(w) > 3 and w not in STOPWORDS]
    return set(words[:8])  # первые значимые слова — обычно стороны сделки


def dedupe(records: list[dict]) -> list[dict]:
    """Группирует записи по (окно даты, пересечение ключевых слов заголовка), берёт самую
    информативную запись из группы как основу и прикладывает остальные как доп. источники."""
    buckets: dict[int, list[dict]] = {}
    for r in records:
        buckets.setdefault(date_bucket(r.get("date", "")), []).append(r)

    merged = []
    for bucket_recs in buckets.values():
        used = [False] * len(bucket_recs)
        for i, r in enumerate(bucket_recs):
            if used[i]:
                continue
            group = [r]
            used[i] = True
            ti = key_tokens(r.get("title", ""))
            for j in range(i + 1, len(bucket_recs)):
                if used[j]:
                    continue
                tj = key_tokens(bucket_recs[j].get("title", ""))
                if ti and tj and len(ti & tj) >= max(2, min(len(ti), len(tj)) // 2):
                    group.append(bucket_recs[j])
                    used[j] = True
            # самая «богатая» запись группы — по числу непустых значимых полей
            def richness(x):
                return sum(bool(x.get(f)) for f in ("sum", "role", "client_side"))
            base = max(group, key=richness)
            base = dict(base)
            base["_merged_sources"] = [
                {"outlet": g.get("source_name") or g.get("firm_name") or "?", "url": g.get("source_url")}
                for g in group if g.get("source_url")
            ]
            base["_group_size"] = len(group)
            merged.append(base)
    return merged


def thinness_score(r: dict) -> int:
    """Больше — «тоньше» запись, выше приоритет на обогащение."""
    score = 0
    if not r.get("sum"):
        score += 2
    if not r.get("role") or len(r.get("role", "")) < 60:
        score += 2
    if r.get("confidence") == "low":
        score += 1
    if r.get("firm_id") is None:
        score += 1  # из телеграм-канала, скорее всего без атрибуции консультанта
    return score


def call_enrich(record: dict, model: str, api_key: str) -> dict | None:
    payload = {
        "model": model,
        "max_tokens": 1200,
        "system": ENRICH_SYSTEM,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{
            "role": "user",
            "content": f"Черновик записи о сделке:\n{json.dumps(record, ensure_ascii=False)}",
        }],
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)
    except Exception as e:
        print(f"    ! enrich error: {e}", file=sys.stderr)
        return None


def load_state(path: str) -> set:
    if os.path.exists(path):
        return set(json.load(open(path, encoding="utf-8")).get("done_keys", []))
    return set()


def save_state(path: str, done_keys: set) -> None:
    json.dump({"done_keys": sorted(done_keys)}, open(path, "w", encoding="utf-8"), ensure_ascii=False)


def rec_key(r: dict) -> str:
    raw = norm_text(r.get("title", "")) + "|" + norm_date(r.get("date", ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inputs", required=True, help="через запятую: collected_telegram.json,collected_deals.json")
    ap.add_argument("--out", default="enriched_deals.json")
    ap.add_argument("--state", default="enrich_state.json")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--limit", type=int, default=None, help="обогатить не больше N записей за этот запуск")
    ap.add_argument("--dedupe-only", action="store_true", help="только дедуп, без обогащения и без API")
    ap.add_argument("--resume", action="store_true", help="пропустить уже обогащённые (по --state)")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not args.dedupe_only:
        sys.exit("Нет ANTHROPIC_API_KEY. Либо задай ключ, либо запусти с --dedupe-only.")

    all_records = []
    for path in args.inputs.split(","):
        path = path.strip()
        if os.path.exists(path):
            recs = json.load(open(path, encoding="utf-8"))
            print(f"  {path}: {len(recs)} сырых записей")
            all_records += recs
        else:
            print(f"  ! не найден: {path}", file=sys.stderr)

    print(f"\nВсего сырых записей: {len(all_records)}")
    unique = dedupe(all_records)
    print(f"После дедупа: {len(unique)} уникальных сделок")

    if args.dedupe_only:
        json.dump(unique, open("deduped_deals.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("→ deduped_deals.json (обогащение не запускалось)")
        return

    unique.sort(key=thinness_score, reverse=True)  # сначала самые «тонкие» — им обогащение даст больше всего

    done_keys = load_state(args.state) if args.resume else set()
    if done_keys:
        print(f"Уже обогащено ранее: {len(done_keys)} — пропускаем их")

    # подгружаем то, что уже накопили в --out (инкрементальный файл), чтобы не терять прогресс
    enriched = json.load(open(args.out, encoding="utf-8")) if (args.resume and os.path.exists(args.out)) else []

    processed = 0
    for rec in unique:
        k = rec_key(rec)
        if k in done_keys:
            continue
        if args.limit is not None and processed >= args.limit:
            break
        print(f"  обогащаю: {rec.get('date','?')} · {rec.get('title','')[:70]} (score={thinness_score(rec)})")
        result = call_enrich(rec, args.model, api_key)
        entry = {"draft": rec, "enriched": result}
        enriched.append(entry)
        done_keys.add(k)
        processed += 1
        # сохраняем ПОСЛЕ КАЖДОЙ записи — не теряем прогресс при таймауте
        json.dump(enriched, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        save_state(args.state, done_keys)
        time.sleep(DELAY_SEC)

    print(f"\nОбработано за этот запуск: {processed}")
    print(f"Всего обогащено накопительно: {len(enriched)} из {len(unique)} уникальных")
    print(f"→ {args.out} (для ручного просмотра) / {args.state} (чекпоинт для --resume)")


if __name__ == "__main__":
    main()
