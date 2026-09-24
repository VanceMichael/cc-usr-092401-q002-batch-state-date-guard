"""批次状态机、日期一致性、塘口归属、乐观锁与旧数据归一的集成测试。"""

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# 在导入应用前指定独立的临时 SQLite 库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402
from app import models  # noqa: E402


class StateMachineTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        # 每个测试用例清空数据(保留表结构)
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())

    def create_pond(self, name="塘1", capacity=1, status="active", **kw):
        payload = {"name": name, "area": 10, "water_depth": 1.5,
                   "status": status, "capacity": capacity, **kw}
        r = self.client.post("/api/ponds/", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def create_batch(self, pond_id, number="B001", **kw):
        payload = {"batch_number": number, "pond_id": pond_id, "species": "草鱼",
                   "stocking_date": "2026-03-01", **kw}
        r = self.client.post("/api/batches/", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def update_batch(self, batch_id, expected_version, **kw):
        return self.client.put(f"/api/batches/{batch_id}/",
                               json={"expected_version": expected_version, **kw})

    def harvest(self, batch, day="2026-08-10"):
        r = self.update_batch(batch["id"], batch["version"],
                              status="harvested", actual_harvest_date=day)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestStatusAndDateRules(StateMachineTestBase):
    def test_actual_harvest_before_stocking_rejected(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"])
        r = self.update_batch(batch["id"], batch["version"],
                              status="harvested", actual_harvest_date="2026-02-01")
        self.assertEqual(r.status_code, 422)
        self.assertIn("实际收获日期不得早于投苗日期", r.json()["detail"])

    def test_estimated_before_stocking_rejected(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"])
        r = self.update_batch(batch["id"], batch["version"],
                              estimated_harvest_date="2026-01-01")
        self.assertEqual(r.status_code, 422)

    def test_actual_before_estimated_rejected(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"], estimated_harvest_date="2026-09-01")
        r = self.update_batch(batch["id"], batch["version"],
                              status="harvested", actual_harvest_date="2026-08-01")
        self.assertEqual(r.status_code, 422)

    def test_cannot_close_without_harvest_date(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"])
        r = self.update_batch(batch["id"], batch["version"], status="closed")
        self.assertEqual(r.status_code, 422)
        # active 不能直接跳到 closed(必须先登记收获),两种提示均属合法拦截
        self.assertTrue(
            "实际收获日期" in r.json()["detail"] or "不允许" in r.json()["detail"]
        )

    def test_active_must_harvest_before_close(self):
        pond = self.create_pond()
        # 新建(养殖中)不能直接关闭
        batch = self.create_batch(pond["id"])
        r = self.update_batch(batch["id"], batch["version"], status="closed")
        self.assertEqual(r.status_code, 422)

        # 登记收获后再关闭,合法
        pond2 = self.create_pond("塘2")
        harvested = self.harvest(self.create_batch(pond2["id"], number="B002"))
        r = self.update_batch(harvested["id"], harvested["version"], status="closed")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "closed")

    def test_revert_harvested_requires_reason(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"])
        harvested = self.harvest(batch)

        # 缺原因被拒绝
        r = self.update_batch(harvested["id"], harvested["version"], status="active")
        self.assertEqual(r.status_code, 422)
        self.assertIn("回退原因", r.json()["detail"])

        # 回退必须清空实际收获日期,否则也被拒绝(即使给了原因)
        r = self.update_batch(harvested["id"], harvested["version"],
                              status="active", reason="录错了")
        self.assertEqual(r.status_code, 422)
        self.assertIn("清空实际收获日期", r.json()["detail"])

        r = self.update_batch(harvested["id"], harvested["version"],
                              status="active", reason="收获日期录入错误,重新养殖观察",
                              actual_harvest_date=None)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "active")
        self.assertIsNone(r.json()["actual_harvest_date"])

    def test_reopen_closed_requires_reason(self):
        pond = self.create_pond()
        batch = self.harvest(self.create_batch(pond["id"]))
        r = self.update_batch(batch["id"], batch["version"], status="closed")
        self.assertEqual(r.status_code, 200)
        closed = r.json()

        r = self.update_batch(closed["id"], closed["version"], status="harvested")
        self.assertEqual(r.status_code, 422)

        r = self.update_batch(closed["id"], closed["version"],
                              status="harvested", reason="买家退货,批次重新入塘")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "harvested")

    def test_closed_batch_core_fields_locked(self):
        pond = self.create_pond()
        harvested = self.harvest(self.create_batch(pond["id"]))
        r = self.update_batch(harvested["id"], harvested["version"], status="closed")
        closed = r.json()
        r = self.update_batch(closed["id"], closed["version"], species="鲫鱼")
        self.assertEqual(r.status_code, 422)
        self.assertIn("已关闭", r.json()["detail"])
        r = self.update_batch(closed["id"], closed["version"], stocking_date="2026-03-02")
        self.assertEqual(r.status_code, 422)


class TestClosedBatchBlocksRecords(StateMachineTestBase):
    def _close(self):
        pond = self.create_pond()
        harvested = self.harvest(self.create_batch(pond["id"]))
        r = self.update_batch(harvested["id"], harvested["version"], status="closed")
        return r.json()

    def test_feeding_blocked(self):
        batch = self._close()
        r = self.client.post("/api/feeding-records/", json={
            "batch_id": batch["id"], "feeding_date": "2026-09-01",
            "feed_type": "颗粒料", "feed_quantity": 5})
        self.assertEqual(r.status_code, 422)
        self.assertIn("已关闭", r.json()["detail"])

    def test_medication_blocked(self):
        batch = self._close()
        r = self.client.post("/api/medication-records/", json={
            "batch_id": batch["id"], "medication_date": "2026-09-01",
            "drug_name": "聚维酮碘"})
        self.assertEqual(r.status_code, 422)

    def test_water_quality_blocked(self):
        batch = self._close()
        r = self.client.post("/api/water-quality-records/", json={
            "batch_id": batch["id"], "record_date": "2026-09-01"})
        self.assertEqual(r.status_code, 422)

    def test_active_batch_accepts_records(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"])
        r = self.client.post("/api/feeding-records/", json={
            "batch_id": batch["id"], "feeding_date": "2026-04-01",
            "feed_type": "颗粒料", "feed_quantity": 5})
        self.assertEqual(r.status_code, 200, r.text)


class TestPondTransfer(StateMachineTestBase):
    def test_transfer_to_inactive_pond_rejected(self):
        p1 = self.create_pond("塘A")
        p2 = self.create_pond("塘B")
        # p2 无在养批次,停用成功
        self.assertEqual(self.client.put(f"/api/ponds/{p2['id']}/",
                                         json={"status": "inactive"}).status_code, 200)
        batch = self.create_batch(p1["id"])
        r = self.update_batch(batch["id"], batch["version"], pond_id=p2["id"])
        self.assertEqual(r.status_code, 422)
        self.assertIn("目标塘口", r.json()["detail"])

    def test_transfer_capacity_conflict(self):
        p1 = self.create_pond("塘A", capacity=1)
        p2 = self.create_pond("塘B", capacity=1)
        self.create_batch(p2["id"], number="B-OTHER", stocking_date="2026-04-01")
        batch = self.create_batch(p1["id"])
        r = self.update_batch(batch["id"], batch["version"], pond_id=p2["id"])
        self.assertEqual(r.status_code, 422)
        self.assertIn("容量", r.json()["detail"])

    def test_transfer_success_when_capacity_allows(self):
        p1 = self.create_pond("塘A")
        p2 = self.create_pond("塘B", capacity=2)
        batch = self.create_batch(p1["id"])
        r = self.update_batch(batch["id"], batch["version"], pond_id=p2["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pond_id"], p2["id"])
        # 转移产生审计版本,动作为 transfer
        hist = self.client.get(f"/api/batches/{batch['id']}/history/").json()
        self.assertEqual(hist[0]["action"], "transfer")

    def test_pond_validity_window_must_cover_period(self):
        p1 = self.create_pond("塘A")
        # 有效期只到 6 月,无法覆盖到 8 月的养殖时段
        p2 = self.create_pond("塘B", active_to="2026-06-30")
        batch = self.create_batch(p1["id"], estimated_harvest_date="2026-08-01")
        r = self.update_batch(batch["id"], batch["version"], pond_id=p2["id"])
        self.assertEqual(r.status_code, 422)
        self.assertIn("有效期", r.json()["detail"])

    def test_deactivate_pond_with_active_batch_blocked(self):
        p1 = self.create_pond("塘A")
        self.create_batch(p1["id"])
        r = self.client.put(f"/api/ponds/{p1['id']}/", json={"status": "inactive"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("先跨塘转移", r.json()["detail"])


class TestOptimisticLocking(StateMachineTestBase):
    def test_stale_version_returns_identifiable_conflict(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"])
        # 操作者 A 先改
        r1 = self.update_batch(batch["id"], batch["version"], species="鲤鱼")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["version"], 2)
        # 操作者 B 拿着旧版本号提交
        r2 = self.update_batch(batch["id"], 1, species="鲫鱼")
        self.assertEqual(r2.status_code, 409)
        detail = r2.json()["detail"]
        self.assertEqual(detail["code"], "VERSION_CONFLICT")
        self.assertEqual(detail["current_version"], 2)
        # 先提交者的修改未被覆盖
        fresh = self.client.get(f"/api/batches/{batch['id']}/").json()
        self.assertEqual(fresh["species"], "鲤鱼")

    def test_expected_version_is_required(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"])
        r = self.client.put(f"/api/batches/{batch['id']}/", json={"species": "鲫鱼"})
        self.assertEqual(r.status_code, 422)

    def test_version_increments_and_audit_written(self):
        pond = self.create_pond()
        batch = self.create_batch(pond["id"], operator="张三")
        harvested = self.harvest(batch)
        r = self.update_batch(harvested["id"], harvested["version"],
                              status="active", reason="误收获,需继续养殖",
                              actual_harvest_date=None, operator="李四")
        self.assertEqual(r.status_code, 200)
        new = r.json()
        self.assertEqual(new["version"], 3)

        hist = self.client.get(f"/api/batches/{batch['id']}/history/").json()
        actions = [(h["version"], h["action"]) for h in hist]
        self.assertIn((3, "revert_harvest"), actions)
        revert = next(h for h in hist if h["version"] == 3)
        self.assertEqual(revert["reason"], "误收获,需继续养殖")
        self.assertEqual(revert["operator"], "李四")
        self.assertEqual(revert["from_status"], "harvested")
        self.assertEqual(revert["to_status"], "active")


class TestConsistentReadModel(StateMachineTestBase):
    def test_detail_analysis_traceability_share_state(self):
        pond = self.create_pond()
        batch = self.harvest(self.create_batch(pond["id"]))

        detail = self.client.get(f"/api/batches/{batch['id']}/").json()
        analysis = self.client.get(f"/api/analysis/cycle/{batch['id']}/").json()
        trace = self.client.get(f"/api/analysis/traceability/{batch['id']}/").json()

        self.assertEqual(detail["status"], analysis["status"])
        self.assertEqual(detail["status"], trace["batch"]["status"])
        self.assertEqual(detail["version"], analysis["version"])
        self.assertEqual(detail["version"], trace["batch"]["version"])
        self.assertEqual(analysis["days_cultured"],
                         (date(2026, 8, 10) - date(2026, 3, 1)).days)

    def test_cycle_days_never_negative(self):
        # 即使库中存在倒挂的旧数据,分析口径也不得给出负天数
        pond = self.create_pond()
        db = SessionLocal()
        db.add(models.Batch(
            batch_number="OLD-NEG", pond_id=pond["id"], species="草鱼",
            stocking_date=date(2026, 5, 1),
            actual_harvest_date=date(2026, 4, 1),
            status="harvested", version=1, data_quality="ok"))
        db.commit()
        db.close()
        bid = self.client.get("/api/batches/by-number/OLD-NEG/").json()["id"]
        analysis = self.client.get(f"/api/analysis/cycle/{bid}/").json()
        self.assertGreaterEqual(analysis["days_cultured"], 0)


class TestAnomalyDetectionAndNormalize(StateMachineTestBase):
    def _add_legacy_rows(self):
        pond = self.create_pond()
        db = SessionLocal()
        db.add(models.Batch(
            batch_number="LEG-DATE", pond_id=pond["id"], species="草鱼",
            stocking_date=date(2026, 5, 1),
            actual_harvest_date=date(2026, 4, 1),
            status="closed", version=1, data_quality="ok"))
        db.add(models.Batch(
            batch_number="LEG-STATUS", pond_id=pond["id"], species="草鱼",
            stocking_date=date(2026, 5, 1),
            actual_harvest_date=None, status="closed",
            version=1, data_quality="ok"))
        db.commit()
        db.close()
        return pond

    def test_anomalies_detected(self):
        self._add_legacy_rows()
        found = self.client.get("/api/batches/anomalies/").json()
        numbers = {a["batch_number"]: a["issues"] for a in found}
        self.assertIn("LEG-DATE", numbers)
        self.assertTrue(any("早于投苗" in i for i in numbers["LEG-DATE"]))
        self.assertIn("LEG-STATUS", numbers)
        self.assertTrue(any("缺少实际收获日期" in i for i in numbers["LEG-STATUS"]))

    def test_normalize_all_repairs_safely(self):
        self._add_legacy_rows()
        report = self.client.post("/api/batches/normalize/",
                                  json={"operator": "月底复核"}).json()
        self.assertEqual(report["anomalies"], 2)
        self.assertEqual(report["normalized"], 2)
        # 归一后不再有异常
        self.assertEqual(self.client.get("/api/batches/anomalies/").json(), [])

        fixed = self.client.get("/api/batches/by-number/LEG-DATE/").json()
        self.assertEqual(fixed["data_quality"], "sanitized")
        self.assertEqual(fixed["status"], "active")
        self.assertIsNone(fixed["actual_harvest_date"])
        self.assertEqual(fixed["version"], 2)

        # 归一动作本身也形成审计版本
        hist = self.client.get(f"/api/batches/{fixed['id']}/history/").json()
        self.assertEqual(hist[0]["action"], "normalize")
        self.assertIn("系统安全归一", hist[0]["reason"])

    def test_normalize_rehomes_batch_on_inactive_pond(self):
        active_pond = self.create_pond("可用塘")
        dead_pond = self.create_pond("报废塘")
        self.client.put(f"/api/ponds/{dead_pond['id']}/", json={"status": "inactive"})
        db = SessionLocal()
        db.add(models.Batch(
            batch_number="LEG-POND", pond_id=dead_pond["id"], species="草鱼",
            stocking_date=date(2026, 5, 1), status="active",
            version=1, data_quality="ok"))
        db.commit()
        db.close()

        report = self.client.post("/api/batches/normalize/", json={}).json()
        item = next(i for i in report["items"] if i["batch_number"] == "LEG-POND")
        self.assertTrue(any("有效塘口" in f for f in item["fixed"]))
        moved = self.client.get("/api/batches/by-number/LEG-POND/").json()
        self.assertEqual(moved["pond_id"], active_pond["id"])


class TestPondModelValidation(StateMachineTestBase):
    def test_capacity_must_be_positive(self):
        r = self.client.post("/api/ponds/", json={
            "name": "异常塘", "area": 1, "water_depth": 1, "capacity": 0})
        self.assertEqual(r.status_code, 422)

    def test_active_window_order(self):
        r = self.client.post("/api/ponds/", json={
            "name": "异常塘", "area": 1, "water_depth": 1,
            "active_from": "2026-09-01", "active_to": "2026-01-01"})
        self.assertEqual(r.status_code, 422)

    def test_delete_pond_with_batches_blocked(self):
        pond = self.create_pond()
        self.create_batch(pond["id"])
        r = self.client.delete(f"/api/ponds/{pond['id']}/")
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
