"""The pipeline's externally-visible contract: SSE phases, terminal payload
shape, and the failure behaviors the zero-tolerance policy requires.

This was ``test_pipeline_parity.py``, which diffed the LangGraph pipeline
against the legacy generator. Legacy has been retired along with Postgres, so
there is nothing left to diff against and the assertions now stand on their own:
they describe what the pipeline must do, rather than that two implementations
agreed. The tests that only existed to pin a legacy divergence (the ``PIPELINE``
flag, "legacy pads an empty search with a canned recommendation") went with it;
the ones that pin *this* pipeline's behavior are all still here.

**The network boundaries are stubbed, the logic between them is not**: the HTTP
call inside ``_llm_chat`` and the search providers in ``SOURCES`` here, plus
embeddings and Qdrant in ``tests/conftest.py``. Everything between them - prompt
construction, JSON parsing, deduplication, chunking, cosine scoring, score
formatting, verdict thresholds - runs for real. Stubbing ``understand_invention``
or ``compare_invention_to_patents`` instead would prove far less.
"""

import json
import re
from unittest.mock import patch

import pytest

from graph.events import PHASES
from graph.pipeline import run_graph_pipeline

from models import PatentRecord
from search.search import PatentSearchError

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

RESULT_KEYS = {
    "title", "date", "concepts", "patents",
    "top_score", "verdict", "query_count", "recommendations",
}
"""Every key ``graph/pipeline.py``'s ``_result_envelope`` promises the frontend."""

CORE_PATENT_FIELDS = {"num", "title", "pct", "risk", "rationale", "source"}
"""What the frontend actually reads off each patents[] entry (App.tsx's
BackendPatent)."""


def _fake_llm_chat(prompt: str, **kwargs: object) -> str:
    """Stand in for the HTTP call in ``_llm_chat``, keyed off the real prompts."""
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


def _drain(pipeline=run_graph_pipeline) -> list[dict]:
    return list(pipeline(DESCRIPTION, CLAIMS))


# --------------------------------------------------------------------------
# The SSE contract
# --------------------------------------------------------------------------

def test_every_agent_reports_a_phase_in_the_expected_order(deterministic):
    """Each agent announces itself, and the ordering the graph's topology
    implies is the ordering the client actually observes.

    ``semantic_search`` and ``fuse`` are excluded from the required set: both
    are TEMP: disabled together with ``index_patents`` in graph/pipeline.py
    (keyword_search feeds validate directly), so neither phase is emitted right
    now. Add them back to this set when re-enabling that wiring.
    """
    phases = [e["phase"] for e in _drain() if e["type"] == "progress"]

    assert {"understand", "queries", "search", "enrich",
            "validate", "compare", "analyze"} <= set(phases)
    assert phases.index("validate") < phases.index("compare")
    assert phases.index("compare") < phases.index("analyze")


def test_stream_ends_with_exactly_one_result(deterministic):
    """The frontend throws without one, and breaks with more than one."""
    events = _drain()
    assert [e["type"] for e in events].count("result") == 1
    assert events[-1]["type"] == "result"
    assert not any(e["type"] == "error" for e in events)


def test_every_phase_is_in_the_frontend_vocabulary(deterministic):
    """An unknown phase does not raise in the UI - it silently renders as the
    first checklist item. Assert here instead, where it is visible."""
    for event in _drain():
        if event["type"] == "progress":
            assert event["phase"] in PHASES


def test_result_payload_carries_the_whole_contract(deterministic):
    """The frontend reads every one of these; a missing key is a blank panel,
    not an error, so it has to be caught here."""
    result = _drain()[-1]

    assert result["type"] == "result"
    assert result["data"].keys() == RESULT_KEYS
    assert result["data"]["patents"], "expected the deterministic corpus to produce matches"
    assert CORE_PATENT_FIELDS <= result["data"]["patents"][0].keys()


# --------------------------------------------------------------------------
# Zero-tolerance failure behavior
# --------------------------------------------------------------------------

def test_one_provider_down_fails_the_request():
    """A provider outage silently narrows the corpus. Answering anyway would
    report a confident novelty verdict computed against prior art that was
    never retrieved, indistinguishable from a healthy run."""
    def broken(query: str, size: int) -> list[PatentRecord]:
        raise RuntimeError("EPO OPS 503")

    half_down = (("Google Patents", FAKE_SOURCES[0][1]), ("European Patent Office", broken))

    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", half_down):
        with pytest.raises(PatentSearchError, match="European Patent Office"):
            _drain()


def test_provider_failure_is_reported_in_query_order_not_completion_order():
    """Providers are now called concurrently. Which failure gets reported must
    still be decided by query order, or the same broken environment would blame
    a different provider run to run and make the error useless to act on."""
    def broken(name: str):
        def search(query: str, size: int) -> list[PatentRecord]:
            raise RuntimeError(f"{name} is down")
        return search

    # Both fail; "Google Patents" is first in SOURCES, so it must always be named.
    both_down = (("Google Patents", broken("google")), ("European Patent Office", broken("epo")))

    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", both_down):
        for _ in range(5):
            with pytest.raises(PatentSearchError, match="Google Patents"):
                _drain()


CANNED_RECOMMENDATION = "No reference targets found. Focus claims on wide utility targets."


def test_empty_corpus_returns_an_honest_empty_report():
    """Finding nothing is a real answer, so it is reported, not raised.

    The one thing it must not do is fill the recommendation slot with advice no
    model produced.
    """
    empty = (("Google Patents", _provider("empty", [])),)

    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", empty):
        result = _drain()[-1]

    data = result["data"]
    assert result["type"] == "result"
    assert data["recommendations"] == [], "fabricated advice reintroduced"
    assert data["patents"] == []
    assert data["top_score"] == 0
    # Real values -- the LLM did run, so these are measured, not invented.
    assert data["concepts"] == CONCEPTS
    assert data["query_count"] == len(QUERIES)


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
            _drain()


def test_zero_similarity_is_reported_honestly(deterministic):
    """A patent with no lexical overlap must show 0, not a floor."""
    unrelated = (("Google Patents", _provider("unrelated", [
        PatentRecord(id="US9999999A", title="Tractor gearbox",
                     abstract="A mechanical gearbox assembly for tractor power take-off.",
                     source="Google Patents"),
    ])),)
    with patch("search.search.SOURCES", unrelated):
        result = _drain()[-1]
    scores = [p["pct"] for p in result["data"]["patents"]]
    assert min(scores) == 0, f"expected an honest zero, got {scores}"
