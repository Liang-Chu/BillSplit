from datetime import date

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import validation
from app.models import User
from app.security import current_user
from app.services import access
from app.services.export import ExportParams, build_export

router = APIRouter(tags=["export"])


class ExportIn(BaseModel):
    from_: date = Field(alias="from")
    to: date
    group_ids: list[str] | None = None
    include_settlements: bool = True

    @model_validator(mode="after")
    def _range(self):
        if self.to < self.from_:
            raise ValueError("'to' must be on or after 'from'")
        return self


@router.post("/exports", response_class=Response, responses={200: {"content": {"application/zip": {}}}})
async def export(body: ExportIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    """ZIP containing expenses.xlsx (Summary / Expenses / Line Items / Payments) and attachments/.

    Rows are bills where the caller is the payer or a participant. Several groups go into one workbook."""
    if body.group_ids:
        allowed = set(access.active_group_ids(db, user.id))
        bad = [g for g in body.group_ids if g not in allowed]
        if bad:
            # guests may export groups they appear in without being members
            from sqlalchemy import or_, select

            from app.models import Expense, ExpenseShare

            guest = set(
                db.scalars(
                    select(Expense.group_id)
                    .join(ExpenseShare, ExpenseShare.expense_id == Expense.id)
                    .where(or_(ExpenseShare.user_id == user.id, Expense.payer_id == user.id))
                )
            )
            if any(g not in guest for g in bad):
                raise validation("Unknown group", group_ids="not one of your groups")
    params = ExportParams(body.from_, body.to, body.group_ids, body.include_settlements)
    data = await run_in_threadpool(build_export, db, user, params)
    filename = f"billsplit-export-{body.from_.isoformat()}_{body.to.isoformat()}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
