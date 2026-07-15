#!/usr/bin/env python3
"""Донабор консультантов из уже собранных (но не использованных триажем)
постов юрфирм — сайты + телеграм-каналы. Триаж по сумме отсеивал почти все
такие посты («Orion консультирует ВТБ» не содержит цифр), хотя сами посты
не нужны как источник суммы — они нужны как источник ИМЕНИ консультанта
для уже существующих карточек.

Без API-вызовов: сопоставление по названию компании (с уже известными
ключами MATCH_KEYS/EXISTING_KEYS + грубым извлечением из текста поста)
и близости дат. Где не уверены — не трогаем карточку, а не гадаем.
"""
import json
import re
import sys
from datetime import date

ADV_PATTERN = re.compile(
    r"консультир|консультант|сопровожд|юридическ\w+ поддержк|представля\w+ интересы|"
    r"выступил\w* (юридическ|консультант)", re.I)

# 'Orion консультирует ВТБ в сделке' -> клиент = 'ВТБ'
# 'Команда Orion консультировала «Ренессанс Капитал» в сделке' -> клиент = 'Ренессанс Капитал'
CLIENT_PATTERNS = [
    re.compile(r"консультир\w*\s+(?:интересы\s+)?«?([А-ЯЁA-Z][^»,.(]{2,40})»?\s+(?:в|по|при)", re.I),
    re.compile(r"сопровожда\w*\s+«?([А-ЯЁA-Z][^»,.(]{2,40})»?\s+(?:в|по|при)", re.I),
    re.compile(r"представля\w*\s+интересы\s+«?([А-ЯЁA-Z][^»,.(]{2,40})»?", re.I),
    re.compile(r"выступил\w*\s+консультант\w*\s+«?([А-ЯЁA-Z][^»,.(]{2,40})»?\s+(?:в|по|при)", re.I),
]


def extract_client(title):
    for pat in CLIENT_PATTERNS:
        m = pat.search(title)
        if m:
            return m.group(1).strip()
    return None


def days_between(d1, d2):
    try:
        a, b = date.fromisoformat(d1[:10]), date.fromisoformat(d2[:10])
        return abs((a - b).days)
    except (ValueError, TypeError):
        return 9999


def name_matches(client, company_name):
    if not client or not company_name:
        return False
    c, n = client.lower(), company_name.lower()
    return c in n or n in c or (len(c) > 4 and c[:6] == n[:6])


FIRM_DISPLAY = {
    "orion": "Orion", "level": "LEVEL Legal Services", "verba": "VERBA LEGAL",
    "lkp": "Лемчик, Крупский и Партнеры", "nikolaev": "МКА «Николаев и партнеры»",
    "amond": "Amond & Smith", "epam": "Адвокатское бюро ЕПАМ",
    "mvp": "Меллинг, Войтишкин и Партнёры", "o2": "O2 Consulting",
    "asari": "ASARI", "b1": "Б1", "alumni": "ALUMNI Partners",
    "ivanyan": "Ivanyan & Partners", "nikolskaya": "Никольская Консалтинг",
    "pgp": "Пепеляев Групп",
}
# телеграм-каналы приходят с полем source вида "@BIRCHLEGAL (Telegram)" — сводим
# известные @handle к тем же человекочитаемым именам, что и firm_id с сайтов
TG_HANDLE_TO_FIRM = {
    "nextons_ru": "Nextons", "mvplegal": "Меллинг, Войтишкин и Партнёры",
    "levellegalservices": "LEVEL Legal Services", "birchlegal": "Birch Legal",
    "kkmpconnect": "ККМП (Кучер Кулешов Максименко и партнеры)", "verbalegal": "VERBA LEGAL",
    "alumnimna": "ALUMNI Partners", "betterchance_ru": "Better Chance",
    "denuolaw": "Denuo", "lkpconsult": "Лемчик, Крупский и Партнеры",
    "pgp_official": "Пепеляев Групп",
}


def firm_display_name(r):
    if r.get("firm_id"):
        return FIRM_DISPLAY.get(r["firm_id"], r["firm_id"])
    src = (r.get("source") or "").lower()
    m = re.search(r"@(\w+)", src)
    if m:
        return TG_HANDLE_TO_FIRM.get(m.group(1), m.group(1))
    return r.get("source", "?")


def main():
    firm_files = sys.argv[1:-2]
    deals_path, out_path = sys.argv[-2], sys.argv[-1]

    deals_data = json.load(open(deals_path, encoding="utf-8"))
    deals = deals_data["deals"]
    companies = deals_data["companies"]

    candidates = []
    for f in firm_files:
        data = json.load(open(f, encoding="utf-8"))
        for r in data:
            text = (r.get("title") or "") + " " + (r.get("raw_text") or "")
            if ADV_PATTERN.search(text) and (r.get("firm_id") or r.get("source")):
                candidates.append(r)
    print(f"Постов-кандидатов на извлечение консультанта: {len(candidates)}")

    filled, skipped_no_client, skipped_no_match, skipped_already_have = 0, 0, 0, 0
    for r in candidates:
        client = extract_client(r.get("title") or "")
        if not client:
            skipped_no_client += 1
            continue
        firm_name = firm_display_name(r)
        matched = None
        for d in deals:
            buyer_name = companies.get(d.get("buyer"), {}).get("name", "")
            target_name = companies.get(d.get("target"), {}).get("name", "")
            if (name_matches(client, buyer_name) or name_matches(client, target_name)) \
                    and days_between(r.get("date", ""), d.get("date", "")) <= 180:
                matched = d
                break
        if not matched:
            skipped_no_match += 1
            continue
        already = any(firm_name.lower() in (a[1] or "").lower() for a in matched["law"]["adv"])
        if already:
            skipped_already_have += 1
            continue
        # заменяем плейсхолдер "не раскрывались" или добавляем ещё одного консультанта
        note = f"по данным {'сайта' if 'tg_post_id' not in r else 'телеграм-канала'} фирмы — {r.get('title','')[:120]}"
        new_entry = ["Юридический консультант", firm_name, note]
        if matched["law"]["adv"] and matched["law"]["adv"][0][1] == "Не раскрывались":
            matched["law"]["adv"] = [new_entry]
        else:
            matched["law"]["adv"].append(new_entry)
        filled += 1

    print(f"\nДозаполнено консультантов: {filled}")
    print(f"  без извлечённого имени клиента: {skipped_no_client}")
    print(f"  клиент не нашёлся среди наших карточек: {skipped_no_match}")
    print(f"  уже был этот консультант: {skipped_already_have}")

    json.dump(deals_data, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
    with_adv = sum(1 for d in deals if d["law"]["adv"][0][1] != "Не раскрывались")
    print(f"\nИтого карточек с консультантом теперь: {with_adv} из {len(deals)} ({with_adv/len(deals)*100:.0f}%)")


if __name__ == "__main__":
    main()
