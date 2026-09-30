"""Service that records mixer load, marks abnormal batches and gates discharge."""

from __future__ import annotations

from threading import RLock
from typing import Any, Mapping

from ..audit.audit import ENTRY_KIND
from ..errors import InterlockBlockedError, NotFoundError, OrderViolationError, ValidationError
from ..service_base import PlantService
from .criteria import LoadCriteria, default_criteria, disposition_for, load_criteria_from_mapping
from .load import CRITERIA_KIND, MARKER_KIND, PROFILE_KIND, RELEASE_KIND, VERDICT_KIND
from .marker import BLOCKED, BatchMarker, LoadRegistry, marker_record
from .profile import LoadPoint, LoadProfile, load_profile
from .verdict import evaluate_load


class LoadService(PlantService):
    """Owns the per-batch load profile and the abnormal-batch lifecycle."""

    origin = "load"
    line = "mix"
    subject = "load.criteria"

    def __init__(self, context, audit, machine=None) -> None:
        super().__init__(context, machine)
        self.audit = audit
        self.registry = LoadRegistry()
        self._lock = RLock()
        self.chute_service = None

    def publish_criteria(self, changes: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Publish one immutable, versioned set of load judgment criteria."""

        current = self._current_criteria()
        criteria = load_criteria_from_mapping(changes or {}, fallback=current)
        token = self.context.versions.bump(self.subject, tick=self.context.clock.now())
        record = self.store.publish(
            CRITERIA_KIND,
            self.origin,
            token.value,
            criteria.to_payload(generation=token.value),
        )
        payload = {**record.payload, "seq": record.seq}
        self.registry.set_criteria(payload)
        self.emit("load.criteria_published", {"seq": record.seq, "generation": token.value})
        self._audit_note(
            "load.criteria_published",
            {"seq": record.seq, "generation": token.value, "criteria": criteria.describe()},
        )
        return {"criteria": payload, "seq": record.seq}

    def record_point(self, *, load_kw: float, batch_id: str | None = None, tick: int | None = None) -> dict[str, Any]:
        """Append one load sample to the profile of one batch."""

        with self._lock:
            batch = self._batch(batch_id)
            if load_kw < 0:
                raise ValidationError("mixer load must not be negative", load_kw=load_kw)
            profile = self.registry.profile(batch.batch_id)
            if profile is None:
                profile = LoadProfile(batch.batch_id)
                self.registry.set_profile(profile)
            point = profile.append(
                LoadPoint(tick=self.context.clock.now() if tick is None else tick, load_kw=round(load_kw, 4))
            )
            self.publish(PROFILE_KIND, {"active": True, **profile.describe()})
            self.emit("load.point_recorded", {"batch_id": batch.batch_id, **point.describe()})
            return {"point": point.describe(), "profile": profile.describe()}

    def evaluate(
        self,
        *,
        batch_id: str | None = None,
        use: str | None = None,
        mark: bool = False,
    ) -> dict[str, Any]:
        """Evaluate the captured curve, optionally marking an abnormal batch."""

        with self._lock:
            batch = self._batch(batch_id)
            profile = self.registry.require_profile(batch.batch_id)
            criteria, criteria_payload = self._criteria_with_payload()
            verdict = evaluate_load(
                profile.points(),
                criteria,
                generation=int(criteria_payload.get("generation", 0)),
                criteria_seq=criteria_payload.get("seq"),
            )
            verdict_record = self.store.publish(
                VERDICT_KIND,
                self.origin,
                self.generation(),
                {"active": True, "batch_id": batch.batch_id, **verdict.describe()},
            )
            payload = {**verdict_record.payload, "seq": verdict_record.seq}
            self.registry.record_verdict(batch.batch_id, payload)
            self.emit("load.evaluated", {"batch_id": batch.batch_id, **verdict.describe()})
            self._audit_note(
                "load.evaluated",
                {"batch_id": batch.batch_id, "seq": verdict_record.seq, "verdict": verdict.describe()},
            )
            result = {"verdict": payload}
            if mark:
                if use is None:
                    raise ValidationError("marking an abnormal batch requires its intended use")
                result["marker"] = self._mark_from_verdict(
                    batch_id=batch.batch_id,
                    use=use,
                    grade=batch.grade,
                    criteria=criteria,
                    verdict_payload=payload,
                )
            return result

    def mark(self, *, use: str, batch_id: str | None = None, reason: str = "load profile abnormal") -> dict[str, Any]:
        """Idempotently mark an abnormal batch according to its intended use."""

        with self._lock:
            batch = self._batch(batch_id)
            existing = self.registry.marker(batch.batch_id)
            if existing is not None and existing.use == use and existing.state == BLOCKED:
                self.emit("load.marker_idempotent", existing.describe())
                return {"marker": existing.describe(), "idempotent": True}
            verdict_payload = self.registry.verdict(batch.batch_id)
            if verdict_payload is None:
                profile = self.registry.require_profile(batch.batch_id)
                criteria, criteria_payload = self._criteria_with_payload()
                verdict = evaluate_load(
                    profile.points(),
                    criteria,
                    generation=int(criteria_payload.get("generation", 0)),
                    criteria_seq=criteria_payload.get("seq"),
                )
                verdict_record = self.store.publish(
                    VERDICT_KIND,
                    self.origin,
                    self.generation(),
                    {"active": True, "batch_id": batch.batch_id, **verdict.describe()},
                )
                verdict_payload = {**verdict_record.payload, "seq": verdict_record.seq}
                self.registry.record_verdict(batch.batch_id, verdict_payload)
                self.emit("load.evaluated", {"batch_id": batch.batch_id, **verdict.describe()})
                self._audit_note(
                    "load.evaluated",
                    {
                        "batch_id": batch.batch_id,
                        "seq": verdict_record.seq,
                        "verdict": verdict.describe(),
                    },
                )
            else:
                criteria, _ = self._criteria_with_payload()
            return {
                "marker": self._mark_from_verdict(
                    batch_id=batch.batch_id,
                    use=use,
                    grade=batch.grade,
                    criteria=criteria,
                    verdict_payload=verdict_payload,
                    reason=reason,
                ),
                "idempotent": False,
            }

    def release(self, *, reason: str = "operator confirmed release", batch_id: str | None = None) -> dict[str, Any]:
        """Release a blocked abnormal batch after explicit confirmation."""

        with self._lock:
            batch = self._batch(batch_id)
            marker = self.registry.require_marker(batch.batch_id)
            if not marker.blocked:
                return {"release": self._release_payload(marker), "idempotent": True}
            if marker.disposition != "rework":
                raise OrderViolationError(
                    "a scrapped batch cannot be released for discharge",
                    batch_id=batch.batch_id,
                    disposition=marker.disposition,
                )
            release_stage = self.store.stage(
                RELEASE_KIND,
                self.origin,
                self.generation(),
                {
                    "active": True,
                    "batch_id": batch.batch_id,
                    "marker_seq": marker.marker_seq,
                    "reason": reason,
                },
            )
            audit_entry, audit_record = self._stage_audit(
                "load.released",
                {"batch_id": batch.batch_id, "marker_seq": marker.marker_seq, "reason": reason},
            )
            self.store.flush()
            self.store.commit_through(audit_record.seq)
            released = self.registry.release(batch.batch_id, seq=release_stage.seq, reason=reason)
            self.audit.accept_staged(audit_entry)
            self.emit("load.released", {"batch_id": batch.batch_id, "seq": release_stage.seq})
            return {"release": self._release_payload(released), "idempotent": False}

    def rollback_release(
        self,
        *,
        reason: str = "release criteria no longer trusted",
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore a released abnormal batch to its discharge-intercepted state."""

        with self._lock:
            batch = self._batch(batch_id)
            if self.context.batches.latest() != batch:
                raise OrderViolationError(
                    "a released batch can only be intercepted before a later batch starts",
                    batch_id=batch.batch_id,
                )
            marker = self.registry.require_marker(batch.batch_id)
            if marker.release_seq is None:
                raise OrderViolationError("marker was never released", batch_id=batch.batch_id)
            targets = self._release_segment_targets(batch.batch_id, marker.release_seq)
            for target in targets:
                self.store.stage_tombstone(target, reason=reason, origin=self.origin, generation=self.generation())
            audit_entry, audit_record = self._stage_audit(
                "load.release_rolled_back",
                {
                    "batch_id": batch.batch_id,
                    "release_seq": marker.release_seq,
                    "targets": targets,
                    "reason": reason,
                },
            )
            self.store.flush()
            self.store.commit_through(audit_record.seq)
            self.audit.accept_staged(audit_entry)
            self._rewind_discharge(batch.batch_id)
            self.emit(
                "load.release_rolled_back",
                {"batch_id": batch.batch_id, "release_seq": marker.release_seq, "tombstones": list(targets)},
            )
            return {
                "blocked": self.registry.is_blocked(batch.batch_id),
                "tombstones": list(targets),
                "audit_seq": audit_record.seq,
            }

    def rollback_criteria_after(
        self,
        seq: int,
        *,
        reason: str = "load criteria no longer trusted",
        reintercept_released: bool = True,
    ) -> dict[str, Any]:
        """Tombstone an untrusted criteria segment and restore the previous one."""

        with self._lock:
            target_record = None
            if seq > 0:
                target_record = self.store.require_durable(seq, action="load.criteria.rollback")
                if target_record.kind != CRITERIA_KIND:
                    raise ValidationError(
                        "rollback target must be load criteria", seq=seq, kind=target_record.kind
                    )
            targets = [
                record.seq
                for record in self.store.committed_records()
                if record.seq > seq and record.kind == CRITERIA_KIND
            ]
            release_targets: list[int] = []
            if reintercept_released:
                release_targets = self._released_segment_for_untrusted_criteria(seq)
                targets.extend(release_targets)
            for target in targets:
                self.store.stage_tombstone(target, reason=reason, origin=self.origin, generation=self.generation())
            audit_entry, audit_record = self._stage_audit(
                "load.criteria_rolled_back",
                {"after_seq": seq, "targets": targets, "release_targets": release_targets, "reason": reason},
            )
            self.store.flush()
            self.store.commit_through(audit_record.seq)
            self.audit.accept_staged(audit_entry)
            self._rebuild_registry()
            blocked_batches = self._rewind_released_segments(release_targets)
            self.emit("load.criteria_rolled_back", {"after_seq": seq, "tombstones": targets})
            return {
                "criteria": self.registry.criteria(),
                "tombstones": targets,
                "blocked": blocked_batches,
                "audit_seq": audit_record.seq,
            }

    def require_discharge_allowed(self, batch_id: str) -> None:
        """Block the physical discharge door while a marker is active."""

        marker = self.registry.marker(batch_id)
        if marker is not None and marker.blocked:
            raise InterlockBlockedError(
                "abnormal batch is intercepted at the discharge door",
                batch_id=batch_id,
                disposition=marker.disposition,
                use=marker.use,
                marker_seq=marker.marker_seq,
            )

    def replay_record(self, record) -> None:
        """Adopt one visible stream record after bootstrap or rollback."""

        if record.kind == CRITERIA_KIND:
            payload = {**record.payload, "seq": record.seq}
            self.registry.set_criteria(payload)
            self.context.versions.adopt_generation(self.subject, int(record.payload.get("generation", record.generation)))
        elif record.kind == PROFILE_KIND:
            payload = record.payload
            profile = LoadProfile(str(payload["batch_id"]), load_profile(payload.get("points", ())))
            self.registry.set_profile(profile)
        elif record.kind == VERDICT_KIND:
            self.registry.record_verdict(str(record.payload["batch_id"]), {**record.payload, "seq": record.seq})
        elif record.kind == MARKER_KIND:
            self._adopt_marker({**record.payload, "marker_seq": record.seq})
        elif record.kind == RELEASE_KIND:
            batch_id = str(record.payload["batch_id"])
            marker = self.registry.marker(batch_id)
            if marker is not None:
                self.registry.mark(
                    marker.released(seq=record.seq, reason=str(record.payload.get("reason", ""))),
                    replace=True,
                )

    def rebuild(self) -> None:
        """Recreate all in-memory load state from visible records."""

        self._rebuild_registry()

    def status(self) -> dict[str, Any]:
        status = self.registry.describe()
        status["current_criteria"] = self._current_payload()
        return status

    def _mark_from_verdict(
        self,
        *,
        batch_id: str,
        use: str,
        grade: str,
        criteria: LoadCriteria,
        verdict_payload: Mapping[str, Any],
        reason: str = "load profile abnormal",
    ) -> dict[str, Any]:
        if not bool(verdict_payload.get("abnormal", False)):
            raise ValidationError("a normal batch cannot carry an abnormal marker", batch_id=batch_id)
        disposition = disposition_for(criteria.dispositions, use=use, grade=grade)
        existing = self.registry.marker(batch_id)
        if existing is not None and existing.blocked:
            if existing.use == use and existing.disposition == disposition:
                return existing.describe()
            raise ValidationError("batch already carries a different marker", batch_id=batch_id)
        criteria_seq = verdict_payload.get("criteria_seq")
        verdict_seq = verdict_payload.get("seq")
        audit_entry, audit_record = self._stage_audit(
            "load.marked",
            {
                "batch_id": batch_id,
                "use": use,
                "grade": grade,
                "disposition": disposition,
                "verdict_seq": verdict_seq,
                "criteria_seq": criteria_seq,
                "reason": reason,
            },
        )
        marker_stage = self.store.stage(
            MARKER_KIND,
            self.origin,
            self.generation(),
            marker_record(
                batch_id=batch_id,
                disposition=disposition,
                use=use,
                grade=grade,
                criteria_generation=int(verdict_payload.get("criteria_generation", self.generation())),
                criteria_seq=criteria_seq,
                verdict_seq=verdict_seq,
                reason=reason,
            ),
        )
        payload = {**marker_stage.payload, "seq": marker_stage.seq, "marker_seq": marker_stage.seq}
        marker = BatchMarker(
            batch_id=batch_id,
            disposition=disposition,
            use=use,
            grade=grade,
            state=BLOCKED,
            criteria_generation=int(verdict_payload.get("criteria_generation", self.generation())),
            criteria_seq=criteria_seq,
            verdict_seq=verdict_seq,
            marker_seq=marker_stage.seq,
            reason=reason,
        )
        self.store.flush()
        self.store.commit_through(marker_stage.seq)
        self.registry.mark(marker)
        self.audit.accept_staged(audit_entry)
        self.emit("load.marked", payload)
        return payload

    def _release_segment_targets(self, batch_id: str, release_seq: int) -> list[int]:
        targets: list[int] = []
        for record in self.store.committed_records():
            if record.seq < release_seq or record.is_tombstone or record.kind == ENTRY_KIND:
                continue
            if self._belongs_to_release_segment(record, batch_id):
                targets.append(record.seq)
        return targets

    def _belongs_to_release_segment(self, record, batch_id: str) -> bool:
        if record.kind.startswith("load."):
            return record.kind == RELEASE_KIND and record.payload.get("batch_id") == batch_id
        if record.kind.startswith("chute."):
            if record.payload.get("batch_id") == batch_id:
                return True
            if record.kind == "chute.manifest":
                loads = record.payload.get("loads", ())
                return any(load.get("batch_id") == batch_id for load in loads)
            return False
        if record.kind == "mix.phase":
            return self.chute_open_batch_id() == batch_id
        if record.kind in {"pump.run", "pump.stroke", "sampler.draw", "sampler.record", "sampler.batchset"}:
            return True
        return False

    def chute_open_batch_id(self) -> str | None:
        door = self.payload_entry("chute", "door")
        if door is None or not door.get("active", False):
            return None
        return str(door.get("batch_id", "")) or None

    def _released_segment_for_untrusted_criteria(self, trusted_seq: int) -> list[int]:
        """Return the current release segment that depended on newer criteria."""

        batch = self.context.batches.latest()
        if batch is None:
            return []
        marker = self.registry.marker(batch.batch_id)
        if marker is None or marker.release_seq is None or marker.criteria_seq is None:
            return []
        if marker.criteria_seq <= trusted_seq:
            return []
        return self._release_segment_targets(batch.batch_id, marker.release_seq)

    def _rewind_released_segments(self, targets: list[int]) -> list[str]:
        if not targets:
            return []
        batch = self.context.batches.latest()
        if batch is None:
            return []
        self._rewind_discharge(batch.batch_id)
        return [batch.batch_id]

    def _rewind_discharge(self, batch_id: str) -> None:
        self._rebuild_registry()
        if self.chute_service is not None:
            self.chute_service.rollback_to_blocked(batch_id)
        if self.machine.is_at("discharged"):
            self.rewind("mixed", "abnormal batch release rolled back; discharge blocked again")

    def _rebuild_registry(self) -> None:
        self.registry.reset()
        for record in self.store.visible():
            self.replay_record(record)

    def _adopt_marker(self, payload: Mapping[str, Any]) -> None:
        state = str(payload.get("state", BLOCKED))
        marker = BatchMarker(
            batch_id=str(payload["batch_id"]),
            disposition=str(payload["disposition"]),
            use=str(payload["use"]),
            grade=str(payload.get("grade", "")),
            state=state,
            criteria_generation=int(payload.get("criteria_generation", 0)),
            criteria_seq=payload.get("criteria_seq"),
            verdict_seq=payload.get("verdict_seq"),
            marker_seq=int(payload.get("marker_seq", payload.get("seq", 0))),
            release_seq=payload.get("release_seq"),
            reason=str(payload.get("reason", "")),
        )
        self.registry.mark(marker, replace=True)

    def _current_criteria(self) -> LoadCriteria:
        payload = self._current_payload()
        return load_criteria_from_mapping(payload, fallback=default_criteria())

    def _criteria_with_payload(self) -> tuple[LoadCriteria, Mapping[str, Any]]:
        payload = self._current_payload()
        return load_criteria_from_mapping(payload, fallback=default_criteria()), payload

    def _current_payload(self) -> Mapping[str, Any]:
        record = self.context.view.latest(CRITERIA_KIND)
        if record is not None:
            payload = {**record.payload, "seq": record.seq}
            self.registry.set_criteria(payload)
            return payload
        payload = self.registry.criteria()
        if payload is not None:
            return payload
        configured = self.context.config.load_monitoring.criteria_mapping()
        return default_criteria().describe() if not configured else dict(configured)

    def _batch(self, batch_id: str | None):
        if batch_id is None:
            batch = self.context.batches.latest()
            if batch is None:
                raise NotFoundError("declare a batch before recording mixer load")
            return batch
        return self.context.batches.require(batch_id)

    def _audit_note(self, name: str, detail: Mapping[str, Any]) -> None:
        self.audit.note(name, dict(detail))

    def _stage_audit(self, name: str, detail: Mapping[str, Any]):
        return self.audit.stage_entry(name, dict(detail))

    @staticmethod
    def _release_payload(marker: BatchMarker) -> dict[str, Any]:
        return {
            "active": not marker.blocked,
            "batch_id": marker.batch_id,
            "state": marker.state,
            "release_seq": marker.release_seq,
            "reason": marker.reason,
        }
