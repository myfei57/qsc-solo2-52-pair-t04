"""The pure decision: does this batch's load trace deviate from normal?"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ValidationError
from .criteria import LoadCriteria
from .features import LoadFeatures

OK = "ok"
ABNORMAL = "abnormal"
INSUFFICIENT = "insufficient_data"

STALL = "stall"
SUSTAINED_OVERLOAD = "sustained_overload"
SHAPE_DEVIATION = "shape_deviation"
INSUFFICIENT_DATA = "insufficient_data"

REASON_MESSAGES = {
    STALL: "motor spent too much of the window at stall torque (caked or choked)",
    SUSTAINED_OVERLOAD: "load stayed over the overload band for too many samples (overloaded)",
    SHAPE_DEVIATION: "mean load sits outside the normal running band",
    INSUFFICIENT_DATA: "not enough load samples for a trustworthy verdict",
}


@dataclass(frozen=True, slots=True)
class LoadVerdict:
    """One versioned judgement over one batch's load features."""

    batch_id: str
    criteria_version: int
    code: str
    abnormal: bool
    reasons: tuple[str, ...]
    features: LoadFeatures

    def reason_detail(self) -> list[dict[str, Any]]:
        return [{"code": code, "message": REASON_MESSAGES.get(code, code)} for code in self.reasons]

    def describe(self, *, include_samples: bool = True) -> dict[str, Any]:
        return {
            "active": True,
            "batch_id": self.batch_id,
            "criteria_version": self.criteria_version,
            "code": self.code,
            "abnormal": self.abnormal,
            "reasons": list(self.reasons),
            "reason_detail": self.reason_detail(),
            "features": self.features.describe(include_samples=include_samples),
        }


def judge(*, features: LoadFeatures, criteria: LoadCriteria) -> LoadVerdict:
    """Evaluate the features against one immutable criteria document."""

    if not features.batch_id:
        raise ValidationError("a verdict needs a batch identity")
    reasons: list[str] = []
    if features.count < criteria.min_samples:
        reasons.append(INSUFFICIENT_DATA)
        return LoadVerdict(
            batch_id=features.batch_id,
            criteria_version=criteria.version,
            code=INSUFFICIENT,
            abnormal=False,
            reasons=tuple(reasons),
            features=features,
        )
    if features.stall_fraction >= criteria.stall_fraction:
        reasons.append(STALL)
    if features.overload_run >= criteria.overload_run:
        reasons.append(SUSTAINED_OVERLOAD)
    if features.deviates:
        reasons.append(SHAPE_DEVIATION)
    return LoadVerdict(
        batch_id=features.batch_id,
        criteria_version=criteria.version,
        code=ABNORMAL if reasons else OK,
        abnormal=bool(reasons),
        reasons=tuple(reasons),
        features=features,
    )
