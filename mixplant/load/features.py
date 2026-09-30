"""Load traces and the features the judge needs.

A reading is one motor power sample taken while the mixer runs.  The trace is
folded into a handful of features once the mixing window closes so the judge
never has to understand the raw sampling cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..errors import ValidationError
from .criteria import LoadCriteria


@dataclass(frozen=True, slots=True)
class LoadSample:
    """One motor load sample belonging to one batch."""

    batch_id: str
    tick: int
    power_kw: float
    index: int

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "tick": self.tick,
            "power_kw": self.power_kw,
            "index": self.index,
        }


@dataclass(frozen=True, slots=True)
class LoadFeatures:
    """The shape of one batch's trace in a handful of numbers."""

    batch_id: str
    count: int
    peak_kw: float
    mean_kw: float
    stall_fraction: float
    overload_run: int
    deviates: bool
    samples: tuple[float, ...]

    def describe(self, *, include_samples: bool = True) -> dict[str, Any]:
        document = {
            "batch_id": self.batch_id,
            "count": self.count,
            "peak_kw": self.peak_kw,
            "mean_kw": self.mean_kw,
            "stall_fraction": self.stall_fraction,
            "overload_run": self.overload_run,
            "deviates": self.deviates,
        }
        if include_samples:
            document["samples"] = list(self.samples)
        return document


def _longest_run(values: Iterable[bool]) -> int:
    best = run = 0
    for flag in values:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


def summarize_load(
    *,
    batch_id: str,
    samples: Iterable[LoadSample],
    criteria: LoadCriteria,
) -> LoadFeatures:
    """Fold a batch's samples into the features the judge reads."""

    powers = tuple(round(item.power_kw, 4) for item in samples if item.batch_id == batch_id)
    if not powers:
        raise ValidationError("a batch needs load samples before it can be judged", batch_id=batch_id)
    count = len(powers)
    mean_kw = round(sum(powers) / count, 4)
    peak_kw = round(max(powers), 4)
    stall_fraction = round(sum(1 for value in powers if value <= criteria.stall_kw) / count, 4)
    overload_run = _longest_run(value >= criteria.overload_kw for value in powers)
    deviates = abs(mean_kw - criteria.rated_power_kw) > criteria.deviation_band_kw
    return LoadFeatures(
        batch_id=batch_id,
        count=count,
        peak_kw=peak_kw,
        mean_kw=mean_kw,
        stall_fraction=stall_fraction,
        overload_run=overload_run,
        deviates=deviates,
        samples=powers,
    )


def sample_from_document(document: Mapping[str, Any], *, index: int) -> LoadSample:
    """Rebuild one sample from a stored ``load.reading`` payload."""

    return LoadSample(
        batch_id=str(document["batch_id"]),
        tick=int(document.get("tick", 0)),
        power_kw=float(document["power_kw"]),
        index=int(document.get("index", index)),
    )
