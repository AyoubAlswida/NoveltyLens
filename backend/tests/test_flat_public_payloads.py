"""The two public payloads are flat, and core semantics never reach them.

Spec section 4.2. The understanding stage exposes a description, a summary and a
list of element **strings**; the strategy stage exposes a list of query
**strings**. Capability objects, `core`, relationships, per-element ids, query
ids, enable/disable switches and coverage claims are all internal or gone, and
none of them may travel in a response.

The `core` check is a **source grep**, because that is what section 4.2.3 asks
for and because it catches the reintroduction rather than one instance of it: a
payload builder that starts reading `core` fails here even if no test exercises
the branch that would expose it.
"""

import ast
import inspect
from pathlib import Path

import pytest

from api import runs as runs_api
from workflow import gate_state

BACKEND = Path(__file__).resolve().parent.parent

WITHDRAWN_CONCEPTS = (
    "core",
    "capabilit",
    "relationship",
    "covered_element",
    "coverage",
    "enabled",
    "rationale",
)
"""Vocabulary that must not appear in a public payload builder.

Substrings, so `covered_element_ids` and `capabilities` are both caught. Each
names something the reviewer can no longer see or set; a payload field carrying
one would offer a control the screens do not have.
"""


def _payload_builder_source(name: str) -> str:
    return inspect.getsource(getattr(gate_state, name))


@pytest.mark.parametrize("builder", ["gate_a_payload", "gate_strategy_payload"])
def test_no_public_payload_builder_mentions_a_withdrawn_concept(builder):
    """Section 4.2.3: **core semantics are never exposed.**

    Every element is stored `core = TRUE` so the internal `NO_CORE_ELEMENT`
    condition is unreachable rather than deleted -- but that is storage. No API
    response, no screen and no export mentions core, and the payload builders are
    where that would leak first.
    """
    source = _payload_builder_source(builder).lower()
    # Strip the docstring: these builders explain in prose what they no longer
    # carry, and the explanation is the opposite of a reintroduction.
    tree = ast.parse(inspect.getsource(getattr(gate_state, builder)).strip())
    function = tree.body[0]
    if (
        function.body
        and isinstance(function.body[0], ast.Expr)
        and isinstance(function.body[0].value, ast.Constant)
    ):
        source = source.replace(str(function.body[0].value.value).lower(), "")

    for concept in WITHDRAWN_CONCEPTS:
        assert concept not in source, (
            f"{builder} mentions {concept!r}; the public payload must not carry a "
            "concept the reviewer cannot see or set"
        )


def test_the_understanding_payload_is_exactly_three_business_fields():
    state = {
        "description": "the inventor's own words",
        "candidate_decomposition": {
            "summary": "a machine summary",
            "features": [
                {
                    "feature_id": "F1",
                    "elements": [
                        {"element_id": "E1", "text": "a membrane", "core": True},
                        {"element_id": "E2", "text": "a flow sensor", "core": False},
                    ],
                },
                {
                    "feature_id": "F2",
                    "elements": [{"element_id": "E1", "text": "a UV-C emitter", "core": True}],
                },
            ],
        },
    }
    payload = gate_state.gate_a_payload(state)

    assert set(payload) == {"original_description", "technical_summary", "technical_elements"}
    # Groups are concatenated in order and their structure is dropped. The two
    # `E1`s in different groups are two different elements and both survive --
    # which is why the flat list can carry no ids.
    assert payload["technical_elements"] == ["a membrane", "a flow sensor", "a UV-C emitter"]
    assert all(isinstance(item, str) for item in payload["technical_elements"])


def test_the_strategy_payload_is_exactly_one_business_field():
    state = {
        "candidate_search_strategy": {
            "queries": [
                {"query_id": "Q1", "query_text": "membrane filtration", "enabled": True},
                {"query_id": "Q2", "query_text": "uv sterilisation", "enabled": False},
            ]
        }
    }
    payload = gate_state.gate_strategy_payload(state)

    assert set(payload) == {"queries"}
    # The disabled proposal is not shown as a query the reviewer cannot switch
    # off -- there is no switch, so every query that survives is one that runs.
    assert payload["queries"] == ["membrane filtration", "uv sterilisation"]
    assert all(isinstance(item, str) for item in payload["queries"])


def test_an_empty_state_produces_empty_lists_not_missing_keys():
    """A proposal that produced nothing is still a shape the screen can render."""
    assert gate_state.gate_a_payload({})["technical_elements"] == []
    assert gate_state.gate_strategy_payload({})["queries"] == []


# --- what the flat list becomes on the way into storage --------------------


def test_every_approved_element_is_written_core():
    """Section 4.2.3, and the reason `NO_CORE_ELEMENT` is unreachable.

    `decomposition_elements.core` is NOT NULL with no default, so a value must be
    supplied. `TRUE` for every element is the honest encoding of "all approved
    elements count" -- the reviewer no longer marks anything as core, and writing
    some of them `FALSE` would record a distinction nobody made.
    """
    group = runs_api._one_technical_group("a summary", ["a membrane", "a flow sensor"])

    assert len(group) == 1, "the flat list is stored as ONE technical group"
    elements = group[0]["elements"]
    assert [element["core"] for element in elements] == [True, True]
    assert [element["element_id"] for element in elements] == ["E1", "E2"]
    assert [element["order"] for element in elements] == [1, 2]


def test_no_relationship_row_is_ever_written():
    """Relationships are gone from the product, so the table gets no rows."""
    group = runs_api._one_technical_group("a summary", ["a membrane"])
    assert group[0]["relationships"] == []


def test_element_ids_are_minted_in_the_reviewers_order_never_submitted():
    """Which is why duplicate and missing ids are unreachable rather than
    validated against: the public shape is a list of strings."""
    group = runs_api._one_technical_group("s", ["third", "first", "second"])
    assert [(e["element_id"], e["text"]) for e in group[0]["elements"]] == [
        ("E1", "third"),
        ("E2", "first"),
        ("E3", "second"),
    ]


def test_every_approved_query_is_enabled_and_claims_no_coverage():
    """Enable/disable is gone, so a stored query that would not run cannot exist;
    coverage is gone, so an empty claim is the honest value rather than a
    fabricated one."""
    queries = runs_api._numbered_queries(["membrane filtration", "uv sterilisation"])

    assert [query["query_id"] for query in queries] == ["Q1", "Q2"]
    assert all(query["enabled"] for query in queries)
    assert all(query["covered_element_ids"] == [] for query in queries)
