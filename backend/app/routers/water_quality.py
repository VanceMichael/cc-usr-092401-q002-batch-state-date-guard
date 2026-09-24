from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import WaterQualityRecord, Batch
from ..schemas import WaterQualityRecordCreate, WaterQualityRecordUpdate, WaterQualityRecordResponse
from ..services.batch_service import assert_batch_accepts_activity

router = APIRouter(
    prefix="/api/water-quality-records",
    tags=["水质监测"]
)

def _get_batch_or_404(db: Session, batch_id: int) -> Batch:
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch

@router.post("/", response_model=WaterQualityRecordResponse)
def create_water_quality_record(record: WaterQualityRecordCreate, db: Session = Depends(get_db)):
    db_batch = _get_batch_or_404(db, record.batch_id)
    assert_batch_accepts_activity(db, db_batch, record_date=record.record_date)

    new_record = WaterQualityRecord(**record.model_dump())
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    return new_record

@router.get("/", response_model=List[WaterQualityRecordResponse])
def get_water_quality_records(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(WaterQualityRecord)
    if batch_id:
        query = query.filter(WaterQualityRecord.batch_id == batch_id)
    records = query.offset(skip).limit(limit).all()
    return records

@router.get("/{record_id}/", response_model=WaterQualityRecordResponse)
def get_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")
    return record

@router.put("/{record_id}/", response_model=WaterQualityRecordResponse)
def update_water_quality_record(record_id: int, record: WaterQualityRecordUpdate, db: Session = Depends(get_db)):
    db_record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")

    update_data = record.model_dump(exclude_unset=True)
    target_batch_id = update_data.get("batch_id", db_record.batch_id)
    target_date = update_data.get("record_date", db_record.record_date)
    if "batch_id" in update_data or "record_date" in update_data:
        target_batch = _get_batch_or_404(db, target_batch_id)
        assert_batch_accepts_activity(db, target_batch, record_date=target_date)

    for key, value in update_data.items():
        setattr(db_record, key, value)

    db.commit()
    db.refresh(db_record)
    return db_record

@router.delete("/{record_id}/")
def delete_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    db_record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")

    db.delete(db_record)
    db.commit()
    return {"message": "水质监测记录删除成功"}
