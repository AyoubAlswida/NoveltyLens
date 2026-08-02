"""Parity between the legacy pipeline and the LangGraph pipeline.

This is the gate that turned ``PIPELINE=langgraph`` on.  Phase 1 could demand
byte-identical SSE streams; Phase 3 cannot, because the graph searches a broader
corpus than legacy can see (see ``test_graph_covers_every_legacy_phase_plus_the_new_agents``).
What is asserted now is that the graph emits every phase legacy does plus exactly
the new agents' phases, and that the terminal payload's *shape* still matches -
its values are allowed to differ, by design.  Where they differ, they differ
because a ticket decided they should.

**The network boundaries are stubbed, the logic between them is not**: the HTTP
call inside ``_llm_chat`` and the search providers in ``SOURCES`` here, plus
embeddings, Qdrant and Postgres in ``tests/conftest.py``.  Everything between
them - prompt construction, JSON parsing, deduplication, chunking, cosine
scoring, score formatting, verdict thresholds - runs for real in both pipelines.
Stubbing ``understand_invention`` or ``compare_invention_to_patents`` instead
would prove far less, because those are exactly the layers that could drift apart.
"""

import json
import re
from unittest.mock import patch

import pytest

from graph.events import PHASES
from graph.pipeline import run_graph_pipeline

from models import PatentRecord
from search.search import PatentSearchError
from server import _run_pipeline

DESCRIPTION = (
    "A portable, refillable drinking-water bottle that automatically sterilizes its "
    "contents with an integrated UV-C emitter and filters particulates through a "
    "replaceable graphene-oxide membrane."
)
CLAIMS = "1. A bottle comprising a UV-C emitter and a graphene-oxide membrane."

CONCEPTS = ["water sterilization", "membrane filtration", "flow sensing"]
KEYWORDS = ["UV-C", "graphene oxide", "flow rate sensor"]
QUERIES = [
    "uv-c water sterilization bottle",
    "graphene oxide filtration membrane",
    "flow rate sensor bottle cap",
]

CORPUS = [
    PatentRecord(id="US1000001A", title="Ultraviolet water sterilizer",
                 abstract="A device sterilizing water with ultraviolet light in a portable vessel.",
                 source="Google Patents"),
    PatentRecord(id="EP1000002A", title="Graphene oxide filtration membrane",
                 abstract="A replaceable membrane of graphene oxide for particulate filtration.",
                 source="European Patent Office"),
    PatentRecord(id="US1000003A", title="Bottle cap flow sensor",
                 abstract="A cap incorporating a flow rate sensor triggering a cleaning cycle.",
                 source=""),
    PatentRecord(id="US1000004A", title="Agricultural gearbox",
                 abstract="A mechanical gearbox assembly for tractor power take-off.",
                 source="Google Patents"),
]


def _fake_llm_chat(prompt: str, **kwargs: object) -> str:
    """Stand in for the HTTP call in ``_llm_chat``, keyed off the real prompts.

    Both pipelines call the same ``llm.py`` functions, so this single stub serves
    both and neither can drift onto a different prompt without the other.
    """
    if "PatentScout AI" in prompt:
        return json.dumps({"concepts": CONCEPTS, "keywords": KEYWORDS, "queries": QUERIES})

    if "screening" in prompt:
        # validate_relevance's prompt also contains "expert patent examiner", so
        # this check must come before the analyze_all_matches one below. Marks
        # every candidate relevant, keeping existing tests' candidate counts
        # unchanged unless a test overrides this stub.
        count = int(re.search(r"screening (\d+) candidate", prompt).group(1))
        return json.dumps({
            "verdicts": [{"index": i, "relevant": True} for i in range(1, count + 1)],
        })

    if "expert patent examiner" in prompt:
        # The prompt states how many patents it is analyzing; honor that exactly,
        # because analyze_all_matches raises on a missing rationale.
        count = int(re.search(r"against (\d+) prior-art patents", prompt).group(1))
        return json.dumps({
            "rationales": [
                {"index": i, "text": f"Deterministic rationale for patent {i}."}
                for i in range(1, count + 1)
            ],
            "recommendations": [f"Deterministic recommendation {i}." for i in range(1, 11)],
        })

    raise AssertionError(f"Unexpected LLM prompt: {prompt[:120]!r}")


def _provider(name: str, records: list[PatentRecord]):
    """Build a deterministic provider returning a fixed slice of the corpus."""
    def search(query: str, size: int) -> list[PatentRecord]:
        return records[:size]
    search.__name__ = f"fake_{name}"
    return search


FAKE_SOURCES = (
    ("Google Patents", _provider("google", [CORPUS[0], CORPUS[3]])),
    ("European Patent Office", _provider("epo", [CORPUS[1], CORPUS[2]])),
)


@pytest.fixture
def deterministic():
    """Pin both non-deterministic boundaries for the duration of a test."""
    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", FAKE_SOURCES):
        yield


def _drain(pipeline) -> list[dict]:
    return list(pipeline(DESCRIPTION, CLAIMS))


# --------------------------------------------------------------------------
# The flag
# --------------------------------------------------------------------------

def test_langgraph_is_the_default_pipeline(monkeypatch):
    """P1.5 flips the default. Asserted so it cannot drift back silently."""
    monkeypatch.delenv("PIPELINE", raising=False)
    import importlib

    import config

    reloaded = importlib.reload(config)
    try:
        assert reloaded.Settings().pipeline == "langgraph"
    finally:
        # Leave the module as the rest of the session expects to find it.
        importlib.reload(config)


def test_invalid_pipeline_value_is_rejected():
    """A typo must fail loudly rather than coerce to a working pipeline.

    Constructed directly rather than through a reload: reloading rebinds
    ``ConfigError`` to a new class object, so the raised instance would no
    longer match the reference this test captured.
    """
    import config

    with pytest.raises(config.ConfigError, match="langraph"):
        config.Settings(pipeline="langraph")


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def test_graph_covers_every_legacy_phase_plus_the_new_agents(deterministic):
    """Phase 3 makes the graph do genuinely more than legacy on the happy path -
    three new agents (semantic search, fusion, validation) legacy has no
    concept of - so the two streams are no longer byte-identical by design, only
    by accident before Phase 3 landed. What must still hold: every phase legacy
    emits, the graph emits too, and the graph's *only* extra phases are exactly
    the three new agents, in the expected relative order."""
    legacy_phases = [e["phase"] for e in _drain(_run_pipeline) if e["type"] == "progress"]
    graph_phases = [e["phase"] for e in _drain(run_graph_pipeline) if e["type"] == "progress"]

    assert set(legacy_phases) <= set(graph_phases)
    assert set(graph_phases) - set(legacy_phases) == {"semantic_search", "fuse", "validate"}
    assert graph_phases.index("fuse") < graph_phases.index("validate") < graph_phases.index("compare")
    assert graph_phases.index("compare") < graph_phases.index("analyze")


CORE_PATENT_FIELDS = {"num", "title", "pct", "risk", "rationale", "source"}
"""What the frontend actually reads off each patents[] entry (App.tsx's
BackendPatent). Legacy's `_format_and_rank` (server.py) also carries a
`full_text_chars` debug field the graph's `format_matches` never had - a
pre-existing, Phase-3-unrelated divergence, not something to pin key-for-key."""


def test_result_shape_matches(deterministic):
    """Phase 3 lets the graph consider more candidates than legacy ever could
    (semantic-only hits) and drop some via validation, so *values* are allowed
    to diverge now - but the payload's shape, the contract the frontend depends
    on, must not."""
    legacy = _drain(_run_pipeline)[-1]
    graph = _drain(run_graph_pipeline)[-1]

    assert legacy["type"] == graph["type"] == "result"
    assert legacy["data"].keys() == graph["data"].keys()
    if legacy["data"]["patents"] and graph["data"]["patents"]:
        assert CORE_PATENT_FIELDS <= legacy["data"]["patents"][0].keys()
        assert CORE_PATENT_FIELDS <= graph["data"]["patents"][0].keys()


def test_stream_ends_with_exactly_one_result(deterministic):
    """The frontend throws without one, and breaks with more than one."""
    events = _drain(run_graph_pipeline)
    assert [e["type"] for e in events].count("result") == 1
    assert events[-1]["type"] == "result"
    assert not any(e["type"] == "error" for e in events)


def test_every_phase_is_in_the_frontend_vocabulary(deterministic):
    """An unknown phase does not raise in the UI - it silently renders as the
    first checklist item. Assert here instead, where it is visible."""
    for event in _drain(run_graph_pipeline):
        if event["type"] == "progress":
            assert event["phase"] in PHASES


# --------------------------------------------------------------------------
# Divergences that are intentional. These assert the Phase 1 behavior changes
# actually happened; P3.8 extends this into full failure-path coverage.
# --------------------------------------------------------------------------

def test_one_provider_down_fails_the_graph_but_not_legacy():
    """P1.3's behavior change, asserted from both sides."""
    def broken(query: str, size: int) -> list[PatentRecord]:
        raise RuntimeError("EPO OPS 503")

    half_down = (("Google Patents", FAKE_SOURCES[0][1]), ("European Patent Office", broken))

    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", half_down):
        # Legacy silently narrows the corpus and still reports a verdict.
        legacy = _drain(_run_pipeline)
        assert legacy[-1]["type"] == "result"

        # The graph refuses to answer from a corpus it knows is incomplete.
        with pytest.raises(PatentSearchError, match="European Patent Office"):
            _drain(run_graph_pipeline)


CANNED_RECOMMENDATION = "No reference targets found. Focus claims on wide utility targets."


def test_empty_corpus_returns_an_honest_empty_report():
    """Finding nothing is a real answer, so it is reported, not raised.

    The one thing it must not do is fill the recommendation slot with advice no
    model produced. Legacy does exactly that; the graph sends an empty list.
    """
    empty = (("Google Patents", _provider("empty", [])),)

    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", empty):
        legacy = _drain(_run_pipeline)
        graph = _drain(run_graph_pipeline)

    assert legacy[-1]["data"]["recommendations"] == [CANNED_RECOMMENDATION]

    data = graph[-1]["data"]
    assert graph[-1]["type"] == "result"
    assert data["recommendations"] == [], "fabricated advice reintroduced"
    assert data["patents"] == []
    assert data["top_score"] == 0
    # Real values -- the LLM did run, so these are measured, not invented.
    assert data["concepts"] == CONCEPTS
    assert data["query_count"] == len(QUERIES)


def test_empty_corpus_differs_from_legacy_only_in_recommendations():
    """Pin the divergence to exactly one field of the terminal payload, so a
    future change that quietly widens it fails here.

    Progress events are allowed to differ here too, unlike before Phase 3:
    keyword search finding nothing doesn't stop the graph's independent
    semantic-search and fusion steps from running - a real, if empty, result -
    and legacy has no concept of either."""
    empty = (("Google Patents", _provider("empty", [])),)

    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", empty):
        legacy = _drain(_run_pipeline)
        graph = _drain(run_graph_pipeline)

    legacy_phases = [e["phase"] for e in legacy if e["type"] == "progress"]
    graph_phases = [e["phase"] for e in graph if e["type"] == "progress"]
    assert set(graph_phases) - set(legacy_phases) <= {"semantic_search", "fuse", "validate"}

    differing = {
        key for key in legacy[-1]["data"]
        if legacy[-1]["data"][key] != graph[-1]["data"][key]
    }
    assert differing == {"recommendations"}, f"unexpected divergence in {differing}"


def test_canned_recommendation_appears_nowhere_in_the_graph_package():
    """A string-level guard against the fallback being copied back in.

    The graph modules describe this fallback in prose but never quote it, so any
    occurrence of the literal is a real reintroduction.
    """
    import pathlib

    graph_dir = pathlib.Path(__file__).resolve().parent.parent / "graph"
    offenders = [
        path.name
        for path in graph_dir.rglob("*.py")
        if CANNED_RECOMMENDATION in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"canned recommendation text found in {offenders}"


def test_llm_failure_raises_rather_than_degrading():
    """No truncated-description query, no canned rationale."""
    with patch("llm._llm_chat", side_effect=RuntimeError("LLM request failed")), \
         patch("search.search.SOURCES", FAKE_SOURCES):
        with pytest.raises(RuntimeError, match="LLM request failed"):
            _drain(run_graph_pipeline)


def test_zero_similarity_is_reported_honestly(deterministic):
    """A patent with no lexical overlap must show 0, not a floor."""
    unrelated = (("Google Patents", _provider("unrelated", [
        PatentRecord(id="US9999999A", title="Tractor gearbox",
                     abstract="A mechanical gearbox assembly for tractor power take-off.",
                     source="Google Patents"),
    ])),)
    with patch("search.search.SOURCES", unrelated):
        result = _drain(run_graph_pipeline)[-1]
    scores = [p["pct"] for p in result["data"]["patents"]]
    assert min(scores) == 0, f"expected an honest zero, got {scores}"
