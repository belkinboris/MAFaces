#!/usr/bin/env python3
"""Разовая миграция данных из static/data/*.json в SQL-схему (db/models.py) —
фундамент под алерты, верификацию фирм и тарифы. Ничего не трогает в проде:
main.py по-прежнему читает JSON напрямую, эту БД пока никто не подключает.

Источники:
  deals_promoted.json — обогащённые карты (эко+юр разбор), уже с компаниями
                         и match_keys (алиасами) — самый ценный источник.
  deals_2026.json      — строгое подмножество deals_promoted.json (проверено
                         отдельно), поэтому не грузим его вторым заходом.
  bulk_deals.json       — сырые записи по одному источнику, без структурных
                         сторон сделки — идут как enrichment_tier=stub.

Дедуп консультантов — намеренно консервативный: точное совпадение нормализо-
ванного имени сливаем сразу, а частичные совпадения («Softline» / «Softline
Venture Partners») только логируем в entity_review_candidates.csv для ручной
проверки. Лучше временно две сущности вместо одной, чем ошибочно слитые два
разных юрлица (ровно та ловушка с ВТБ/Ромашкой, которую обсуждали).

Использование:
  python3 pipeline/migrate_to_db.py            # DATABASE_URL по умолчанию sqlite:///./kompas.db
  python3 pipeline/migrate_to_db.py --reset    # пересоздать таблицы с нуля
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import (  # noqa: E402
    Advisor, AdvisorKind, AdvisorAlias, AmountConfidence, Base, Company,
    CompanyAlias, Deal, DealAdvisor, DealSource, EnrichmentTier,
)
from db.session import SessionLocal, engine  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "data")
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "entity_review_candidates.csv")

_PUNCT_RE = re.compile(r"[«»\"'“”().,]")
_WS_RE = re.compile(r"\s+")
LEGAL_FORMS = {"ооо", "зао", "оао", "пао", "ао", "гк", "нко", "ип"}

_AMOUNT_RE = re.compile(r"(\d[\d\s]*[.,]?\d*)\s*(млрд|млн)\.?\s*(?:руб|₽|р\.)", re.I)
_HEDGE_RE = re.compile(r"оценк|оценива|около|приблизительно|по данным аналитик|по оценк|источник.*утвержда|~|порядка|эксперт", re.I)
_UNDISCLOSED_RE = re.compile(r"не раскрыв|не раскрыт|конфиденциальн", re.I)
_SKIP_ADVISOR_RE = re.compile(r"не раскрыв|не привлека|стороны сделки|^—$", re.I)


def normalize_name(name: str) -> str:
    s = _PUNCT_RE.sub(" ", (name or "").lower())
    tokens = [t for t in s.split() if t not in LEGAL_FORMS]
    return _WS_RE.sub(" ", " ".join(tokens)).strip()


def is_placeholder_advisor(name: str) -> bool:
    name = (name or "").strip()
    return not name or bool(_SKIP_ADVISOR_RE.search(name))


def parse_amount(text: str):
    """-> (сумма в млн ₽ или None, AmountConfidence). Текст всегда сохраняем
    отдельно (amount_raw) — эта функция только классифицирует, не заменяет источник."""
    text = text or ""
    m = _AMOUNT_RE.search(text)
    if not m:
        return None, AmountConfidence.undisclosed
    value = float(m.group(1).replace(" ", "").replace(",", "."))
    if m.group(2).lower() == "млрд":
        value *= 1000
    confidence = AmountConfidence.estimated if _HEDGE_RE.search(text) else AmountConfidence.disclosed
    return value, confidence


def parse_date(date_raw):
    if not date_raw or date_raw == "unknown":
        return None
    try:
        return date.fromisoformat(date_raw)
    except ValueError:
        return None


def get_or_create_advisor(session, raw_name, kind, cache, names, review_pairs):
    norm = normalize_name(raw_name)
    if not norm:
        return None
    if norm in cache:
        return cache[norm]
    for existing_norm, existing_id in list(cache.items()):
        if len(norm) >= 4 and len(existing_norm) >= 4 and (norm in existing_norm or existing_norm in norm):
            review_pairs.append((raw_name.strip(), existing_id))
    adv = Advisor(name=raw_name.strip(), kind=kind)
    session.add(adv)
    session.flush()  # нужен adv.id для алиаса
    session.add(AdvisorAlias(advisor_id=adv.id, alias=norm))
    cache[norm] = adv.id
    names[adv.id] = raw_name.strip()
    return adv.id


def load_companies(session, data, seen_company_ids, seen_aliases):
    for cid, co in data["companies"].items():
        if cid in seen_company_ids:
            continue
        kpi = co.get("kpi") or []
        session.add(Company(
            id=cid, name=co.get("name") or cid, industry=co.get("ind"),
            description=co.get("desc"),
            kpi_label=kpi[0] if len(kpi) > 0 else None,
            kpi_value=kpi[1] if len(kpi) > 1 else None,
            auto_generated=True,
        ))
        seen_company_ids.add(cid)

        aliases = set(data.get("match_keys", {}).get(cid, []))
        name_norm = normalize_name(co.get("name") or "")
        if name_norm:
            aliases.add(name_norm)
        for alias in aliases:
            alias = alias.strip()
            if alias and alias not in seen_aliases:
                session.add(CompanyAlias(company_id=cid, alias=alias))
                seen_aliases.add(alias)


def load_deals(session, data, seen_company_ids, advisor_cache, advisor_names, review_pairs):
    n = 0
    for d in data["deals"]:
        if session.get(Deal, d["id"]):
            continue
        eco = d.get("eco") or {}
        law = d.get("law") or {}
        amount_text = eco.get("sum") or d.get("sum") or ""
        amount_value, amount_conf = parse_amount(amount_text)
        buyer_id = d.get("buyer") if d.get("buyer") in seen_company_ids else None
        target_id = d.get("target") if d.get("target") in seen_company_ids else None

        deal = Deal(
            id=d["id"], date_raw=d.get("date"), date_value=parse_date(d.get("date")),
            title=d["title"], industry=d.get("ind"), deal_type=d.get("type"),
            kind=d.get("kind"), status=d.get("status"),
            buyer_company_id=buyer_id, target_company_id=target_id,
            amount_raw=amount_text or None, amount_value_mln_rub=amount_value, amount_confidence=amount_conf,
            rationale=eco.get("rationale") or None, context=eco.get("context") or None,
            structure=law.get("struct") or None, approvals=law.get("appr") or None, terms=law.get("terms") or None,
            enrichment_tier=EnrichmentTier.full, source_batch="deals_promoted.json",
        )
        session.add(deal)
        n += 1

        for entry in law.get("adv", []):
            if len(entry) < 2 or is_placeholder_advisor(entry[1]):
                continue
            side, name = entry[0], entry[1]
            note = entry[2] if len(entry) > 2 else None
            adv_id = get_or_create_advisor(session, name, AdvisorKind.legal, advisor_cache, advisor_names, review_pairs)
            session.add(DealAdvisor(deal_id=deal.id, advisor_id=adv_id, raw_name=name.strip(), side=side, note=note or None))

        finadv = eco.get("finadv") or ""
        if not is_placeholder_advisor(finadv):
            for chunk in finadv.split(";"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                sep = "—" if "—" in chunk else ("-" if "-" in chunk else None)
                name, note = (chunk.split(sep, 1) if sep else (chunk, ""))
                name = name.strip()
                if is_placeholder_advisor(name):
                    continue
                adv_id = get_or_create_advisor(session, name, AdvisorKind.investment, advisor_cache, advisor_names, review_pairs)
                session.add(DealAdvisor(deal_id=deal.id, advisor_id=adv_id, raw_name=name, side="финансовый консультант", note=note.strip() or None))

        for src in d.get("src", []):
            if len(src) >= 2:
                session.add(DealSource(deal_id=deal.id, title=src[0], url=src[1]))
    return n


def load_bulk(session, records):
    n = 0
    for rec in records:
        title = (rec.get("title") or "").strip()
        if not title:
            continue
        stub_id = "b" + hashlib.sha1(f"{rec.get('date')}|{title}".encode("utf-8")).hexdigest()[:12]
        if session.get(Deal, stub_id):
            continue
        amount_text = f"{title} {rec.get('role','')}".strip()
        amount_value, amount_conf = parse_amount(amount_text)
        if amount_value is not None and amount_conf == AmountConfidence.disclosed:
            # одна запись, один неверифицированный источник — не даём то же
            # доверие, что суммам из обогащённых карт
            amount_conf = AmountConfidence.estimated
        deal = Deal(
            id=stub_id, date_raw=rec.get("date"), date_value=parse_date(rec.get("date")),
            title=title, industry=rec.get("ind"),
            amount_raw=amount_text if amount_value is not None else None,
            amount_value_mln_rub=amount_value, amount_confidence=amount_conf,
            enrichment_tier=EnrichmentTier.stub, source_batch="bulk_deals.json",
        )
        session.add(deal)
        src = rec.get("src")
        if src and len(src) >= 2:
            session.add(DealSource(deal_id=stub_id, title=src[0], url=src[1]))
        n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="пересоздать таблицы (стирает текущую БД)")
    args = parser.parse_args()

    if args.reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    session = SessionLocal()
    try:
        seen_company_ids, seen_aliases = set(), set()
        advisor_cache, advisor_names, review_pairs = {}, {}, []

        promoted = json.load(open(os.path.join(DATA_DIR, "deals_promoted.json"), encoding="utf-8"))
        load_companies(session, promoted, seen_company_ids, seen_aliases)
        session.flush()
        n_deals_full = load_deals(session, promoted, seen_company_ids, advisor_cache, advisor_names, review_pairs)

        bulk = json.load(open(os.path.join(DATA_DIR, "bulk_deals.json"), encoding="utf-8"))
        n_deals_stub = load_bulk(session, bulk)

        session.commit()

        print(f"Компаний: {len(seen_company_ids)}")
        print(f"Консультантов (уникальных сущностей после консервативного дедупа): {len(advisor_names)}")
        print(f"Сделок добавлено: {n_deals_full + n_deals_stub} (полных карт: {n_deals_full}, необогащённых: {n_deals_stub})")
        print(f"Возможных дублей консультантов на ручной пересмотр: {len(review_pairs)}")

        if review_pairs:
            with open(REPORT_PATH, "w", encoding="utf-8") as f:
                f.write("новое_имя,похоже_на_advisor_id,похоже_на_имя\n")
                for new_name, existing_id in review_pairs:
                    f.write(f"\"{new_name}\",{existing_id},\"{advisor_names.get(existing_id, '')}\"\n")
            print(f"Отчёт: {REPORT_PATH}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
