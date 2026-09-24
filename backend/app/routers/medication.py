from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import MedicationRecord, Batch
from ..schemas import MedicationRecordCreate, MedicationRecordUpdate, MedicationRecordResponse
from ..services.batch_service import assert_batch_accepts_activity

router = APIRouter(
    prefix="/api/medication-records",
    tags=["用药记录"]
)

def _get_batch_or_404(db: Session, batch_id: int) -> Batch:
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch

@router.post("/", response_model=MedicationRecordResponse)
def create_medication_record(record: MedicationRecordCreate, db: Session = Depends(get_db)):
    db_batch = _get_batch_or_404(db, record.batch_id)
    assert_batch_accepts_activity(db, db_batch, record_date=record.medication_date)

    new_record = MedicationRecord(**record.model_dump())
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    return new_record

@router.get("/", response_model=List[MedicationRecordResponse])
def get_medication_records(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(MedicationRecord)
    if batch_id:
        query = query.filter(MedicationRecord.batch_id == batch_id)
    records = query.offset(skip).limit(limit).all()
    return records

@router.get("/{record_id}/", response_model=MedicationRecordResponse)
def get_medication_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(MedicationRecord).filter(MedicationRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="用药记录不存在")
    return record

@router.put("/{record_id}/", response_model=MedicationRecordResponse)
def update_medication_record(record_id: int, record: MedicationRecordUpdate, db: Session = Depends(get_db)):
    db_record = db.query(MedicationRecord).filter(MedicationRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="用药记录不存在")

    update_data = record.model_dump(exclude_unset=True)
    target_batch_id = update_data.get("batch_id", db_record.batch_id)
    target_date = update_data.get("medication_date", db_record.medication_date)
    if "batch_id" in update_data or "medication_date" in update_data:
        target_batch = _get_batch_or_404(db, target_batch_id)
        assert_batch_accepts_activity(db, target_batch, record_date=target_date)

    for key, value in update_data.items():
        setattr(db_record, key, value)

    db.commit()
    db.refresh(db_record)
    return db_record

@router.delete("/{record_id}/")
def delete_medication_record(record_id: int, db: Session = Depends(get_db)):
    db_record = db.query(MedicationRecord).filter(MedicationRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="用药记录不存在")

    db.delete(db_record)
    db.commit()
    return {"message": "用药记录删除成功"}
