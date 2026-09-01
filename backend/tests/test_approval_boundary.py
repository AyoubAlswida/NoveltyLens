"""The approval boundary: the point of Implementation Gate C.

`verify_features` consumes a **human-approved** feature and a **human-approved**
candidate set. Everything upstream of it produces candidates. This module asserts
that the two can never be confused — not by a missing field, not by a plausible
substitute, not by a version that was approved separately, and not by a code path
somebody adds later in a hurry.

**Three properties, and the third is the one that matters.**

1. A missing or malformed approval raises `UnapprovedArtifactError`.
2. **No LLM call happens when it does.** The exception is cheap; a cell asserting
   something about a patent in answer to an unapproved question is not.
3. `workflow/approvals.py` is the **only** module that writes an approved
   artifact, asserted by source contract rather than by convention.

The postgres-marked tests here exercise the write side against the real database,
because the `is_approved`/`approved_by`/`approved_at` CHECK is a property of the
schema and the in-memory double cannot exhibit it.
"""

from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from graph.verification_nodes import verify_features_node
from workflow import approvals
from workflow.types import FencedError, UnapprovedArtifactError

BACKEND = Path(__file__).resolve().parent.parent

APPROVED_FEATURE = {
    "feature_id": "F1",
    "feature_version": "1",
    "feature_text": "closed-loop control of exhaust heat recovery",
    "elements": [
        {"element_id": "E1", "text": "a heat exchanger within the exhaust duct", "core": True, "order": 1},
    ],
    "relationships": [],
}


def _state(**overrides):
    """A state that would verify successfully, minus whatever a test removes."""
    base = {
        "run_id": "run-1",
        "verification_requested": True,
        "approved_decomposition": {
            "decomposition_id": "dec-1",
            "version": 1,
            "approved_by": "testnovelty@gmail.com",
            "approved_at": "2026-08-28T10:00:00+00:00",
            "features": [APPROVED_FEATURE],
        },
        "decomposition_version": 1,
        "approved_candidate_set": [
            {"publication_number": "EP4778509A1", "normalized_number": "EP4778509"}
        ],
        "candidate_set_version": 1,
        "passages_by_publication": {},
        "enrichment": {},
    }
    base.update(overrides)
    return base


# --- the four refusals, each with no LLM call ------------------------------


def test_a_missing_approved_decomposition_refuses_and_calls_no_model():
    """Workflow Gate A never completed. There is no question to ask."""
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError) as caught:
            verify_features_node(_state(approved_decomposition=None))
    chat.assert_not_called()
    assert "Workflow Gate A" in str(caught.value)


def test_a_candidate_decomposition_is_never_read_as_a_substitute():
    """**The substitution this gate exists to prevent.** A populated
    `candidate_decomposition` is machine output that looks exactly like the thing
    the verifier needs, which is precisely why it must not be reachable."""
    state = _state(approved_decomposition=None)
    state["candidate_decomposition"] = {
        "component": "graph.verification_nodes.decompose_features",
        "features": [APPROVED_FEATURE],
    }
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError):
            verify_features_node(state)
    chat.assert_not_called()


def test_a_missing_approved_candidate_set_refuses_and_calls_no_model():
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError) as caught:
            verify_features_node(_state(approved_candidate_set=None))
    chat.assert_not_called()
    assert "Workflow Gate B" in str(caught.value)


def test_a_ranked_candidate_list_is_never_read_as_a_substitute():
    state = _state(approved_candidate_set=None)
    state["ranked_candidates"] = ["EP4778509", "EP3010333"]
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError):
            verify_features_node(state)
    chat.assert_not_called()


def test_an_approved_artifact_with_no_approver_is_refused():
    """`approved_by=''` satisfies `IS NOT NULL` while naming nobody."""
    approved = dict(_state()["approved_decomposition"], approved_by="")
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError) as caught:
            verify_features_node(_state(approved_decomposition=approved))
    chat.assert_not_called()
    assert "approver" in str(caught.value)


def test_an_approved_artifact_with_no_approval_time_is_refused():
    approved = dict(_state()["approved_decomposition"], approved_at="")
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError) as caught:
            verify_features_node(_state(approved_decomposition=approved))
    chat.assert_not_called()
    assert "approval time" in str(caught.value)


def test_an_approved_decomposition_with_no_features_is_refused():
    approved = dict(_state()["approved_decomposition"], features=[])
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError):
            verify_features_node(_state(approved_decomposition=approved))
    chat.assert_not_called()


def test_a_version_mismatch_between_the_two_approvals_is_refused():
    """Each artifact was approved; they were not approved *together*. Cells built
    from them would name a pairing no human ever looked at."""
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError) as caught:
            verify_features_node(_state(decomposition_version=2))
    chat.assert_not_called()
    assert "no human ever approved" in str(caught.value)


def test_a_missing_version_is_refused_rather_than_defaulted_to_one():
    """Defaulting would make a cell record a version it was not built from."""
    with patch("llm._llm_chat") as chat:
        with pytest.raises(UnapprovedArtifactError):
            verify_features_node(_state(candidate_set_version=None))
    chat.assert_not_called()


def test_an_approved_but_empty_candidate_set_is_not_a_refusal():
    """"None of these are worth verifying" is a real human decision. It produces
    a matrix with no cells, which is a different thing from a matrix nobody
    approved -- and conflating them would force an approver to keep a publication
    they rejected.

    This is the one test here that gets past the approval checks, so it is also
    the only one that needs `emit_progress` stubbed: outside a compiled graph
    there is no LangGraph stream writer to emit through.
    """
    with patch("llm._llm_chat") as chat, patch("graph.verification_nodes.emit_progress"):
        result = verify_features_node(_state(approved_candidate_set=[]))
    chat.assert_not_called()
    assert result["cells"] == []


# --- the source contract ---------------------------------------------------
#
# `ast`, not a substring search, for the two checks below that would otherwise
# match the docstring *describing* the forbidden thing rather than a use of it.
# That mistake has been made repeatedly in this suite, and a source-contract test
# that fires on its own prose is one somebody eventually relaxes.


def _defined_and_called_names(path: Path) -> set[str]:
    """Every name a module defines or calls. Comments and strings excluded."""
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - the suite would fail elsewhere first
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
    return names


def _state_keys_read(path: Path) -> set[str]:
    """Keys a module actually reads out of a mapping -- `x["k"]`, `x.get("k")`.

    A key named only in a docstring is not one of these, which is the entire
    reason this is an AST walk.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if isinstance(node.slice.value, str):
                keys.add(node.slice.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


def _python_sources():
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__"} for part in path.parts):
            continue
        yield path


def test_only_approvals_py_writes_the_approved_columns():
    """The rule a schema cannot express.

    The CHECK keeps `is_approved`, `approved_by` and `approved_at` consistent
    with each other, but nothing in the database can tell whether the email in
    `approved_by` belongs to somebody who actually approved anything. That is
    only enforceable by keeping the write in one place.

    `approved_understandings`, `approved_search_strategies` and
    `approved_strategy_queries` are covered here for the same reason as the other
    two. `understanding_proposals` and `search_strategy_proposals` deliberately
    are NOT: neither has an `is_approved` column or approver columns, so a row in
    either cannot record an approval however it is written, and listing them would
    imply the opposite.
    """
    offenders = []
    for path in _python_sources():
        # approvals.py is the writer. These two modules write a deliberately
        # *unapproved* row, to prove such a row cannot be loaded as approved --
        # which is a test of the boundary, not a breach of it.
        if path.name in {
            "approvals.py",
            "test_approval_boundary.py",
            "test_invention_understanding.py",
            "test_search_strategy.py",
        }:
            continue
        text = path.read_text(encoding="utf-8").lower()
        for table in (
            "feature_decompositions",
            "candidate_sets",
            "approved_understandings",
            "approved_search_strategies",
            "approved_strategy_queries",
        ):
            if f"insert into {table}" in text or f"update {table}" in text:
                offenders.append(f"{path.relative_to(BACKEND)} -> {table}")
    assert offenders == [], f"approved artifacts written outside approvals.py: {offenders}"


def test_no_module_defines_or_calls_an_auto_approval():
    """Anything *named* like an auto-approval is one.

    `tests/harness.py` is excluded from nothing here: it approves **explicitly**,
    naming an approver on every call, which is what keeps it a fixture rather
    than a loophole. `approvals.py` names `approve_if_valid` in its docstring as
    a thing that must not exist — a mention, not a definition, which is why this
    walks the AST instead of the text.
    """
    forbidden = ("auto_approve", "autoapprove", "approve_if", "skip_approval", "force_approve")
    offenders = []
    for path in _python_sources():
        for name in _defined_and_called_names(path):
            lowered = name.lower()
            if any(needle in lowered for needle in forbidden):
                offenders.append(f"{path.relative_to(BACKEND)} -> {name}")
    assert offenders == [], f"auto-approval path found: {offenders}"


def test_the_verifier_node_never_reads_the_candidate_fields():
    """Source-level, because a fallback would be one `or` away and would still
    pass every behavioural test above — it would only fire when the approved
    field was missing, which those tests make raise first.

    The check is on the keys the module actually *reads out of state*, so the
    docstrings that name `candidate_decomposition` in order to say it is never
    read do not trip it.
    """
    read = _state_keys_read(BACKEND / "graph" / "verification_nodes.py")
    forbidden = {"candidate_decomposition", "ranked_candidates"}
    assert not (read & forbidden), (
        f"verification_nodes.py reads machine-output state: {sorted(read & forbidden)}"
    )
    assert {"approved_decomposition", "approved_candidate_set"} <= read, (
        "sanity check on the analysis itself: the approved keys must be found, or "
        "this test would pass against a module that reads nothing at all"
    )


def test_the_application_never_imports_the_test_harness():
    """A production run provably cannot reach the harness's approvals."""
    offenders = []
    for path in _python_sources():
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "harness" in text and "import" in text:
            for line in text.splitlines():
                if "harness" in line and line.strip().startswith(("import ", "from ")):
                    offenders.append(f"{path.relative_to(BACKEND)}: {line.strip()}")
    assert offenders == [], f"application code imports the test harness: {offenders}"


# --- the write side, against the real database -----------------------------


@pytest.mark.postgres
class TestApprovalWrites:
    """`workflow/approvals.py` against real PostgreSQL."""

    OWNER = "approval-worker"

    def _leased(self, pg, make_run):
        from workflow import lease

        run_id = make_run()
        token = lease.acquire(pg, run_id=run_id, owner_id=self.OWNER, ttl_seconds=60.0)
        return run_id, token

    def test_an_approval_with_no_approver_is_refused_at_write_time(self, pg, make_run):
        run_id, token = self._leased(pg, make_run)
        with pytest.raises(UnapprovedArtifactError):
            approvals.approve_decomposition(
                pg, run_id=run_id, owner_id=self.OWNER, fence_token=token,
                version=1, features=[APPROVED_FEATURE],
                approved_by="", approved_at="2026-08-28T10:00:00+00:00",
            )
        assert pg.execute(
            "SELECT count(*) FROM feature_decompositions WHERE run_id = %s", (run_id,)
        ).fetchone()[0] == 0

    def test_an_approval_with_no_time_is_refused_at_write_time(self, pg, make_run):
        run_id, token = self._leased(pg, make_run)
        with pytest.raises(UnapprovedArtifactError):
            approvals.approve_decomposition(
                pg, run_id=run_id, owner_id=self.OWNER, fence_token=token,
                version=1, features=[APPROVED_FEATURE],
                approved_by="testnovelty@gmail.com", approved_at="",
            )

    def test_a_fenced_worker_cannot_write_an_approval(self, pg, make_run):
        from workflow import lease

        run_id, token = self._leased(pg, make_run)
        pg.execute(
            """
            UPDATE run_leases SET acquired_at = now() - interval '120 seconds',
                   expires_at = now() - interval '60 seconds' WHERE run_id = %s
            """,
            (run_id,),
        )
        lease.acquire(pg, run_id=run_id, owner_id="replacement", ttl_seconds=60.0)

        with pytest.raises(FencedError):
            approvals.approve_decomposition(
                pg, run_id=run_id, owner_id=self.OWNER, fence_token=token,
                version=1, features=[APPROVED_FEATURE],
                approved_by="testnovelty@gmail.com",
                approved_at="2026-08-28T10:00:00+00:00",
            )

    def test_the_schema_refuses_a_half_stamped_approval(self, pg, make_run):
        """The database's own half of the rule: `is_approved` cannot be true with
        no approver, and cannot be false with one."""
        run_id, _ = self._leased(pg, make_run)
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                """
                INSERT INTO feature_decompositions
                    (run_id, version, feature_id, feature_version, feature_text, is_approved)
                VALUES (%s, 1, 'F1', '1', 'a feature', TRUE)
                """,
                (run_id,),
            )

    def test_an_unapproved_row_cannot_be_loaded_as_approved(self, pg, make_run):
        """`is_approved` is filtered in SQL: an unapproved row is not fetched and
        then rejected, it is not fetched."""
        run_id, _ = self._leased(pg, make_run)
        pg.execute(
            """
            INSERT INTO feature_decompositions
                (run_id, version, feature_id, feature_version, feature_text, is_approved)
            VALUES (%s, 1, 'F1', '1', 'a feature nobody approved', FALSE)
            """,
            (run_id,),
        )
        assert approvals.load_approved_decompositions(pg, run_id=run_id, version=1) == []
        with pytest.raises(UnapprovedArtifactError):
            approvals.require_approved_decompositions(pg, run_id=run_id, version=1)

    def test_an_approved_decomposition_round_trips_with_its_approver(self, pg, make_run):
        run_id, token = self._leased(pg, make_run)
        approvals.approve_decomposition(
            pg, run_id=run_id, owner_id=self.OWNER, fence_token=token,
            version=1, features=[APPROVED_FEATURE],
            approved_by="testnovelty@gmail.com",
            approved_at="2026-08-28T10:00:00+00:00",
        )
        loaded = approvals.require_approved_decompositions(pg, run_id=run_id, version=1)
        assert len(loaded) == 1
        assert loaded[0].feature_id == "F1"
        assert loaded[0].approved_by == "testnovelty@gmail.com"
        assert loaded[0].element_ids == ("E1",)

    def test_a_candidate_set_with_no_approved_version_refuses(self, pg, make_run):
        run_id, _ = self._leased(pg, make_run)
        with pytest.raises(UnapprovedArtifactError) as caught:
            approvals.require_approved_candidate_set(pg, run_id=run_id, version=1)
        assert "never read as a substitute" in str(caught.value)

    def test_an_approved_empty_candidate_set_is_stored_and_returns_empty(self, pg, make_run):
        """Distinguishable from "no set was approved", which raises."""
        run_id, token = self._leased(pg, make_run)
        approvals.approve_candidate_set(
            pg, run_id=run_id, owner_id=self.OWNER, fence_token=token,
            version=1, publications=[],
            approved_by="testnovelty@gmail.com",
            approved_at="2026-08-28T10:00:00+00:00",
        )
        assert approvals.require_approved_candidate_set(pg, run_id=run_id, version=1) == []

    def test_the_harness_writes_through_approvals_and_names_an_approver(self, pg, make_run):
        """The harness is not a second writer. It is a caller."""
        from tests import harness

        run_id, token = self._leased(pg, make_run)
        payload = harness.approve_decomposition(
            pg, run_id=run_id, owner_id=self.OWNER, fence_token=token
        )
        assert payload["approved_by"] == harness.HARNESS_APPROVER
        assert payload["approved_at"]
        stored = pg.execute(
            "SELECT approved_by, is_approved FROM feature_decompositions WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        assert stored == (harness.HARNESS_APPROVER, True)
