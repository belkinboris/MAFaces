#!/usr/bin/env python3
"""Промоушен обогащённых записей 2026 года в полноценные карточки «Реестра».

Вход:  enriched_2026_final.json (результат enrich_deals.py)
Выход: static/data/deals_2026.json — {deals:[...], companies:{...}, match_keys:{...}, consumed_urls:[...]}
       Сайт подгружает этот файл и вливает карточки в DEALS, компании в COMPANIES.

Принципы честности:
- Только записи enrichment_confidence high/medium.
- Поля, для которых данных нет, получают "—" (сайт их скрывает), а не выдумку.
- Статус определяется по формулировкам источника: «может купить» → «Обсуждается».
- Компании-автопрофили помечаются как сформированные автоматически.
"""
import hashlib
import json
import os
import re
import sys

INDUSTRIES = {"Нефть и газ","Уголь","ГМК и добыча","Энергетика","Химия и удобрения","Агро",
"Пищепром и напитки","Ритейл","E-commerce","Потребительские товары","ИТ и интернет","Телеком",
"Банки","Страхование","Инвестиции и рынок ЦБ","Транспорт и логистика","Порты и инфраструктура",
"Автопром","Недвижимость","Строительство","Фарма и медицина","Медиа","Машиностроение"}

IND_FALLBACK = {
    "Лесная промышленность и целлюлозно-бумажное производство": "Потребительские товары",
    "Лесная промышленность": "Потребительские товары",
    "Сельское хозяйство": "Агро",
    "Финансовые институты": "Банки",
    "IT": "ИТ и интернет", "ТМТ": "ИТ и интернет", "TMT": "ИТ и интернет",
    "Услуги": "Потребительские товары",
    "Спорт": "Медиа",
}

# Уже промоутнутые вручную (полные карточки в index.html) — не дублируем
ALREADY_PROMOTED = {
    "https://nikolaew.ru/media/regulyatornoe-soglasovanie-m-a-sdelki-komanda-nikolaev-i-partnery-poluchila-blagodarnost-ot-ao-kompo/",
    "https://www.birchlegal.ru/news/3318/",
    "https://www.birchlegal.ru/news/3141/",
    "https://o2consult.com/news/komanda-02-consulting-osushchestvila-yuridicheskoe-soprovozhdenie-sdelki-selectel-v-svyazi-s-sozdani/",
    "https://www.rbc.ru/story/697b311c9a7947bd3b3806ee?utm_source=rbc.ru&amp;amp;utm_medium=inhouse_media&amp;amp;utm_campaign=697b2e869a79471a843e09dd&amp;amp;utm_content=story_697b311c9a7947bd3b3806ee&amp;amp;utm_term=10.4Z_noauth",
    "https://t.me/dealsma/7165",
    "https://www.forbes.ru/investicii/562988-proizvoditel-kabelej-inkab-planiruet-privlec-do-2-4-mlrd-rublej-v-hode-ipo",
    "https://www.kommersant.ru/doc/8765262",
    "https://www.rbc.ru/finances/02/07/2026/6a4557699a794761c07c3b7b",
    "https://www.kommersant.ru/doc/8764183",
}

# Существующие компании каталога: id -> ключи (для резолва сторон в уже известные профили)
EXISTING_KEYS = {
    "rencap":["ренессанс капитал"],"citibank":["ситибанк","ренкап банк"],"carlsberg":["carlsberg"],
    "baltika":["балтика"],"vginvest":["вг инвест"],"rosatom":["росатом","uranium one"],"yandex":["яндекс","yandex"],
    "berizaryad":["бери заряд"],"hugoboss":["hugo boss","хьюго босс"],"stockmann":["стокманн"],
    "delo":["«дело»"],"cargill":["cargill"],"avtodom":["автодом"],"mercedes":["mercedes-benz","мерседес"],
    "varton":["вартон"],"technored":["технорэд","technored"],"kompsystem":["композит систем"],
    "ektos":["эктосинтез","эктос"],"tokk":["токк"],"metarus":["метарус"],"erlan":["эрлан"],
    "adv":["группа адв","группы адв"],"selectel":["selectel"],"itmo":["итмо"],"sheremetyevo":["шереметьево"],
    "domodedovo":["домодедово"],"inkab":["инкаб"],"bik":["бик"],"invest18":["инвестиции 18"],
    "canc":["цанц"],"absolutstrah":["абсолют страхование"],"mid":["мать и дитя","мд медикал"],
    "ilyinskaya":["ильинская больница"],
}

GENERIC_PARTIES = re.compile(r"физическ|не указан|неизвестн|не раскрыв|инвестор[ыа]?$|акционер|основател|менеджмент|консорциум|частн|^кредитор|^банки\b", re.I)
# Список из нескольких сторон через запятую («Кредиторы: Сбербанк, Т-Банк, ...»)
# сам по себе не название одной компании — resolve_company() заведёт по нему
# мусорный профиль. Порог 2 — «А и Б» или «А, Б» ещё может быть одной стороной
# с двойным названием, три и больше запятых почти всегда перечисление.
PARTY_LIST_RE = re.compile(r",.*,.*,")

FIN_HINTS = re.compile(r"[^.]*(?:финансирован\w+|кредитн\w+ лини\w+|заёмны[хе] средств|собственных средств|обеспечит\s+финанс|предоставит\s+финанс|кредит[а-я]* (?:сбербанк|банк))[^.]*\.", re.I)
SHARE_HINTS = re.compile(r"[^.]*\b\d{1,3}(?:[.,]\d+)?\s?%[^.]*\.", re.I)
TARGET_FIN_HINTS = re.compile(r"[^.]*(?:выручк\w+|ebitda|чистая прибыль|мультипликатор|p/e\b)[^.]*\.", re.I)


def extract_first(pattern, *texts):
    for t in texts:
        if not t: continue
        m = pattern.search(t)
        if m: return m.group(0).strip()
    return None


def norm_date(d):
    d = (d or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d): return d
    if re.fullmatch(r"\d{4}-\d{2}", d): return d + "-15"
    if re.fullmatch(r"\d{4}", d): return d + "-06-15"
    return d[:10] if len(d) >= 10 else "unknown"


def slug(url_or_name):
    return "g" + hashlib.sha1(url_or_name.encode("utf-8")).hexdigest()[:8]


def detect_status(title, full_text):
    tl = title.lower()
    if re.search(r"сорвал|не состоял", tl): return "Не состоялась"
    if re.search(r"может куп|планирует|ведет переговор|намерен|рассматрива|выставл|ищет покупател|интересуется", tl): return "Обсуждается"
    # совершенный вид в заголовке = сделка состоялась, независимо от планов в контексте
    if re.search(r"приобрел|купил|продал|закрыл|провел|провёл|выкупил|получил|перешл|стал[аи]? владельц|вошл|консолидировал|привлек", tl): return "Закрыта"
    t = full_text.lower()
    if re.search(r"сорвал|не состоял|отказал[аи]сь от сделки", t): return "Не состоялась"
    if re.search(r"сделка закрыта|закрытие сделки состоял|завершена", t): return "Закрыта"
    if re.search(r"может куп|ведет переговор|намерен приобрес|выставлен[аоы]? на продажу", t): return "Обсуждается"
    return "Закрыта"


def detect_type(title, role):
    t = (title + " " + role).lower()
    if "ipo" in t: return "IPO · размещение акций"
    if re.search(r"совместн\w+ предприят|созда\w+ сп\b", t): return "M&A · создание СП"
    if re.search(r"банкрот|аукцион|торг", t): return "Продажа с торгов"
    if re.search(r"венчурн|инвестиro|раунд", t): return "Венчурная инвестиция"
    return "M&A"


ESTIMATE_WORDS = re.compile(r"оценк|аналит|эксперт|возможн|стартов|начальн|по некоторым данным|не раскрыв|предполож|ориентировочн", re.I)


def short_sum(sum_text):
    if not sum_text: return "Не раскрыта"
    # Официальное правило: в ленту идёт только раскрытая сторонами/консультантами цена.
    # Оценки аналитиков, экспертные вилки и стартовые цены торгов остаются внутри карточки (eco.sum).
    if ESTIMATE_WORDS.search(sum_text): return "Не раскрыта"
    m = re.search(r"([~≈]?\s?\d[\d\s.,–—-]*\s*(?:млрд|млн|тыс)\.?\s*(?:руб(?:лей)?\.?|₽|\$|долл\w*|евро|€))", sum_text)
    if m:
        s = re.sub(r"\s+", " ", m.group(1)).strip()
        s = s.replace("руб.", "₽").replace("рублей", "₽").replace("руб", "₽")
        if s.startswith("~") or s.startswith("≈"): return "Не раскрыта"
        return s
    return "Не раскрыта"


def detect_appr(text):
    sents = re.split(r"(?<=[.;])\s+", text)
    hits = [s for s in sents if re.search(r"ФАС|Правкомисс|распоряжени|Президент|ЦБ РФ|Банк России|антимонопол", s)]
    return " ".join(hits[:2]) if hits else "Публично не сообщалось"


def parse_advisor(adv_str):
    """'BIRCH (за покупателя ...)' -> ('BIRCH', 'за покупателя ...')"""
    m = re.match(r"^(.*?)\s*[—(–-]\s*(.*)$", adv_str)
    if m:
        return m.group(1).strip().rstrip("(").strip(), m.group(2).rstrip(")").strip()
    return adv_str.strip(), ""


def clean_party(p):
    return re.sub(r"\s*\((покупатель|продавец|продавцы|инвестор\w*)\)\s*$", "", p, flags=re.I).strip()


LEGAL_FORMS = {"ооо", "зао", "оао", "пао", "ао", "гк", "нко", "ип"}


def normalize_name(name):
    """Тот же принцип, что и в db/migrate_to_db.py: без орг.-правовой формы
    и пунктуации, схлопнутые пробелы — база для сравнения имён компаний."""
    s = re.sub(r"[«»\"'().,]", " ", (name or "").lower())
    tokens = [t for t in s.split() if t not in LEGAL_FORMS]
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def resolve_company(name, new_companies, match_keys, ind, review):
    """Сливаем с существующим профилем только при точном совпадении
    нормализованного имени. Раньше сливали по одной лишь подстроке
    (`k in low or low in k`) — из-за этого разные юрлица с похожими именами
    («Softline» / «Softline Venture Partners») попадали в один профиль.
    Частичное совпадение теперь не сливает автоматически — компания
    заводится новая, а пара откладывается в review на ручной пересмотр
    (тот же принцип, что и для консультантов в migrate_to_db.py)."""
    low = name.lower()
    norm = normalize_name(name)
    all_keys = {**EXISTING_KEYS, **match_keys}
    for cid, keys in all_keys.items():
        if any(normalize_name(k) == norm for k in keys):
            return cid, False
    if norm:
        for cid, keys in all_keys.items():
            for k in keys:
                nk = normalize_name(k)
                if len(norm) >= 4 and len(nk) >= 4 and (nk in norm or norm in nk):
                    review.append((name.strip(), cid))
                    break
    cid = slug(low)
    new_companies[cid] = {
        "name": name,
        "ind": ind,
        "desc": "Профиль сформирован автоматически из сделок 2026 года; данные уточняются.",
        "kpi": ["Профиль", "Автоматический"],
    }
    match_keys[cid] = [norm] if len(norm) > 3 else []
    return cid, True


def main():
    src_path = sys.argv[1] if len(sys.argv) > 1 else "enriched_2026_final.json"
    enriched = json.load(open(src_path, encoding="utf-8"))
    good = [r for r in enriched if r.get("enrichment_confidence") in ("high", "medium")
            and r.get("source_url") not in ALREADY_PROMOTED]
    print(f"Кандидатов на промоушен: {len(good)}")

    deals, companies, match_keys, consumed, review = [], {}, {}, [], []
    for r in good:
        role = r.get("role", "") or ""
        extra = r.get("extra_details", "") or ""
        full_text = r.get("title", "") + " " + role + " " + extra
        ind_raw = r.get("industry", "")
        ind = ind_raw if ind_raw in INDUSTRIES else IND_FALLBACK.get(ind_raw, ind_raw or "Инвестиции и рынок ЦБ")

        parties = [clean_party(p) for p in (r.get("parties") or []) if p and not GENERIC_PARTIES.search(p) and not PARTY_LIST_RE.search(p)]
        buyer_id, target_id = None, None
        for i, p in enumerate((r.get("parties") or [])[:4]):
            cp = clean_party(p)
            if not cp or GENERIC_PARTIES.search(cp) or PARTY_LIST_RE.search(cp):
                continue
            cid, _ = resolve_company(cp, companies, match_keys, ind, review)
            if buyer_id is None and ("покупатель" in p.lower() or i == 0):
                buyer_id = cid
            elif target_id is None:
                target_id = cid
        if buyer_id is None:
            continue  # без единой распознанной стороны карточка бессмысленна

        legal_advs = []
        for a in r.get("legal_advisors") or []:
            firm, note = parse_advisor(a)
            legal_advs.append(["Юридический консультант", firm, note])
        fin_advs = r.get("financial_advisors") or []

        sources = [[s.get("outlet", "?"), s.get("url", "")] for s in (r.get("sources") or []) if s.get("url")][:6]
        if not sources and r.get("source_url"):
            sources = [[r.get("source_name", "Источник"), r["source_url"]]]

        fin_hint = extract_first(FIN_HINTS, extra, role)
        share_hint = extract_first(SHARE_HINTS, extra, role)
        target_fin_hint = extract_first(TARGET_FIN_HINTS, extra, role)

        deals.append({
            "id": slug(r.get("source_url") or r.get("title", "")),
            "date": norm_date(r.get("date", "")),
            "title": r.get("title", "").strip(),
            "buyer": buyer_id, "target": target_id,
            "ind": ind,
            "type": detect_type(r.get("title", ""), role),
            "status": detect_status(r.get("title", ""), full_text),
            "sum": short_sum(r.get("sum")),
            "eco": {
                "sum": r.get("sum") or "Не раскрыта",
                "share": share_hint or "—", "val": "—",
                "target_fin": target_fin_hint or "—",
                "fin": fin_hint or "—",
                "rationale": role or "—",
                "context": "—",
                "finadv": "; ".join(fin_advs) if fin_advs else "Не привлекался",
            },
            "law": {
                "struct": "—",
                "appr": detect_appr(full_text),
                "adv": legal_advs or [["Стороны сделки", "Не раскрывались", "Юридические консультанты в публичных источниках не раскрывались"]],
                "terms": "—",
            },
            "src": sources,
            "extra": extra or None,
            "auto": True,
        })
        consumed.append(r["source_url"])

    out = {"deals": deals, "companies": companies, "match_keys": match_keys, "consumed_urls": consumed}

    # Слияние пар «слух + закрытие» одной сделки: если у двух карточек пересечение
    # значимых слов заголовка >= 3 и одна «Закрыта», а другая «Обсуждается» — оставляем
    # закрытую, источники слуха переносим в неё.
    STOP = {"ооо","ао","пао","гк","компания","группа","доля","долей","акций","сделка","бизнес","приобрел","приобрела","купил","купила","может","купить","продал","продала","россии"}
    def toks(t):
        return {w for w in re.sub(r"[«»\"'().,–—-]"," ",t.lower()).split() if len(w)>3 and w not in STOP}
    drop = set()
    for i, a in enumerate(deals):
        for j, b in enumerate(deals):
            if i >= j or i in drop or j in drop: continue
            if len(toks(a["title"]) & toks(b["title"])) >= 3:
                closed, rumor = (a, b) if a["status"]=="Закрыта" else (b, a) if b["status"]=="Закрыта" else (None, None)
                if closed and rumor and rumor["status"]=="Обсуждается":
                    seen = {s[1] for s in closed["src"]}
                    closed["src"] += [s for s in rumor["src"] if s[1] not in seen][:2]
                    drop.add(deals.index(rumor))
    if drop:
        merged_titles = [deals[k]["title"][:60] for k in sorted(drop)]
        deals = [d for k, d in enumerate(deals) if k not in drop]
        out["deals"] = deals
        print(f"Слито пар слух+закрытие: {len(drop)} ({'; '.join(merged_titles)})")

    json.dump(out, open("deals_2026.json", "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Карточек создано: {len(deals)}")
    print(f"Новых компаний-автопрофилей: {len(companies)}")
    print(f"Потреблено URL (удалить из bulk): {len(consumed)}")
    from collections import Counter
    print("Статусы:", dict(Counter(d['status'] for d in deals)))
    with_sum = sum(1 for d in deals if d['sum'] != 'Не раскрыта')
    with_adv = sum(1 for d in deals if d['law']['adv'][0][1] != 'Не раскрывались')
    print(f"С суммой: {with_sum}, с консультантом: {with_adv}")
    print(f"Возможных дублей компаний на ручной пересмотр: {len(review)}")
    if review:
        report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "company_review_candidates_2026.csv")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("новое_имя,похоже_на_id\n")
            for new_name, existing_cid in review:
                f.write(f"\"{new_name}\",{existing_cid}\n")
        print(f"Отчёт: {report_path}")


if __name__ == "__main__":
    main()
