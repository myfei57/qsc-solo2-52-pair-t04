"""Pure evaluation of a mixer load curve."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..errors import ValidationError
from .criteria import LoadCriteria
from .profile import LoadPoint

OVERLOAD = "overload"
STALL_DROP = "stall_drop"
STALLED_RAMP = "stalled_ramp"
INSUFFICIENT = "insufficient_samples"
NORMAL = "normal"


@dataclass(frozen=True, slots=True)
class LoadVerdict:
    """The explainable decision behind an abnormal batch marker."""

    abnormal: bool
    code: str
    reasons: tuple[str, ...]
    peak_load_kw: float
    peak_tick: int | None
    duration_ticks: int
    points: int
    criteria_generation: int
    criteria_seq: int | None
    metrics: Mapping[str, Any]
    warnings: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "abnormal": self.abnormal,
            "code": self.code,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "peak_load_kw": self.peak_load_kw,
            "peak_tick": self.peak_tick,
            "duration_ticks": self.duration_ticks,
            "points": self.points,
            "criteria_generation": self.criteria_generation,
            "criteria_seq": self.criteria_seq,
            "metrics": dict(self.metrics),
        }


def evaluate_load(
    points: Sequence[LoadPoint],
    criteria: LoadCriteria,
    *,
    generation: int = 0,
    criteria_seq: int | None = None,
) -> LoadVerdict:
    """Return an explainable normal or abnormal verdict for a complete curve."""

    if not points:
        raise ValidationError("cannot evaluate an empty mixer load profile")
    ordered = tuple(points)
    peak = max(point.load_kw for point in ordered)
    peak_point = max(ordered, key=lambda point: point.load_kw)
    duration = ordered[-1].tick - ordered[0].tick
    reasons: list[str] = []

    if peak > criteria.max_load_kw:
        reasons.append(OVERLOAD)

    drop = _largest_sustained_drop(
        ordered,
        min_points=criteria.stall_min_points,
        ratio=criteria.stall_drop_ratio,
    )
    if drop is not None:
        reasons.append(STALL_DROP)

    ramp = _failed_sustained_ramp(
        ordered,
        min_points=criteria.ramp_min_points,
        min_delta_kw=criteria.ramp_min_delta_kw,
        load_threshold_kw=criteria.max_load_kw * criteria.ramp_min_load_ratio,
    )
    if ramp is not None:
        reasons.append(STALLED_RAMP)

    warnings: list[str] = []

    insufficient = len(ordered) < criteria.min_profile_points
    if insufficient:
        warnings.append(INSUFFICIENT)

    metrics = {
        "max_load_kw": criteria.max_load_kw,
        "stall_drop": None if drop is None else dict(drop),
        "stalled_ramp": None if ramp is None else dict(ramp),
        "stall_drop_ratio": criteria.stall_drop_ratio,
        "ramp_min_delta_kw": criteria.ramp_min_delta_kw,
        "ramp_load_threshold_kw": criteria.max_load_kw * criteria.ramp_min_load_ratio,
        "min_profile_points": criteria.min_profile_points,
    }
    code = NORMAL
    if reasons:
        code = reasons[0]
    return LoadVerdict(
        abnormal=bool(reasons),
        code=code,
        reasons=tuple(reasons),
        peak_load_kw=round(peak, 4),
        peak_tick=peak_point.tick,
        duration_ticks=duration,
        points=len(ordered),
        criteria_generation=generation,
        criteria_seq=criteria_seq,
        metrics=metrics,
        warnings=tuple(warnings),
    )


def _largest_sustained_drop(
    points: Sequence[LoadPoint],
    *,
    min_points: int,
    ratio: float,
) -> dict[str, Any] | None:
    """Find an early load followed by a sustained lumpy/stall collapse."""

    for index, start in enumerate(points):
        tail = points[index + 1 : index + min_points]
        if len(tail) < min_points - 1 or start.load_kw <= 0:
            continue
        threshold = start.load_kw * (1.0 - ratio)
        if all(point.load_kw <= threshold for point in tail):
            return {
                "from_tick": start.tick,
                "from_load_kw": start.load_kw,
                "threshold_kw": round(threshold, 4),
                "through_tick": tail[-1].tick,
                "samples": len(tail) + 1,
            }
    return None


def _failed_sustained_ramp(
    points: Sequence[LoadPoint],
    *,
    min_points: int,
    min_delta_kw: float,
    load_threshold_kw: float,
) -> dict[str, Any] | None:
    """Flag a flat/high-friction run after the mixer should be gaining load."""

    for start_index in range(len(points) - min_points + 1):
        window = points[start_index : start_index + min_points]
        if (
            window[0].load_kw >= load_threshold_kw
            and window[-1].load_kw - window[0].load_kw < min_delta_kw
        ):
            return {
                "from_tick": window[0].tick,
                "to_tick": window[-1].tick,
                "delta_kw": round(window[-1].load_kw - window[0].load_kw, 4),
                "required_delta_kw": min_delta_kw,
                "load_threshold_kw": round(load_threshold_kw, 4),
                "samples": len(window),
            }
    return None
