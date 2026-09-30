"""Versioned bounds a per-batch load trace is judged against.

A criteria document is immutable: changing the bounds publishes a new version
and bumps the ``load:criteria`` generation.  Verdicts remember the version
they were produced with, so a console can always explain why a batch was
quarantined and an untrusted generation can be rolled back wholesale.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from ..errors import ValidationError

CRITERIA_KIND = "load.criteria"
DEFAULT_CRITERIA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LoadCriteria:
    """The load-shape bounds shared by every batch judged at one version."""

    version: int = DEFAULT_CRITERIA_VERSION
    rated_power_kw: float = 55.0
    overload_pct: float = 1.20
    stall_pct: float = 0.45
    stall_fraction: float = 0.25
    overload_run: int = 3
    deviate_pct: float = 0.35
    min_samples: int = 4

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValidationError("criteria version starts at one", version=self.version)
        if self.rated_power_kw <= 0:
            raise ValidationError("rated power must be positive", rated_power_kw=self.rated_power_kw)
        if not 0 < self.stall_pct < self.overload_pct:
            raise ValidationError(
                "stall band must sit below the overload band",
                stall_pct=self.stall_pct,
                overload_pct=self.overload_pct,
            )
        if not 0 < self.stall_fraction <= 1:
            raise ValidationError("stall fraction must be within (0, 1]", stall_fraction=self.stall_fraction)
        if self.overload_run < 1:
            raise ValidationError("overload run must be positive", overload_run=self.overload_run)
        if self.deviate_pct <= 0:
            raise ValidationError("deviation must be positive", deviate_pct=self.deviate_pct)
        if self.min_samples < 1:
            raise ValidationError("a criteria set needs at least one sample", min_samples=self.min_samples)

    @property
    def overload_kw(self) -> float:
        return round(self.rated_power_kw * self.overload_pct, 4)

    @property
    def stall_kw(self) -> float:
        return round(self.rated_power_kw * self.stall_pct, 4)

    @property
    def deviation_band_kw(self) -> float:
        return round(self.rated_power_kw * self.deviate_pct, 4)

    def next_version(self) -> "LoadCriteria":
        """Return an identical document carrying the next generation."""

        return replace(self, version=self.version + 1)

    def describe(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "rated_power_kw": self.rated_power_kw,
            "overload_pct": self.overload_pct,
            "stall_pct": self.stall_pct,
            "stall_fraction": self.stall_fraction,
            "overload_run": self.overload_run,
            "deviate_pct": self.deviate_pct,
            "min_samples": self.min_samples,
            "overload_kw": self.overload_kw,
            "stall_kw": self.stall_kw,
            "deviation_band_kw": self.deviation_band_kw,
        }

    def to_document(self) -> dict[str, Any]:
        return {
            "active": True,
            "version": self.version,
            "rated_power_kw": self.rated_power_kw,
            "overload_pct": self.overload_pct,
            "stall_pct": self.stall_pct,
            "stall_fraction": self.stall_fraction,
            "overload_run": self.overload_run,
            "deviate_pct": self.deviate_pct,
            "min_samples": self.min_samples,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "LoadCriteria":
        return cls(
            version=int(document.get("version", DEFAULT_CRITERIA_VERSION)),
            rated_power_kw=float(document.get("rated_power_kw", 55.0)),
            overload_pct=float(document.get("overload_pct", 1.20)),
            stall_pct=float(document.get("stall_pct", 0.45)),
            stall_fraction=float(document.get("stall_fraction", 0.25)),
            overload_run=int(document.get("overload_run", 3)),
            deviate_pct=float(document.get("deviate_pct", 0.35)),
            min_samples=int(document.get("min_samples", 4)),
        )


def default_criteria() -> LoadCriteria:
    """Return the bounds the station boots with before any generation bump."""

    return LoadCriteria()
