"""Typed models passed between the novelty-search pipeline stages."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class PatentRecord:
    """A patent normalized by any search provider."""

    id: str
    title: str
    abstract: str = ""
    inventors: list[str] = field(default_factory=list)
    assignee: str = ""
    publication_date: str = ""
    source: str = ""
    url: str = ""
    serpapi_id: str = ""
    """Google Patents Details identifier (e.g. 'patent/US1234567B2/en'), used to fetch full text."""
    full_text: str = ""
    """Full description + claims text fetched from Google Patents Details, when available."""


@dataclass(slots=True)
class InventionAnalysis:
    """Structured output of the LLM invention-understanding step."""

    queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ComparisonResult:
    """A patent and its embedding cosine similarity score to an invention."""

    patent: PatentRecord
    similarity_score: float

@dataclass(slots=True)
class MatchAnalysis:
    """Per-patent rationales and key-overlap tags (both aligned 1:1 with the
    input patents) plus the shared uniqueness recommendations, from a single
    batched LLM call."""

    rationales: list[str] = field(default_factory=list)
    key_overlaps: list[list[str]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
