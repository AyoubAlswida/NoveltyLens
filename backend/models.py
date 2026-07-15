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


@dataclass(slots=True)
class SearchQuery:
    """A search expression produced during invention understanding."""

    text: str


@dataclass(slots=True)
class InventionAnalysis:
    """Structured output of the LLM invention-understanding step."""

    queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    synonyms: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class ComparisonResult:
    """A patent and its TF-IDF similarity score to an invention."""

    patent: PatentRecord
    similarity_score: float


@dataclass(slots=True)
class MatchAnalysis:
    """Per-patent rationales (aligned 1:1 with the input patents) plus the
    shared uniqueness recommendations, from a single batched LLM call."""

    rationales: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
