"""批次状态机、日期一致性、跨塘转移、乐观锁与历史数据归一的端到端测试。

通过 FastAPI TestClient 走真实 HTTP 层，使用独立的 sqlite 临时库。
"""
import os
import tempfile
import unittest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app import models  # noqa: E402


class BatchStateMachineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        db = SessionLocal()
        try:
            for table in reversed(models.Base.metadata.sorted_tables):
                db.execute(table.delete())
            db.commit()
        finally:
            db.close()

    # ---------- 辅助 ----------

    def make_pond(self, name="1号塘", status="active", capacity=1,
                  active_from=None, active_until=None):
        payload = {"name": name, "area": 10.0, "water_depth": 2.0,
                   "status": status, "capacity": capacity}
        if active_from:
            payload["active_from"] = active_from
        if active_until:
            payload["active_until"] = active_until
        r = self.client.post("/api/ponds/", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def make_batch(self, pond_id, number="B2026-001", stocking="2026-03-01",
                   estimated="2026-09-01", **extra):
        payload = {"batch_number": number, "pond_id": pond_id, "species": "草鱼",
                   "stocking_date": stocking, "estimated_harvest_date": estimated}
        payload.update(extra)
        r = self.client.post("/api/batches/", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ---------- 日期一致性 ----------

    def test_actual_harvest_before_stocking_rejected(self):
        pond = self.make_pond()
        batch = self.make_batch(pond["id"])
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"],
            "status": "harvested",
            "actual_harvest_date": "2026-02-01",  # 早于投苗日 2026-03-01
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("实际收获日期不能早于投苗日期", r.json()["detail"])
        # 状态未被破坏
        after = self.client.get(f"/api/batches/{batch['id']}/").json()
        self.assertEqual(after["status"], "active")
        self.assertIsNone(after["actual_harvest_date"])

    def test_estimated_harvest_before_stocking_rejected(self):
        pond = self.make_pond()
        r = self.client.post("/api/batches/", json={
            "batch_number": "B-NEG", "pond_id": pond["id"], "species": "鲫鱼",
            "stocking_date": "2026-03-01", "estimated_harvest_date": "2026-01-01",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("预计收获日期不能早于投苗日期", r.json()["detail"])

    def test_harvested_requires_actual_date_and_active_forbids_it(self):
        pond = self.make_pond()
        batch = self.make_batch(pond["id"])
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "status": "harvested",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("必须填写实际收获日期", r.json()["detail"])

        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "actual_harvest_date": "2026-08-01",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("养殖中的批次不能保留实际收获日期", r.json()["detail"])

    def test_happy_path_harvest_and_cycle_days(self):
        pond = self.make_pond()
        batch = self.make_batch(pond["id"])
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "status": "harvested",
            "actual_harvest_date": "2026-08-15",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "harvested")

        analysis = self.client.get(f"/api/analysis/cycle/{batch['id']}/").json()
        self.assertEqual(analysis["days_cultured"], 167)
        self.assertEqual(analysis["status"], "harvested")
        # 详情与分析展示同一最终状态
        detail = self.client.get(f"/api/batches/{batch['id']}/").json()
        self.assertEqual(detail["status"], analysis["status"])
        self.assertEqual(detail["version"], analysis["version"])

    # ---------- 状态机回退与审计 ----------

    def test_rollback_requires_reason_and_writes_audit(self):
        pond = self.make_pond()
        batch = self.make_batch(pond["id"])
        batch = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "status": "harvested",
            "actual_harvest_date": "2026-08-15",
        }).json()

        # 无原因回退被拒
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "status": "active",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("必须填写原因", r.json()["detail"])

        # 有原因回退成功，实际收获日期随状态机清空
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "status": "active",
            "reason": "出塘录入错误，实际未捕捞",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "active")
        self.assertIsNone(r.json()["actual_harvest_date"])

        revisions = self.client.get(f"/api/batches/{batch['id']}/revisions/").json()
        types = [rev["change_type"] for rev in revisions]
        self.assertIn("create", types)
        self.assertIn("update", types)
        self.assertIn("rollback", types)
        rollback = next(rev for rev in revisions if rev["change_type"] == "rollback")
        self.assertEqual(rollback["reason"], "出塘录入错误，实际未捕捞")
        self.assertIn('"status": "active"', rollback["snapshot"])

    def test_closed_batch_rejects_new_daily_records(self):
        pond = self.make_pond()
        batch = self.make_batch(pond["id"])
        batch = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "status": "closed",
        }).json()
        self.assertEqual(batch["status"], "closed")

        for url, payload in [
            ("/api/feeding-records/", {"batch_id": batch["id"], "feeding_date": "2026-05-01",
                                       "feed_type": "颗粒料", "feed_quantity": 10.0}),
            ("/api/medication-records/", {"batch_id": batch["id"], "medication_date": "2026-05-01",
                                          "drug_name": "二氧化氯"}),
            ("/api/water-quality-records/", {"batch_id": batch["id"], "record_date": "2026-05-01"}),
            ("/api/cost-records/", {"batch_id": batch["id"], "cost_date": "2026-05-01",
                                    "cost_type": "feed", "amount": 100.0}),
            ("/api/harvest-sales/", {"batch_id": batch["id"], "sale_date": "2026-05-01",
                                     "weight": 100.0, "unit_price": 20.0}),
        ]:
            r = self.client.post(url, json=payload)
            self.assertEqual(r.status_code, 409, f"{url}: {r.text}")
            self.assertIn("已关闭", r.json()["detail"])

    def test_harvested_batch_rejects_feeding_but_allows_settlement(self):
        pond = self.make_pond()
        batch = self.make_batch(pond["id"])
        batch = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "status": "harvested",
            "actual_harvest_date": "2026-08-15",
        }).json()

        r = self.client.post("/api/feeding-records/", json={
            "batch_id": batch["id"], "feeding_date": "2026-08-16",
            "feed_type": "颗粒料", "feed_quantity": 5.0})
        self.assertEqual(r.status_code, 409)
        self.assertIn("已出塘", r.json()["detail"])

        # 已出塘仍允许销售与成本结算
        r = self.client.post("/api/harvest-sales/", json={
            "batch_id": batch["id"], "sale_date": "2026-08-16",
            "weight": 100.0, "unit_price": 20.0})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/cost-records/", json={
            "batch_id": batch["id"], "cost_date": "2026-08-16",
            "cost_type": "labor", "amount": 500.0})
        self.assertEqual(r.status_code, 200, r.text)

    # ---------- 跨塘转移 ----------

    def test_transfer_to_inactive_pond_rejected(self):
        pond_a = self.make_pond("A塘")
        pond_b = self.make_pond("B塘", status="inactive")
        batch = self.make_batch(pond_a["id"])
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "pond_id": pond_b["id"],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("已停用", r.json()["detail"])

    def test_transfer_checks_validity_window_and_capacity(self):
        pond_a = self.make_pond("A塘")
        # 目标塘有效期只到 2026-06-30，覆盖不了到 2026-09-01 的养殖窗口
        pond_b = self.make_pond("B塘", active_from="2026-01-01", active_until="2026-06-30")
        batch = self.make_batch(pond_a["id"])
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "pond_id": pond_b["id"],
            "transfer_date": "2026-04-01",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("有效期", r.json()["detail"])

        # 容量：C塘容量1且同时段已有在养批次
        pond_c = self.make_pond("C塘", capacity=1)
        self.make_batch(pond_c["id"], number="B2026-002",
                        stocking="2026-02-01", estimated="2026-10-01")
        batch = self.client.get(f"/api/batches/{batch['id']}/").json()
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "pond_id": pond_c["id"],
            "transfer_date": "2026-04-01",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("容量上限", r.json()["detail"])

    def test_transfer_success_writes_audit(self):
        pond_a = self.make_pond("A塘")
        pond_b = self.make_pond("B塘", capacity=2)
        batch = self.make_batch(pond_a["id"])
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "pond_id": pond_b["id"],
            "transfer_date": "2026-04-01",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pond_id"], pond_b["id"])
        revisions = self.client.get(f"/api/batches/{batch['id']}/revisions/").json()
        self.assertEqual(revisions[0]["change_type"], "transfer")

    def test_harvested_batch_cannot_transfer(self):
        pond_a = self.make_pond("A塘")
        pond_b = self.make_pond("B塘")
        batch = self.make_batch(pond_a["id"])
        batch = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "status": "harvested",
            "actual_harvest_date": "2026-08-15",
        }).json()
        r = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "pond_id": pond_b["id"],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("仅养殖中的批次可以跨塘转移", r.json()["detail"])

    # ---------- 乐观锁 ----------

    def test_stale_version_gets_conflict_not_overwrite(self):
        pond = self.make_pond()
        batch = self.make_batch(pond["id"])

        # 操作者甲先提交
        r1 = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "species": "鲈鱼",
        })
        self.assertEqual(r1.status_code, 200, r1.text)

        # 操作者乙拿着过期版本提交 -> 可识别的 409，且不覆盖甲的修改
        r2 = self.client.put(f"/api/batches/{batch['id']}/", json={
            "version": batch["version"], "species": "鳜鱼",
        })
        self.assertEqual(r2.status_code, 409)
        self.assertIn("版本冲突", r2.json()["detail"])

        after = self.client.get(f"/api/batches/{batch['id']}/").json()
        self.assertEqual(after["species"], "鲈鱼")
        self.assertEqual(after["version"], batch["version"] + 1)

    def test_missing_version_rejected(self):
        pond = self.make_pond()
        batch = self.make_batch(pond["id"])
        r = self.client.put(f"/api/batches/{batch['id']}/", json={"species": "鲈鱼"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("版本号", r.json()["detail"])

    # ---------- 历史矛盾数据：识别与归一 ----------

    def _corrupt_batch(self, batch_id, **fields):
        """绕过状态机直接改库，模拟历史遗留的矛盾数据。"""
        db = SessionLocal()
        try:
            batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
            for key, value in fields.items():
                setattr(batch, key, value)
            db.commit()
        finally:
            db.close()

    def test_inconsistencies_detected_and_normalized(self):
        import datetime as dt
        pond = self.make_pond(capacity=10)
        pond3 = self.make_pond("3号塘")

        # 负周期：实际收获日早于投苗日，且状态为已出塘
        b1 = self.make_batch(pond["id"], number="B-NEG")
        self._corrupt_batch(b1["id"], actual_harvest_date=dt.date(2026, 1, 1),
                            status="harvested")
        # 状态与日期矛盾：养殖中却有实际收获日
        b2 = self.make_batch(pond["id"], number="B-CONTRA",
                             stocking="2026-04-01", estimated="2026-10-01")
        self._corrupt_batch(b2["id"], actual_harvest_date=dt.date(2026, 8, 1))
        # 挂在停用塘口（不可自动归一）：先建批次再停用塘口
        b3 = self.make_batch(pond3["id"], number="B-INACTIVE",
                             stocking="2026-05-01", estimated="2026-11-01")
        r = self.client.put(f"/api/ponds/{pond3['id']}/", json={"status": "inactive"})
        self.assertEqual(r.status_code, 200, r.text)

        report = self.client.get("/api/batches/inconsistencies/").json()
        self.assertEqual(report["total"], 3)
        by_number = {item["batch_number"]: item for item in report["items"]}
        codes1 = {i["code"] for i in by_number["B-NEG"]["issues"]}
        self.assertIn("harvest_before_stocking", codes1)
        codes3 = {i["code"] for i in by_number["B-INACTIVE"]["issues"]}
        self.assertIn("inactive_pond", codes3)

        result = self.client.post("/api/batches/normalize/").json()
        normalized = {n["batch_number"] for n in result["normalized"]}
        self.assertIn("B-NEG", normalized)
        self.assertIn("B-CONTRA", normalized)
        skipped = {s["batch_number"] for s in result["skipped"]}
        self.assertIn("B-INACTIVE", skipped)  # 塘口归属问题留待人工

        # 归一后的最终状态一致且周期不再为负
        after1 = self.client.get(f"/api/batches/{b1['id']}/").json()
        self.assertEqual(after1["status"], "active")
        self.assertIsNone(after1["actual_harvest_date"])
        after2 = self.client.get(f"/api/batches/{b2['id']}/").json()
        self.assertEqual(after2["status"], "harvested")

        analysis = self.client.get(f"/api/analysis/cycle/{b2['id']}/").json()
        self.assertGreaterEqual(analysis["days_cultured"], 0)

        # 归一过程留下审计版本，且再次归一是幂等的
        revisions = self.client.get(f"/api/batches/{b1['id']}/revisions/").json()
        self.assertIn("normalize", [rev["change_type"] for rev in revisions])
        report2 = self.client.get("/api/batches/inconsistencies/").json()
        self.assertEqual(report2["total"], 1)  # 仅剩需人工处理的停用塘口


if __name__ == "__main__":
    unittest.main()
