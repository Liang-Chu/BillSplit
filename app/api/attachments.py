from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import forbidden, not_found
from app.ids import new_id, utcnow
from app.models import Attachment, Expense, Settlement, User
from app.schemas.common import AttachmentOut
from app.security import current_user
from app.serializers import UserCache, attachment_out
from app.services import access
from app.services.files import ProcessedFile, process_upload
from app.services.ocr import get_ocr
from app.services.resolve import resolve
from app.storage import get_storage

router = APIRouter(tags=["attachments"])


async def read_upload(file: UploadFile) -> bytes:
    max_bytes = get_settings().upload_max_mb * 1024 * 1024
    return await file.read(max_bytes + 1)


def store_upload(db: Session, pf: ProcessedFile, filename: str | None, user: User) -> Attachment:
    now = datetime.now(UTC)
    aid = new_id()
    key = f"attachments/{now:%Y/%m}/{aid}.{pf.ext}"
    thumb_key = f"thumbs/{now:%Y/%m}/{aid}.jpg"
    storage = get_storage()
    backend, status = storage.put(key, pf.data, pf.mime)
    if backend == "r2":
        tb, ts = storage.put(thumb_key, pf.thumb, "image/jpeg")
        if tb != "r2":  # keep the pair on one backend
            storage.local.put(key, pf.data, pf.mime)
            backend, status = "local", "pending"
    else:
        storage.local.put(thumb_key, pf.thumb, "image/jpeg")
    name = (filename or f"upload.{pf.ext}").rsplit("/", 1)[-1][:200]
    if pf.mime == "image/jpeg" and not name.lower().endswith((".jpg", ".jpeg")):
        name = name.rsplit(".", 1)[0] + ".jpg"  # HEIC converted
    a = Attachment(
        id=aid, original_name=name, mime=pf.mime, size=len(pf.data), sha256=pf.sha256, storage_backend=backend,
        storage_key=key, thumb_key=thumb_key, sync_status=status, uploaded_by_id=user.id,
    )
    db.add(a)
    db.flush()
    return a


@router.post("/attachments", response_model=AttachmentOut, status_code=201)
async def upload(file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    pf = process_upload(await read_upload(file), get_settings().upload_max_mb * 1024 * 1024)
    a = store_upload(db, pf, file.filename, user)
    return attachment_out(a, UserCache(db))


def _visible(db: Session, attachment_id: str, user: User) -> Attachment:
    a = db.get(Attachment, attachment_id)
    if a is None or a.deleted_at is not None:
        raise not_found("Attachment")
    if a.uploaded_by_id == user.id:
        return a
    if a.owner_type == "expense":
        e = db.get(Expense, a.owner_id)
        if e and (access.membership(db, e.group_id, user.id) or access.is_on_expense(db, e, user.id)):
            return a
    if a.owner_type == "settlement":
        s = db.get(Settlement, a.owner_id)
        if s and (access.membership(db, s.group_id, user.id) or user.id in (s.from_user_id, s.to_user_id)):
            return a
    raise not_found("Attachment")


@router.get("/attachments/{attachment_id}", response_model=AttachmentOut)
def get_attachment(attachment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    return attachment_out(_visible(db, attachment_id, user), UserCache(db))


def _serve(a: Attachment, key: str, mime: str, filename: str, inline: bool):
    storage = get_storage()
    if a.storage_backend == "r2":
        url = storage.backend("r2").presigned_url(key, filename)
        if url:
            return RedirectResponse(url, status_code=302)
    data = storage.get(a.storage_backend, key)
    disposition = "inline" if inline else "attachment"
    return Response(
        data, media_type=mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get("/attachments/{attachment_id}/file")
def get_file(attachment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    a = _visible(db, attachment_id, user)
    return _serve(a, a.storage_key, a.mime, a.original_name, inline=a.mime != "application/pdf")


@router.get("/attachments/{attachment_id}/thumb")
def get_thumb(attachment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    a = _visible(db, attachment_id, user)
    if not a.thumb_key:
        raise not_found("Thumbnail")
    return _serve(a, a.thumb_key, "image/jpeg", "thumb.jpg", inline=True)


@router.delete("/attachments/{attachment_id}", status_code=204)
def delete_attachment(attachment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    a = _visible(db, attachment_id, user)
    allowed = a.uploaded_by_id == user.id
    if not allowed and a.owner_type == "expense":
        e = db.get(Expense, a.owner_id)
        m = access.membership(db, e.group_id, user.id) if e else None
        allowed = m is not None and m.role == "owner"
    if not allowed:
        raise forbidden()
    a.deleted_at = utcnow()


@router.post("/ocr/receipt", tags=["ocr"])
async def ocr_receipt(
    file: UploadFile = File(...), keep: bool = Form(False), keep_q: bool = Query(False, alias="keep"),
    user: User = Depends(current_user), db: Session = Depends(get_db, scope="function"),
):
    """Best-effort receipt reading. `keep=true` (form field or query) also stores the file as an attachment."""
    from fastapi.concurrency import run_in_threadpool

    pf = process_upload(await read_upload(file), get_settings().upload_max_mb * 1024 * 1024)
    result = await run_in_threadpool(get_ocr().read, pf.data, pf.mime)
    result["suggestion"] = resolve(db, user, result.get("merchant"), None, None)
    result["attachment"] = None
    if keep or keep_q:
        a = store_upload(db, pf, file.filename, user)
        result["attachment"] = attachment_out(a, UserCache(db)).model_dump(mode="json")
    return result
