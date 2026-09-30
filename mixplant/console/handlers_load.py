"""Routes for the mixer load monitoring and quarantine page."""

from __future__ import annotations

from typing import Any

from .response import inject_snapshot
from .router import Request, Response, Router

COMMANDS = (
    ("POST", "/api/load/observe", "load.observe"),
    ("POST", "/api/load/observe-series", "load.observe_series"),
    ("POST", "/api/load/judge", "load.judge"),
    ("POST", "/api/load/mark", "load.mark"),
    ("POST", "/api/load/release", "load.release"),
    ("POST", "/api/load/rollback", "load.rollback"),
    ("POST", "/api/load/rollback-criteria", "load.rollback_criteria"),
    ("POST", "/api/load/criteria", "load.criteria"),
)


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the load monitoring routes."""

    router.add(
        "GET",
        "/pages/load",
        lambda _: Response.html(
            inject_snapshot(pages.load("load"), runtime.controller.load_view())
        ),
    )
    router.add("GET", "/api/load", lambda _: Response.json(runtime.controller.load_view()))
    router.add("GET", "/api/load/{batch_id}", _batch(runtime))
    for method, path, command in COMMANDS:
        router.add(method, path, _command(runtime, command))


def _batch(runtime: Any):
    def handler(request: Request) -> Response:
        return Response.json(runtime.load.batch_status(request.params["batch_id"]))

    return handler


def _command(runtime: Any, command: str):
    def handler(request: Request) -> Response:
        return Response.json(runtime.dispatch(command, dict(request.body)))

    return handler
