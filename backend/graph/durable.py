"""Persisting machine evidence from a graph node, under the lease that is live.

## Why the lease comes from `config` and not from `GraphState`

`workflow/store.py`'s evidence writers are all fenced: they take an `owner_id`
and a `fence_token` and refuse (`FN001`) if the caller is not the current holder.
A graph node therefore needs the lease -- and the one place it must *not* come
from is graph state, because graph state is checkpointed. A checkpointed fence
token is stale the moment a different worker resumes the run, so the first write
after any recovery would fence itself against a token it had merely remembered.

`workflow/executor.py` rebuilds `config["configurable"]` on every acquisition, so
what a node reads here is the authority this process holds *now*.

## Why a missing lease is a skip and not a failure

A node runs in exactly two situations: under the executor, which always supplies
the lease (asserted in `tests/test_workflow_executor.py`, not assumed here), and
in a direct call from a test. In the second there is no `verification_runs` row,
so the foreign key would refuse the write anyway -- there is nothing to persist
*to*, which is a different thing from choosing not to persist.

Nothing here invents, defaults or softens a value: the rows written are the cells
the verifier actually produced, and a write that fails propagates.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def lease_of(config: Any) -> Optional[tuple[str, int]]:
    """`(owner_id, fence_token)` from a LangGraph config, or `None`.

    `None` means this node is not executing under the durable executor. It never
    means "the lease was lost" -- that is `FencedError`, raised by the write.
    """
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    owner_id = configurable.get("owner_id")
    fence_token = configurable.get("fence_token")
    if not owner_id or fence_token is None:
        return None
    return str(owner_id), int(fence_token)


def persist_cells(run_id: str, config: Any, cells: list[dict[str, Any]]) -> int:
    """Write each matrix cell and its result. Returns how many were written.

    **The matrix has to outlive the checkpoint.** A checkpoint answers "where is
    execution?"; `verification_cells` / `verification_results` are what a review
    overlay binds to (`(cell_id, result_version)`), what a rerun bumps a version
    against, and what a report is assembled from months later. A matrix that
    existed only in graph state could be reviewed on screen and then be
    unreadable the moment the checkpoint was compacted.

    Each write is `ON CONFLICT DO NOTHING` on both rows, so replaying a recovered
    node re-writes the identical cell rather than duplicating or mutating it.
    """
    lease = lease_of(config)
    if lease is None or not run_id or not cells:
        return 0

    import db
    from workflow import store

    owner_id, fence_token = lease
    with db.connect() as conn:
        for cell in cells:
            store.save_cell_result(
                conn,
                run_id=run_id,
                owner_id=owner_id,
                fence_token=fence_token,
                cell=cell,
            )
    logger.info("persisted %d verification cell(s) for run %s", len(cells), run_id)
    return len(cells)


def persist_passages(run_id: str, config: Any, passages: list[Any]) -> int:
    """Write the passages of record. Returns how many rows were newly inserted.

    **A citation has to outlive the run that made it.** `evidence_passages` is
    what a locator resolves against months later -- `claim 4`, `paragraph
    [0031]`, `description characters 4120-5233` -- and what a stored verdict is
    checked against. A passage that existed only in graph state would leave every
    citation in an approved report unresolvable the moment the checkpoint was
    compacted.

    `record_passages` enforces the replay contract (migration 0018): an identical
    replay writes nothing, and evidence that differs on any content or provenance
    column raises `FN003` rather than silently overwriting text a verdict was
    validated against.
    """
    lease = lease_of(config)
    if lease is None or not run_id or not passages:
        return 0

    import db
    from workflow import evidence_store

    owner_id, fence_token = lease
    with db.connect() as conn:
        written = evidence_store.record_passages(
            conn,
            run_id=run_id,
            owner_id=owner_id,
            fence_token=fence_token,
            passages=passages,
        )
    logger.info("persisted %d evidence passage(s) for run %s", written, run_id)
    return written


__all__ = ["lease_of", "persist_cells", "persist_passages"]
