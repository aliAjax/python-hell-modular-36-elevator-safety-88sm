from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    PermitBlocked,
    ValidationError,
)


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_ts(value, field):
    if not value:
        raise ValidationError(field + " is required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(field + " must be ISO-8601")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _positive(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number <= 0:
        raise ValidationError(field + " must be positive")
    return number


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")


def _validate_inspection(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("maintenance requires equipment")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")


def _validate_alarm(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("alarm requires equipment")
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ("closed", "false_alarm")
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] == "closed":
        raise ValidationError("rescue_job requires an active alarm")
    key = data.get("dedupe_key")
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
            raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _validate_permit(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


def _equipment_inspections(equipment_id, lookup):
    return [
        i
        for i in _all(lookup, "inspection")
        if i["data"].get("equipment_id") == equipment_id
    ]


def _equipment_remediations(equipment_id, lookup):
    alarm_ids = {
        a["id"]
        for a in _all(lookup, "alarm")
        if a["data"].get("equipment_id") == equipment_id
    }
    return [
        r
        for r in _all(lookup, "remediation")
        if r["status"] != "closed"
        and (r["data"].get("equipment_id") == equipment_id or r["data"].get("alarm_id") in alarm_ids)
    ]


def _equipment_open_rescue_jobs(equipment_id, lookup):
    alarm_ids = {
        a["id"]
        for a in _all(lookup, "alarm")
        if a["data"].get("equipment_id") == equipment_id
    }
    return [
        job
        for job in _all(lookup, "rescue_job")
        if job["data"].get("alarm_id") in alarm_ids and job["status"] not in ("completed", "aborted")
    ]


def _permit_blockers(permit, lookup, now=None):
    """Return every reason a permit cannot currently be granted."""
    blockers = []
    equipment = _find_one(lookup, "equipment", "id", permit["data"].get("equipment_id"))
    if not equipment:
        return [{"code": "equipment_missing", "message": "permit requires equipment"}], None, None
    if equipment["status"] not in ("in_service", "suspended"):
        blockers.append({
            "code": "equipment_out_of_service",
            "message": "equipment is %s and must return to suspended before permit grant" % equipment["status"],
            "equipment_id": equipment["id"],
            "equipment_status": equipment["status"],
        })
    passed = [i for i in _equipment_inspections(equipment["id"], lookup) if i["status"] == "passed"]
    latest = max(passed, key=lambda i: i["data"].get("passed_at") or i["data"].get("scheduled_at") or "") if passed else None
    if not latest:
        blockers.append({
            "code": "no_passed_inspection",
            "message": "permit requires a passed inspection",
            "equipment_id": equipment["id"],
        })
    else:
        passed_at = _parse_ts(
            latest["data"].get("passed_at") or latest["data"].get("scheduled_at"),
            "inspection passed_at",
        )
        interval_days = _positive(equipment["data"].get("inspection_interval_days"), "inspection_interval_days")
        if passed_at + timedelta(days=interval_days) < (now or _utcnow()):
            blockers.append({
                "code": "inspection_overdue",
                "message": "inspection %s passed_at %s exceeds the %s-day inspection interval"
                % (latest["id"], latest["data"].get("passed_at"), interval_days),
                "inspection_id": latest["id"],
                "passed_at": latest["data"].get("passed_at"),
                "interval_days": interval_days,
            })
    open_remediations = _equipment_remediations(equipment["id"], lookup)
    if open_remediations:
        blockers.append({
            "code": "open_remediation",
            "message": "open remediation blocks the permit: %s"
            % ", ".join(r["id"] for r in open_remediations),
            "remediation_ids": [r["id"] for r in open_remediations],
        })
    open_jobs = _equipment_open_rescue_jobs(equipment["id"], lookup)
    if open_jobs:
        blockers.append({
            "code": "open_rescue_job",
            "message": "unfinished rescue job blocks the permit: %s"
            % ", ".join(j["id"] for j in open_jobs),
            "rescue_job_ids": [j["id"] for j in open_jobs],
        })
    return blockers, latest, equipment


def _grant_permit(actor, entity, data, lookup):
    blockers, latest, equipment = _permit_blockers(entity, lookup)
    if blockers:
        raise PermitBlocked(blockers)
    patch = {
        "granted_by": actor.user_id,
        "granted_at": _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z"),
        "inspection_id": latest["id"],
    }
    # A suspended equipment only returns to service through a granted
    # return-to-service permit; other permit purposes leave status untouched.
    effects = []
    if equipment["status"] == "suspended" and entity["data"].get("purpose") == "return_to_service":
        effects.append({
            "entity_id": equipment["id"],
            "action": "return_to_service",
            "data": {"permit_id": entity["id"], "inspection_id": latest["id"]},
        })
    return patch, effects


def _pass_inspection(actor, entity, data, lookup):
    inspection_id = entity["id"]
    passed_at = data.get("passed_at") or _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")
    _parse_ts(passed_at, "passed_at")
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    effects = []
    if equipment and equipment["status"] == "out_of_service":
        # A failed equipment only comes back to suspended, pending a new permit.
        effects.append({
            "entity_id": equipment["id"],
            "action": "suspend",
            "data": {"reason": "reinspection_passed", "inspection_id": inspection_id},
        })
    patch = {"passed_at": passed_at, "inspected_by": actor.user_id}
    return patch, effects


def _fail_inspection(actor, entity, data, lookup):
    inspection_id = entity["id"]
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    effects = []
    if equipment:
        if equipment["status"] != "out_of_service":
            effects.append({
                "entity_id": equipment["id"],
                "action": "out_of_service",
                "data": {"reason": "inspection_failed", "inspection_id": inspection_id},
            })
        for permit in _all(lookup, "permit"):
            if (
                permit["data"].get("equipment_id") == equipment["id"]
                and permit["status"] == "pending_review"
            ):
                effects.append({
                    "entity_id": permit["id"],
                    "action": "revoke",
                    "data": {"reason": "inspection %s failed" % inspection_id, "inspection_id": inspection_id},
                })
    patch = {"failed_at": _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z"), "inspected_by": actor.user_id}
    return patch, effects


def _reschedule_inspection(actor, entity, data, lookup):
    # A reinspection can only be arranged once the failed equipment is stopped.
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    if equipment["status"] != "out_of_service":
        raise InvalidTransition(
            "equipment %s must be out_of_service before rescheduling, current status %s"
            % (equipment["id"], equipment["status"])
        )
    new_scheduled_at = data.get("rescheduled_at") or data.get("scheduled_at")
    _parse_ts(new_scheduled_at, "rescheduled_at")
    return {"rescheduled_at": new_scheduled_at}, []


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}, []


def _complete_rescue(actor, entity, data, lookup):
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if not jobs or any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    return {"resolved_by": actor.user_id}, []


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "dispatched", "remediation": "open",
        "permit": "blocked",
    }
    TRANSITIONS = {
        "equipment": {
            # suspend also models the automatic recovery from a failed
            # inspection: out_of_service -> suspended (reinspection passed).
            "suspend": (("in_service", "out_of_service"), "suspended"),
            "out_of_service": (("in_service", "suspended"), "out_of_service"),
            "return_to_service": (("suspended",), "in_service"),
        },
        "inspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "reschedule": (("failed",), "scheduled"),
        },
        "maintenance": {
            "start": (("planned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
        },
        "alarm": {
            "dispatch": (("received",), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
        },
        "rescue_job": {
            "arrive": (("dispatched",), "on_site"),
            "complete": (("on_site",), "completed"),
            "abort": (("dispatched", "on_site"), "aborted"),
        },
        "remediation": {
            "submit_evidence": (("open",), "evidence_submitted"),
            "verify": (("evidence_submitted",), "verified"),
            "reject": (("evidence_submitted",), "open"),
            "close": (("verified",), "closed"),
        },
        "permit": {
            "request_review": (("blocked",), "pending_review"),
            "grant": (("pending_review",), "granted"),
            "revoke": (("granted", "pending_review"), "revoked"),
            "expire": (("granted",), "expired"),
        },
    }
    CREATE_REQUIRED = {
        "equipment": ("asset_no", "equipment_type", "location", "inspection_interval_days"),
        "inspection": ("equipment_id", "scheduled_at", "cycle_days"),
        "maintenance": ("equipment_id", "work_type", "planned_at"),
        "alarm": ("equipment_id", "code", "occurred_at"),
        "rescue_job": ("alarm_id", "dedupe_key", "team"),
        "remediation": ("issue", "owner", "due_at"),
        "permit": ("equipment_id", "purpose", "requested_by"),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
        ("permit", "revoke"): ("reason",),
    }
    CREATE_ROLES = {
        "equipment": ("admin", "inspector"),
        "inspection": ("admin", "inspector"),
        "maintenance": ("admin", "maintenance"),
        "alarm": ("admin", "dispatcher", "inspector"),
        "rescue_job": ("admin", "dispatcher"),
        "remediation": ("admin", "inspector", "maintenance"),
        "permit": ("admin", "inspector"),
    }
    ROLE_ACTIONS = {
        "suspend": ("admin", "inspector"),
        "out_of_service": ("admin", "inspector"),
        "return_to_service": ("admin", "inspector"),
        "pass": ("admin", "inspector"),
        "fail": ("admin", "inspector"),
        "reschedule": ("admin", "inspector"),
        "start": ("admin", "maintenance"),
        "complete": ("admin", "maintenance", "dispatcher"),
        "dispatch": ("admin", "dispatcher"),
        "mark_false": ("admin", "dispatcher", "inspector"),
        "resolve": ("admin", "dispatcher"),
        "close": ("admin", "dispatcher", "inspector"),
        "arrive": ("admin", "dispatcher"),
        "abort": ("admin", "dispatcher"),
        "submit_evidence": ("admin", "maintenance", "inspector"),
        "verify": ("admin", "inspector"),
        "reject": ("admin", "inspector"),
        "request_review": ("admin", "inspector"),
        "grant": ("admin", "inspector"),
        "revoke": ("admin", "inspector"),
        "expire": ("admin", "inspector"),
    }
    CUSTOM_CREATE = {
        "equipment": lambda a, d, l: _validate_equipment(d, l),
        "inspection": lambda a, d, l: _validate_inspection(d, l),
        "maintenance": lambda a, d, l: _validate_maintenance(d, l),
        "alarm": lambda a, d, l: _validate_alarm(d, l),
        "rescue_job": lambda a, d, l: _validate_rescue(d, l),
        "remediation": lambda a, d, l: _validate_remediation(d, l),
        "permit": lambda a, d, l: _validate_permit(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("permit", "grant"): _grant_permit,
        ("inspection", "pass"): _pass_inspection,
        ("inspection", "fail"): _fail_inspection,
        ("inspection", "reschedule"): _reschedule_inspection,
        ("remediation", "verify"): _verify_remediation,
        ("alarm", "close"): _complete_rescue,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        effects = []
        if custom:
            extra, effects = custom(actor, entity, data, lookup)
        else:
            extra = {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch, effects

    def permit_readiness(self, permit, lookup=None):
        """Read-only snapshot of the blockers that stop a permit grant."""
        blockers, latest, equipment = _permit_blockers(permit, lookup or (lambda k, f, v: []))
        return {
            "permit_id": permit["id"],
            "status": permit["status"],
            "equipment_id": permit["data"].get("equipment_id"),
            "equipment_status": equipment["status"] if equipment else None,
            "current_inspection_id": latest["id"] if latest else None,
            "ready": not blockers,
            "blockers": blockers,
        }
