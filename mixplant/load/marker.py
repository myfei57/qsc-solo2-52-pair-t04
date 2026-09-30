"""The batch marker and its in-memory registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import DuplicateError, NotFoundError, ValidationError
from .criteria import REWORK, SCRAP

MARK_SCOPE = "batch"
MARKER_EFFECT = "discharge_gate_only"
BLOCKED = "blocked"
RELEASED = "released"


@dataclass(frozen=True, slots=True)
class BatchMarker:
    """A whole-batch quality marker whose physical effect is discharge blocking."""

    batch_id: str
    disposition: str
    use: str
    grade: str
    state: str
    criteria_generation: int
    criteria_seq: int | None
    verdict_seq: int | None
    marker_seq: int
    release_seq: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.batch_id:
            raise ValidationError("a marker needs a batch identity")
        if self.disposition not in (SCRAP, REWORK):
            raise ValidationError("unknown marker disposition", disposition=self.disposition)
        if not self.use:
            raise ValidationError("a marker needs a declared use")
        if self.state not in (BLOCKED, RELEASED):
            raise ValidationError("unknown marker state", state=self.state)

    @property
    def blocked(self) -> bool:
        return self.state == BLOCKED

    def released(self, *, seq: int, reason: str) -> "BatchMarker":
        return BatchMarker(
            batch_id=self.batch_id,
            disposition=self.disposition,
            use=self.use,
            grade=self.grade,
            state=RELEASED,
            criteria_generation=self.criteria_generation,
            criteria_seq=self.criteria_seq,
            verdict_seq=self.verdict_seq,
            marker_seq=self.marker_seq,
            release_seq=seq,
            reason=reason,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "active": self.state == BLOCKED,
            "blocked": self.blocked,
            "state": self.state,
            "scope": MARK_SCOPE,
            "effect": MARKER_EFFECT,
            "batch_id": self.batch_id,
            "disposition": self.disposition,
            "use": self.use,
            "grade": self.grade,
            "criteria_generation": self.criteria_generation,
            "criteria_seq": self.criteria_seq,
            "verdict_seq": self.verdict_seq,
            "marker_seq": self.marker_seq,
            "release_seq": self.release_seq,
            "reason": self.reason,
        }


def marker_record(
    *,
    batch_id: str,
    disposition: str,
    use: str,
    grade: str,
    criteria_generation: int,
    criteria_seq: int | None,
    verdict_seq: int | None,
    reason: str,
) -> dict[str, Any]:
    """Build the durable payload for one abnormal batch marker."""

    marker = BatchMarker(
        batch_id=batch_id,
        disposition=disposition,
        use=use,
        grade=grade,
        state=BLOCKED,
        criteria_generation=criteria_generation,
        criteria_seq=criteria_seq,
        verdict_seq=verdict_seq,
        marker_seq=0,
        reason=reason,
    )
    payload = marker.describe()
    payload.pop("marker_seq")
    return payload


class LoadRegistry:
    """Keeps profiles, verdicts, markers and releases keyed by batch."""

    __slots__ = ("_profiles", "_verdicts", "_markers", "_criteria")

    def __init__(self) -> None:
        self._profiles: dict[str, Any] = {}
        self._verdicts: dict[str, Mapping[str, Any]] = {}
        self._markers: dict[str, BatchMarker] = {}
        self._criteria: Mapping[str, Any] | None = None

    def profile(self, batch_id: str):
        return self._profiles.get(batch_id)

    def require_profile(self, batch_id: str):
        profile = self.profile(batch_id)
        if profile is None:
            raise NotFoundError("no mixer load profile was recorded", batch_id=batch_id)
        return profile

    def set_profile(self, profile: Any) -> Any:
        self._profiles[profile.batch_id] = profile
        return profile

    def record_verdict(self, batch_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._verdicts[batch_id] = dict(payload)
        return payload

    def verdict(self, batch_id: str) -> Mapping[str, Any] | None:
        return self._verdicts.get(batch_id)

    def require_verdict(self, batch_id: str) -> Mapping[str, Any]:
        verdict = self.verdict(batch_id)
        if verdict is None:
            raise NotFoundError("the load profile was never evaluated", batch_id=batch_id)
        return verdict

    def mark(self, marker: BatchMarker, *, replace: bool = False) -> BatchMarker:
        existing = self._markers.get(marker.batch_id)
        if existing is not None and not replace:
            if (
                existing.disposition == marker.disposition
                and existing.use == marker.use
                and existing.state == marker.state
            ):
                return existing
            raise DuplicateError(
                "batch already carries a different abnormal marker",
                batch_id=marker.batch_id,
                existing=existing.describe(),
                requested=marker.describe(),
            )
        self._markers[marker.batch_id] = marker
        return marker

    def marker(self, batch_id: str) -> BatchMarker | None:
        return self._markers.get(batch_id)

    def require_marker(self, batch_id: str) -> BatchMarker:
        marker = self.marker(batch_id)
        if marker is None:
            raise NotFoundError("batch is not marked abnormal", batch_id=batch_id)
        return marker

    def release(self, batch_id: str, *, seq: int, reason: str) -> BatchMarker:
        marker = self.require_marker(batch_id)
        if not marker.blocked:
            return marker
        released = marker.released(seq=seq, reason=reason)
        self._markers[batch_id] = released
        return released

    def is_blocked(self, batch_id: str) -> bool:
        marker = self.marker(batch_id)
        return marker is not None and marker.blocked

    def set_criteria(self, payload: Mapping[str, Any] | None) -> None:
        self._criteria = None if payload is None else dict(payload)

    def criteria(self) -> Mapping[str, Any] | None:
        return self._criteria

    def reset(self) -> None:
        """Drop in-memory state before replaying the visible stream."""

        self._profiles.clear()
        self._verdicts.clear()
        self._markers.clear()
        self._criteria = None

    def batches(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def describe(self) -> dict[str, Any]:
        return {
            "criteria": dict(self._criteria) if self._criteria is not None else None,
            "profiles": {
                profile.batch_id: profile.describe() for profile in self._profiles.values()
            },
            "profile_list": [profile.describe() for profile in self._profiles.values()],
            "verdicts": {batch_id: dict(payload) for batch_id, payload in self._verdicts.items()},
            "markers": [marker.describe() for marker in self._markers.values()],
            "blocked": [batch_id for batch_id, marker in self._markers.items() if marker.blocked],
        }
