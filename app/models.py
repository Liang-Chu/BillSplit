from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.ids import new_id, utcnow


class UTCDateTime(TypeDecorator):
    """Stores aware datetimes as ISO-8601 UTC text; always returns aware UTC datetimes."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def process_result_value(self, value: str | None, dialect):
        if value is None:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _id() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


def _fk(target: str, nullable: bool = False, ondelete: str | None = None) -> Mapped[Any]:
    return mapped_column(String(36), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- users & auth


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = _id()
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by_id: Mapped[str | None] = _fk("users.id", nullable=True)
    merged_into_id: Mapped[str | None] = _fk("users.id", nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = _id()
    user_id: Mapped[str] = _fk("users.id", ondelete="CASCADE")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------- groups & periods


class Group(TimestampMixin, Base):
    __tablename__ = "groups"

    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3))
    owner_id: Mapped[str] = _fk("users.id")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    join_code: Mapped[str | None] = mapped_column(String(16), unique=True)
    join_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    cadence: Mapped[str | None] = mapped_column(String(16))
    cadence_n: Mapped[int | None] = mapped_column(Integer)
    anchor_day: Mapped[int | None] = mapped_column(Integer)
    anchor_date: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    grace_days: Mapped[int] = mapped_column(Integer, default=3)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("cadence IS NULL OR cadence IN ('monthly','weekly','every_n_days')", name="ck_group_cadence"),
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[str] = mapped_column(String(36), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(10), default="member")
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(lazy="joined")

    __table_args__ = (CheckConstraint("role IN ('owner','admin','member')", name="ck_member_role"),)


class UserGroupSetting(Base):
    """Per-user preferences for a group (types are per-user, so the default type is too)."""

    __tablename__ = "user_group_settings"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(36), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    default_expense_type_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("expense_types.id", ondelete="SET NULL")
    )


class UserPreference(Base):
    """Per-user preferences. `group_order` is the user's ordering of their available groups;
    its first entry is the user's preferred group for new expenses (not the protected default group)."""

    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    group_order: Mapped[list] = mapped_column(JSON, default=list)
    group_order_revision: Mapped[int] = mapped_column(Integer, default=0)


class Period(Base):
    __tablename__ = "periods"

    id: Mapped[str] = _id()
    group_id: Mapped[str] = _fk("groups.id", ondelete="CASCADE")
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    grace_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(8), default="open")
    frozen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (CheckConstraint("status IN ('open','grace','frozen')", name="ck_period_status"),)


class PeriodMember(Base):
    __tablename__ = "period_members"

    period_id: Mapped[str] = mapped_column(String(36), ForeignKey("periods.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)


class PeriodBalance(Base):
    __tablename__ = "period_balances"

    period_id: Mapped[str] = mapped_column(String(36), ForeignKey("periods.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    paid: Mapped[int] = mapped_column(Integer, default=0)
    owed: Mapped[int] = mapped_column(Integer, default=0)
    net: Mapped[int] = mapped_column(Integer, default=0)  # outstanding net at freeze (+ = is owed)


class PeriodTransfer(Base):
    __tablename__ = "period_transfers"

    id: Mapped[str] = _id()
    period_id: Mapped[str] = _fk("periods.id", ondelete="CASCADE")
    from_user_id: Mapped[str] = _fk("users.id")
    to_user_id: Mapped[str] = _fk("users.id")
    amount: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- routing


class Routing(Base):
    __tablename__ = "routings"

    id: Mapped[str] = _id()
    user_id: Mapped[str] = _fk("users.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(String(100))
    group_id: Mapped[str | None] = _fk("groups.id", nullable=True, ondelete="SET NULL")
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("user_id", "name"),)


class ExpenseType(Base):
    __tablename__ = "expense_types"

    id: Mapped[str] = _id()
    user_id: Mapped[str] = _fk("users.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(String(100))
    routing_id: Mapped[str] = _fk("routings.id")
    icon: Mapped[str | None] = mapped_column(String(50))
    color: Mapped[str | None] = mapped_column(String(20))
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("user_id", "name"),)


class AutoRule(Base):
    __tablename__ = "auto_rules"

    id: Mapped[str] = _id()
    user_id: Mapped[str] = _fk("users.id", ondelete="CASCADE")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    field: Mapped[str] = mapped_column(String(10), default="merchant")
    mode: Mapped[str] = mapped_column(String(10), default="contains")
    pattern: Mapped[str] = mapped_column(String(255))
    set_type_id: Mapped[str | None] = _fk("expense_types.id", nullable=True, ondelete="SET NULL")
    set_group_id: Mapped[str | None] = _fk("groups.id", nullable=True, ondelete="SET NULL")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        CheckConstraint("field IN ('merchant','title')", name="ck_rule_field"),
        CheckConstraint("mode IN ('contains','equals','regex')", name="ck_rule_mode"),
    )


# ---------------------------------------------------------------- expenses


class Expense(TimestampMixin, Base):
    __tablename__ = "expenses"

    id: Mapped[str] = _id()
    group_id: Mapped[str] = _fk("groups.id")
    period_id: Mapped[str] = _fk("periods.id")
    expense_type_id: Mapped[str | None] = _fk("expense_types.id", nullable=True, ondelete="SET NULL")
    title: Mapped[str] = mapped_column(String(200))
    merchant: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[int] = mapped_column(Integer)
    expense_date: Mapped[date] = mapped_column(Date, index=True)
    payer_id: Mapped[str] = _fk("users.id")
    split_method: Mapped[str] = mapped_column(String(10))
    notes: Mapped[str | None] = mapped_column(Text)
    ocr_result: Mapped[dict | None] = mapped_column(JSON)
    created_by_id: Mapped[str] = _fk("users.id")
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    shares: Mapped[list["ExpenseShare"]] = relationship(
        back_populates="expense", cascade="all, delete-orphan", order_by="ExpenseShare.user_id"
    )
    items: Mapped[list["ExpenseItem"]] = relationship(
        back_populates="expense", cascade="all, delete-orphan", order_by="ExpenseItem.position"
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_expense_amount"),
        CheckConstraint(
            "split_method IN ('equal','exact','percent','shares','itemized')", name="ck_expense_split"
        ),
    )


class ExpenseShare(Base):
    __tablename__ = "expense_shares"

    expense_id: Mapped[str] = mapped_column(String(36), ForeignKey("expenses.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True, index=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    split_value: Mapped[str | None] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    checked_by_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    settlement_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("settlements.id", ondelete="SET NULL"), index=True
    )

    expense: Mapped[Expense] = relationship(back_populates="shares")


class ExpenseItem(Base):
    __tablename__ = "expense_items"

    id: Mapped[str] = _id()
    expense_id: Mapped[str] = _fk("expenses.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0)

    expense: Mapped[Expense] = relationship(back_populates="items")
    participants: Mapped[list["ExpenseItemParticipant"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class ExpenseItemParticipant(Base):
    __tablename__ = "expense_item_participants"

    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("expense_items.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[str] = _id()
    group_id: Mapped[str] = _fk("groups.id")
    period_id: Mapped[str] = _fk("periods.id")
    from_user_id: Mapped[str] = _fk("users.id")
    to_user_id: Mapped[str] = _fk("users.id")
    amount: Mapped[int] = mapped_column(Integer)
    settled_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[str] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (CheckConstraint("amount > 0", name="ck_settlement_amount"),)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[str] = _id()
    owner_type: Mapped[str | None] = mapped_column(String(10), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_backend: Mapped[str] = mapped_column(String(8))
    storage_key: Mapped[str] = mapped_column(String(255))
    thumb_key: Mapped[str | None] = mapped_column(String(255))
    sync_status: Mapped[str] = mapped_column(String(8), default="ok")
    uploaded_by_id: Mapped[str] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("owner_type IS NULL OR owner_type IN ('expense','settlement')", name="ck_attachment_owner"),
        CheckConstraint("storage_backend IN ('r2','local')", name="ck_attachment_backend"),
        CheckConstraint("sync_status IN ('ok','pending')", name="ck_attachment_sync"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = _id()
    actor_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(32))
    diff: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
