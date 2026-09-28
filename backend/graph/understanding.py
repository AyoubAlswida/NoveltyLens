"""Query Understanding and Query Expansion nodes.

``understand_node`` wraps the existing ``backend/llm.py`` analysis;
``expand_queries_node`` turns that analysis into the bounded retrieval plan.
Neither catches anything: ``understand_invention`` raises on a failed LLM call, on a
non-JSON response, and on malformed JSON, and every one of those must fail the
request.  There is deliberately no ``except`` in this module.

In particular there is no "use a truncated description as the query" fallback.
Quick Search does, however, always add a small set of deterministic lexical
anchor queries built from the model's own extracted concepts and the user's
domain phrase. Those are a declared part of the retrieval plan, not a fallback
activated after poor results. They protect recall when a good semantic
paraphrase omits the exact component vocabulary used by an older patent.
"""

import re

from graph.events import emit_progress
from graph.state import GraphState
from llm import understand_invention
from models import InventionAnalysis
from workflow.types import UnapprovedArtifactError


MAX_QUICK_RETRIEVAL_QUERIES = 8
"""Five model-planned angles plus at most three lexical concept anchors.

Each query is one Lens call. Three anchors cover the structural/functional core
without allowing an added subsystem to double the provider spend or crowd out
the source mechanism the benchmark is trying to recover.
"""

MAX_ANCHOR_QUERY_TERMS = 7
MAX_LEXICAL_ANCHORS = 3
_DOMAIN_BOUNDARIES = frozenset(
    {
        "that",
        "which",
        "whose",
        "wherein",
        "with",
        "containing",
        "comprising",
        "including",
        "includes",
        "having",
        "has",
        "using",
        "uses",
    }
)
_ANCHOR_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "to",
        "from",
        "into",
        "in",
        "on",
        "at",
        "by",
        "via",
        "its",
        "their",
        "each",
        "same",
        "one",
        "several",
        "actual",
        "small",
    }
)


def _query_tokens(text: str) -> list[str]:
    """Patent-search tokens in source order, deduplicated case-insensitively."""
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", text):
        normalized = token.casefold()
        if normalized in _ANCHOR_STOPWORDS or normalized in seen:
            continue
        seen.add(normalized)
        tokens.append(token)
    return tokens


def _domain_anchor_tokens(description: str) -> list[str]:
    """Take the concrete platform nearest the first descriptive boundary.

    Hyphenated prose is split here so ``dry-pet-food dispenser`` and
    ``pet food dispenser`` deduplicate naturally. The last three prefix terms
    normally hold the platform noun while dropping generic leading modifiers
    such as ``automatic`` or ``portable``.
    """
    tokens = _query_tokens(description.replace("-", " "))
    prefix: list[str] = []
    for token in tokens:
        if token.casefold() in _DOMAIN_BOUNDARIES and prefix:
            break
        prefix.append(token)
        if len(prefix) == 6:
            break
    return prefix[-3:]


def _distributed_terms(tokens: list[str], count: int) -> list[str]:
    """Sample a compound requirement across its whole span, in source order."""
    if count <= 0 or not tokens:
        return []
    if len(tokens) <= count:
        return tokens
    if count == 1:
        return [tokens[0]]
    indices = [round(index * (len(tokens) - 1) / (count - 1)) for index in range(count)]
    return [tokens[index] for index in dict.fromkeys(indices)]


def _normalized_query(query: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", query.casefold()))


def build_retrieval_queries(
    description: str,
    primary_queries: list[str],
    concepts: list[str],
    keywords: list[str],
    *,
    max_queries: int,
    prefer_trailing_phrase: bool = False,
) -> list[str]:
    """Add traceable lexical anchors to a bounded model-planned retrieval set.

    The model remains responsible for patent-language synonyms and varied search
    perspectives. The anchors complement that plan with phrases whose provenance
    is mechanically auditable: the user's leading domain phrase plus an extracted
    keyword-phrase pair (Quick Search) or approved requirement (Advanced Search),
    in source order.
    No patent number, product
    vocabulary, search result, or benchmark-specific term is consulted.
    """
    queries: list[str] = []
    seen: set[str] = set()

    def append(query: str) -> None:
        cleaned = " ".join(str(query).split())
        normalized = _normalized_query(cleaned)
        if not cleaned or not normalized or normalized in seen:
            return
        seen.add(normalized)
        queries.append(cleaned)

    for query in primary_queries:
        append(query)

    domain = _domain_anchor_tokens(description)
    paired_keywords = [
        " ".join(keywords[index : index + 2])
        for index in range(min(len(keywords) - 1, MAX_LEXICAL_ANCHORS))
    ] if not prefer_trailing_phrase else []
    anchor_sources: list[tuple[str, bool]] = [
        (source, True) for source in paired_keywords
    ] + [(source, prefer_trailing_phrase) for source in concepts]
    # A valid understanding normally has several concepts. If it has fewer
    # than the three anchor slots, fill the remaining source material from
    # adjacent essential keyword phrases returned by the same call. This is
    # still pre-search, deterministic expansion—not a response to weak results.
    if not anchor_sources and keywords:
        anchor_sources = [(" ".join(keywords), True)]

    domain_terms = {item.casefold() for item in domain}
    anchors_added = 0
    for concept, use_trailing_phrase in anchor_sources:
        if len(queries) >= max_queries or anchors_added >= MAX_LEXICAL_ANCHORS:
            break
        concept_tokens = [
            token
            for token in _query_tokens(concept)
            if token.casefold() not in domain_terms
        ]
        available = MAX_ANCHOR_QUERY_TERMS - len(domain)
        # Advanced Search feeds this helper the approved, independently
        # checkable requirements. The trailing phrase preserves a specific
        # component relationship after the mandatory domain prefix instead of
        # reusing an over-combined model query or sampling isolated nouns.
        if use_trailing_phrase:
            selected = concept_tokens[-available:]
            anchor = [*domain, *selected]
            if len(anchor) >= 3:
                before = len(queries)
                append(" ".join(anchor))
                anchors_added += len(queries) - before
            continue

        # Preserve short keyword phrases when the Quick Search understanding
        # associated them with this concept. Sampling isolated terms can turn
        # "trap door" and "support bar" into unrelated single words and lose
        # the exact lexical bridge an older patent uses.
        concept_terms = {token.casefold() for token in concept_tokens}
        selected: list[str] = []
        selected_terms: set[str] = set()
        for keyword in keywords:
            keyword_tokens = _query_tokens(keyword)
            if not ({token.casefold() for token in keyword_tokens} & concept_terms):
                continue
            for token in keyword_tokens:
                normalized = token.casefold()
                if normalized in domain_terms or normalized in selected_terms:
                    continue
                selected.append(token)
                selected_terms.add(normalized)
                if len(selected) == available:
                    break
            if len(selected) == available:
                break
        if len(selected) < available:
            remaining = [
                token for token in concept_tokens
                if token.casefold() not in selected_terms
            ]
            selected.extend(_distributed_terms(remaining, available - len(selected)))
        anchor = [*domain, *selected]
        if len(anchor) >= 3:
            before = len(queries)
            append(" ".join(anchor))
            anchors_added += len(queries) - before

    if not queries:
        raise ValueError("Invention understanding produced no executable search queries")
    if len(queries) > max_queries:
        raise ValueError("Retrieval query plan exceeds its provider-call limit")
    return queries


def build_quick_retrieval_queries(
    description: str,
    analysis: InventionAnalysis,
) -> list[str]:
    """Build Quick Search's five model angles plus lexical recall anchors."""
    return build_retrieval_queries(
        description,
        list(analysis.queries),
        list(analysis.concepts),
        list(analysis.keywords),
        max_queries=MAX_QUICK_RETRIEVAL_QUERIES,
    )


def understand_node(state: GraphState) -> dict:
    """Extract concepts, keywords and search queries from the invention.

    Raises:
        Exception: whatever ``understand_invention`` raises - an unreachable or
            erroring LLM, a response that is not JSON, or JSON in the wrong
            shape.  All of them fail the request.
        ValueError: if the call succeeded but produced no usable queries.  An
            empty query set cannot retrieve prior art, so continuing would
            guarantee a result built on nothing.
    """
    emit_progress("understand", "Extracting technical concepts & keywords…")

    description = state["description"]
    claims = state.get("claims")
    analysis = understand_invention(description)
    if not analysis.queries:
        raise ValueError("Invention understanding returned no search queries")

    return {
        "analysis": analysis,
        # The text ranking scores patents against.  Assembled here so the
        # ranking node never has to know how it was built.
        "invention_text": (
            f"{description} {claims or ''} "
            f"{' '.join(analysis.concepts)} {' '.join(analysis.keywords)}"
        ),
        # Quick Search has no reviewed limitation list, but its existing LLM
        # call already returns technical concepts. Preserve those boundaries so
        # breadth-aware risk is generic across both search modes.
        "similarity_units": analysis.concepts,
    }


def expand_queries_node(state: GraphState) -> dict:
    """Build the complete, bounded Quick Search retrieval plan."""
    queries = build_quick_retrieval_queries(state["description"], state["analysis"])
    emit_progress(
        "queries",
        f"Generated {len(queries)} search queries",
        count=len(queries),
        queries=queries,
    )
    return {"queries": queries}


def prepare_approved_search_node(state: GraphState) -> dict:
    """Build shared search inputs after the two human-approved artifacts.

    Advanced Search has already paid for a reviewed technical summary and
    reviewed requirements. Its queries are either system-generated (flow v2) or
    human-approved (legacy flow v1). Running ``understand_invention`` before
    those reviews duplicated the work, generated queries nobody would use, and
    made the progress screen claim the search plan existed before the reviewer
    approved what the invention meant.

    This node performs no model call and emits no progress phase. It preserves
    the ordinary downstream contract by constructing ``InventionAnalysis`` and
    ``invention_text`` from approved text only. Missing approved input fails
    loudly; raw or candidate text is never substituted for an approval.
    """
    summary = str(state.get("approved_summary") or "").strip()
    approved = state.get("approved_decomposition") or {}
    queries = [str(query).strip() for query in (state.get("queries") or ()) if str(query).strip()]

    limitations = [
        str(element.get("text") or "").strip()
        for feature in (approved.get("features") or ())
        for element in (feature.get("elements") or ())
        if str(element.get("text") or "").strip()
    ]
    if not summary:
        raise UnapprovedArtifactError(
            "Advanced Search cannot start without the approved technical summary"
        )
    if not limitations:
        raise UnapprovedArtifactError(
            "Advanced Search cannot start without approved invention requirements"
        )
    if not queries:
        raise UnapprovedArtifactError(
            "Advanced Search cannot start without validated search queries"
        )

    description = str(state.get("description") or "").strip()
    claims = str(state.get("claims") or "").strip()
    invention_text = "\n\n".join(
        part for part in (description, claims, summary, *limitations) if part
    )
    return {
        "analysis": InventionAnalysis(
            queries=queries,
            keywords=[],
            concepts=limitations,
        ),
        "invention_text": invention_text,
        # Preserve the reviewer-approved boundaries. Ranking can now reward a
        # patent for covering the invention broadly instead of letting one
        # strongly matching paragraph stand in for every limitation.
        "similarity_units": limitations,
    }


__all__ = [
    "build_quick_retrieval_queries",
    "build_retrieval_queries",
    "expand_queries_node",
    "prepare_approved_search_node",
    "understand_node",
]
