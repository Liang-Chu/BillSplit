from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import APIError
from app.models import Group, GroupMember, User
from app.schemas.common import TokenOut
from app.security import (
    current_user_any,
    hash_password,
    issue_tokens,
    login_limiter,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password,
)
from app.serializers import me_out
from app.services import audit
from app.services.people import check_claimable, group_placeholders
from app.services.group_order import sync_users
from app.services.seed import seed_user

router = APIRouter(prefix="/auth", tags=["auth"])

USERNAME = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
PASSWORD = Field(min_length=8, max_length=256)


def find_group_by_code(db: Session, code: str) -> Group:
    g = db.scalars(select(Group).where(func.upper(Group.join_code) == code.strip().upper())).first()
    if g is None or not g.join_enabled or g.archived_at is not None or g.is_default:
        raise APIError(404, "invalid_join_code", "Join code is invalid or disabled")
    return g


def token_response(db: Session, user: User) -> TokenOut:
    return TokenOut(**issue_tokens(db, user), user=me_out(db, user))


class JoinPreviewOut(BaseModel):
    class G(BaseModel):
        id: str
        name: str
        member_count: int

    class P(BaseModel):
        id: str
        display_name: str

    group: G
    placeholders: list[P]


@router.get("/join-preview", response_model=JoinPreviewOut)
def join_preview(code: str, db: Session = Depends(get_db, scope="function")):
    g = find_group_by_code(db, code)
    count = db.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == g.id, GroupMember.left_at.is_(None))
    )
    return JoinPreviewOut(
        group=JoinPreviewOut.G(id=g.id, name=g.name, member_count=count),
        placeholders=[JoinPreviewOut.P(id=p.id, display_name=p.display_name) for p in group_placeholders(db, g.id)],
    )


class RegisterIn(BaseModel):
    join_code: str
    username: str = USERNAME
    password: str = PASSWORD
    display_name: str = Field(min_length=1, max_length=100)
    email: str | None = None
    claim_placeholder_id: str | None = None


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db, scope="function")):
    g = find_group_by_code(db, body.join_code)
    if db.scalar(select(User.id).where(func.lower(User.username) == body.username.lower())):
        raise APIError(409, "username_taken", "Username is already taken")
    if body.claim_placeholder_id:
        user = check_claimable(db, g.id, body.claim_placeholder_id)
        user.is_placeholder = False
        action = "claim_convert"
    else:
        user = User(display_name=body.display_name)
        db.add(user)
        action = "register"
    user.username = body.username
    user.display_name = body.display_name
    user.email = body.email
    user.password_hash = hash_password(body.password)
    db.flush()
    seed_user(db, user, currency=g.currency)
    m = db.get(GroupMember, (g.id, user.id))
    if m is None:
        db.add(GroupMember(group_id=g.id, user_id=user.id, role="member"))
    elif m.left_at is not None:
        m.left_at = None
    audit.log(db, user.id, "user", user.id, action, {"group_id": g.id})
    sync_users(db, [user.id])
    return token_response(db, user)


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db, scope="function")):
    key = f"{body.username.lower()}|{request.client.host if request.client else ''}"
    login_limiter.check(key)
    user = db.scalars(select(User).where(func.lower(User.username) == body.username.lower())).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise APIError(401, "invalid_credentials", "Wrong username or password")
    if user.disabled_at is not None:
        raise APIError(403, "account_disabled", "Account is disabled")
    login_limiter.reset(key)
    return token_response(db, user)


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, db: Session = Depends(get_db, scope="function")):
    user = rotate_refresh_token(db, body.refresh_token)
    return token_response(db, user)


@router.post("/logout", status_code=204)
def logout(body: RefreshIn, _: User = Depends(current_user_any), db: Session = Depends(get_db, scope="function")):
    revoke_refresh_token(db, body.refresh_token)
    return Response(status_code=204)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = PASSWORD


@router.post("/change-password", status_code=204)
def change_password(body: ChangePasswordIn, user: User = Depends(current_user_any), db: Session = Depends(get_db, scope="function")):
    if not verify_password(body.current_password, user.password_hash):
        raise APIError(401, "invalid_credentials", "Current password is wrong")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    db.add(user)
    audit.log(db, user.id, "user", user.id, "change_password")
    return Response(status_code=204)
