from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User
from app.security import hash_password
from app.services.seed import seed_user


def create_super_admin(db: Session, username: str, password: str, display_name: str = "Admin") -> User:
    u = User(username=username, display_name=display_name, password_hash=hash_password(password), is_super_admin=True)
    db.add(u)
    db.flush()
    seed_user(db, u)
    return u


def bootstrap_admin(db: Session) -> User | None:
    s = get_settings()
    if not (s.admin_username and s.admin_password):
        return None
    if db.scalar(select(func.count()).select_from(User)):
        return None
    u = create_super_admin(db, s.admin_username, s.admin_password, s.admin_display_name)
    db.commit()
    return u
