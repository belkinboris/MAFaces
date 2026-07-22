"""Схема БД — компании/консультанты с алиасами для идентификации сущностей,
сделки с нормализованными суммами, и заготовка под аккаунты/алерты.

Зачем алиасы (CompanyAlias/AdvisorAlias), а не просто уникальное имя:
в сырых данных одна и та же фирма встречается под разными строками
("Softline" и "Softline Venture Partners", "ООО «Софтлайн»" и "Софтлайн") —
без таблицы алиасов их нельзя надёжно свести в одну сущность для статистики.
Дедуп через алиасы делаем консервативно (см. pipeline/migrate_to_db.py) —
лучше временно посчитать одну фирму за две, чем ошибочно слить два разных
юрлица в одно.

Зачем amount_confidence, а не просто amount: сумму на карте показываем
только если она достоверно известна; если это оценка аналитиков/СМИ —
это отдельный статус, а не то же самое, что официальное раскрытие.

Зачем enrichment_tier: разница между "полноценной картой" и "записью,
до которой пока не дошли руки" — рабочий статус пайплайна, а не то,
что должен видеть пользователь как отдельный тип карточки.
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, ForeignKey, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------- компании ---

class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    legal_name: Mapped[str | None] = mapped_column(String(400), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kpi_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kpi_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    auto_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    aliases: Mapped[list["CompanyAlias"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class CompanyAlias(Base):
    __tablename__ = "company_aliases"
    __table_args__ = (UniqueConstraint("alias", name="uq_company_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    alias: Mapped[str] = mapped_column(String(300))  # нормализованная (lower, без орг.-правовой формы)

    company: Mapped[Company] = relationship(back_populates="aliases")


# ------------------------------------------------------------ консультанты ---

class AdvisorKind(str, enum.Enum):
    legal = "legal"
    investment = "investment"


class Advisor(Base):
    __tablename__ = "advisors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[AdvisorKind] = mapped_column(Enum(AdvisorKind), default=AdvisorKind.legal)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    aliases: Mapped[list["AdvisorAlias"]] = relationship(back_populates="advisor", cascade="all, delete-orphan")


class AdvisorAlias(Base):
    __tablename__ = "advisor_aliases"
    __table_args__ = (UniqueConstraint("alias", name="uq_advisor_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    advisor_id: Mapped[int] = mapped_column(ForeignKey("advisors.id"))
    alias: Mapped[str] = mapped_column(String(300))

    advisor: Mapped[Advisor] = relationship(back_populates="aliases")


# ---------------------------------------------------------------- аккаунты ---

class UserRole(str, enum.Enum):
    individual = "individual"
    corporate = "corporate"
    firm = "firm"  # юрфирма/консультант с верифицированным профилем


class UserTier(str, enum.Enum):
    free = "free"
    paid = "paid"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(300), unique=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.individual)
    tier: Mapped[UserTier] = mapped_column(Enum(UserTier), default=UserTier.free)
    firm_id: Mapped[int | None] = mapped_column(ForeignKey("advisors.id"), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)  # бейдж «подтверждено фирмой»
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    saved_filters: Mapped[list["SavedFilter"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class SavedFilter(Base):
    """Подписка на алерт: «сообщи о сделках в отрасли X от суммы Y»."""
    __tablename__ = "saved_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    keyword: Mapped[str | None] = mapped_column(String(200), nullable=True)
    min_amount_mln_rub: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    user: Mapped[User] = relationship(back_populates="saved_filters")


# ------------------------------------------------------------------ сделки ---

class AmountConfidence(str, enum.Enum):
    disclosed = "disclosed"      # официально раскрыта сторонами/консультантами
    estimated = "estimated"      # оценка СМИ/аналитиков, не подтверждена сторонами
    undisclosed = "undisclosed"  # суммы нет вообще


class EnrichmentTier(str, enum.Enum):
    full = "full"   # собрана из нескольких источников, есть эко/юр-разбор
    stub = "stub"   # одна запись с одним источником, до обогащения руки не дошли


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    date_raw: Mapped[str | None] = mapped_column(String(20), nullable=True)  # как в источнике, вкл. "unknown"
    date_value: Mapped[date | None] = mapped_column(Date, nullable=True)     # то же самое, распарсенное — для сортировки/фильтра
    title: Mapped[str] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    deal_type: Mapped[str | None] = mapped_column(String(200), nullable=True)  # исходный текст типа сделки
    kind: Mapped[str | None] = mapped_column(String(30), nullable=True)  # acquisition/jv/financing/credit/structured/ipo
    status: Mapped[str | None] = mapped_column(String(60), nullable=True)

    buyer_company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    target_company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)

    amount_raw: Mapped[str | None] = mapped_column(String(300), nullable=True)  # исходная строка — всегда сохраняем текст
    amount_value_mln_rub: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    amount_confidence: Mapped[AmountConfidence] = mapped_column(Enum(AmountConfidence), default=AmountConfidence.undisclosed)

    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)   # eco.rationale
    context: Mapped[str | None] = mapped_column(Text, nullable=True)     # eco.context
    structure: Mapped[str | None] = mapped_column(Text, nullable=True)   # law.struct
    approvals: Mapped[str | None] = mapped_column(Text, nullable=True)   # law.appr
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)       # law.terms

    enrichment_tier: Mapped[EnrichmentTier] = mapped_column(Enum(EnrichmentTier), default=EnrichmentTier.stub)
    verified_by_firm_id: Mapped[int | None] = mapped_column(ForeignKey("advisors.id"), nullable=True)  # Шаг 2, пока пусто
    source_batch: Mapped[str | None] = mapped_column(String(60), nullable=True)  # какой файл/прогон породил запись — для отладки миграции
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    advisors: Mapped[list["DealAdvisor"]] = relationship(back_populates="deal", cascade="all, delete-orphan")
    sources: Mapped[list["DealSource"]] = relationship(back_populates="deal", cascade="all, delete-orphan")


class DealAdvisor(Base):
    """Консультант на сделке. advisor_id может быть пустым, если имя из
    текста не удалось надёжно сопоставить ни с одной сущностью в Advisor —
    тогда raw_name остаётся единственным источником правды, ничего не теряем."""
    __tablename__ = "deal_advisors"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"))
    advisor_id: Mapped[int | None] = mapped_column(ForeignKey("advisors.id"), nullable=True)
    raw_name: Mapped[str] = mapped_column(String(300))
    side: Mapped[str | None] = mapped_column(String(120), nullable=True)  # "за покупателя" и т.п.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    deal: Mapped[Deal] = relationship(back_populates="advisors")
    advisor: Mapped[Advisor | None] = relationship()


class DealSource(Base):
    __tablename__ = "deal_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"))
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(600))

    deal: Mapped[Deal] = relationship(back_populates="sources")
