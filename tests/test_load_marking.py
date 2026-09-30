"""Mixer load profiles mark abnormal batches and keep them from discharge."""

from __future__ import annotations

import concurrent.futures
import pytest

from mixplant.errors import InterlockBlockedError, OrderViolationError, ValidationError
from mixplant.runtime import PlantRuntime
from tests.support import advance_to_mixed, run


def _record(runtime: PlantRuntime, values: list[float]) -> None:
    for value in values:
        run(runtime, "load.point", {"load_kw": value})


def test_normal_profile_allows_discharge(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    _record(runtime, [10, 18, 26, 34])
    verdict = run(runtime, "load.evaluate")["verdict"]
    assert verdict["abnormal"] is False
    assert verdict["reasons"] == []
    run(runtime, "chute.arrive", {"truck_id": "T-01"})
    run(runtime, "chute.open")
    assert runtime.chute.status()["door_open"] is True


def test_overloaded_profile_is_marked_by_use_and_blocks_only_discharge(
    runtime: PlantRuntime,
) -> None:
    advance_to_mixed(runtime)
    _record(runtime, [20, 35, 48, 60, 50])
    marker = run(runtime, "load.mark", {"use": "structural"})["marker"]
    assert marker["scope"] == "batch"
    assert marker["effect"] == "discharge_gate_only"
    assert marker["disposition"] == "scrap"
    assert marker["blocked"] is True
    run(runtime, "chute.arrive", {"truck_id": "T-01"})
    with pytest.raises(InterlockBlockedError):
        run(runtime, "chute.open")
    assert runtime.state()["subsystems"]["load"]["blocked"] == ["B-001"]


def test_disposition_is_resolved_from_grade_and_use(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    _record(runtime, [20, 35, 48, 60])
    marker = run(runtime, "load.mark", {"use": "backfill"})["marker"]
    assert marker["disposition"] == "rework"


def test_duplicate_marker_is_idempotent_under_concurrency(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    _record(runtime, [20, 35, 48, 60])
    run(runtime, "load.evaluate")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(runtime, "load.mark", {"use": "backfill"}), range(2)))
    assert [result.get("idempotent", False) for result in results].count(False) == 1
    markers = [record for record in runtime.store.visible() if record.kind == "load.marker"]
    assert len(markers) == 1


def test_normal_batch_cannot_be_marked(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    _record(runtime, [10, 18, 26, 34])
    run(runtime, "load.evaluate")
    with pytest.raises(ValidationError):
        run(runtime, "load.mark", {"use": "backfill"})


def test_scrap_marker_cannot_be_released_for_discharge(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    _record(runtime, [20, 35, 48, 60])
    run(runtime, "load.mark", {"use": "structural"})
    with pytest.raises(OrderViolationError):
        run(runtime, "load.release")


def test_release_can_be_confirmed_and_then_rolled_back_to_interception(
    runtime: PlantRuntime,
) -> None:
    advance_to_mixed(runtime)
    _record(runtime, [20, 35, 48, 60])
    run(runtime, "load.mark", {"use": "backfill"})
    run(runtime, "chute.arrive", {"truck_id": "T-01"})
    assert run(runtime, "load.release")["release"]["state"] == "released"
    run(runtime, "chute.open")
    run(runtime, "chute.load", {"truck_id": "T-01", "volume_m3": 6})
    result = run(runtime, "load.rollback_release")
    assert result["blocked"] is True
    assert runtime.chute.status()["door_open"] is False
    assert runtime.chute.status()["manifest"]["loads"] == []
    assert runtime.machines["mix"].phase == "mixed"
    with pytest.raises(InterlockBlockedError):
        run(runtime, "chute.open")


def test_verdict_and_release_lifecycle_are_audited(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    _record(runtime, [20, 35, 48, 60])
    run(runtime, "load.mark", {"use": "backfill"})
    run(runtime, "load.release")
    run(runtime, "load.rollback_release")
    names = set(runtime.audit.counts())
    assert {"load.evaluated", "load.marked", "load.released", "load.release_rolled_back"} <= names


def test_untrusted_criteria_after_release_rolls_back_to_interception(
    runtime: PlantRuntime,
) -> None:
    advance_to_mixed(runtime)
    run(runtime, "load.criteria", {"criteria": {"max_load_kw": 40}})
    _record(runtime, [10, 20, 30, 50])
    run(runtime, "load.mark", {"use": "backfill"})
    run(runtime, "chute.arrive", {"truck_id": "T-01"})
    run(runtime, "load.release")
    run(runtime, "chute.open")
    result = run(runtime, "load.rollback_criteria", {"seq": 0})
    assert result["blocked"] == ["B-001"]
    assert runtime.machines["mix"].phase == "mixed"
    with pytest.raises(InterlockBlockedError):
        run(runtime, "chute.open")


def test_criteria_segment_can_roll_back(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    first = run(runtime, "load.criteria", {"criteria": {"max_load_kw": 40}})["seq"]
    run(runtime, "load.criteria", {"criteria": {"max_load_kw": 70}})
    result = run(runtime, "load.rollback_criteria", {"seq": first})
    assert result["tombstones"]
    assert runtime.state()["subsystems"]["load"]["current_criteria"]["max_load_kw"] == 40
