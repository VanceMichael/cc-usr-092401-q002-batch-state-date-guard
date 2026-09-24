"""批次状态机核心服务。

把批次状态、投苗日期、预计/实际收获日期、塘口归属收敛为一个一致的状态机:

    active(养殖中) ──收获──▶ harvested(已收获) ──关闭──▶ closed(已出塘/关闭)
       ▲   revert_harvest(填原因)        reopen(填原因)
       └──────────────── harvested ────────┘  closed

任何字段修改都必须经过 apply_batch_update,经状态/日期/塘口三重校验后整体提交,
并生成不可变的 BatchVersion 审计快照;expected_version 不匹配时拒绝写入(乐观锁)。
"""
import json
from datetime import date, datetime
from typing import Optional, List, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import and_

from ..models import (
    Batch, Pond, BatchVersion,
    FeedingRecord, WaterQualityRecord, MedicationRecord,
)

ACTIVE = "active"
HARVESTED = "harvested"
CLOSED = "closed"
VALID_STATUSES = {ACTIVE, HARVESTED, CLOSED}

# 允许的状态转移:键=目标状态,值=允许的来源状态集合
ALLOWED_TRANSITIONS = {
    ACTIVE: {ACTIVE, HARVESTED},       # harvested -> active 属于合法回退,需原因
    HARVESTED: {ACTIVE, HARVESTED, CLOSED},  # closed -> harvested 回退需原因
    CLOSED: {HARVESTED, CLOSED},       # 关闭只能来自已收获
}

# 视为"回退/重开"的转移,必须填写原因
REVERSAL_TRANSITIONS = {(HARVESTED, ACTIVE), (CLOSED, HARVESTED)}

CONFLICT = 409
UNPROCESSABLE = 422


class VersionConflict(HTTPException):
    def __init__(self, current_version: int, detail: str = ""):
        super().__init__(
            status_code=CONFLICT,
            detail={
                "code": "VERSION_CONFLICT",
                "message": "批次已被其他操作者修改,请刷新后基于最新版本重新提交",
                "current_version": current_version,
                "detail": detail,
            },
        )


def _snapshot(batch: Batch) -> dict:
    return {
        "batch_number": batch.batch_number,
        "pond_id": batch.pond_id,
        "species": batch.species,
        "stocking_date": batch.stocking_date.isoformat() if batch.stocking_date else None,
        "estimated_harvest_date": batch.estimated_harvest_date.isoformat() if batch.estimated_harvest_date else None,
        "actual_harvest_date": batch.actual_harvest_date.isoformat() if batch.actual_harvest_date else None,
        "status": batch.status,
        "version": batch.version,
    }


def write_version(db: Session, batch: Batch, action: str, *,
                  from_status: Optional[str], to_status: Optional[str],
                  from_pond_id: Optional[int], to_pond_id: Optional[int],
                  reason: Optional[str], operator: Optional[str]) -> BatchVersion:
    version = BatchVersion(
        batch_id=batch.id,
        version=batch.version,
        action=action,
        from_status=from_status,
        to_status=to_status,
        from_pond_id=from_pond_id,
        to_pond_id=to_pond_id,
        reason=reason,
        operator=operator,
        snapshot=json.dumps(_snapshot(batch), ensure_ascii=False),
    )
    db.add(version)
    return version


def pond_is_valid_on(pond: Pond, day: date) -> bool:
    """塘口在给定日期是否处于有效期内(空边界表示不限)。"""
    if pond.status != ACTIVE:
        return False
    if pond.active_from and day < pond.active_from:
        return False
    if pond.active_to and day > pond.active_to:
        return False
    return True


def pond_covers_interval(pond: Pond, start: date, end: Optional[date]) -> bool:
    """塘口当前是否可用且有效期覆盖整个养殖时段。"""
    if pond.status != ACTIVE:
        return False
    if pond.active_from and start < pond.active_from:
        return False
    if end and pond.active_to and end > pond.active_to:
        return False
    return True


def pond_window_covers(pond: Pond, start: date, end: Optional[date]) -> bool:
    """只校验有效期日期边界(允许塘口当前已停用),用于转出塘的历史时段校验。"""
    if pond.active_from and start < pond.active_from:
        return False
    if end and pond.active_to and end > pond.active_to:
        return False
    return True


def _overlaps(start_a: date, end_a: Optional[date], start_b: date, end_b: Optional[date]) -> bool:
    a_end = end_a or date(9999, 12, 31)
    b_end = end_b or date(9999, 12, 31)
    return start_a <= b_end and start_b <= a_end


def check_pond_capacity(db: Session, pond: Pond, *,
                        interval_start: date, interval_end: Optional[date],
                        ignore_batch_id: Optional[int] = None) -> int:
    """校验塘口在指定养殖时段的在养批次数量不超过 capacity,返回当前占用数。"""
    capacity = pond.capacity if pond.capacity is not None else 1
    others = db.query(Batch).filter(
        Batch.pond_id == pond.id,
        Batch.status != CLOSED,
    )
    if ignore_batch_id is not None:
        others = others.filter(Batch.id != ignore_batch_id)

    overlapping = 0
    for b in others.all():
        if _overlaps(interval_start, interval_end, b.stocking_date, b.actual_harvest_date):
            overlapping += 1

    if overlapping >= max(capacity, 1):
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                f"塘口「{pond.name}」在该时段容量为 {capacity} 个批次,"
                f"已有 {overlapping} 个在养批次时段重叠"
            ),
        )
    return overlapping


def validate_dates(stocking_date: date,
                   estimated_harvest_date: Optional[date],
                   actual_harvest_date: Optional[date]) -> None:
    if estimated_harvest_date and estimated_harvest_date < stocking_date:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="预计收获日期不得早于投苗日期",
        )
    if actual_harvest_date and actual_harvest_date < stocking_date:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="实际收获日期不得早于投苗日期",
        )
    if estimated_harvest_date and actual_harvest_date and \
            actual_harvest_date < estimated_harvest_date:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="实际收获日期不得早于预计收获日期",
        )


def validate_status_transition(old_status: str, new_status: str,
                               reason: Optional[str]) -> bool:
    """返回是否为回退转移;非法转移直接 422,回退缺原因直接 422。"""
    if new_status not in VALID_STATUSES:
        raise HTTPException(status_code=UNPROCESSABLE, detail=f"未知批次状态: {new_status}")
    if old_status not in ALLOWED_TRANSITIONS.get(new_status, set()):
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=f"不允许将批次从「{old_status}」变更为「{new_status}」",
        )
    is_reversal = (old_status, new_status) in REVERSAL_TRANSITIONS
    if is_reversal and not (reason and reason.strip()):
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="将已收获/已关闭批次回退为养殖状态必须填写回退原因",
        )
    return is_reversal


# 已关闭批次中,以下核心字段不再允许直接编辑(只能先按状态机重开并说明原因)
CLOSED_LOCKED_FIELDS = {
    "pond_id", "stocking_date", "estimated_harvest_date",
    "actual_harvest_date", "batch_number", "species",
}


def create_batch(db: Session, data: dict, operator: Optional[str] = None,
                 reason: Optional[str] = None) -> Batch:
    pond = db.query(Pond).filter(Pond.id == data["pond_id"]).first()
    if not pond:
        raise HTTPException(status_code=404, detail="塘口不存在")

    status = data.get("status") or ACTIVE
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=UNPROCESSABLE, detail=f"未知批次状态: {status}")

    stocking_date = data["stocking_date"]
    est = data.get("estimated_harvest_date")
    act = data.get("actual_harvest_date")
    validate_dates(stocking_date, est, act)

    if db.query(Batch).filter(Batch.batch_number == data["batch_number"]).first():
        raise HTTPException(status_code=400, detail="批次号已存在")

    interval_end = act or est
    if not pond_covers_interval(pond, stocking_date, interval_end):
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=f"塘口「{pond.name}」已停用或有效期不能覆盖该批次养殖时段",
        )
    check_pond_capacity(db, pond, interval_start=stocking_date, interval_end=interval_end)

    # 新建批次的状态/日期必须自洽
    if act is not None and status == ACTIVE:
        status = HARVESTED
    if status == HARVESTED and act is None:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="置为已收获时必须填写实际收获日期",
        )
    if status == CLOSED and act is None:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="关闭状态的批次必须填写实际收获日期",
        )

    batch = Batch(
        batch_number=data["batch_number"],
        pond_id=data["pond_id"],
        species=data["species"],
        stocking_date=stocking_date,
        estimated_harvest_date=est,
        actual_harvest_date=act,
        status=status,
        version=1,
        data_quality="ok",
    )
    db.add(batch)
    db.flush()
    write_version(db, batch, "create", from_status=None, to_status=status,
                  from_pond_id=None, to_pond_id=pond.id,
                  reason=reason, operator=operator)
    db.commit()
    db.refresh(batch)
    return batch


def apply_batch_update(db: Session, batch: Batch, changes: dict,
                       expected_version: int, *,
                       reason: Optional[str] = None,
                       operator: Optional[str] = None) -> Batch:
    """状态机化的批次更新。changes 只含要修改的业务字段。"""
    # 1) 乐观锁:后提交者拿到可识别的版本冲突,而不是静默覆盖
    if expected_version != batch.version:
        raise VersionConflict(batch.version)

    old_status = batch.status
    old_pond_id = batch.pond_id

    new_status = changes.get("status", old_status)
    new_pond_id = changes.get("pond_id", old_pond_id)
    new_stocking = changes.get("stocking_date", batch.stocking_date)
    new_est = changes.get("estimated_harvest_date", batch.estimated_harvest_date)
    new_act = changes.get("actual_harvest_date", batch.actual_harvest_date)

    # 2) 已关闭批次的核心字段锁定,必须先重开(带原因)
    if old_status == CLOSED and new_status == CLOSED:
        touched_locked = {k for k in changes if k in CLOSED_LOCKED_FIELDS}
        if touched_locked:
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail="批次已关闭并出塘,核心信息不可直接修改;如需调整请先重开批次并填写原因",
            )

    # 3) 状态转移合法性 + 回退原因
    is_reversal = validate_status_transition(old_status, new_status, reason)

    # 4) 日期一致性
    validate_dates(new_stocking, new_est, new_act)

    # 5) 状态与日期自洽:有实际收获日期则至少是 harvested;
    #    回到养殖中必须清空实际收获日期
    if new_status == ACTIVE and new_act is not None:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="养殖中的批次不能保留实际收获日期,回退为养殖中时须清空实际收获日期",
        )
    if new_status == HARVESTED and new_act is None:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="置为已收获时必须填写实际收获日期",
        )
    if new_status == CLOSED and new_act is None:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="关闭批次前必须先登记实际收获日期",
        )

    # 6) 塘口归属:目标塘存在、有效,两个塘(转出/转入)的有效期与容量都要校验
    target_pond: Optional[Pond] = None
    transferring = new_pond_id != old_pond_id
    if transferring:
        target_pond = db.query(Pond).filter(Pond.id == new_pond_id).first()
        if not target_pond:
            raise HTTPException(status_code=404, detail="目标塘口不存在")
        source_pond = db.query(Pond).filter(Pond.id == old_pond_id).first()
        if not source_pond:
            raise HTTPException(
                status_code=UNPROCESSABLE, detail="原塘口记录缺失,无法执行跨塘转移"
            )

        interval_end = new_act or new_est
        # 转入塘:有效期覆盖整个养殖时段,且该时段与其他在养批次重叠不超容量
        if not pond_covers_interval(target_pond, new_stocking, interval_end):
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail=(
                    f"目标塘口「{target_pond.name}」已停用或有效期不能覆盖 "
                    f"{new_stocking}"
                    + (f" 至 {interval_end} 的养殖时段" if interval_end else " 起的养殖时段")
                ),
            )
        check_pond_capacity(
            db, target_pond,
            interval_start=new_stocking, interval_end=interval_end,
            ignore_batch_id=batch.id,
        )
        # 转出塘:有效期日期窗口必须覆盖该批次已发生的养殖时段,
        # 防止把历史归属改写到一个从未覆盖该时段的塘(塘口当前已停用不阻断转移)
        if not pond_window_covers(source_pond, new_stocking, new_act):
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail=f"原塘口「{source_pond.name}」的有效期未覆盖该批次养殖时段,跨塘转移不成立",
            )
    elif not db.query(Pond).filter(Pond.id == new_pond_id).first():
        raise HTTPException(status_code=404, detail="塘口不存在")

    # 调整塘口或养殖时段时,重新核算目标塘(转入塘或当前塘)的同时段容量
    interval_changed = transferring or any(
        k in changes for k in ("stocking_date", "estimated_harvest_date", "actual_harvest_date")
    )
    if interval_changed and new_status != CLOSED:
        effective_pond = target_pond or db.query(Pond).filter(Pond.id == new_pond_id).first()
        if effective_pond is not None and not transferring:
            if not pond_covers_interval(effective_pond, new_stocking, new_act or new_est):
                raise HTTPException(
                    status_code=UNPROCESSABLE,
                    detail=f"塘口「{effective_pond.name}」的有效期不能覆盖调整后的养殖时段",
                )
            check_pond_capacity(
                db, effective_pond,
                interval_start=new_stocking, interval_end=new_act or new_est,
                ignore_batch_id=batch.id,
            )

    # 所有校验通过后再整体写入,避免半成功状态
    for key, value in changes.items():
        setattr(batch, key, value)
    batch.version += 1

    # 归一标记在人工修改后视为数据已恢复一致
    if batch.data_quality == "sanitized":
        batch.data_quality = "ok"

    action = _classify_action(old_status, new_status, old_pond_id, new_pond_id,
                              is_reversal, changes)
    write_version(
        db, batch, action,
        from_status=old_status, to_status=new_status,
        from_pond_id=old_pond_id, to_pond_id=new_pond_id,
        reason=(reason if (is_reversal or action in ("reopen", "revert_harvest")) else reason),
        operator=operator,
    )
    db.commit()
    db.refresh(batch)
    return batch


def _classify_action(old_status, new_status, old_pond, new_pond, is_reversal, changes):
    transferred = new_pond != old_pond
    if (old_status, new_status) == (CLOSED, HARVESTED):
        return "reopen"
    if (old_status, new_status) == (HARVESTED, ACTIVE):
        return "revert_harvest"
    if (old_status, new_status) == (ACTIVE, HARVESTED):
        return "harvest"
    if new_status == CLOSED and old_status != CLOSED:
        return "close"
    if transferred:
        return "transfer"
    return "update"


def assert_batch_accepts_activity(db: Session, batch: Batch, *,
                                  record_date: Optional[date] = None) -> None:
    """关闭批次不得继续新增投喂、用药或水质记录。"""
    if batch.status == CLOSED:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=f"批次「{batch.batch_number}」已关闭并出塘,不能再新增投喂、用药或水质记录",
        )
    if record_date and batch.actual_harvest_date and record_date > batch.actual_harvest_date:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=f"记录日期 {record_date} 晚于批次实际收获日期 {batch.actual_harvest_date}",
        )


def canonical_state(batch: Batch) -> dict:
    """所有页面共用的最终状态口径:周期天数永不为负。"""
    harvest = batch.actual_harvest_date
    days = None
    if harvest:
        days = max((harvest - batch.stocking_date).days, 0)
    return {
        "status": batch.status,
        "version": batch.version,
        "data_quality": batch.data_quality,
        "stocking_date": batch.stocking_date,
        "estimated_harvest_date": batch.estimated_harvest_date,
        "actual_harvest_date": harvest,
        "days_cultured": days,
    }


def find_anomalies(db: Session) -> List[Tuple[Batch, List[str]]]:
    """识别旧的矛盾记录,返回 (批次, 问题列表)。"""
    results: List[Tuple[Batch, List[str]]] = []
    batches = db.query(Batch).order_by(Batch.id).all()
    pond_ids = {b.pond_id for b in batches}
    ponds = {p.id: p for p in db.query(Pond).filter(Pond.id.in_(pond_ids)).all()}

    for b in batches:
        issues: List[str] = []
        pond = ponds.get(b.pond_id)
        if pond is None:
            issues.append("塘口不存在(悬空归属)")
        elif pond.status != ACTIVE:
            if b.status != CLOSED:
                issues.append("在养批次挂在已停用塘口")
        elif not pond_is_valid_on(pond, b.stocking_date):
            issues.append(f"塘口在投苗日期 {b.stocking_date} 不在有效期内")

        if b.estimated_harvest_date and b.estimated_harvest_date < b.stocking_date:
            issues.append("预计收获日期早于投苗日期")
        if b.actual_harvest_date and b.actual_harvest_date < b.stocking_date:
            issues.append("实际收获日期早于投苗日期(周期为负)")
        if b.actual_harvest_date and b.estimated_harvest_date and \
                b.actual_harvest_date < b.estimated_harvest_date:
            issues.append("实际收获日期早于预计收获日期")
        if b.actual_harvest_date is None and b.status in (HARVESTED, CLOSED):
            issues.append(f"状态为{b.status}但缺少实际收获日期")
        if b.actual_harvest_date is not None and b.status == ACTIVE:
            issues.append("状态为养殖中却存在实际收获日期")
        if b.status not in VALID_STATUSES:
            issues.append(f"未知状态值:{b.status}")
        if issues:
            results.append((b, issues))
    return results


def normalize_batch(db: Session, batch: Batch, issues: List[str], *,
                    operator: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """对单条矛盾记录做安全归一。

    原则:不删除业务数据、不凭空捏造收获日期;通过清空倒挂日期、回退状态、
    改挂到有效塘口等保守手段恢复一致性。返回 (已修复项, 仍需人工处理项)。
    """
    fixed: List[str] = []
    remaining: List[str] = []
    from_status = batch.status
    from_pond_id = batch.pond_id

    # 日期倒挂:清空倒挂的收获日期(比强行改投苗日更安全,保留投苗事实)
    if batch.actual_harvest_date and batch.actual_harvest_date < batch.stocking_date:
        batch.actual_harvest_date = None
        fixed.append("清空早于投苗日期的实际收获日期")
    if batch.estimated_harvest_date and batch.estimated_harvest_date < batch.stocking_date:
        batch.estimated_harvest_date = None
        fixed.append("清空早于投苗日期的预计收获日期")
    if batch.actual_harvest_date and batch.estimated_harvest_date and \
            batch.actual_harvest_date < batch.estimated_harvest_date:
        batch.estimated_harvest_date = None
        fixed.append("清空晚于实际收获日期的预计收获日期")

    # 状态与日期对齐
    if batch.status not in VALID_STATUSES:
        batch.status = ACTIVE
        fixed.append(f"未知状态回置为养殖中")
    if batch.actual_harvest_date is None and batch.status in (HARVESTED, CLOSED):
        batch.status = ACTIVE
        fixed.append("缺少实际收获日期,状态回置为养殖中")
    if batch.actual_harvest_date is not None and batch.status == ACTIVE:
        batch.status = HARVESTED
        fixed.append("存在实际收获日期,状态对齐为已收获")

    # 塘口归属:停用/失效/悬空则改挂到任一在投苗日有效的塘口
    pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
    need_rehome = (pond is None) or (
        batch.status != CLOSED and not pond_is_valid_on(pond, batch.stocking_date)
    )
    if need_rehome:
        candidate = db.query(Pond).filter(Pond.status == ACTIVE).order_by(Pond.id).first()
        if candidate is None:
            remaining.append("没有可用的有效塘口,需要先启用塘口后重新归属")
        else:
            batch.pond_id = candidate.id
            fixed.append(f"批次改挂到有效塘口「{candidate.name}」")

    if fixed:
        batch.data_quality = "sanitized"
        batch.version += 1
        write_version(
            db, batch, "normalize",
            from_status=from_status, to_status=batch.status,
            from_pond_id=from_pond_id, to_pond_id=batch.pond_id,
            reason="系统安全归一:" + ";".join(fixed),
            operator=operator or "system",
        )
    return fixed, remaining
