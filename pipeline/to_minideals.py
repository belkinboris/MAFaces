#!/usr/bin/env python3
"""Конвертирует одобренный collected_deals.json в JS-блок для MINI_DEALS в static/index.html.

Запуск:
  python3 to_minideals.py collected_deals.json > mini_deals_snippet.js

Берёт только записи с confidence high/medium (low отсекается — сначала проверь их руками
и подними confidence, если данные подтверждаются).
"""
import json
import sys

INDUSTRIES = {"Нефть и газ","Уголь","ГМК и добыча","Энергетика","Химия и удобрения","Агро",
"Пищепром и напитки","Ритейл","E-commerce","Потребительские товары","ИТ и интернет","Телеком",
"Банки","Страхование","Инвестиции и рынок ЦБ","Транспорт и логистика","Порты и инфраструктура",
"Автопром","Недвижимость","Строительство","Фарма и медицина","Медиа","Машиностроение"}


def norm_date(d: str) -> str:
    d = (d or "").strip()
    if len(d) == 4:
        return f"{d}-01-01"
    if len(d) == 7:
        return f"{d}-01"
    return d[:10] or "1970-01-01"


def js_str(s: str) -> str:
    return json.dumps(s or "", ensure_ascii=False)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "collected_deals.json"
    deals = json.load(open(path, encoding="utf-8"))
    kept = [d for d in deals if d.get("confidence") in ("high", "medium")]
    skipped = len(deals) - len(kept)
    kept.sort(key=lambda d: norm_date(d.get("date", "")), reverse=True)

    lines = []
    for d in kept:
        ind = d.get("industry") if d.get("industry") in INDUSTRIES else "Инвестиции и рынок ЦБ"
        role = d.get("role", "")
        if d.get("client_side"):
            role = f"За {d['client_side']} — {role}"
        if d.get("sum"):
            role += f" Сумма: {d['sum']}."
        role += " (по данным сайта фирмы)"
        lines.append(
            " {date:%s,title:%s,ind:%s,firm:%s,role:%s,src:[%s,%s]},"
            % (js_str(norm_date(d.get("date"))), js_str(d.get("title")), js_str(ind),
               js_str(d.get("firm_id")), js_str(role),
               js_str(d.get("source_name") or d.get("firm_name")), js_str(d.get("source_url"))))

    print("/* Сгенерировано to_minideals.py — вставь внутрь const MINI_DEALS = [ ... ] */")
    print("\n".join(lines))
    print(f"\n/* Записей: {len(kept)}; отсечено low-confidence: {skipped} —",
          "проверь их в исходном JSON вручную */", file=sys.stderr)


if __name__ == "__main__":
    main()
