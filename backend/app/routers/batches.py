import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import Batch, Pond, BatchVersion
from ..schemas import (
    BatchCreate, BatchUpdate, BatchResponse, BatchVersionResponse,
    BatchAnomaly, NormalizeReport, NormalizeItem,
)
from ..services import batch_service as svc

router = APIRouter(
    prefix="/api/batches",
    tags=["批次管理"]
)


def _history(db: Session, batch: Batch) -> List[BatchVersionResponse]:
    rows = db.query(BatchVersion).filter(
        BatchVersion.batch_id == batch.id
    ).order_by(BatchVersion.version.desc()).all()
    result = []
    for row in rows:
        result.append(BatchVersionResponse(
            id=row.id, batch_id=row.batch_id, version=row.version, action=row.action,
            from_status=row.from_status, to_status=row.to_status,
            from_pond_id=row.from_pond_id, to_pond_id=row.to_pond_id,
            reason=row.reason, operator=row.operator,
            snapshot=json.loads(row.snapshot),
            created_at=row.created_at,
        ))
    return result


# ---------------------------------------------------------------------------
# 集合级路由(必须声明在 /{batch_id}/ 之前,避免路径参数截获)
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[BatchResponse])
def get_batches(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return db.query(Batch).offset(skip).limit(limit).all()


@router.post("/", response_model=BatchResponse)
def create_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    data = batch.model_dump(exclude={"operator", "reason"})
    return svc.create_batch(db, data, operator=batch.operator, reason=batch.reason)


@router.get("/anomalies/", response_model=List[BatchAnomaly])
def list_anomalies(db: Session = Depends(get_db)):
    """识别旧的矛盾记录:日期倒挂、状态/日期不符、归属停用塘口等。"""
    found = svc.find_anomalies(db)
    return [
        BatchAnomaly(batch_id=b.id, batch_number=b.batch_number,
                     issues=issues, fixable=True)
        for b, issues in found
    ]


@router.post("/normalize/", response_model=NormalizeReport)
def normalize_all(payload: dict = None, db: Session = Depends(get_db)):
    """扫描并安全归一全部矛盾记录。返回逐项报告,无法自动修复的列入 remaining。"""
    found = svc.find_anomalies(db)
    operator = (payload or {}).get("operator")
    items: List[NormalizeItem] = []
    normalized = 0
    for batch, issues in found:
        fixed, remaining = svc.normalize_batch(db, batch, issues, operator=operator)
        if fixed:
            normalized += 1
        items.append(NormalizeItem(
            batch_id=batch.id, batch_number=batch.batch_number,
            fixed=fixed, remaining=remaining,
        ))
    db.commit()
    return NormalizeReport(
        scanned=db.query(Batch).count(),
        anomalies=len(found),
        normalized=normalized,
        items=items,
    )


@router.get("/by-number/{batch_number}/", response_model=BatchResponse)
def get_batch_by_number(batch_number: str, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.batch_number == batch_number).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


# ---------------------------------------------------------------------------
# 单项路由
# ---------------------------------------------------------------------------

@router.get("/{batch_id}/", response_model=BatchResponse)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


@router.get("/{batch_id}/history/", response_model=List[BatchVersionResponse])
def get_batch_history(batch_id: int, db: Session = Depends(get_db)):
    """审计版本历史:回退原因、操作者、每次变更后的完整快照。"""
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return _history(db, batch)


@router.put("/{batch_id}/", response_model=BatchResponse)
def update_batch(batch_id: int, batch: BatchUpdate, db: Session = Depends(get_db)):
    db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not db_batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    changes = batch.model_dump(
        exclude_unset=True,
        exclude={"expected_version", "reason", "operator"},
    )
    return svc.apply_batch_update(
        db, db_batch, changes,
        expected_version=batch.expected_version,
        reason=batch.reason, operator=batch.operator,
    )


@router.post("/{batch_id}/normalize/", response_model=NormalizeItem)
def normalize_one(batch_id: int, payload: dict = None, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    issues = []
    for b, iss in svc.find_anomalies(db):
        if b.id == batch_id:
            issues = iss
            break
    operator = (payload or {}).get("operator")
    fixed, remaining = svc.normalize_batch(db, batch, issues, operator=operator)
    db.commit()
    db.refresh(batch)
    return NormalizeItem(batch_id=batch.id, batch_number=batch.batch_number,
                         fixed=fixed, remaining=remaining)


@router.delete("/{batch_id}/")
def delete_batch(batch_id: int, db: Session = Depends(get_db)):
    db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not db_batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    # 已关闭批次保留为历史档案;其余状态允许删除但仍受外键约束
    db.delete(db_batch)
    db.commit()
    return {"message": "批次删除成功"}
