"""Per-batch quarantine state and the grade based disposition table.

The mark is **whole-batch**: once a batch is quarantined the batch is
``under_review`` and its discharge door is blocked.  Quarantine never decides
by itself whether the material is scrapped or reworked; that disposition comes
from a table keyed by the batch's grade (its use) and is only applied when an
operator confirms the release.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import DuplicateError, NotFoundError, ValidationError

UNDER_REVIEW = "under_review"
RELEASED = "released"

SCRAP = "scrap"
REWORK = "rework"

# What a confirmed abnormal batch becomes depends on its use (its grade).
# Structural concrete is scrapped: a questionable load must never reach a pour.
# Lower/non structural grades are sent back through the drum for rework.
DISPOSITION_BY_GRADE: Mapping[str, str] = {
    "C40": SCRAP,
    "C30": SCRAP,
    "C20": REWORK,
}
DEFAULT_DISPOSITION = REWORK

QUARANTINE = "quarantine"


def disposition_for(grade: str) -> str:
    """Return the disposition policy a grade implies for an abnormal batch."""

    return DISPOSITION_BY_GRADE.get(grade, DEFAULT_DISPOSITION)


@dataclass(frozen=True, slots=True)
class HoldEntry:
    """The whole-batch quarantine record for one batch."""

    batch_id: str
    grade: str
    state: str = UNDER_REVIEW
    tick: int = 0
    reason: str = ""
    triggers: tuple[str, ...] = field(default_factory=tuple)
    criteria_version: int = 0
    disposition: str = ""
    mark_seq: int | None = None
    verdict_seq: int | None = None
    release_tick: int | None = None
    release_issuer: str | None = None
    rollback_seq: int | None = None
    idempotency_key: str | None = None

    def is_blocked(self) -> bool:
        return self.state == UNDER_REVIEW

    def describe(self) -> dict[str, Any]:
        return {
            "active": self.state == UNDER_REVIEW,
            "batch_id": self.batch_id,
            "grade": self.grade,
            "state": self.state,
            "tick": self.tick,
            "reason": self.reason,
            "triggers": list(self.triggers),
            "criteria_version": self.criteria_version,
            "disposition": self.disposition,
            "mark_seq": self.mark_seq,
            "verdict_seq": self.verdict_seq,
            "release_tick": self.release_tick,
            "release_issuer": self.release_issuer,
            "rollback_seq": self.rollback_seq,
            "idempotency_key": self.idempotency_key,
        }


class HoldRegistry:
    """One hold entry per batch; marking an already held batch is a no-op."""

    __slots__ = ("_entries", "_keys")

    def __init__(self) -> None:
        self._entries: dict[str, HoldEntry] = {}
        self._keys: dict[str, str] = {}

    def mark(
        self,
        batch_id: str,
        *,
        grade: str,
        tick: int,
        reason: str,
        triggers: tuple[str, ...],
        criteria_version: int,
        mark_seq: int | None = None,
        verdict_seq: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[HoldEntry, bool]:
        """Quarantine a batch; return the entry and whether it was newly made."""

        existing = self._entries.get(batch_id)
        if existing is not None and existing.state == UNDER_REVIEW:
            if idempotency_key and existing.idempotency_key and idempotency_key != existing.idempotency_key:
                raise DuplicateError(
                    "batch is already quarantined under another request",
                    batch_id=batch_id,
                )
            if idempotency_key:
                self._keys.setdefault(idempotency_key, batch_id)
            return existing, False
        if existing is not None and existing.state == RELEASED:
            raise ValidationError("a released batch cannot be re-quarantined", batch_id=batch_id)
        if idempotency_key:
            owner = self._keys.get(idempotency_key)
            if owner is not None and owner != batch_id:
                raise DuplicateError("idempotency key was already spent on another batch", key=idempotency_key)
            self._keys[idempotency_key] = batch_id
        entry = HoldEntry(
            batch_id=batch_id,
            grade=grade,
            state=UNDER_REVIEW,
            tick=tick,
            reason=reason,
            triggers=tuple(triggers),
            criteria_version=criteria_version,
            disposition=disposition_for(grade),
            mark_seq=mark_seq,
            verdict_seq=verdict_seq,
            idempotency_key=idempotency_key,
        )
        self._entries[batch_id] = entry
        return entry, True

    def release(
        self,
        batch_id: str,
        *,
        tick: int,
        issuer: str,
        disposition: str | None = None,
    ) -> HoldEntry:
        """Confirm the release and freeze the grade based disposition."""

        entry = self.require(batch_id)
        if entry.state != UNDER_REVIEW:
            raise ValidationError("only a held batch can be released", batch_id=batch_id, state=entry.state)
        if disposition is not None and disposition not in (SCRAP, REWORK):
            raise ValidationError("disposition must be scrap or rework", disposition=disposition)
        object.__setattr__(
            entry,
            "state",
            RELEASED,
        )
        object.__setattr__(entry, "release_tick", tick)
        object.__setattr__(entry, "release_issuer", issuer)
        object.__setattr__(entry, "disposition", disposition or entry.disposition)
        return entry

    def reopen(self, entry: HoldEntry) -> HoldEntry:
        """Put a released batch back into quarantine during a rollback."""

        restored = HoldEntry(
            batch_id=entry.batch_id,
            grade=entry.grade,
            state=UNDER_REVIEW,
            tick=entry.tick,
            reason=entry.reason,
            triggers=entry.triggers,
            criteria_version=entry.criteria_version,
            disposition=entry.disposition,
            mark_seq=entry.mark_seq,
            verdict_seq=entry.verdict_seq,
            release_tick=None,
            release_issuer=None,
            rollback_seq=entry.rollback_seq,
            idempotency_key=entry.idempotency_key,
        )
        self._entries[entry.batch_id] = restored
        return restored

    def adopt(self, entry: HoldEntry) -> HoldEntry:
        """Adopt a hold rebuilt from the stream after a restart."""

        self._entries[entry.batch_id] = entry
        if entry.idempotency_key:
            self._keys[entry.idempotency_key] = entry.batch_id
        return entry

    def find(self, batch_id: str) -> HoldEntry | None:
        return self._entries.get(batch_id)

    def require(self, batch_id: str) -> HoldEntry:
        entry = self._entries.get(batch_id)
        if entry is None:
            raise NotFoundError("batch has no quarantine record", batch_id=batch_id)
        return entry

    def is_blocked(self, batch_id: str) -> bool:
        entry = self._entries.get(batch_id)
        return entry is not None and entry.is_blocked()

    def blocked_batch(self) -> str | None:
        """Return the batch that currently keeps the discharge door blocked."""

        for entry in self._entries.values():
            if entry.state == UNDER_REVIEW:
                return entry.batch_id
        return None

    def all(self) -> tuple[HoldEntry, ...]:
        return tuple(self._entries.values())
