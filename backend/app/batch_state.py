"""批次状态机与一致性校验。

把批次状态、投苗日期、预计/实际收获日期、塘口归属收敛为一套一致的规则：

- 状态机: active(养殖中) -> harvested(已出塘) -> closed(已关闭)。
  前进无需原因；回退（如 harvested -> active）必须给出原因并生成审计版本。
- 日期一致性: 预计/实际收获日期不得早于投苗日期；active 不得有实际收获日期；
  harvested 必须有实际收获日期。周期天数因此不可能为负。
- 跨塘转移: 目标塘必须处于启用状态且有效期覆盖批次养殖窗口，原塘与目标塘的
  有效期都要校验（原塘违例只记录不拦截，避免把批次困在已停用塘口）；
  目标塘同一时段在养批次数不得超过其容量。
- 乐观锁: 每次变更携带 version，不一致时返回 409 版本冲突，后提交者不会覆盖前者。
- 所有变更（含系统归一）写入 BatchRevision 审计版本。
"""
import json
from datetime import date
from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Batch, BatchRevision, Pond

VALID_STATUSES = ("active", "harvested", "closed")
STATUS_ORDER = {"active": 0, "harvested": 1, "closed": 2}
STATUS_LABELS = {"active": "养殖中", "harvested": "已出塘", "closed": "已关闭"}

# 关闭/已出塘批次禁止新增的日常记录类型（投喂、用药、水质）
RECORD_KIND_LABELS = {"feeding": "投喂", "medication": "用药", "water_quality": "水质"}


def _err(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def validate_status_value(status: str) -> None:
    if status not in VALID_STATUSES:
        raise _err(400, f"非法批次状态: {status}，允许值为 {list(VALID_STATUSES)}")


def validate_dates(
    stocking_date: date,
    estimated_harvest_date: Optional[date],
    actual_harvest_date: Optional[date],
    status: str,
) -> None:
    """校验合并后的最终状态，保证状态与日期互相一致、周期天数不为负。"""
    validate_status_value(status)
    if estimated_harvest_date and estimated_harvest_date < stocking_date:
        raise _err(400, "预计收获日期不能早于投苗日期")
    if actual_harvest_date and actual_harvest_date < stocking_date:
        raise _err(400, "实际收获日期不能早于投苗日期")
    if status == "harvested" and not actual_harvest_date:
        raise _err(400, "已出塘批次必须填写实际收获日期")
    if status == "active" and actual_harvest_date:
        raise _err(400, "养殖中的批次不能保留实际收获日期，请将状态改为已出塘或清空该日期")


def batch_window(batch: Batch) -> Tuple[date, Optional[date]]:
    """批次占用塘口的时间窗口：[投苗日期, 实际/预计收获日期]，结束为空表示开口。"""
    end = batch.actual_harvest_date or batch.estimated_harvest_date
    return batch.stocking_date, end


def _windows_overlap(a_start: date, a_end: Optional[date], b_start: date, b_end: Optional[date]) -> bool:
    if a_end and b_start > a_end:
        return False
    if b_end and a_start > b_end:
        return False
    return True


def check_pond_validity(pond: Pond, seg_start: date, seg_end: Optional[date], label: str) -> Optional[str]:
    """检查塘口有效期是否覆盖给定时段，返回违例描述（不抛异常，由调用方决定）。"""
    if pond.active_from and seg_start < pond.active_from:
        return f"{label}{pond.name}有效期自{pond.active_from}起，不覆盖{seg_start}"
    if pond.active_until:
        if seg_end is None or seg_end > pond.active_until:
            return f"{label}{pond.name}有效期至{pond.active_until}止，不覆盖该时段"
    return None


def check_pond_capacity(
    db: Session, pond: Pond, win_start: date, win_end: Optional[date], exclude_batch_id: Optional[int] = None
) -> None:
    """同一时段内在养批次数不得超过塘口容量。"""
    capacity = pond.capacity if pond.capacity is not None else 1
    query = db.query(Batch).filter(Batch.pond_id == pond.id, Batch.status == "active")
    if exclude_batch_id:
        query = query.filter(Batch.id != exclude_batch_id)
    overlapping = 0
    for other in query.all():
        o_start, o_end = batch_window(other)
        if _windows_overlap(win_start, win_end, o_start, o_end):
            overlapping += 1
    if overlapping >= capacity:
        raise _err(400, f"塘口{pond.name}在该时段已达容量上限({capacity}个在养批次)")


def validate_new_batch(db: Session, pond: Pond, stocking_date: date, end: Optional[date]) -> None:
    """新建批次的塘口校验：启用状态 + 有效期 + 同时段容量。"""
    if pond.status != "active":
        raise _err(400, f"塘口{pond.name}已停用，无法创建批次")
    violation = check_pond_validity(pond, stocking_date, end, "塘口")
    if violation:
        raise _err(400, violation)
    check_pond_capacity(db, pond, stocking_date, end)


def validate_transfer(
    db: Session,
    batch: Batch,
    target_pond: Pond,
    win_end: Optional[date],
    transfer_date: date,
) -> List[str]:
    """跨塘转移校验。返回需要写入审计的警告（原塘违例不拦截，避免困住批次）。"""
    warnings: List[str] = []
    if transfer_date < batch.stocking_date:
        raise _err(400, "转移日期不能早于投苗日期")
    # 预计收获日早于转移日期说明估计已过时，占用窗口按开口处理（更保守）
    if win_end and transfer_date > win_end:
        win_end = None
    if target_pond.status != "active":
        raise _err(400, f"目标塘口{target_pond.name}已停用，无法转入")

    source_pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
    if source_pond:
        # 原塘在 [投苗, 转移] 时段的有效性问题属于历史矛盾，记录进审计但不拦截转移
        violation = check_pond_validity(source_pond, batch.stocking_date, transfer_date, "原塘口")
        if violation:
            warnings.append(violation)
        if source_pond.status != "active":
            warnings.append(f"原塘口{source_pond.name}已停用")

    violation = check_pond_validity(target_pond, transfer_date, win_end, "目标塘口")
    if violation:
        raise _err(400, violation)
    check_pond_capacity(db, target_pond, transfer_date, win_end, exclude_batch_id=batch.id)
    return warnings


def snapshot_batch(batch: Batch) -> str:
    return json.dumps(
        {
            "batch_number": batch.batch_number,
            "pond_id": batch.pond_id,
            "species": batch.species,
            "stocking_date": batch.stocking_date.isoformat() if batch.stocking_date else None,
            "estimated_harvest_date": batch.estimated_harvest_date.isoformat()
            if batch.estimated_harvest_date
            else None,
            "actual_harvest_date": batch.actual_harvest_date.isoformat() if batch.actual_harvest_date else None,
            "status": batch.status,
            "version": batch.version,
        },
        ensure_ascii=False,
    )


def record_revision(
    db: Session, batch: Batch, change_type: str, reason: Optional[str], changed_fields: List[str]
) -> BatchRevision:
    revision = BatchRevision(
        batch_id=batch.id,
        revision=batch.version,
        change_type=change_type,
        reason=reason,
        changed_fields=json.dumps(changed_fields, ensure_ascii=False),
        snapshot=snapshot_batch(batch),
    )
    db.add(revision)
    return revision


def apply_batch_update(db: Session, batch: Batch, payload) -> Batch:
    """应用一次批次更新：乐观锁 -> 状态机 -> 日期一致性 -> 跨塘转移 -> 审计版本。"""
    data = payload.dict(exclude_unset=True)
    reason = (data.pop("reason", None) or "").strip() or None
    transfer_date = data.pop("transfer_date", None) or date.today()
    version = data.pop("version", None)

    # 1. 乐观锁：后提交者拿到可识别的 409，而不是覆盖前者
    if version is None:
        raise _err(400, "缺少版本号(version)，请刷新后重试")
    if version != batch.version:
        raise _err(
            409,
            f"版本冲突：该批次已被他人修改（当前版本{batch.version}，您的版本{version}），请刷新后重试",
        )

    # 2. 计算合并后的最终状态
    old_status = batch.status
    new_status = data.get("status", old_status)
    validate_status_value(new_status)

    is_rollback = STATUS_ORDER[new_status] < STATUS_ORDER[old_status]
    if is_rollback and not reason:
        raise _err(
            400,
            f"从{STATUS_LABELS[old_status]}回退到{STATUS_LABELS[new_status]}必须填写原因(reason)",
        )

    # 回退到养殖中时，实际收获日期不再成立，随状态机一并清空（旧值留在审计快照中）
    if new_status == "active" and old_status != "active" and "actual_harvest_date" not in data:
        data["actual_harvest_date"] = None

    merged = {
        "stocking_date": data.get("stocking_date", batch.stocking_date),
        "estimated_harvest_date": data.get("estimated_harvest_date", batch.estimated_harvest_date),
        "actual_harvest_date": data.get("actual_harvest_date", batch.actual_harvest_date),
    }
    validate_dates(merged["stocking_date"], merged["estimated_harvest_date"], merged["actual_harvest_date"], new_status)

    # 3. 跨塘转移：仅允许养殖中的批次转移，校验两个塘的有效期与同时段容量
    change_type = "rollback" if is_rollback else "update"
    transfer_warnings: List[str] = []
    new_pond_id = data.get("pond_id", batch.pond_id)
    if new_pond_id != batch.pond_id:
        if old_status != "active":
            raise _err(400, "仅养殖中的批次可以跨塘转移")
        target_pond = db.query(Pond).filter(Pond.id == new_pond_id).first()
        if not target_pond:
            raise _err(400, "目标塘口不存在")
        win_end = merged["actual_harvest_date"] or merged["estimated_harvest_date"]
        transfer_warnings = validate_transfer(db, batch, target_pond, win_end, transfer_date)
        change_type = "transfer"

    # 4. 应用字段并记录审计版本
    changed_fields = []
    for key, value in data.items():
        if getattr(batch, key) != value:
            changed_fields.append(key)
            setattr(batch, key, value)

    batch.version += 1
    audit_reason = reason
    if transfer_warnings:
        note = "；".join(transfer_warnings)
        audit_reason = f"{reason}；{note}" if reason else note
    record_revision(db, batch, change_type, audit_reason, changed_fields)
    return batch


def ensure_batch_accepting_records(db: Session, batch_id: int, record_kind: str) -> Batch:
    """关闭/已出塘的批次不得继续新增投喂、用药或水质记录。"""
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise _err(404, "批次不存在")
    if batch.status != "active":
        label = RECORD_KIND_LABELS.get(record_kind, record_kind)
        raise _err(
            409,
            f"批次{batch.batch_number}{STATUS_LABELS.get(batch.status, batch.status)}，不能继续新增{label}记录",
        )
    return batch


def ensure_batch_not_closed(db: Session, batch_id: int, record_label: str) -> Batch:
    """已关闭批次完全冻结（成本、销售等也不得新增）；已出塘仍允许结算类记录。"""
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise _err(404, "批次不存在")
    if batch.status == "closed":
        raise _err(409, f"批次{batch.batch_number}已关闭，不能继续新增{record_label}记录")
    return batch


# ---------- 历史矛盾数据：识别与安全归一 ----------

def detect_issues(batch: Batch, pond: Optional[Pond]) -> List[dict]:
    """识别单个批次的矛盾项。auto_fixable=False 的项需要人工处理。"""
    issues: List[dict] = []

    def add(code: str, message: str, auto_fixable: bool = True):
        issues.append({"code": code, "message": message, "auto_fixable": auto_fixable})

    status_known = batch.status in VALID_STATUSES
    if not status_known:
        add("invalid_status", f"状态值非法: {batch.status}")

    if batch.actual_harvest_date and batch.actual_harvest_date < batch.stocking_date:
        add(
            "harvest_before_stocking",
            f"实际收获日期{batch.actual_harvest_date}早于投苗日期{batch.stocking_date}，周期天数为负",
        )
    if batch.estimated_harvest_date and batch.estimated_harvest_date < batch.stocking_date:
        add(
            "estimated_before_stocking",
            f"预计收获日期{batch.estimated_harvest_date}早于投苗日期{batch.stocking_date}",
        )

    if status_known:
        if batch.status == "harvested" and not batch.actual_harvest_date:
            add("harvested_without_harvest_date", "已出塘批次缺少实际收获日期")
        if batch.status == "active" and batch.actual_harvest_date:
            add("active_with_harvest_date", "养殖中的批次却填有实际收获日期")

    if batch.status == "active" and pond:
        if pond.status != "active":
            add("inactive_pond", f"批次所在塘口{pond.name}已停用", auto_fixable=False)
        else:
            _, win_end = batch_window(batch)
            violation = check_pond_validity(pond, batch.stocking_date, win_end, "塘口")
            if violation:
                add("pond_validity_expired", violation, auto_fixable=False)
    return issues


def normalize_batch(db: Session, batch: Batch) -> List[str]:
    """对矛盾批次做确定性、不编造数据的安全归一，返回执行的动作列表。

    原则：不可能的日期清空而不是篡改；状态按日期证据推导；塘口归属问题
    涉及经营决策，只报告不自动迁移。
    """
    actions: List[str] = []

    if batch.status not in VALID_STATUSES:
        batch.status = "harvested" if batch.actual_harvest_date else "active"
        actions.append(f"非法状态归一为{batch.status}")

    if batch.actual_harvest_date and batch.actual_harvest_date < batch.stocking_date:
        batch.actual_harvest_date = None
        actions.append("清空早于投苗日期的实际收获日期")
        if batch.status == "harvested":
            batch.status = "active"
            actions.append("状态回置为active（出塘证据已失效）")

    if batch.estimated_harvest_date and batch.estimated_harvest_date < batch.stocking_date:
        batch.estimated_harvest_date = None
        actions.append("清空早于投苗日期的预计收获日期")

    if batch.status == "harvested" and not batch.actual_harvest_date:
        batch.status = "active"
        actions.append("缺少实际收获日期，状态回置为active")
    elif batch.status == "active" and batch.actual_harvest_date:
        batch.status = "harvested"
        actions.append("存在实际收获日期，状态归一为harvested")

    if actions:
        batch.version += 1
        record_revision(db, batch, "normalize", "系统自动归一: " + "；".join(actions), [])
    return actions
