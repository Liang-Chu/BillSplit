from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(Out):
    id: str
    display_name: str
    username: str | None
    is_placeholder: bool


class MeOut(Out):
    id: str
    username: str | None
    display_name: str
    email: str | None
    is_super_admin: bool
    must_change_password: bool
    default_group_id: str | None = Field(
        None, description="The protected, auto-created, only-me private ledger. Never changes with group ordering."
    )
    preferred_group_id: str | None = Field(
        None,
        description="First group in the caller's own group order (`/me/group-order`): the default for new expenses. "
        "Not the same as `default_group_id`.",
    )
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: MeOut


class Recurrence(BaseModel):
    cadence: Literal["monthly", "weekly", "every_n_days"] = "monthly"
    cadence_n: int | None = Field(None, ge=1, le=366)
    anchor_day: int | None = Field(None, ge=1, le=28)
    anchor_date: date | None = None
    timezone: str = "UTC"
    grace_days: int = Field(3, ge=0, le=60)


class PeriodOut(Out):
    id: str
    group_id: str
    starts_on: date
    ends_on: date | None
    grace_until: datetime | None
    status: Literal["open", "grace", "frozen"]
    frozen_at: datetime | None


class PeriodBrief(Out):
    id: str
    starts_on: date
    ends_on: date | None
    status: str


class GroupOut(BaseModel):
    id: str
    name: str
    description: str | None
    currency: str
    is_default: bool
    is_recurring: bool
    recurrence: Recurrence | None
    my_role: str
    member_count: int
    current_period: PeriodBrief | None
    join_enabled: bool
    default_expense_type_id: str | None = None  # caller's own default type for this group
    archived_at: datetime | None
    created_at: datetime
    recurrence_effective_from: date | None = None


class MemberOut(BaseModel):
    user: UserOut
    role: str
    joined_at: datetime
    left_at: datetime | None


class AttachmentOut(BaseModel):
    id: str
    original_name: str
    mime: str
    size: int
    file_url: str
    thumb_url: str | None
    uploaded_by: UserOut
    created_at: datetime


class Page(BaseModel):
    items: list[Any]
    next_cursor: str | None = None


class Items(BaseModel):
    items: list[Any]


class ExpenseTypeRef(BaseModel):
    id: str
    name: str
    owner_id: str


class ShareOut(BaseModel):
    user: UserOut
    is_guest: bool
    split_value: str | None
    amount: int
    checked_at: datetime | None
    checked_by: UserOut | None
    settlement_id: str | None


class ItemOut(BaseModel):
    id: str
    name: str
    amount: int
    participant_ids: list[str]


class ExpensePermissions(BaseModel):
    can_edit: bool
    can_delete: bool


class ExpenseOut(BaseModel):
    id: str
    group_id: str
    period_id: str
    currency: str
    title: str
    merchant: str | None
    amount: int
    expense_date: date
    expense_type: ExpenseTypeRef | None
    payer: UserOut
    split_method: str
    shares: list[ShareOut]
    items: list[ItemOut]
    attachments: list[AttachmentOut]
    notes: str | None
    ocr_result: dict | None = None
    fully_checked: bool
    permissions: ExpensePermissions
    created_by: UserOut
    created_at: datetime
    updated_at: datetime


class ShareRef(BaseModel):
    expense_id: str
    user_id: str


class SettlementOut(BaseModel):
    id: str
    group_id: str
    period_id: str
    currency: str
    from_user: UserOut
    to_user: UserOut
    amount: int
    settled_on: date
    note: str | None
    linked_shares: list[ShareRef]
    attachments: list[AttachmentOut]
    created_by: UserOut
    created_at: datetime


class ExpensePage(BaseModel):
    items: list[ExpenseOut]
    next_cursor: str | None = None


class SettlementPage(BaseModel):
    items: list[SettlementOut]
    next_cursor: str | None = None
