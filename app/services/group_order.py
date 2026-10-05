"""Per-user group order. The first available group is the user's preferred group for new expenses.

"Available" = the user is an active member and the group is not archived. The stored order is
normalized on every read and after membership changes: unavailable ids are dropped (relative order kept),
newly available groups are appended at the end. Any change bumps `group_order_revision`.
Writes use compare-and-swap on the revision so concurrent updates never overwrite each other."""

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import APIError, not_found, validation
from app.models import Group, GroupMember, User, UserPreference
from app.services import audit


@dataclass
class GroupOrder:
    group_ids: list[str]
    revision: int

    @property
    def preferred_group_id(self) -> str | None:
        return self.group_ids[0] if self.group_ids else None

    def as_dict(self) -> dict:
        return {"group_ids": self.group_ids, "preferred_group_id": self.preferred_group_id, "revision": self.revision}


def available_groups(db: Session, user_id: str) -> list[Group]:
    """Available groups in the initial/default order: default group first, then name, then id."""
    rows = db.scalars(
        select(Group)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .where(GroupMember.user_id == user_id, GroupMember.left_at.is_(None), Group.archived_at.is_(None))
    ).all()
    return sorted(rows, key=lambda g: (not g.is_default, g.name, g.id))


def normalize(stored: list[str], available: list[str]) -> list[str]:
    avail = set(available)
    seen: set[str] = set()
    out = []
    for gid in stored:
        if gid in avail and gid not in seen:
            out.append(gid)
            seen.add(gid)
    out.extend(gid for gid in available if gid not in seen)
    return out


def sync(db: Session, user_id: str) -> GroupOrder:
    """Normalize the stored order against current memberships; bump the revision if it changed."""
    db.flush()
    available = [g.id for g in available_groups(db, user_id)]
    for _ in range(5):
        pref = db.get(UserPreference, user_id)
        if pref is None:
            pref = UserPreference(user_id=user_id, group_order=available, group_order_revision=1)
            db.add(pref)
            db.flush()
            return GroupOrder(list(available), 1)
        db.refresh(pref)
        current = normalize(list(pref.group_order or []), available)
        if current == list(pref.group_order or []):
            return GroupOrder(current, pref.group_order_revision)
        if _cas(db, user_id, pref.group_order_revision, current):
            return GroupOrder(current, pref.group_order_revision + 1)
    raise APIError(409, "group_order_conflict", "Group order changed. Refresh and retry.")


def _cas(db: Session, user_id: str, expected_revision: int, new_order: list[str]) -> bool:
    db.flush()
    res = db.execute(
        update(UserPreference)
        .where(UserPreference.user_id == user_id, UserPreference.group_order_revision == expected_revision)
        .values(group_order=new_order, group_order_revision=expected_revision + 1)
        .execution_options(synchronize_session=False)
    )
    cached = db.get(UserPreference, user_id)
    if cached is not None:
        db.expire(cached)
    return res.rowcount == 1


def sync_users(db: Session, user_ids) -> None:
    """Call after membership/archival changes for every affected registered user."""
    db.flush()
    for uid in set(user_ids):
        u = db.get(User, uid)
        if u is not None and not u.is_placeholder and u.merged_into_id is None:
            sync(db, uid)


def sync_group_members(db: Session, group_id: str) -> None:
    db.flush()
    sync_users(
        db,
        db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group_id, GroupMember.left_at.is_(None))),
    )


def preferred_group_id(db: Session, user_id: str) -> str | None:
    """The single source of truth for the default group of a new expense (forms, resolve, OCR)."""
    return sync(db, user_id).preferred_group_id


def _conflict(current: GroupOrder) -> APIError:
    return APIError(
        409, "group_order_conflict", "Group order changed. Refresh and retry.", {"current": current.as_dict()}
    )


def put_order(db: Session, user: User, group_ids: list[str], revision: int) -> GroupOrder:
    if len(set(group_ids)) != len(group_ids):
        dupes = sorted({g for g in group_ids if group_ids.count(g) > 1})
        raise validation("group_ids contains duplicates", group_ids=f"duplicate ids: {', '.join(dupes)}")
    # Unknown groups or groups the user is not an active member of → not_found (no information leak)
    active = set(
        db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user.id, GroupMember.left_at.is_(None)))
    )
    for gid in group_ids:
        if gid not in active:
            raise not_found("Group")
    current = sync(db, user.id)
    if revision != current.revision or set(group_ids) != set(current.group_ids):
        raise _conflict(current)
    if group_ids == current.group_ids:
        return current  # no-op: same order, same revision
    if not _cas(db, user.id, current.revision, list(group_ids)):
        raise _conflict(sync(db, user.id))
    audit.log(db, user.id, "user", user.id, "group_order_update", {"group_ids": list(group_ids)})
    return GroupOrder(list(group_ids), current.revision + 1)
