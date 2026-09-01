"""The approved feature decomposition the verifier consumes (spec section 2).

The verifier never invents, merges, splits, reorders, generalizes, or rewrites a
requirement. It is handed a decomposition a human approved at Gate A and its
whole job is to answer against exactly those requirements - which is what spec
section 9's checks 5 and 6 make binding rather than advisory.

This module models the decomposition and enforces the structural half of Gate A's
completion rules (spec section 12.1). The other half - that a *person* explicitly
approved a specific version - is a workflow property, not a data property, and is
represented here only by the recorded approver and timestamp.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApprovedElement:
    """One required technical element of an approved feature."""

    element_id: str
    text: str
    core: bool
    order: int


@dataclass(frozen=True, slots=True)
class ApprovedRelationship:
    """One required relationship between elements of an approved feature."""

    relationship_id: str
    text: str
    order: int


@dataclass(frozen=True, slots=True)
class ApprovedDecomposition:
    """A versioned, human-approved decomposition of one technical feature.

    ``elements`` and ``relationships`` are held in approved display order, which
    is the order the prompt renders them in and the order the response must
    return them in.
    """

    decomposition_id: str
    decomposition_version: str
    approved_by: str
    approved_at: str
    feature_id: str
    feature_version: str
    feature_text: str
    elements: tuple[ApprovedElement, ...]
    relationships: tuple[ApprovedRelationship, ...] = ()

    @property
    def element_ids(self) -> tuple[str, ...]:
        return tuple(element.element_id for element in self.elements)

    @property
    def relationship_ids(self) -> tuple[str, ...]:
        return tuple(relationship.relationship_id for relationship in self.relationships)

    @property
    def requirement_count(self) -> int:
        """Total approved requirements - elements and relationships together.

        This is what spec section 6's ``T`` counts and what section 14 scales
        ``max_tokens`` by.
        """
        return len(self.elements) + len(self.relationships)

    @classmethod
    def from_json(cls, data: dict) -> "ApprovedDecomposition":
        """Load the spec section 2 wire shape, ordering by the approved ``order``."""
        elements = sorted(
            (
                ApprovedElement(
                    element_id=str(item["element_id"]),
                    text=str(item["text"]),
                    core=bool(item["core"]),
                    order=int(item["order"]),
                )
                for item in data.get("elements", [])
            ),
            key=lambda element: element.order,
        )
        relationships = sorted(
            (
                ApprovedRelationship(
                    relationship_id=str(item["relationship_id"]),
                    text=str(item["text"]),
                    order=int(item["order"]),
                )
                for item in data.get("relationships", [])
            ),
            key=lambda relationship: relationship.order,
        )
        decomposition = cls(
            decomposition_id=str(data["decomposition_id"]),
            decomposition_version=str(data["decomposition_version"]),
            approved_by=str(data["approved_by"]),
            approved_at=str(data["approved_at"]),
            feature_id=str(data["feature_id"]),
            feature_version=str(data["feature_version"]),
            feature_text=str(data["feature_text"]),
            elements=tuple(elements),
            relationships=tuple(relationships),
        )
        validate_decomposition(decomposition)
        return decomposition


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def validate_decomposition(decomposition: ApprovedDecomposition) -> None:
    """Enforce the structural half of Gate A's completion rules (spec section 12.1).

    Raises ``ValueError``, not a cell state: an invalid decomposition is a
    programming or Gate-A error upstream of the matrix, not a verdict about a
    publication. A decomposition with zero elements is invalid and cannot pass
    Gate A (spec section 2), which is also why ``derive_outcome`` refuses an empty
    status list rather than inventing an outcome for it.
    """
    if not decomposition.elements:
        raise ValueError("a decomposition with no technical elements cannot pass Gate A")

    for element in decomposition.elements:
        if not element.element_id.strip():
            raise ValueError("every approved element needs a stable, non-empty ID")
        if not element.text.strip():
            raise ValueError(f"element {element.element_id} has empty text")
    for relationship in decomposition.relationships:
        if not relationship.relationship_id.strip():
            raise ValueError("every approved relationship needs a stable, non-empty ID")
        if not relationship.text.strip():
            raise ValueError(f"relationship {relationship.relationship_id} has empty text")

    # Uniqueness is required *across both kinds*, not within each. Spec section
    # 13.3's reference implementation merges element and relationship reviews
    # into one dict keyed by requirement ID, so an element and a relationship
    # sharing an ID would silently drop one requirement's reviewed status and
    # derive a reviewed outcome from the wrong set.
    repeated = _duplicates([*decomposition.element_ids, *decomposition.relationship_ids])
    if repeated:
        raise ValueError(f"requirement IDs must be unique across elements and relationships: {repeated}")

    for label, ordered in (
        ("element", decomposition.elements),
        ("relationship", decomposition.relationships),
    ):
        orders = [item.order for item in ordered]
        if len(set(orders)) != len(orders):
            raise ValueError(f"{label} order values must be unique: {orders}")
        if orders != sorted(orders):
            raise ValueError(f"{label}s must be held in approved order: {orders}")


def requirement_ids(decomposition: ApprovedDecomposition) -> Sequence[str]:
    """Every approved requirement ID, elements first, each in approved order."""
    return (*decomposition.element_ids, *decomposition.relationship_ids)
