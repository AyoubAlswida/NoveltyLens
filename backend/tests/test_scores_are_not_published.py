"""The similarity score is transient: it orders and it rates, then it is gone.

Spec section 5. The percentage and the overall verdict were removed from every
output the product has - the SSE ``result`` event, the stored row, the History
response and the JSON export the frontend builds from that same object.

These tests **walk the serialized structure** rather than checking named keys.
A named-key assertion only catches the field coming back under the name it left
under; the failure mode worth guarding is the one section 12 names -- "do not
let a removed percentage come back as a nearby number wearing a different name"
-- so the walk looks for a *value* that is a similarity number in a place a
rating belongs, whatever it is called.
"""

import json
from unittest.mock import patch

import pytest

import server
from auth import issue_token
from fastapi.testclient import TestClient
from graph.pipeline import run_graph_pipeline
from graph.ranking import format_matches
from models import ComparisonResult, PatentRecord
from tests.test_pipeline_contract import (
    CLAIMS,
    DESCRIPTION,
    FAKE_SOURCES,
    _fake_llm_chat,
)

AUTH = {"Authorization": f"Bearer {issue_token('testnovelty@gmail.com')}"}

BANNED_KEYS = {"pct", "top_score", "verdict", "score", "similarity", "percent", "match_level"}
"""Names a reintroduced score would plausibly arrive under."""


@pytest.fixture
def deterministic():
    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", FAKE_SOURCES):
        yield


def _walk(node, path="$"):
    """Yield every ``(path, key, value)`` scalar entry in a nested structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _offending(payload):
    """Every entry whose *name* suggests a published similarity figure."""
    return [
        (path, key, value)
        for path, key, value in _walk(payload)
        if key.lower() in BANNED_KEYS
    ]


def _numeric_ratings(payload):
    """Every 0-100 number sitting where a rating belongs.

    Catches a percentage returning under an innocent name. ``query_count`` is
    excluded by name because it is a genuine count, not a rating.
    """
    allowed_numeric_keys = {"query_count", "rank", "family_size"}
    return [
        (path, key, value)
        for path, key, value in _walk(payload)
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and key.lower() not in allowed_numeric_keys
    ]


def test_the_result_event_publishes_no_score_or_verdict(deterministic):
    events = list(run_graph_pipeline(DESCRIPTION, CLAIMS))
    data = events[-1]["data"]

    assert data["patents"], "a run with no matches would make this vacuous"
    assert _offending(data) == []
    assert _numeric_ratings(data) == []


def test_the_stored_run_publishes_no_score_or_verdict(deterministic, report_store):
    list(run_graph_pipeline(DESCRIPTION, CLAIMS))

    stored = report_store.runs[0]
    assert stored["matches"], "a run with no matches would make this vacuous"
    assert _offending(stored) == []


def test_the_history_response_publishes_no_score_or_verdict(deterministic, report_store):
    list(run_graph_pipeline(DESCRIPTION, CLAIMS))

    entries = TestClient(server.app, headers=AUTH).get("/api/history").json()
    assert entries and entries[0]["patents"], "a bare response would make this vacuous"
    assert _offending(entries) == []


def test_the_json_export_payload_publishes_no_score_or_verdict(deterministic):
    """`services/export.ts` serializes the report object verbatim.

    ``downloadReportJson`` is ``JSON.stringify(data)`` over exactly the object
    asserted here, so pinning this payload pins the export without needing a
    browser.
    """
    events = list(run_graph_pipeline(DESCRIPTION, CLAIMS))
    round_tripped = json.loads(json.dumps(events[-1]["data"]))

    assert _offending(round_tripped) == []
    assert _numeric_ratings(round_tripped) == []


def _match_with_score(similarity: float) -> dict:
    comparison = ComparisonResult(
        patent=PatentRecord(id="US1A", title="A patent", abstract="", source="Google Patents"),
        similarity_score=similarity,
        unit_similarity_scores=[similarity],
    )
    return format_matches(
        [comparison], ["a rationale"], [["a tag"]], [["L1"]], 1
    )[0]


@pytest.mark.parametrize(
    "percent, expected",
    [
        (75, "High"),
        (74, "Medium"),
        (26, "Low"),
        (25, "Low"),
        # The ends, so a future refactor cannot satisfy the boundaries above by
        # accident while inverting the scale.
        (100, "High"),
        (0, "Low"),
    ],
)
def test_the_aggregate_risk_thresholds_hold_at_their_boundaries(percent, expected):
    assert _match_with_score(percent / 100)["risk"] == expected


def test_matches_are_ordered_strongest_first_without_a_published_score():
    """Ordering survives the score's removal.

    ``format_matches`` receives comparisons in arbitrary order here to prove the
    sort is real rather than an accident of the input already being sorted.
    """
    comparisons = [
        ComparisonResult(
            patent=PatentRecord(id=f"US{index}A", title=f"Patent {index}", abstract="", source="x"),
            similarity_score=similarity,
            unit_similarity_scores=[similarity],
        )
        for index, similarity in enumerate([0.30, 0.90, 0.10, 0.60])
    ]
    formatted = format_matches(
        comparisons,
        [f"rationale {i}" for i in range(4)],
        [[f"tag {i}"] for i in range(4)],
        [["L1"] for _ in range(4)],
        1,
    )

    assert [match["num"] for match in formatted] == ["US1A", "US3A", "US0A", "US2A"]
    # And the ratings come out grouped as a side effect of that single sort,
    # which is why section 5.2 forbids grouping first and sorting within groups.
    assert [match["risk"] for match in formatted] == ["High", "Medium", "Low", "Low"]


def test_a_strong_subsystem_match_is_not_rated_high_without_broad_coverage():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-SUBSYSTEM", title="Partial match"),
        similarity_score=0.88,
        unit_similarity_scores=[0.92, 0.87, 0.81, 0.30, 0.22, 0.18],
    )

    match = format_matches(
        [comparison], ["matches only the known subsystem"], [[]],
        [["L1", "L2", "L3"]], 6,
    )[0]

    assert match["risk"] == "Medium"


def test_a_strong_match_with_broad_limitation_coverage_can_be_high():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-BROAD", title="Broad match"),
        similarity_score=0.82,
        unit_similarity_scores=[0.84, 0.79, 0.76, 0.73, 0.68, 0.31],
    )

    match = format_matches(
        [comparison], ["matches five of six limitations"], [[]],
        [["L1", "L2", "L3", "L4", "L5"]], 6,
    )[0]

    assert match["risk"] == "High"


def test_a_weak_match_to_only_one_limitation_is_low():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-WEAK-PARTIAL", title="Weak partial match"),
        similarity_score=0.48,
        unit_similarity_scores=[0.61, 0.31, 0.28, 0.22],
    )

    match = format_matches(
        [comparison], ["weakly matches one limitation"], [[]], [["L1"]], 4
    )[0]

    assert match["risk"] == "Low"


def test_a_strong_match_to_only_one_limitation_is_low():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-STRONG-PARTIAL", title="Strong partial match"),
        similarity_score=0.52,
        unit_similarity_scores=[0.78, 0.34, 0.29, 0.24],
    )

    match = format_matches(
        [comparison], ["strongly matches one limitation"], [[]], [["L1"]], 4
    )[0]

    assert match["risk"] == "Low"


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [(0.45, "Medium"), (0.44, "Low")],
)
def test_medium_requires_the_aggregate_score_boundary_and_half_coverage(
    similarity, expected
):
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-MEDIUM-BOUNDARY", title="Coverage boundary"),
        similarity_score=similarity,
        unit_similarity_scores=[0.62, 0.58, 0.31, 0.24],
    )

    match = format_matches(
        [comparison], ["matches two of four limitations"], [[]], [["L1", "L2"]], 4
    )[0]

    assert match["risk"] == expected


def test_three_of_seven_limitations_is_medium_when_similarity_is_meaningful():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US5719557A", title="Smoke detector"),
        similarity_score=0.6972,
    )
    match = format_matches(
        [comparison], ["covers three reviewed limitations"], [[]],
        [["L1", "L2", "L3"]], 7,
    )[0]
    assert match["risk"] == "Medium"


def test_two_of_seven_limitations_remains_low_despite_meaningful_similarity():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-NARROW", title="Narrow subsystem"),
        similarity_score=0.6972,
    )
    match = format_matches(
        [comparison], ["covers only two reviewed limitations"], [[]],
        [["L1", "L2"]], 7,
    )[0]
    assert match["risk"] == "Low"


@pytest.mark.parametrize("similarity, expected", [(0.62, "Medium"), (0.44, "Low")])
def test_substantial_core_overlap_is_medium_despite_several_new_additions(
    similarity, expected
):
    """A grouped covered relation plus another substantive unit is not an
    isolated hit merely because new feedback features occupy three lines.
    """
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-CORE", title="Prior core mechanism"),
        similarity_score=similarity,
        unit_similarity_scores=[0.72, 0.63, 0.27, 0.21, 0.18],
    )
    match = format_matches(
        [comparison], ["shares a substantial mechanism"], [[]], [["L1"]], 5
    )[0]
    assert match["risk"] == expected


def test_semantic_breadth_without_excerpt_coverage_cannot_become_medium():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-SEMANTIC-ONLY", title="Similar vocabulary"),
        similarity_score=0.78,
        unit_similarity_scores=[0.72, 0.63, 0.27, 0.21, 0.18],
    )
    match = format_matches(
        [comparison], ["no direct disclosed relationship"], [[]], [[]], 5
    )[0]
    assert match["risk"] == "Low"


def test_substantial_core_overlap_without_broad_coverage_does_not_become_high():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-PARTIAL", title="Prior core mechanism"),
        similarity_score=0.88,
        unit_similarity_scores=[0.82, 0.69, 0.27, 0.21, 0.18],
    )
    match = format_matches(
        [comparison], ["new additions absent"], [[]], [["L1"]], 5
    )[0]
    assert match["risk"] == "Medium"


def test_risk_refuses_missing_reviewed_limitations():
    comparison = ComparisonResult(
        patent=PatentRecord(id="US-NO-PROFILE", title="Missing profile"),
        similarity_score=0.80,
    )

    with pytest.raises(ValueError, match="reviewed technical limitations"):
        format_matches([comparison], ["cannot be rated"], [[]], [[]], 0)
