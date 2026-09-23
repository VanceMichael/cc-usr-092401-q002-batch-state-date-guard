from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import Batch, BatchRevision, Pond
from ..schemas import (
    BatchCreate, BatchUpdate, BatchResponse, BatchRevisionResponse,
    InconsistencyReport, NormalizeResult,
)
from ..batch_state import (
    apply_batch_update, detect_issues, normalize_batch, record_revision,
    validate_dates, validate_new_batch,
)

router = APIRouter(
    prefix="/api/batches",
    tags=["批次管理"]
)

@router.post("/", response_model=BatchResponse)
def create_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    db_pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
    if not db_pond:
        raise HTTPException(status_code=404, detail="塘口不存在")

    db_batch = db.query(Batch).filter(Batch.batch_number == batch.batch_number).first()
    if db_batch:
        raise HTTPException(status_code=400, detail="批次号已存在")

    status = batch.status or "active"
    validate_dates(
        batch.stocking_date,
        batch.estimated_harvest_date,
        batch.actual_harvest_date,
        status,
    )
    end = batch.actual_harvest_date or batch.estimated_harvest_date
    validate_new_batch(db, db_pond, batch.stocking_date, end)

    new_batch = Batch(**batch.dict())
    db.add(new_batch)
    db.flush()
    record_revision(db, new_batch, "create", None, [])
    db.commit()
    db.refresh(new_batch)
    return new_batch

@router.get("/", response_model=List[BatchResponse])
def get_batches(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    batches = db.query(Batch).offset(skip).limit(limit).all()
    return batches

@router.get("/inconsistencies/", response_model=InconsistencyReport)
def get_inconsistencies(db: Session = Depends(get_db)):
    """识别历史遗留的矛盾批次（负周期、状态与日期不符、挂在停用塘口等）。"""
    items = []
    for batch in db.query(Batch).all():
        pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
        issues = detect_issues(batch, pond)
        if issues:
            items.append({
                "batch_id": batch.id,
                "batch_number": batch.batch_number,
                "status": batch.status,
                "issues": issues,
            })
    return {"total": len(items), "items": items}

@router.post("/normalize/", response_model=NormalizeResult)
def normalize_batches(db: Session = Depends(get_db)):
    """安全归一矛盾批次：只修正确定性问题并写审计版本，塘口归属问题留待人工。"""
    normalized, skipped = [], []
    for batch in db.query(Batch).all():
        pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
        issues = detect_issues(batch, pond)
        if not issues:
            continue
        actions = normalize_batch(db, batch)
        manual = [i["message"] for i in issues if not i["auto_fixable"]]
        if actions:
            normalized.append({
                "batch_id": batch.id,
                "batch_number": batch.batch_number,
                "actions": actions,
            })
        if manual:
            skipped.append({
                "batch_id": batch.id,
                "batch_number": batch.batch_number,
                "reasons": manual,
            })
    db.commit()
    return {"normalized": normalized, "skipped": skipped}

@router.get("/{batch_id}/", response_model=BatchResponse)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch

@router.get("/{batch_id}/revisions/", response_model=List[BatchRevisionResponse])
def get_batch_revisions(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    revisions = db.query(BatchRevision).filter(
        BatchRevision.batch_id == batch_id
    ).order_by(BatchRevision.id.desc()).all()
    return revisions

@router.get("/by-number/{batch_number}/", response_model=BatchResponse)
def get_batch_by_number(batch_number: str, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.batch_number == batch_number).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch

@router.put("/{batch_id}/", response_model=BatchResponse)
def update_batch(batch_id: int, batch: BatchUpdate, db: Session = Depends(get_db)):
    db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not db_batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    apply_batch_update(db, db_batch, batch)
    db.commit()
    db.refresh(db_batch)
    return db_batch

@router.delete("/{batch_id}/")
def delete_batch(batch_id: int, db: Session = Depends(get_db)):
    db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not db_batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    db.query(BatchRevision).filter(BatchRevision.batch_id == batch_id).delete()
    db.delete(db_batch)
    db.commit()
    return {"message": "批次删除成功"}
