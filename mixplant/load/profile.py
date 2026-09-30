"""The sampled load curve captured for one mixing batch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError

MappingProxyLike = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LoadPoint:
    """One mixer load reading, ordered by tick."""

    tick: int
    load_kw: float

    def describe(self) -> dict[str, Any]:
        return {"tick": self.tick, "load_kw": self.load_kw}


class LoadProfile:
    """The immutable-ish time series collected for one batch."""

    __slots__ = ("batch_id", "_points")

    def __init__(self, batch_id: str, points: tuple[LoadPoint, ...] = ()) -> None:
        if not batch_id:
            raise ValidationError("a load profile needs a batch identity")
        self.batch_id = batch_id
        self._points = tuple(points)

    def append(self, point: LoadPoint) -> LoadPoint:
        if self._points and point.tick < self._points[-1].tick:
            raise ValidationError(
                "load samples must be recorded in tick order",
                batch_id=self.batch_id,
                previous_tick=self._points[-1].tick,
                tick=point.tick,
            )
        self._points = (*self._points, point)
        return point

    def adopt(self, points: tuple[LoadPoint, ...]) -> None:
        """Replace the in-memory curve after replay or rollback."""

        self._points = tuple(points)

    def points(self) -> tuple[LoadPoint, ...]:
        return self._points

    def values(self) -> tuple[float, ...]:
        return tuple(point.load_kw for point in self._points)

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "points": [point.describe() for point in self._points],
            "samples": len(self._points),
            "duration_ticks": 0 if not self._points else self._points[-1].tick - self._points[0].tick,
        }


def load_profile(points: list[dict[str, Any]] | tuple[MappingProxyLike, ...]) -> tuple[LoadPoint, ...]:
    """Build typed points from a stored payload."""

    typed: list[LoadPoint] = []
    previous_tick: int | None = None
    for raw in points:
        point = LoadPoint(tick=int(raw["tick"]), load_kw=round(float(raw["load_kw"]), 4))
        if point.load_kw < 0:
            raise ValidationError("mixer load must not be negative", load_kw=point.load_kw)
        if previous_tick is not None and point.tick < previous_tick:
            raise ValidationError("stored load samples must be in tick order")
        previous_tick = point.tick
        typed.append(point)
    return tuple(typed)
