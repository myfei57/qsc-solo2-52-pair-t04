"""Read model behind the mixing and cycle overview."""

from __future__ import annotations

from typing import Any


def mix_view(*, mix_service: Any, cement_service: Any, water_service: Any, load_service: Any = None) -> dict[str, Any]:
    """Build the mixing view for the console."""

    view = {
        "mixer": mix_service.status(),
        "inputs": list(mix_service.inputs()),
        "cement": cement_service.status(),
        "water": water_service.status(),
        "cycle": mix_service.cycles.current().describe() if mix_service.cycles.current() else None,
    }
    if load_service is not None:
        view["load"] = load_service.status()
    return view
