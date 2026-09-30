"""Read model behind the mixer load / quarantine console page."""

from __future__ import annotations

from typing import Any


def load_view(*, load_service: Any, batches: Any) -> dict[str, Any]:
    """Build the per-batch load monitoring view."""

    batch_ids = [batch.batch_id for batch in batches.all()]
    return {
        "criteria": load_service.criteria.describe(),
        "gate": {
            "blocked": load_service.registry.blocked_batch() is not None,
            "blocked_batch": load_service.registry.blocked_batch(),
        },
        "holds": [entry.describe() for entry in load_service.registry.all()],
        "batches": [load_service.batch_status(batch_id) for batch_id in batch_ids],
    }
