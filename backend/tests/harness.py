"""Explicit approval fixtures, so the verifier can be integrated before the
Workflow Gate A and B endpoints exist.

**This is not an auto-approval path and must never become one.** Every function
here takes an `approved_by` and writes through `workflow/approvals.py` — the same
module the real endpoints will call, under the same lease, producing the same
rows. What it does not do, and must never do, is decide on a human's behalf. A
test that wants an approved decomposition has to name an approver, which is
exactly the friction that keeps "just approve it for the test" from becoming
"just approve it".

**Why a harness at all.** `verify_features` requires an approved decomposition
and an approved candidate set. Those come from Workflow Gates A and B, which
Implementation Gate D builds. Integrating the verifier first therefore needs
*some* source of approved artifacts, and there are only two options: a code path
in the application that approves things itself, or a fixture outside it. The
first is the thing this whole gate exists to prevent.

**A production run cannot use any of this.** Nothing in `server.py`, `graph/` or
`workflow/` imports this module, and `tests/test_approval_boundary.py` asserts
that by source contract. `tests/` is not on the deployed image's import path in
any configuration, but the grep is the guarantee, not the packaging.
"""

from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

import psycopg

from workflow import approvals

HARNESS_APPROVER = "testnovelty@gmail.com"
"""The seeded account. Named as a constant so a test that approves something is
visibly approving it *as somebody*, rather than passing a bare string that reads
like a placeholder."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def feature(
    feature_id: str = "F1",
    *,
    text: str = "A closed-loop exhaust heat recovery system",
    elements: Sequence[tuple[str, str, bool]] = (
        ("E1", "a heat exchanger disposed within an exhaust duct", True),
        ("E2", "a coolant pump supplying the heat exchanger", True),
    ),
    relationships: Sequence[tuple[str, str]] = (
        ("R1", "the pump supplies coolant to the heat exchanger"),
    ),
    version: str = "1",
) -> dict[str, Any]:
    """One feature in the spec §2 wire shape.

    Defaults describe a real, checkable feature rather than `"foo"`: an element a
    passage can plainly state or plainly not state is what makes an assertion
    about the verifier meaningful.
    """
    return {
        "feature_id": feature_id,
        "feature_version": version,
        "feature_text": text,
        "elements": [
            {"element_id": eid, "text": etext, "core": core, "order": index}
            for index, (eid, etext, core) in enumerate(elements, start=1)
        ],
        "relationships": [
            {"relationship_id": rid, "text": rtext, "order": index}
            for index, (rid, rtext) in enumerate(relationships, start=1)
        ],
    }


def approve_decomposition(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    features: Optional[Sequence[dict[str, Any]]] = None,
    version: int = 1,
    approved_by: str = HARNESS_APPROVER,
) -> dict[str, Any]:
    """Approve a decomposition and return the `GraphState` payload for it.

    The return value is the **only** legitimate way to populate
    `approved_decomposition` in a test, and it comes back from the same module
    the endpoints will use. A test that hand-builds that dict is testing a state
    shape rather than an approval, and `test_approval_boundary.py` covers the
    hand-built case separately — deliberately, to prove the verifier's checks
    catch a forged one.
    """
    approved_at = _now()
    payload = list(features) if features is not None else [feature()]
    approvals.approve_decomposition(
        conn,
        run_id=run_id,
        owner_id=owner_id,
        fence_token=fence_token,
        version=version,
        features=payload,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    return {
        "decomposition_id": f"{run_id}:decomposition:{version}",
        "version": version,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "features": payload,
    }


def approve_candidate_set(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    publications: Sequence[tuple[str, str]],
    version: int = 1,
    approved_by: str = HARNESS_APPROVER,
    manually_added: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Approve a candidate set and return the `GraphState` payload for it."""
    approvals.approve_candidate_set(
        conn,
        run_id=run_id,
        owner_id=owner_id,
        fence_token=fence_token,
        version=version,
        publications=list(publications),
        approved_by=approved_by,
        approved_at=_now(),
        manually_added=manually_added,
    )
    return approvals.load_approved_candidate_set(conn, run_id=run_id, version=version)
