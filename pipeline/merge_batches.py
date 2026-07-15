#!/usr/bin/env python3
"""Сливает несколько выходов promote_all.py в один файл + сквозной дедуп
между партиями (не только внутри одной партии, как делает сам promote_all.py)."""
import json
import re
import sys
from datetime import date

STOP = {"ооо","ао","пао","гк","компания","группа","доля","долей","акций","сделка","бизнес",
        "приобрел","приобрела","купил","купила","может","купить","продал","продала","россии",
        "структурн","инвестиционн","совместн","предприят","создают","создала","создаёт",
        "организац","группой","инвесторов","залог","закрыт","провел","провёл","получил",
        "заключил","заключила","консолидировал","привлек","привлекла","выкупил","выкупила",
        "компании","стороны","участием","рамках"}
QUOTED = re.compile(r"«([^»]{2,40})»")


def toks(t):
    # слова из названий в кавычках («...») не считаем для этого сигнала — общее
    # имя компании само по себе не значит, что это одна и та же сделка (компания
    # может участвовать в нескольких разных сделках). Для имён — отдельный сигнал 2.
    without_quoted = QUOTED.sub(" ", t)
    return {w[:6] for w in re.sub(r"[«»\"'().,–—-]", " ", without_quoted.lower()).split()
            if len(w) > 4 and w not in STOP}


def quoted_names(t):
    return {m.lower() for m in QUOTED.findall(t or "")}


def amount_of(t):
    m = re.search(r"(\d[\d\s.,]*)\s*(млрд|млн)", t or "", re.I)
    if not m:
        return None
    val = float(m.group(1).replace(" ", "").replace(",", "."))
    return val * (1000 if m.group(2).lower() == "млрд" else 1)


def days_between(d1, d2):
    try:
        a = date.fromisoformat(d1); b = date.fromisoformat(d2)
        return abs((a - b).days)
    except (ValueError, TypeError):
        return 9999


def is_dup(a, b):
    close_in_time = days_between(a["date"], b["date"]) <= 90
    if close_in_time and len(toks(a["title"]) & toks(b["title"])) >= 5:
        return True
    shared_name = quoted_names(a["title"]) & quoted_names(b["title"])
    amt_a, amt_b = amount_of(a["title"]), amount_of(b["title"])
    if shared_name and amt_a and amt_b and abs(amt_a - amt_b) / max(amt_a, amt_b) < 0.05 \
            and days_between(a["date"], b["date"]) <= 45:
        return True
    return False


def richness(d):
    has_adv = 1 if d["law"]["adv"] and d["law"]["adv"][0][1] not in ("Не раскрывались",) else 0
    return (has_adv, len(d.get("src", [])))


def main():
    files = sys.argv[1:-1]
    out_path = sys.argv[-1]
    merged_deals, companies, match_keys, consumed = [], {}, {}, []
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        for c in d["deals"]:
            c.setdefault("kind", "acquisition")
        merged_deals += d["deals"]
        companies.update(d["companies"])
        match_keys.update(d["match_keys"])
        consumed += d.get("consumed_urls", [])

    print(f"До сквозного дедупа: {len(merged_deals)}")
    drop = set()
    for i, a in enumerate(merged_deals):
        if i in drop: continue
        for j in range(i + 1, len(merged_deals)):
            if j in drop: continue
            b = merged_deals[j]
            if is_dup(a, b):
                keep, lose = (a, b) if richness(a) >= richness(b) else (b, a)
                seen = {s[1] for s in keep["src"]}
                keep["src"] += [s for s in lose["src"] if s[1] not in seen]
                drop.add(j if keep is a else i)
    if drop:
        titles = [merged_deals[k]["title"][:60] for k in sorted(drop)]
        print(f"Слито сквозных дублей: {len(drop)}")
        for t in titles: print("  -", t)
    merged_deals = [d for k, d in enumerate(merged_deals) if k not in drop]

    # защита от коллизий id
    seen_ids = {}
    for d in merged_deals:
        if d["id"] in seen_ids:
            seen_ids[d["id"]] += 1
            d["id"] = f"{d['id']}-{seen_ids[d['id']]}"
        else:
            seen_ids[d["id"]] = 0

    out = {"deals": merged_deals, "companies": companies, "match_keys": match_keys, "consumed_urls": consumed}
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\nИтого карточек: {len(merged_deals)}, компаний: {len(companies)}")


if __name__ == "__main__":
    main()
