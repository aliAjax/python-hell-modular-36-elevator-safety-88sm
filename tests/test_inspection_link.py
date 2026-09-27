import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermitBlocked
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


class InspectionLinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("inspector-1", "inspector")
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None):
        return self.service.transition(self.actor, entity["id"], action, data or {})

    def equipment_and_inspection(self, interval_days=365, scheduled_at=None):
        equipment = self.create("equipment", {
            "asset_no": "E-300", "equipment_type": "elevator",
            "location": "Tower B", "inspection_interval_days": interval_days,
        })
        inspection = self.create("inspection", {
            "equipment_id": equipment["id"],
            "scheduled_at": scheduled_at or iso(datetime.now(timezone.utc)),
            "cycle_days": interval_days,
        })
        return equipment, inspection

    def permit(self, equipment):
        permit = self.create("permit", {
            "equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops",
        })
        return self.act(permit, "request_review", {})

    def test_fail_stops_equipment_and_voids_pending_permit(self):
        equipment, inspection = self.equipment_and_inspection()
        pending = self.permit(equipment)

        failed = self.act(inspection, "fail", {"findings": "brake worn"})

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.service.get(equipment["id"])["status"], "out_of_service")
        self.assertEqual(self.service.get(pending["id"])["status"], "revoked")
        self.assertEqual(self.service.get(pending["id"])["data"]["inspection_id"], inspection["id"])

    def test_failed_permit_cannot_be_revived_for_grant(self):
        equipment, inspection = self.equipment_and_inspection()
        pending = self.permit(equipment)
        self.act(inspection, "fail", {"findings": "bad"})
        # The old inspection application must not restore the permit.
        with self.assertRaises(InvalidTransition):
            self.act(pending, "grant", {})

    def test_reschedule_requires_stopped_equipment(self):
        equipment, inspection = self.equipment_and_inspection()
        self.act(inspection, "fail", {"findings": "bad"})
        self.service.transition(Actor("admin", "admin"), equipment["id"], "suspend", {})
        failed = self.service.get(inspection["id"])
        with self.assertRaises(InvalidTransition):
            self.act(failed, "reschedule", {"rescheduled_at": iso(datetime.now(timezone.utc) + timedelta(days=7))})

    def test_reinspection_pass_returns_equipment_to_suspended_only(self):
        equipment, inspection = self.equipment_and_inspection()
        self.act(inspection, "fail", {"findings": "bad"})
        when = iso(datetime.now(timezone.utc) + timedelta(days=7))
        reinspection = self.create("inspection", {
            "equipment_id": equipment["id"], "scheduled_at": when, "cycle_days": 365,
        })
        passed = self.act(reinspection, "pass", {"findings": "fixed", "passed_at": when})

        self.assertEqual(passed["status"], "passed")
        self.assertEqual(passed["data"]["passed_at"], when)
        self.assertEqual(self.service.get(equipment["id"])["status"], "suspended")

    def test_grant_after_reinspection_returns_equipment_to_service(self):
        equipment, inspection = self.equipment_and_inspection()
        self.act(inspection, "fail", {"findings": "bad"})
        when = iso(datetime.now(timezone.utc) + timedelta(days=7))
        reinspection = self.create("inspection", {
            "equipment_id": equipment["id"], "scheduled_at": when, "cycle_days": 365,
        })
        self.act(reinspection, "pass", {"findings": "fixed", "passed_at": when})
        permit = self.permit(equipment)

        granted = self.act(permit, "grant", {})

        self.assertEqual(granted["status"], "granted")
        self.assertEqual(granted["data"]["inspection_id"], reinspection["id"])
        self.assertEqual(self.service.get(equipment["id"])["status"], "in_service")

    def test_grant_blocked_by_open_remediation(self):
        equipment, inspection = self.equipment_and_inspection()
        self.act(inspection, "pass", {"findings": "ok"})
        remediation = self.create("remediation", {
            "equipment_id": equipment["id"], "issue": "door alignment",
            "owner": "Maint", "due_at": "2026-10-01",
        })
        permit = self.permit(equipment)
        with self.assertRaises(PermitBlocked) as caught:
            self.act(permit, "grant", {})
        codes = {b["code"] for b in caught.exception.blockers}
        self.assertEqual(codes, {"open_remediation"})
        self.assertIn(remediation["id"], caught.exception.blockers[0]["remediation_ids"])

        readiness = self.service.permit_readiness(permit["id"])
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["current_inspection_id"], inspection["id"])
        self.assertEqual([b["code"] for b in readiness["blockers"]], ["open_remediation"])

    def test_grant_blocked_by_unfinished_rescue_job(self):
        equipment, inspection = self.equipment_and_inspection()
        self.act(inspection, "pass", {"findings": "ok"})
        alarm = self.create("alarm", {
            "equipment_id": equipment["id"], "code": "TRAP", "occurred_at": iso(datetime.now(timezone.utc)),
        })
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Alpha"})
        job = self.service.create(self.admin, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "job-9", "team": "Alpha"})
        permit = self.permit(equipment)
        with self.assertRaises(PermitBlocked) as caught:
            self.act(permit, "grant", {})
        codes = {b["code"] for b in caught.exception.blockers}
        self.assertEqual(codes, {"open_rescue_job"})
        self.assertEqual(caught.exception.blockers[0]["rescue_job_ids"], [job["id"]])

    def test_grant_blocked_when_inspection_exceeds_interval(self):
        equipment, inspection = self.equipment_and_inspection(interval_days=30)
        old_passed_at = datetime.now(timezone.utc) - timedelta(days=45)
        self.act(inspection, "pass", {"findings": "ok", "passed_at": iso(old_passed_at)})
        permit = self.permit(equipment)
        with self.assertRaises(PermitBlocked) as caught:
            self.act(permit, "grant", {})
        codes = {b["code"] for b in caught.exception.blockers}
        self.assertEqual(codes, {"inspection_overdue"})
        blocker = caught.exception.blockers[0]
        self.assertEqual(blocker["inspection_id"], inspection["id"])
        self.assertEqual(blocker["interval_days"], 30.0)

    def test_all_blockers_reported_together(self):
        equipment, inspection = self.equipment_and_inspection(interval_days=30)
        self.act(inspection, "pass", {"findings": "ok", "passed_at": iso(datetime.now(timezone.utc) - timedelta(days=40))})
        self.create("remediation", {
            "equipment_id": equipment["id"], "issue": "x", "owner": "m", "due_at": "2026-10-01",
        })
        alarm = self.create("alarm", {
            "equipment_id": equipment["id"], "code": "TRAP", "occurred_at": iso(datetime.now(timezone.utc)),
        })
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Alpha"})
        self.service.create(self.admin, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "job-10", "team": "Alpha"})
        permit = self.permit(equipment)
        with self.assertRaises(PermitBlocked) as caught:
            self.act(permit, "grant", {})
        self.assertEqual(
            {b["code"] for b in caught.exception.blockers},
            {"inspection_overdue", "open_remediation", "open_rescue_job"},
        )


if __name__ == "__main__":
    unittest.main()
