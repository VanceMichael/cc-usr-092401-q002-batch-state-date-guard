from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import Pond, Batch
from ..schemas import PondCreate, PondUpdate, PondResponse

router = APIRouter(
    prefix="/api/ponds",
    tags=["塘口管理"]
)

VALID_POND_STATUS = {"active", "inactive"}


def _validate_pond_fields(status, active_from, active_to, capacity):
    if status is not None and status not in VALID_POND_STATUS:
        raise HTTPException(status_code=422, detail=f"未知塘口状态: {status}")
    if active_from and active_to and active_to < active_from:
        raise HTTPException(status_code=422, detail="塘口有效期止不得早于有效期起")
    if capacity is not None and capacity < 1:
        raise HTTPException(status_code=422, detail="塘口同时段容量至少为 1")


def _assert_no_active_batches(db: Session, pond: Pond):
    rows = db.query(Batch).filter(
        Batch.pond_id == pond.id, Batch.status != "closed"
    ).all()
    if rows:
        names = "、".join(b.batch_number for b in rows[:5])
        raise HTTPException(
            status_code=422,
            detail=f"该塘口仍有在养/已收获批次({names}),请先跨塘转移后再停用",
        )


@router.post("/", response_model=PondResponse)
def create_pond(pond: PondCreate, db: Session = Depends(get_db)):
    db_pond = db.query(Pond).filter(Pond.name == pond.name).first()
    if db_pond:
        raise HTTPException(status_code=400, detail="塘口名称已存在")
    _validate_pond_fields(pond.status, pond.active_from, pond.active_to, pond.capacity)
    new_pond = Pond(**pond.model_dump())
    db.add(new_pond)
    db.commit()
    db.refresh(new_pond)
    return new_pond


@router.get("/", response_model=List[PondResponse])
def get_ponds(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    ponds = db.query(Pond).offset(skip).limit(limit).all()
    return ponds


@router.get("/{pond_id}/", response_model=PondResponse)
def get_pond(pond_id: int, db: Session = Depends(get_db)):
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise HTTPException(status_code=404, detail="塘口不存在")
    return pond


@router.put("/{pond_id}/", response_model=PondResponse)
def update_pond(pond_id: int, pond: PondUpdate, db: Session = Depends(get_db)):
    db_pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not db_pond:
        raise HTTPException(status_code=404, detail="塘口不存在")

    update_data = pond.model_dump(exclude_unset=True)

    merged_status = update_data.get("status", db_pond.status)
    merged_from = update_data.get("active_from", db_pond.active_from)
    merged_to = update_data.get("active_to", db_pond.active_to)
    merged_capacity = update_data.get("capacity", db_pond.capacity)
    _validate_pond_fields(merged_status, merged_from, merged_to, merged_capacity)

    # 停用或把有效期收缩到无法覆盖现有批次时,要求先转移在养批次
    becomes_inactive = merged_status == "inactive" and db_pond.status != "inactive"
    if becomes_inactive:
        _assert_no_active_batches(db, db_pond)
    elif "active_from" in update_data or "active_to" in update_data:
        for b in db.query(Batch).filter(Batch.pond_id == db_pond.id,
                                       Batch.status != "closed").all():
            interval_end = b.actual_harvest_date or b.estimated_harvest_date
            if (merged_from and b.stocking_date < merged_from) or \
                    (merged_to and interval_end and interval_end > merged_to):
                raise HTTPException(
                    status_code=422,
                    detail=f"新有效期无法覆盖在养批次 {b.batch_number} 的养殖时段,请先转移或调整有效期",
                )
    if "capacity" in update_data:
        overlap_count = db.query(Batch).filter(
            Batch.pond_id == db_pond.id, Batch.status != "closed"
        ).count()
        if overlap_count > max(merged_capacity, 1):
            raise HTTPException(
                status_code=422,
                detail=f"当前在养批次 {overlap_count} 个,超过新容量 {merged_capacity},请先转移",
            )

    for key, value in update_data.items():
        setattr(db_pond, key, value)

    db.commit()
    db.refresh(db_pond)
    return db_pond


@router.delete("/{pond_id}/")
def delete_pond(pond_id: int, db: Session = Depends(get_db)):
    db_pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not db_pond:
        raise HTTPException(status_code=404, detail="塘口不存在")

    in_use = db.query(Batch).filter(Batch.pond_id == pond_id).count()
    if in_use:
        raise HTTPException(status_code=422, detail="该塘口存在批次记录,不能删除(可改为停用)")

    db.delete(db_pond)
    db.commit()
    return {"message": "塘口删除成功"}
