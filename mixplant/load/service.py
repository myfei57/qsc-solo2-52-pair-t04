"""Load service: sample per batch, judge the trace, hold and release.

This service is the write side of mixer load monitoring.  It deliberately
keeps three concerns separate:

* the **verdict** (does the trace deviate from normal?) is versioned by the
  criteria generation it was produced with;
* the **mark/quarantine** is a whole-batch fact that blocks the discharge
  door (the only physical effect of a mark);
* the **disposition** (scrap versus rework) is derived from the batch grade,
  i.e. its use, and only frozen when a release is confirmed.

Marking is idempotent: a second mark of a batch already under review is a
no-op and never produces a second hold.  A release followed by "the criteria
turned out untrustworthy" is a partial failure handled by a scoped rollback
that tombstones exactly the batch's verdict/mark/release segment in one
commit and puts the batch back behind the blocked door.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from ..service_base import PlantService
from .criteria import CRITERIA_KIND, LoadCriteria, default_criteria
from .features import LoadSample, sample_from_document, summarize_load
from .registry import HoldRegistry
from .verdict import INSUFFICIENT, judge

CRITERIA_SUBJECT = "load:criteria"
GATE_LEAF = "gate"
MARK_LEAF_PREFIX = "hold_"
RELEASE_LEAF_PREFIX = "release_"


def leaf_for(prefix: str, batch_id: str) -> str:
    """Encode a batch id into a single record-leaf token."""

    token = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(batch_id))
    return f"{prefix}{token}"


class LoadService(PlantService):
    """Owns the versioned criteria, the per-batch holds and the load gate."""

    origin = "load"
    line = "mix"
    subject = "load"

    def __init__(self, context, audit=None, machine=None) -> None:
        super().__init__(context, machine)
        self.audit = audit
        self.registry = HoldRegistry()
        self.criteria = default_criteria()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ samples

    def record_load(self, *, batch_id: str, power_kw: float, tick: int | None = None) -> dict[str, Any]:
        """Append one motor load sample for one batch."""

        with self._lock:
            self.context.batches.require(batch_id)
            if power_kw < 0:
                from ..errors import ValidationError

                raise ValidationError("motor load must not be negative", power_kw=power_kw)
            index = len(self._samples(batch_id)) + 1
            when = self.context.clock.now() if tick is None else tick
            sample = LoadSample(batch_id=batch_id, tick=when, power_kw=power_kw, index=index)
            self.publish("load.reading", {**sample.describe()})
            self.emit("load.sampled", sample.describe())
            return {"sample": sample.describe(), "samples": index}

    def record_loads(self, *, batch_id: str, powers: list[float]) -> dict[str, Any]:
        """Append several samples in order (one tick each)."""

        if not powers:
            from ..errors import ValidationError

            raise ValidationError("at least one load sample is required")
        with self._lock:
            for power in powers:
                self.record_load(batch_id=batch_id, power_kw=float(power))
            return {"batch_id": batch_id, "samples": len(self._samples(batch_id))}

    def _samples(self, batch_id: str) -> tuple[LoadSample, ...]:
        records = self.store.history("load.reading")
        samples = []
        for position, record in enumerate(records, start=1):
            if str(record.payload.get("batch_id")) == batch_id:
                samples.append(sample_from_document(record.payload, index=position))
        return tuple(samples)

    # ---------------------------------------------------------------- criteria

    def criteria_generation(self) -> int:
        return self.context.versions.current_generation(CRITERIA_SUBJECT)

    def publish_criteria(self, **changes: Any) -> dict[str, Any]:
        """Publish a new, immutable criteria generation."""

        with self._lock:
            token = self.context.versions.bump(CRITERIA_SUBJECT, tick=self.context.clock.now())
            current = self.criteria
            candidate = LoadCriteria(
                version=current.version + 1,
                rated_power_kw=float(changes.get("rated_power_kw", current.rated_power_kw)),
                overload_pct=float(changes.get("overload_pct", current.overload_pct)),
                stall_pct=float(changes.get("stall_pct", current.stall_pct)),
                stall_fraction=float(changes.get("stall_fraction", current.stall_fraction)),
                overload_run=int(changes.get("overload_run", current.overload_run)),
                deviate_pct=float(changes.get("deviate_pct", current.deviate_pct)),
                min_samples=int(changes.get("min_samples", current.min_samples)),
            )
            document = candidate.to_document()
            self.publish(CRITERIA_KIND, document)
            self.criteria = candidate
            self.context.versions.generations.adopt(CRITERIA_SUBJECT, candidate.version)
            self.emit("load.criteria_published", {"version": candidate.version})
            self._audit(
                "load.criteria_published",
                {"version": candidate.version, "criteria": candidate.describe()},
            )
            return {"criteria": candidate.describe(), "generation": candidate.version}

    def adopt_criteria(self, criteria: LoadCriteria) -> None:
        """Adopt a criteria document rebuilt from the stream."""

        self.criteria = criteria
        self.context.versions.generations.adopt(CRITERIA_SUBJECT, criteria.version)

    # ----------------------------------------------------------------- judging

    def judge_batch(
        self,
        batch_id: str,
        *,
        criteria: LoadCriteria | None = None,
        auto_hold: bool = True,
    ) -> dict[str, Any]:
        """Evaluate a batch's trace; quarantine it when the trace is abnormal."""

        with self._lock:
            self.context.batches.require(batch_id)
            bounds = criteria or self.criteria
            samples = self._samples(batch_id)
            features = summarize_load(batch_id=batch_id, samples=samples, criteria=bounds)
            verdict = judge(features=features, criteria=bounds)
            verdict_record = self.publish(
                "load.verdict",
                verdict.describe(),
            )
            self.emit("load.judged", {"batch_id": batch_id, "abnormal": verdict.abnormal,
                                      "code": verdict.code, "criteria_version": bounds.version})
            self._audit(
                "load.judged",
                {"batch_id": batch_id, "verdict": verdict.describe(include_samples=False)},
            )
            result = {"verdict": verdict.describe(), "seq": verdict_record.seq}
            if verdict.abnormal and auto_hold:
                grade = self.context.batches.require(batch_id).grade
                marked = self._apply_hold(
                    batch_id,
                    grade=grade,
                    reason="auto",
                    triggers=verdict.reasons,
                    criteria_version=bounds.version,
                    verdict_seq=verdict_record.seq,
                )
                result["hold"] = marked
            elif verdict.code == INSUFFICIENT:
                self.raise_alarm(
                    "load.judgement_inconclusive",
                    {"batch_id": batch_id, "samples": features.count, "min_samples": bounds.min_samples},
                )
            return result

    def mark_batch(
        self,
        batch_id: str,
        *,
        reason: str = "operator",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Manually quarantine a whole batch; repeat marks are idempotent."""

        with self._lock:
            grade = self.context.batches.require(batch_id).grade
            hold = self._apply_hold(
                batch_id,
                grade=grade,
                reason=reason,
                triggers=("operator",),
                criteria_version=self.criteria.version,
                idempotency_key=idempotency_key,
            )
            return {"hold": hold}

    def _apply_hold(
        self,
        batch_id: str,
        *,
        grade: str,
        reason: str,
        triggers: tuple[str, ...],
        criteria_version: int,
        verdict_seq: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Insert-if-absent quarantine plus a single blocking gate leaf."""

        entry, created = self.registry.mark(
            batch_id,
            grade=grade,
            tick=self.context.clock.now(),
            reason=reason,
            triggers=triggers,
            criteria_version=criteria_version,
            verdict_seq=verdict_seq,
            idempotency_key=idempotency_key,
        )
        if not created:
            # A repeat mark is a duplicate, not a second quarantine: answer the
            # caller with the existing hold and append nothing but an audit note.
            self._audit("load.mark_deduplicated", {"batch_id": batch_id,
                                                    "idempotency_key": idempotency_key})
            return entry.describe()
        hold_record = self.publish(
            "load." + leaf_for(MARK_LEAF_PREFIX, batch_id),
            entry.describe(),
        )
        self.registry.adopt(
            replace(entry, mark_seq=hold_record.seq)
        )
        self._publish_gate(blocked=True, batch_id=batch_id)
        self.emit("load.marked", {"batch_id": batch_id, "triggers": list(triggers),
                                   "disposition": entry.disposition})
        self.raise_alarm(
            "load.batch_quarantined",
            {"batch_id": batch_id, "triggers": list(triggers), "disposition": entry.disposition},
        )
        self._audit("load.marked", {"batch_id": batch_id, "hold": self.registry.require(batch_id).describe()})
        return self.registry.require(batch_id).describe()

    # ---------------------------------------------------------------- release

    def release_batch(
        self,
        batch_id: str,
        *,
        issuer: str = "operator",
        disposition: str | None = None,
    ) -> dict[str, Any]:
        """Confirm the release and freeze the grade-based disposition."""

        with self._lock:
            entry = self.registry.release(
                batch_id, tick=self.context.clock.now(), issuer=issuer, disposition=disposition
            )
            record = self.publish(
                "load." + leaf_for(RELEASE_LEAF_PREFIX, batch_id),
                {
                    "active": True,
                    "batch_id": batch_id,
                    "state": entry.state,
                    "disposition": entry.disposition,
                    "issuer": issuer,
                    "criteria_version": entry.criteria_version,
                },
            )
            # The release leaf is the newest per-batch load record, so the
            # derived state carries the released flag the console shows.
            self.publish(
                "load." + leaf_for(MARK_LEAF_PREFIX, batch_id),
                entry.describe(),
            )
            self._publish_gate(blocked=False, batch_id=batch_id)
            self.emit("load.released", {"batch_id": batch_id, "disposition": entry.disposition,
                                         "issuer": issuer, "seq": record.seq})
            self._audit("load.released", {"batch_id": batch_id, "release": {
                "seq": record.seq,
                "disposition": entry.disposition,
                "issuer": issuer,
            }})
            return {"release": entry.describe(), "seq": record.seq}

    # --------------------------------------------------------------- rollback

    def rollback_batch(
        self,
        batch_id: str,
        *,
        criteria_version: int | None = None,
        reason: str = "criteria untrusted after release",
        issuer: str = "operator",
    ) -> dict[str, Any]:
        """Roll a batch's whole verdict/mark/release segment back to blocked.

        Partial failure handling: the segment is neutralised in a single
        commit, the discharge gate is put back in the blocking position and
        the recovery is audited.  Nothing here can silently release material.
        """

        with self._lock:
            self.context.batches.require(batch_id)
            wanted = self.registry.find(batch_id)
            version = criteria_version or (wanted.criteria_version if wanted else self.criteria.version)
            segment = self._segment_seqs(batch_id, criteria_version=version)
            if not segment:
                from ..errors import NotFoundError

                raise NotFoundError("no load segment to roll back for batch", batch_id=batch_id)
            tombstones = self.store.tombstone_many(
                segment,
                reason=reason,
                origin=self.origin,
                generation=self.criteria_generation(),
            )
            reopened = self._reblock(batch_id, reason=reason, last_seq=tombstones[-1].seq)
            self.emit("load.rolled_back", {"batch_id": batch_id, "tombstones": list(segment),
                                            "reason": reason, "issuer": issuer})
            self._audit("load.rolled_back", {"batch_id": batch_id, "reason": reason, "issuer": issuer,
                                              "tombstones": list(segment),
                                              "tombstone_seq": tombstones[-1].seq,
                                              "hold": reopened})
            return {
                "batch_id": batch_id,
                "tombstones": list(segment),
                "tombstone_seq": tombstones[-1].seq,
                "hold": reopened,
            }

    def rollback_criteria(
        self,
        version: int,
        *,
        reason: str = "criteria generation judged untrustworthy",
        issuer: str = "operator",
    ) -> dict[str, Any]:
        """Roll every batch judged at a criteria generation back to blocked."""

        with self._lock:
            affected = sorted(
                {
                    str(record.payload.get("batch_id"))
                    for record in self.store.history("load.verdict")
                    if int(record.payload.get("criteria_version", 0)) == version
                    and record.payload.get("batch_id")
                }
            )
            if not affected:
                from ..errors import NotFoundError

                raise NotFoundError("no verdicts use the criteria generation", version=version)
            results = []
            for batch_id in affected:
                results.append(
                    self.rollback_batch(
                        batch_id, criteria_version=version, reason=reason, issuer=issuer
                    )
                )
            return {"criteria_version": version, "batches": affected, "rollbacks": results}

    def _segment_seqs(self, batch_id: str, *, criteria_version: int) -> list[int]:
        """Collect the surviving verdict/hold/release records of one batch."""

        seqs: list[int] = []
        for kind in ("load.verdict", "load." + leaf_for(MARK_LEAF_PREFIX, batch_id),
                     "load." + leaf_for(RELEASE_LEAF_PREFIX, batch_id)):
            for record in self.store.history(kind):
                if str(record.payload.get("batch_id")) != batch_id:
                    continue
                if int(record.payload.get("criteria_version", 0)) == criteria_version:
                    seqs.append(record.seq)
        return sorted(seqs)

    def _reblock(self, batch_id: str, *, reason: str, last_seq: int) -> dict[str, Any]:
        """Put a batch back behind the blocking door after a rollback."""

        grade = self.context.batches.require(batch_id).grade
        previous = self.registry.find(batch_id)
        if previous is None:
            entry, _ = self.registry.mark(
                batch_id,
                grade=grade,
                tick=self.context.clock.now(),
                reason=f"rollback: {reason}",
                triggers=("rollback_safety_hold",),
                criteria_version=self.criteria.version,
            )
        else:
            entry = self.registry.reopen(previous)
            entry = replace(entry, rollback_seq=last_seq)
            self.registry.adopt(entry)
        self.publish(
            "load." + leaf_for(MARK_LEAF_PREFIX, batch_id),
            entry.describe(),
        )
        self._publish_gate(blocked=True, batch_id=batch_id)
        self.raise_alarm(
            "load.release_rolled_back",
            {"batch_id": batch_id, "reason": reason, "tombstone_seq": last_seq},
        )
        return entry.describe()

    # ------------------------------------------------------------------ views

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        """Return the verdict and quarantine view the console renders."""

        samples = [item.describe() for item in self._samples(batch_id)]
        verdict = None
        for record in reversed(self.store.history("load.verdict")):
            if str(record.payload.get("batch_id")) == batch_id:
                verdict = dict(record.payload)
                break
        entry = self.registry.find(batch_id)
        return {
            "batch_id": batch_id,
            "criteria": self.criteria.describe(),
            "sample_count": len(samples),
            "samples": samples,
            "verdict": verdict,
            "hold": None if entry is None else entry.describe(),
            "blocked": self.registry.is_blocked(batch_id),
        }

    def status(self) -> dict[str, Any]:
        blocked = self.registry.blocked_batch()
        return {
            "criteria": self.criteria.describe(),
            "blocked_batch": blocked,
            "blocked": blocked is not None,
            "holds": [entry.describe() for entry in self.registry.all()],
            "sampled_batches": self._sampled_batch_ids(),
        }

    def _sampled_batch_ids(self) -> list[str]:
        ids: list[str] = []
        for record in self.store.history("load.reading"):
            batch_id = str(record.payload.get("batch_id"))
            if batch_id not in ids:
                ids.append(batch_id)
        return ids

    def is_blocked(self, batch_id: str | None) -> bool:
        return bool(batch_id) and self.registry.is_blocked(batch_id)

    def has_samples(self, batch_id: str) -> bool:
        """Return whether any load sample was recorded for the batch."""

        return any(
            str(record.payload.get("batch_id")) == batch_id
            for record in self.store.history("load.reading")
        )

    def reset_gate(self, batch_id: str) -> None:
        """Clear the blocking gate when a new mixing cycle starts."""

        blocked = self.registry.blocked_batch()
        if blocked and blocked != batch_id:
            # An older batch is still under review; it keeps blocking the door.
            self._publish_gate(blocked=True, batch_id=blocked)
            return
        if blocked == batch_id:
            return
        self._publish_gate(blocked=False, batch_id="")

    # ----------------------------------------------------------------- helpers

    def _publish_gate(self, *, blocked: bool, batch_id: str) -> None:
        self.publish(
            "load." + GATE_LEAF,
            {"active": blocked, "batch_id": batch_id if blocked else "", "blocked": blocked},
        )

    def _audit(self, name: str, detail: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.note(name, detail)
