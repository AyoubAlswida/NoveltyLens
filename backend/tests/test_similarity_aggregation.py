"""Coverage-aware ranking tests.

The score stays transient, but its ordering is product behaviour: a single
matching paragraph must not outrank a patent that covers the invention broadly.
"""

import pytest

from compare import (
    _aggregate_similarity,
    _coherent_similarity_window,
    compare_invention_to_patents,
)
from models import PatentRecord


def test_broad_coverage_beats_one_isolated_perfect_match():
    broad = _aggregate_similarity(
        [[0.80, 0.20], [0.75, 0.10], [0.70, 0.15]],
        [0.70, 0.68, 0.65],
    )
    isolated = _aggregate_similarity(
        [[1.00, 0.10], [0.05, 0.03], [0.04, 0.02]],
        [0.45, 0.08, 0.06],
    )

    assert broad > isolated


def test_score_uses_the_documented_weighted_components():
    score = _aggregate_similarity(
        [[0.9, 0.3], [0.6, 0.2]],
        [0.7, 0.5],
    )

    # coverage=(0.9+0.6)/2; top-three=(0.9+0.6+0.3)/3; title=0.7
    assert score == pytest.approx(0.60 * 0.75 + 0.25 * 0.60 + 0.15 * 0.70)


@pytest.mark.parametrize(
    ("matrix", "title_scores"),
    [([], [0.2]), ([[]], [0.2]), ([[0.2]], [])],
)
def test_aggregation_refuses_incomplete_inputs(matrix, title_scores):
    with pytest.raises(ValueError, match="non-empty inputs"):
        _aggregate_similarity(matrix, title_scores)


def test_scattered_matches_cannot_be_collected_across_a_long_document():
    matrix = [
        [0.95, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10],
        [0.10, 0.10, 0.10, 0.95, 0.10, 0.10, 0.10],
        [0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.95],
    ]

    coherent = _coherent_similarity_window(matrix, window_size=3)

    assert sum(max(row) >= 0.90 for row in coherent) == 1


def test_adjacent_support_is_preserved_by_the_coherent_window():
    matrix = [
        [0.10, 0.91, 0.20, 0.10],
        [0.10, 0.20, 0.88, 0.10],
    ]

    coherent = _coherent_similarity_window(matrix, window_size=2)

    assert [max(row) for row in coherent] == pytest.approx([0.91, 0.88])


def test_ranking_keeps_each_limitations_best_match_as_transient_evidence(monkeypatch):
    vectors = iter([
        [[1.0, 0.0], [0.8, 0.6]],
        [[1.0, 0.0], [0.6, 0.0]],
    ])
    monkeypatch.setattr("compare.embed_texts", lambda _texts: next(vectors))
    monkeypatch.setattr("compare.chunk_text", lambda text, *_args: [text])

    result = compare_invention_to_patents(
        "whole invention",
        [PatentRecord(id="P1", title="title", abstract="abstract", full_text="claims")],
        ["first limitation", "second limitation"],
    )[0]

    assert result.unit_similarity_scores == pytest.approx([1.0, 0.8])
