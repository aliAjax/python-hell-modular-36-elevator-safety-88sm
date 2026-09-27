import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class InspectionLinkageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repository = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repository, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1", interval=365):
        return self.service.create(self.actor, "equipment", {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": interval})

    def inspection(self, equipment):
        return self.service.create(self.actor, "inspection", {"equipment_id": equipment["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365})

    def permit(self, equipment):
        return self.service.create(self.actor, "permit", {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"})

    def act(self, entity, action, data=None):
        return self.service.transition(self.actor, entity["id"], action, data or {})

    def test_fail_stops_equipment_and_voids_pending_permit(self):
        equipment = self.equipment()
        inspection = self.inspection(equipment)
        permit = self.act(self.permit(equipment), "request_review")
        self.assertEqual(permit["status"], "pending_review")

        inspection = self.act(inspection, "fail", {"findings": "brake worn"})
        self.assertEqual(inspection["status"], "failed")
        self.assertIn("failed_at", inspection["data"])

        equipment = self.service.get(equipment["id"])
        self.assertEqual(equipment["status"], "out_of_service")
        self.assertEqual(equipment["data"]["last_inspection_id"], inspection["id"])
        self.assertEqual(equipment["data"]["block_reason"], "inspection_failed")

        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "revoked")

        actions = [entry["action"] for entry in self.service.audit_log(equipment["id"])]
        self.assertIn("auto_out_of_service", actions)

    def test_reschedule_requires_equipment_out_of_service(self):
        equipment = self.equipment()
        inspection = self.act(self.inspection(equipment), "fail", {"findings": "x"})
        equipment = self.service.get(equipment["id"])
        self.assertEqual(equipment["status"], "out_of_service")

        # legacy inconsistent data: failed inspection but equipment back in service
        self.repository.update_entity(equipment["id"], None, "in_service", equipment["data"])
        with self.assertRaises(ConflictError) as ctx:
            self.act(inspection, "reschedule")
        self.assertIn("out_of_service", str(ctx.exception))

        self.repository.update_entity(equipment["id"], None, "out_of_service", equipment["data"])
        inspection = self.act(inspection, "reschedule")
        self.assertEqual(inspection["status"], "scheduled")

    def test_repass_moves_equipment_to_suspended_awaiting_permit(self):
        equipment = self.equipment()
        inspection = self.act(self.inspection(equipment), "fail", {"findings": "x"})
        inspection = self.act(inspection, "reschedule")
        inspection = self.act(inspection, "pass", {"findings": "ok"})
        self.assertIn("passed_at", inspection["data"])

        equipment = self.service.get(equipment["id"])
        self.assertEqual(equipment["status"], "suspended")
        self.assertEqual(equipment["data"]["block_reason"], "awaiting_permit")
        self.assertEqual(equipment["data"]["last_inspection_id"], inspection["id"])

        permit = self.act(self.permit(equipment), "request_review")
        permit = self.act(permit, "grant")
        self.assertEqual(permit["status"], "granted")

        equipment = self.act(equipment, "return_to_service")
        self.assertEqual(equipment["status"], "in_service")
        self.assertIsNone(equipment["data"]["block_reason"])

    def test_grant_blocked_by_open_remediation_and_rescue_job(self):
        equipment = self.equipment()
        self.act(self.inspection(equipment), "pass", {"findings": "ok"})
        self.service.create(self.actor, "remediation", {"equipment_id": equipment["id"], "issue": "door", "owner": "M", "due_at": "2026-10-01"})
        alarm = self.service.create(self.actor, "alarm", {"equipment_id": equipment["id"], "code": "TRIP", "occurred_at": "2026-09-27T10:00:00Z"})
        self.service.create(self.actor, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "j1", "team": "A"})

        permit = self.act(self.permit(equipment), "request_review")
        with self.assertRaises(ConflictError) as ctx:
            self.act(permit, "grant")
        message = str(ctx.exception)
        self.assertIn("remediation", message)
        self.assertIn("rescue", message)

    def test_grant_blocked_by_overdue_inspection(self):
        equipment = self.equipment(interval=30)
        inspection = self.act(self.inspection(equipment), "pass", {"findings": "ok"})
        data = dict(inspection["data"])
        data["passed_at"] = "2026-01-01T00:00:00+00:00"
        self.repository.update_entity(inspection["id"], None, "passed", data)

        permit = self.act(self.permit(equipment), "request_review")
        with self.assertRaises(ConflictError) as ctx:
            self.act(permit, "grant")
        self.assertIn("overdue", str(ctx.exception))

    def test_grant_blocked_by_equipment_out_of_service(self):
        equipment = self.equipment()
        self.act(self.inspection(equipment), "fail", {"findings": "x"})
        permit = self.act(self.permit(equipment), "request_review")
        with self.assertRaises(ConflictError) as ctx:
            self.act(permit, "grant")
        self.assertIn("out_of_service", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
