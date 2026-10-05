from sqlalchemy.orm import Session

from app.models import AuditLog


def log(db: Session, actor_id: str | None, entity: str, entity_id: str, action: str, diff: dict | None = None) -> None:
    db.add(AuditLog(actor_id=actor_id, entity=entity, entity_id=entity_id, action=action, diff=diff))
