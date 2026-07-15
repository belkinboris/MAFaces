#!/usr/bin/env python3
"""Приводит enriched_final_snapshot.json (формат {draft, enriched}) к плоскому
формату, который понимает promote_all.py.

Главное отличие этой партии от партии 2026 года: нет поля `parties`
(готового списка сторон с ролями). Извлекаем покупателя/цель эвристикой
из заголовка. Если уверенности нет — оставляем сторону пустой (честно),
а не гадаем: неверно приписанный покупатель хуже отсутствующего.
"""
import json
import re
import sys

BUY_VERBS = r"приобрел[аи]?|приобрёл|купил[аи]?|выкупил[аи]?|консолидировал[аи]?|получил[аи]?|нарастил[аи]?"
SELL_VERBS = r"продал[аи]?|продаёт|реализовал[аи]?"

QUOTED = re.compile(r"«([^»]{2,60})»")
CAP_PHRASE = re.compile(r"\b([А-ЯЁA-Z][а-яёa-zA-Z0-9\-]*(?:\s+[А-ЯЁA-Z][а-яёa-zA-Z0-9\-]*){0,3})\b")

GENERIC = re.compile(
    r"^(лицензи|акци|доли|доля|активы|бизнес|пакет|фонд|компани|проект|здани|"
    r"завод|станци|месторождени)", re.I)


def first_candidate(text):
    """Первая непустая кандидатная компания: сначала в кавычках, потом капитализированная фраза."""
    m = QUOTED.search(text)
    if m and not GENERIC.match(m.group(1)):
        return m.group(1).strip()
    for m in CAP_PHRASE.finditer(text):
        cand = m.group(1).strip()
        if len(cand) > 2 and not GENERIC.match(cand) and cand.lower() not in ("это", "или"):
            return cand
    return None


LABELED = re.compile(r"покупатель\s*[—-]\s*([^,.;\n]{2,60})", re.I)
SELLER_LABELED = re.compile(r"продавец\s*[—-]\s*([^,.;\n]{2,60})", re.I)


def extract_parties(title, extra_details=""):
    """Возвращает (buyer, target) или (None, None), если не уверены."""
    # Приоритет 1: явная метка «покупатель — X» в тексте (реже, но надёжнее регулярки по заголовку)
    lm = LABELED.search(extra_details or "")
    if lm:
        buyer = lm.group(1).strip().rstrip(".")
        sm = SELLER_LABELED.search(extra_details or "")
        target = sm.group(1).strip().rstrip(".") if sm else first_candidate(title)
        return buyer, target
    # Убираем уточнение в скобках вида "(через «дочку»)" — иначе регулярка находит
    # дочернюю структуру вместо реального покупателя
    clean_title = re.sub(r"\(через\s+[^)]+\)", "", title).strip()
    # Паттерн 1: "X приобрёл ... у Y" -> buyer=X, seller/target=Y
    m = re.search(rf"^(.*?)\s+(?:{BUY_VERBS})\s+(.*?)(?:\s+у\s+(.+))?$", clean_title, re.I)
    if m:
        buyer_raw = m.group(1).strip()
        seller_raw = (m.group(3) or "").strip()
        buyer = first_candidate(buyer_raw)
        target = first_candidate(seller_raw) if seller_raw else None
        if buyer:
            return buyer, target
    # Паттерн 2: "Продажа Y компании/банку X" -> buyer=X (после компании/банку), target=Y
    m = re.search(r"^Продажа\s+(.*?)\s+(?:компании|банку|фонду)\s+(.+)$", clean_title, re.I)
    if m:
        target = first_candidate(m.group(1))
        buyer = first_candidate(m.group(2))
        if buyer:
            return buyer, target
    # Паттерн 3: "X продал Y" -> seller известен, buyer нет (честно оставляем пустым)
    m = re.search(rf"^(.*?)\s+(?:{SELL_VERBS})\s+(.*)$", clean_title, re.I)
    if m:
        target = first_candidate(m.group(2))
        return None, target
    # Ничего не нашли структурно — берём первую заметную компанию как «участника»
    # сделки без уточнения роли (лучше показать хоть что-то честно нейтральное,
    # чем ничего, если заголовок явно про конкретную компанию)
    return None, first_candidate(clean_title)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "enriched_final_snapshot.json"
    out = sys.argv[2] if len(sys.argv) > 2 else "enriched_flat_for_promote.json"

    data = json.load(open(src, encoding="utf-8"))
    flat = []
    no_buyer = 0
    for r in data:
        d, e = r.get("draft", {}), r.get("enriched", {})
        conf = e.get("enrichment_confidence")
        if conf not in ("high", "medium"):
            continue
        title = e.get("title") or d.get("title") or ""
        buyer, target = extract_parties(title, e.get("extra_details") or "")
        parties = []
        if buyer:
            parties.append(f"{buyer} (покупатель)")
        if target:
            parties.append(target)
        if not buyer:
            no_buyer += 1

        flat.append({
            "title": title,
            "date": e.get("date") or d.get("date"),
            "industry": e.get("industry"),
            "sum": e.get("sum"),
            "parties": parties,
            "role": "",  # нет отдельного поля role в этой партии — используем extra_details
            "extra_details": e.get("extra_details"),
            "legal_advisors": e.get("legal_advisors") or [],
            "financial_advisors": e.get("financial_advisors") or [],
            "status_hint": e.get("status"),  # уже посчитан агентом, используем как приоритет
            "source_url": d.get("url"),
            "source_name": d.get("source"),
            "sources": e.get("sources") or [],
            "enrichment_confidence": conf,
        })

    json.dump(flat, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Всего годных (high/medium): {len(flat)}")
    print(f"Без распознанного покупателя (честно оставлено пустым): {no_buyer}")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
