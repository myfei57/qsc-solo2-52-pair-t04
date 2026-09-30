"""Per-batch mixer load monitoring: judge, quarantine, release and roll back."""

from __future__ import annotations

import threading

import pytest

from mixplant.config import StationConfig
from mixplant.errors import (
    DuplicateError,
    InterlockBlockedError,
    NotFoundError,
    ValidationError,
)
from mixplant.load.criteria import LoadCriteria, default_criteria
from mixplant.load.features import LoadSample, summarize_load
from mixplant.load.registry import RELEASED, UNDER_REVIEW, disposition_for
from mixplant.load.verdict import (
    ABNORMAL,
    INSUFFICIENT,
    OK,
    SHAPE_DEVIATION,
    STALL,
    SUSTAINED_OVERLOAD,
    judge,
)
from mixplant.runtime import PlantRuntime
from tests.support import advance_to_mixed, run

NORMAL = [52.0, 54.0, 55.0, 53.0, 56.0, 55.0, 54.0, 53.0]
STALLED = [50.0, 20.0, 21.0, 19.0, 18.0, 53.0, 22.0, 20.0]
OVERLOADED = [80.0, 81.0, 82.0, 79.0, 80.0, 81.0]


def _samples(batch_id: str, powers: list[float]) -> tuple[LoadSample, ...]:
    return tuple(
        LoadSample(batch_id=batch_id, tick=index, power_kw=power, index=index + 1)
        for index, power in enumerate(powers)
    )


def test_normal_trace_is_ok() -> None:
    verdict = judge(
        features=summarize_load(batch_id="B", samples=_samples("B", NORMAL), criteria=default_criteria()),
        criteria=default_criteria(),
    )
    assert verdict.code == OK
    assert verdict.abnormal is False
    assert verdict.reasons == ()


def test_stall_trace_is_flagged() -> None:
    criteria = default_criteria()
    verdict = judge(
        features=summarize_load(batch_id="B", samples=_samples("B", STALLED), criteria=criteria),
        criteria=criteria,
    )
    assert verdict.code == ABNORMAL
    assert STALL in verdict.reasons
    assert SHAPE_DEVIATION in verdict.reasons


def test_sustained_overload_is_flagged() -> None:
    criteria = default_criteria()
    verdict = judge(
        features=summarize_load(batch_id="B", samples=_samples("B", OVERLOADED), criteria=criteria),
        criteria=criteria,
    )
    assert SUSTAINED_OVERLOAD in verdict.reasons


def test_too_few_samples_is_inconclusive_not_abnormal() -> None:
    criteria = default_criteria()
    verdict = judge(
        features=summarize_load(batch_id="B", samples=_samples("B", [90.0, 90.0]), criteria=criteria),
        criteria=criteria,
    )
    assert verdict.code == INSUFFICIENT
    assert verdict.abnormal is False


def test_criteria_bounds_are_validated() -> None:
    with pytest.raises(ValidationError):
        LoadCriteria(version=1, stall_pct=1.5, overload_pct=1.2)
    with pytest.raises(ValidationError):
        LoadCriteria(version=0)


def test_disposition_follows_the_grade_that_names_the_use() -> None:
    assert disposition_for("C40") == "scrap"
    assert disposition_for("C30") == "scrap"
    assert disposition_for("C20") == "rework"
    assert disposition_for("unknown-grade") == "rework"


def _batch_with_trace(runtime: PlantRuntime, batch_id: str, powers: list[float], *, grade: str = "C30") -> dict:
    if grade == "C20":
        run(runtime, "recipe.publish", {
            "name": "c20", "grade": "C20",
            "factors": {"sand": 760.0, "gravel": 1010.0, "cement": 260.0, "water": 180.0},
        })
        run(runtime, "recipe.select", {"name": "c20"})
        run(runtime, "batch.declare", {"batch_id": batch_id, "quantity": 6})
    else:
        advance_to_mixed(runtime, batch_id=batch_id)
    run(runtime, "load.observe_series", {"batch_id": batch_id, "powers": powers})
    return run(runtime, "load.judge", {"batch_id": batch_id})


def test_abnormal_c30_batch_is_held_as_scrap_and_blocks_the_door(runtime: PlantRuntime) -> None:
    result = _batch_with_trace(runtime, "B-001", STALLED)
    assert result["verdict"]["abnormal"] is True
    assert result["hold"]["state"] == UNDER_REVIEW
    assert result["hold"]["disposition"] == "scrap"
    assert runtime.state()["facts"]["load.blocked"] is True
    run(runtime, "chute.arrive", {"truck_id": "T-1"})
    with pytest.raises(InterlockBlockedError):
        run(runtime, "chute.open")


def test_normal_batch_opens_the_door(runtime: PlantRuntime) -> None:
    _batch_with_trace(runtime, "B-OK", NORMAL)
    assert runtime.state()["facts"]["load.blocked"] is False
    run(runtime, "chute.arrive", {"truck_id": "T-9"})
    run(runtime, "chute.open")


def test_c20_abnormal_batch_is_held_for_rework(runtime: PlantRuntime) -> None:
    result = _batch_with_trace(runtime, "B-C20", OVERLOADED, grade="C20")
    assert result["hold"]["disposition"] == "rework"


def test_repeat_marks_are_idempotent_under_one_key(runtime: PlantRuntime) -> None:
    _batch_with_trace(runtime, "B-001", STALLED)
    seen: list[dict] = []

    def mark() -> None:
        seen.append(run(runtime, "load.mark", {"batch_id": "B-001", "idempotency_key": "k-1"}))

    threads = [threading.Thread(target=mark) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    holds = [hold for hold in runtime.load.status()["holds"] if hold["batch_id"] == "B-001"]
    assert len(holds) == 1
    # exactly one mark audited, every duplicate call answered with the same hold
    assert runtime.audit.counts()["load.marked"] == 1
    assert all(item["hold"]["batch_id"] == "B-001" for item in seen)


def test_an_idempotency_key_cannot_be_reused_on_another_batch(runtime: PlantRuntime) -> None:
    _batch_with_trace(runtime, "B-001", STALLED)
    run(runtime, "load.mark", {"batch_id": "B-001", "idempotency_key": "shared"})
    run(runtime, "batch.declare", {"batch_id": "B-002", "quantity": 6})
    with pytest.raises(DuplicateError):
        run(runtime, "load.mark", {"batch_id": "B-002", "idempotency_key": "shared"})


def test_release_freezes_disposition_and_clears_the_door(runtime: PlantRuntime) -> None:
    _batch_with_trace(runtime, "B-001", STALLED)
    released = run(runtime, "load.release", {"batch_id": "B-001", "issuer": "op-3"})
    assert released["release"]["state"] == RELEASED
    assert released["release"]["release_issuer"] == "op-3"
    assert runtime.state()["facts"]["load.blocked"] is False
    assert runtime.audit.counts()["load.released"] == 1
    with pytest.raises(ValidationError):
        run(runtime, "load.release", {"batch_id": "B-001"})


def test_rollback_after_release_reblocks_in_one_segment(runtime: PlantRuntime) -> None:
    _batch_with_trace(runtime, "B-001", STALLED)
    run(runtime, "load.release", {"batch_id": "B-001"})
    assert runtime.state()["facts"]["load.blocked"] is False
    rolled = run(runtime, "load.rollback", {"batch_id": "B-001", "reason": "sensor untrusted"})
    assert rolled["hold"]["state"] == UNDER_REVIEW
    assert rolled["tombstones"] == sorted(rolled["tombstones"])
    assert runtime.state()["facts"]["load.blocked"] is True
    run(runtime, "chute.arrive", {"truck_id": "T-1"})
    with pytest.raises(InterlockBlockedError):
        run(runtime, "chute.open")
    assert runtime.audit.counts()["load.rolled_back"] == 1


def test_rollback_of_an_unknown_segment_is_refused(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime, batch_id="B-001")
    with pytest.raises(NotFoundError):
        run(runtime, "load.rollback", {"batch_id": "B-001"})


def test_a_criteria_generation_rolls_every_batch_back(runtime: PlantRuntime) -> None:
    _batch_with_trace(runtime, "G-1", NORMAL)
    run(runtime, "load.criteria", {"stall_pct": 0.9, "overload_pct": 1.5, "deviate_pct": 1.5})
    assert runtime.load.criteria.version == 2
    run(runtime, "load.judge", {"batch_id": "G-1"})  # normal under the loose, untrusted v2
    run(runtime, "load.mark", {"batch_id": "G-1"})
    run(runtime, "load.release", {"batch_id": "G-1"})
    assert runtime.state()["facts"]["load.blocked"] is False
    result = run(runtime, "load.rollback_criteria", {"version": 2})
    assert result["batches"] == ["G-1"]
    assert runtime.state()["facts"]["load.blocked"] is True


def test_judgement_is_audited_and_published(runtime: PlantRuntime) -> None:
    _batch_with_trace(runtime, "B-001", STALLED)
    assert runtime.audit.counts()["load.judged"] == 1
    verdict = runtime.store.latest("load.verdict")
    assert verdict is not None
    assert verdict.payload["batch_id"] == "B-001"
    assert verdict.payload["criteria_version"] == 1


def test_finishing_the_window_auto_judges_a_sampled_batch(runtime: PlantRuntime) -> None:
    from tests import support

    support.advance_to_mixing(runtime, batch_id="B-001")
    run(runtime, "load.observe_series", {"batch_id": "B-001", "powers": STALLED})
    run(runtime, "clock.advance", {"ticks": 8})
    finished = run(runtime, "mix.finish")
    assert finished["load_verdict"]["abnormal"] is True
    assert runtime.state()["facts"]["load.blocked"] is True


def test_load_state_survives_a_restart(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    advance_to_mixed(first, batch_id="B-R1")
    run(first, "load.observe_series", {"batch_id": "B-R1", "powers": STALLED})
    run(first, "load.judge", {"batch_id": "B-R1"})
    run(first, "load.mark", {"batch_id": "B-R1", "idempotency_key": "k-r"})
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    hold = second.load.registry.require("B-R1")
    assert hold.state == UNDER_REVIEW
    assert hold.disposition == "scrap"
    assert second.state()["facts"]["load.blocked"] is True
    # the idempotency key is rebuilt too
    run(second, "load.mark", {"batch_id": "B-R1", "idempotency_key": "k-r"})
    assert len(second.load.status()["holds"]) == 1
    with pytest.raises(InterlockBlockedError):
        run(second, "chute.open")
    second.close()


def test_console_serves_the_load_view_and_page(runtime: PlantRuntime) -> None:
    from mixplant.console.pages import PageStore
    from mixplant.console.router import Request
    from mixplant.console.server import ConsoleServer

    _batch_with_trace(runtime, "B-001", STALLED)
    server = ConsoleServer(runtime, pages=PageStore(runtime.config.web_dir))
    api = server.handle(Request(method="GET", path="/api/load"))
    assert api.ok is True
    page = server.handle(Request(method="GET", path="/pages/load"))
    assert page.ok is True
    batch = server.handle(Request(method="GET", path="/api/load/B-001"))
    assert batch.ok is True
    assert '"B-001"' in batch.text
