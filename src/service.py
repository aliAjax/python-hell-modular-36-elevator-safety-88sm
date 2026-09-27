import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch, effects = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        # Cascade effects are validated against fresh state first, so an
        # unsatisfied blocker (e.g. equipment not stopped) aborts the whole
        # action before the primary entity moves.
        for effect in effects:
            self._apply_effect(actor, effect, caused_by=(entity_id, action))
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch, "effects": effects},
        )
        return updated

    def _apply_effect(self, actor, effect, caused_by):
        target_id = effect["entity_id"]
        target = self.repository.get_entity(target_id)
        if not target:
            raise NotFoundError("cascade target entity not found: " + target_id)
        effect_data = dict(effect.get("data") or {})
        effect_data["caused_by"] = "%s:%s" % caused_by
        next_status, patch, nested = self.rules.validate_transition(
            actor, target, effect["action"], effect_data, self._lookup
        )
        if nested:
            raise ConflictError("nested cascade effects are not supported")
        merged = dict(target["data"])
        merged.update(patch)
        updated = self.repository.update_entity(target_id, target["version"], next_status, merged)
        self.audit.record(
            target_id,
            actor,
            effect["action"],
            target["status"],
            updated["status"],
            {"patch": patch, "cascade_from": caused_by[0], "cascade_action": caused_by[1]},
        )
        return updated

    def permit_readiness(self, permit_id):
        permit = self.repository.get_entity(permit_id)
        if not permit:
            raise NotFoundError("entity not found: " + permit_id)
        if self.rules.normalize_kind(permit["kind"]) != "permit":
            raise ValidationError("entity %s is not a permit" % permit_id)
        return self.rules.permit_readiness(permit, self._lookup)

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
