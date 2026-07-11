#!/usr/bin/env python3
"""Пайплайн сбора сделок с сайтов юридических/инвестиционных фирм для «Реестра».

Как работает:
  1. Для каждой фирмы из firms.json находит раздел новостей (типовые пути + ссылки с главной).
  2. Собирает ссылки на статьи, отбирает похожие на анонсы сделок (по ключевым словам).
  3. Каждую статью отдаёт Claude API → строгий JSON записи о сделке (или null, если это не сделка).
  4. Складывает всё в collected_deals.json — ДЛЯ РУЧНОГО АПРУВА. Ничего не публикуется автоматически.

Запуск:
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 collect_deals.py                     # все фирмы (сайты)
  python3 collect_deals.py --firms orion,alrud # выборочно по сайтам
  python3 collect_deals.py --telegram dealsma,LawFirms          # публичные Telegram-каналы (t.me/s/...)
  python3 collect_deals.py --telegram dealsma --telegram-pages 15  # глубже в историю канала
  python3 collect_deals.py --dry-run           # только собрать ссылки-кандидаты, без API (бесплатно)
  python3 collect_deals.py --model claude-haiku-4-5-20251001  # дешевле на простом извлечении

Telegram: используется публичное HTML-превью t.me/s/<channel> — без токена бота и авторизации.
Работает только для каналов с открытым просмотром через веб; закрытые чаты недоступны.
Уважай ToS канала и не долби запросами чаще, чем задано DELAY_SEC.

Дальше: просмотри collected_deals.json, удали лишнее/поправь, затем:
  python3 to_minideals.py collected_deals.json > mini_deals_snippet.js
и вставь содержимое в MINI_DEALS в static/index.html.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
UA = "ReestrBot/0.1 (+deal research; contact: owner)"
DELAY_SEC = 1.5          # пауза между запросами к одному сайту — не душим чужие серверы
MAX_ARTICLES_PER_FIRM = 40
MAX_ARTICLE_CHARS = 12000

DEAL_HINTS = re.compile(
    r"сдел|консультир|сопровожда|приобрет|购|acqui|m&a|merger|купил|покупк|прода|слияни|"
    r"deal|transaction|advis|инвестиц|выкуп|доли|акци|due diligence|закрыт",
    re.IGNORECASE,
)

EXTRACT_SYSTEM = """Ты извлекаешь структурированные записи о сделках из новостей юридических и инвестиционных фирм.
Верни СТРОГО JSON без пояснений и без markdown-ограждений.

Если статья — анонс участия фирмы в КОНКРЕТНОЙ сделке (M&A, СП, инвестиция, финансирование, выход иностранца и т.п.), верни:
{
  "is_deal": true,
  "date": "YYYY-MM-DD или YYYY-MM или YYYY (дата сделки/публикации, как в статье)",
  "title": "краткое название сделки по-русски: кто что купил/продал/создал",
  "industry": "одна отрасль из списка: Нефть и газ, Уголь, ГМК и добыча, Энергетика, Химия и удобрения, Агро, Пищепром и напитки, Ритейл, E-commerce, Потребительские товары, ИТ и интернет, Телеком, Банки, Страхование, Инвестиции и рынок ЦБ, Транспорт и логистика, Порты и инфраструктура, Автопром, Недвижимость, Строительство, Фарма и медицина, Медиа, Машиностроение",
  "client_side": "кого консультировала фирма (покупатель/продавец/банк/название клиента)",
  "role": "1-2 предложения: роль фирмы, названные партнёры/юристы, ключевые детали (сумма, доля, согласования) — ТОЛЬКО из текста статьи",
  "sum": "сумма сделки как в статье или null",
  "parties": ["стороны сделки"],
  "confidence": "high | medium | low"
}

Если статья НЕ про конкретную сделку (рейтинги, назначения, мероприятия, аналитика, судебные споры) — верни {"is_deal": false}.

ЖЕЛЕЗНЫЕ ПРАВИЛА: не выдумывай ничего, чего нет в тексте; не додумывай суммы и даты; если данных мало — confidence: "low"."""


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_html(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;|&#160;", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Все ссылки страницы: (absolute_url, anchor_text)."""
    out = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>([\s\S]*?)</a>', html, re.I):
        href, text = m.group(1), strip_html(m.group(2))[:200]
        absu = urllib.parse.urljoin(base_url, href)
        if urllib.parse.urlparse(absu).netloc == urllib.parse.urlparse(base_url).netloc:
            out.append((absu, text))
    return out


def find_news_section(site: str, default_paths: list[str]) -> str | None:
    """Возвращает URL раздела новостей: типовые пути, затем поиск ссылки «новости/news» с главной."""
    for path in default_paths:
        url = site.rstrip("/") + path
        try:
            html = http_get(url)
            if len(strip_html(html)) > 500:
                return url
        except Exception:
            continue
        time.sleep(0.5)
    try:
        home = http_get(site)
        for absu, text in extract_links(home, site):
            if re.search(r"новост|пресс|news|press|media|publica", (absu + " " + text), re.I):
                return absu
    except Exception:
        pass
    return None


def candidate_articles(news_url: str) -> list[tuple[str, str]]:
    """Ссылки из раздела новостей, похожие на анонсы сделок."""
    html = http_get(news_url)
    seen, out = set(), []
    for absu, text in extract_links(html, news_url):
        if absu in seen or absu.rstrip("/") == news_url.rstrip("/"):
            continue
        seen.add(absu)
        if DEAL_HINTS.search(text) or DEAL_HINTS.search(urllib.parse.unquote(absu)):
            out.append((absu, text))
    return out[:MAX_ARTICLES_PER_FIRM]


def call_claude(article_text: str, url: str, firm_name: str, model: str, api_key: str) -> dict | None:
    payload = {
        "model": model,
        "max_tokens": 800,
        "system": EXTRACT_SYSTEM,
        "messages": [{
            "role": "user",
            "content": f"Фирма: {firm_name}\nURL: {url}\n\nТекст статьи:\n{article_text[:MAX_ARTICLE_CHARS]}",
        }],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)
    except Exception as e:
        print(f"    ! API/parse error: {e}", file=sys.stderr)
        return None


def fetch_telegram_channel(username: str, before_id: int | None = None, max_pages: int = 5) -> list[dict]:
    """Читает публичное HTML-превью канала (t.me/s/<username>) без авторизации.

    Пролистывает вглубь через ?before=<id>, т.к. на превью-странице ~20 последних постов.
    Возвращает список постов: {post_id, text, urls_in_post}.
    """
    posts, seen_ids, next_before = [], set(), before_id
    for _ in range(max_pages):
        url = f"https://t.me/s/{username}" + (f"?before={next_before}" if next_before else "")
        try:
            html = http_get(url)
        except Exception as e:
            print(f"    ! telegram fetch error: {e}", file=sys.stderr)
            break
        # каждый пост обёрнут в data-post="username/ID"
        post_blocks = re.split(r'data-post="[^"]+/(\d+)"', html)[1:]
        ids_texts = list(zip(post_blocks[0::2], post_blocks[1::2]))
        if not ids_texts:
            break
        min_id = None
        for pid, block in ids_texts:
            pid = int(pid)
            min_id = pid if min_id is None else min(min_id, pid)
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            text_block = block.split('class="tgme_widget_message_text', 1)
            body = strip_html(text_block[1][:20000]) if len(text_block) > 1 else strip_html(block[:20000])
            urls = re.findall(r'href="(https?://(?!t\.me)[^"]+)"', block)
            posts.append({"post_id": pid, "text": body, "urls_in_post": list(dict.fromkeys(urls))})
        if min_id is None or (next_before is not None and min_id >= next_before):
            break
        next_before = min_id
        time.sleep(DELAY_SEC)
    return posts


def process_telegram_channel(username: str, model: str, api_key: str, max_pages: int) -> list[dict]:
    """Прогоняет посты канала через экстрактор; фирма для is_deal=true не проставляется —
    источник помечается как сам канал (агрегатор), это отдельная категория CHANNEL_DEALS на сайте."""
    results = []
    posts = fetch_telegram_channel(username, max_pages=max_pages)
    print(f"  постов получено: {len(posts)}")
    for p in posts:
        if not DEAL_HINTS.search(p["text"]):
            continue
        rec = call_claude(p["text"], f"https://t.me/{username}/{p['post_id']}", f"Telegram: {username}", model, api_key)
        if rec and rec.get("is_deal"):
            primary_source = p["urls_in_post"][0] if p["urls_in_post"] else f"https://t.me/{username}/{p['post_id']}"
            rec.update({"firm_id": None, "firm_name": f"канал @{username}",
                        "source_url": primary_source, "source_name": f"@{username} (Telegram)"})
            results.append(rec)
            print(f"    + {rec.get('date','?')} · {rec.get('title','')[:80]} [{rec.get('confidence')}]")
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--firms", help="id фирм через запятую (по умолчанию все сайты фирм)")
    ap.add_argument("--telegram", help="username каналов через запятую, например dealsma,LawFirms")
    ap.add_argument("--telegram-pages", type=int, default=5, help="сколько страниц вглубь листать на канал (~20 постов/страница)")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--dry-run", action="store_true", help="только собрать ссылки-кандидаты, без вызовов API")
    ap.add_argument("--out", default="collected_deals.json")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not args.dry_run:
        sys.exit("Нет ANTHROPIC_API_KEY. Либо задай ключ, либо запусти с --dry-run.")

    results, candidates_log = [], []

    if args.telegram:
        for username in [x.strip().lstrip("@") for x in args.telegram.split(",")]:
            print(f"\n=== Telegram: @{username} ===")
            if args.dry_run:
                posts = fetch_telegram_channel(username, max_pages=args.telegram_pages)
                candidates_log += [{"firm": f"tg:{username}", "url": f"https://t.me/{username}/{p['post_id']}",
                                     "anchor": p["text"][:120]} for p in posts if DEAL_HINTS.search(p["text"])]
                print(f"  постов-кандидатов: {len(candidates_log)}")
                continue
            results += process_telegram_channel(username, args.model, api_key, args.telegram_pages)

    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "firms.json"), encoding="utf-8"))
    firms = cfg["firms"]
    if args.firms is not None:
        wanted = {x.strip() for x in args.firms.split(",")} if args.firms else set()
        firms = [f for f in firms if f["id"] in wanted]
    elif args.telegram:
        firms = []  # если явно просили только telegram, сайты не трогаем

    for firm in firms:
        print(f"\n=== {firm['name']} ({firm['site']}) ===")
        try:
            news = find_news_section(firm["site"], cfg["default_news_paths"])
        except Exception as e:
            print(f"  ! сайт недоступен: {e}", file=sys.stderr)
            continue
        if not news:
            print("  раздел новостей не найден — пропуск")
            continue
        print(f"  новости: {news}")
        try:
            arts = candidate_articles(news)
        except Exception as e:
            print(f"  ! ошибка чтения раздела: {e}", file=sys.stderr)
            continue
        print(f"  кандидатов: {len(arts)}")
        for url, anchor in arts:
            candidates_log.append({"firm": firm["id"], "url": url, "anchor": anchor})
            if args.dry_run:
                continue
            time.sleep(DELAY_SEC)
            try:
                text = strip_html(http_get(url))
            except Exception as e:
                print(f"    ! {url}: {e}", file=sys.stderr)
                continue
            rec = call_claude(text, url, firm["name"], args.model, api_key)
            if rec and rec.get("is_deal"):
                rec.update({"firm_id": firm["id"], "firm_name": firm["name"],
                            "source_url": url, "source_name": firm["name"]})
                results.append(rec)
                print(f"    + {rec.get('date','?')} · {rec.get('title','')[:80]} [{rec.get('confidence')}]")

    if args.dry_run:
        json.dump(candidates_log, open("candidates.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\nDry-run: {len(candidates_log)} ссылок-кандидатов → candidates.json")
        return

    # дедупликация по (firm, url)
    seen, unique = set(), []
    for r in results:
        key = (r["firm_id"], r["source_url"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    json.dump(unique, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nИтого сделок: {len(unique)} → {args.out}")
    print("Дальше: просмотри файл (это ручной апрув!), удали мусор и запусти to_minideals.py")


if __name__ == "__main__":
    main()
