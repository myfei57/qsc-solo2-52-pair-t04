"""Versioned criteria used to decide whether a mixer load profile is abnormal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError

SCRAP = "scrap"
REWORK = "rework"
DISPOSITIONS = (SCRAP, REWORK)
WILDCARD = "*"


@dataclass(frozen=True, slots=True)
class LoadCriteria:
    """The numeric bounds and disposition policy for one criteria generation."""

    max_load_kw: float
    stall_drop_ratio: float
    stall_min_points: int
    ramp_min_delta_kw: float
    ramp_min_load_ratio: float
    ramp_min_points: int
    min_profile_points: int
    dispositions: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.max_load_kw <= 0:
            raise ValidationError("maximum mixer load must be positive", max_load_kw=self.max_load_kw)
        if not 0.0 <= self.stall_drop_ratio < 1.0:
            raise ValidationError(
                "stall drop ratio must be in [0, 1)", stall_drop_ratio=self.stall_drop_ratio
            )
        if self.stall_min_points < 2:
            raise ValidationError("stall detection needs at least two points", stall_min_points=self.stall_min_points)
        if self.ramp_min_delta_kw < 0:
            raise ValidationError("ramp delta must not be negative", ramp_min_delta_kw=self.ramp_min_delta_kw)
        if not 0.0 <= self.ramp_min_load_ratio <= 1.0:
            raise ValidationError(
                "ramp load ratio must be in [0, 1]", ramp_min_load_ratio=self.ramp_min_load_ratio
            )
        if self.ramp_min_points < 2:
            raise ValidationError("ramp detection needs at least two points", ramp_min_points=self.ramp_min_points)
        if self.min_profile_points < 2:
            raise ValidationError("a load profile needs at least two points", min_profile_points=self.min_profile_points)
        for key, disposition in self.dispositions.items():
            self._validate_key(key)
            if disposition not in DISPOSITIONS:
                raise ValidationError("unknown abnormal batch disposition", key=key, disposition=disposition)
        object.__setattr__(self, "dispositions", dict(self.dispositions))

    @staticmethod
    def _validate_key(key: str) -> None:
        if key == WILDCARD:
            return
        if key.count(":") != 1 or not all(part for part in key.split(":")):
            raise ValidationError(
                "disposition key must look like 'use:grade' or '*'", key=key
            )

    def disposition(self, *, use: str, grade: str) -> str:
        """Resolve a use-specific label before applying the grade fallback."""

        return disposition_for(self.dispositions, use=use, grade=grade)

    def describe(self) -> dict[str, Any]:
        return {
            "max_load_kw": self.max_load_kw,
            "stall_drop_ratio": self.stall_drop_ratio,
            "stall_min_points": self.stall_min_points,
            "ramp_min_delta_kw": self.ramp_min_delta_kw,
            "ramp_min_load_ratio": self.ramp_min_load_ratio,
            "ramp_min_points": self.ramp_min_points,
            "min_profile_points": self.min_profile_points,
            "dispositions": dict(self.dispositions),
        }

    def to_payload(self, *, generation: int) -> dict[str, Any]:
        return {"active": True, "generation": generation, **self.describe()}


def default_criteria() -> LoadCriteria:
    """Conservative bounds for a mixer that can be stalled by lumps or overload."""

    return LoadCriteria(
        max_load_kw=55.0,
        stall_drop_ratio=0.25,
        stall_min_points=3,
        ramp_min_delta_kw=6.0,
        ramp_min_load_ratio=0.65,
        ramp_min_points=3,
        min_profile_points=4,
        dispositions={WILDCARD: REWORK, "structural:C30": SCRAP},
    )


def load_criteria_from_mapping(
    document: Mapping[str, Any] | None,
    *,
    fallback: LoadCriteria | None = None,
) -> LoadCriteria:
    """Build criteria from console JSON while leaving omitted bounds at defaults."""

    base = (fallback or default_criteria()).describe()
    if document:
        for key in (
            "max_load_kw",
            "stall_drop_ratio",
            "stall_min_points",
            "ramp_min_delta_kw",
            "ramp_min_load_ratio",
            "ramp_min_points",
            "min_profile_points",
        ):
            if key in document:
                base[key] = document[key]
        raw_dispositions = document.get("dispositions")
        if raw_dispositions is not None:
            if not isinstance(raw_dispositions, Mapping):
                raise ValidationError("dispositions must be a mapping")
            base["dispositions"] = {str(key): str(value) for key, value in raw_dispositions.items()}
    return LoadCriteria(**base)


def disposition_for(dispositions: Mapping[str, str], *, use: str, grade: str) -> str:
    """Look up ``use:grade``, then ``use:*`` and finally the wildcard policy."""

    if not use:
        raise ValidationError("an abnormal batch needs a declared use")
    explicit = dispositions.get(f"{use}:{grade}")
    if explicit is not None:
        return explicit
    use_default = dispositions.get(f"{use}:{WILDCARD}")
    if use_default is not None:
        return use_default
    wildcard = dispositions.get(WILDCARD)
    if wildcard is None:
        raise ValidationError("no disposition policy is configured for this use", use=use, grade=grade)
    return wildcard
