"""Review-burden limits for the inventor-facing understanding stage.

Two different kinds of number live here and the difference matters.

**`MAX_CAPABILITIES` is derived, not chosen.** The verification matrix is
`features x publications`, `verify_features_node` refuses to build more than
`MATRIX_CELL_CAP` cells, and `rank_and_reduce` narrows to
`MAX_APPROVED_CANDIDATES` publications. So the largest number of capabilities
that can ever survive a full candidate set is the quotient of those two. Writing
`5` here instead would be a second copy of a reviewed constant that could drift
out of step with the constant it came from; a reviewer raising `MATRIX_CELL_CAP`
deliberately should not have to remember this file.

**`MAX_ELEMENTS_PER_CAPABILITY` and `MAX_ELEMENTS_TOTAL` are chosen** (owner
decision, 2026-08-30), and they are *not* matrix-derived: elements do not
multiply cells. One cell asks about one capability and every element inside it at
once, so adding an element lengthens a prompt and lengthens what a reviewer reads,
and neither of those is the cell cap. They are review-burden and prompt-size
judgments, held here as named constants so raising one is a deliberate edit with
this reasoning next to it.

**The UI must not present these as matrix-derived.** A reviewer told "you may
have five capabilities because the matrix caps at 75 cells" has been shown an
implementation detail in place of a reason. Tell them the limit; the derivation
belongs here.
"""

from graph.evidence_nodes import MAX_APPROVED_CANDIDATES
from graph.verification_nodes import MATRIX_CELL_CAP

MAX_CAPABILITIES = MATRIX_CELL_CAP // MAX_APPROVED_CANDIDATES
"""Derived: the most capabilities that can coexist with a full candidate set.

75 // 15 = 5 today. Never hardcode the 5 -- see this module's docstring."""

MAX_ELEMENTS_PER_CAPABILITY = 8
"""Chosen (owner decision, 2026-08-30). Bounds one verifier prompt and one
reviewer's reading of a single capability. Not matrix-derived."""

MAX_ELEMENTS_TOTAL = 25
"""Chosen (owner decision, 2026-08-30). Bounds the whole understanding a person
edits in one sitting, independently of how it is grouped -- five capabilities of
eight elements each would be forty, which is more than the stage is meant to ask
anyone to review."""

MAX_STRATEGY_QUERIES = 10
"""The most queries an approved search strategy may carry.

**Chosen, and NOT an owner decision yet.** It is recorded here as outstanding
rather than presented as settled, in the same spirit as `RETRIEVAL_FLOOR` being
`None`: what this number should be depends on the discovery provider's rate and
quota behaviour, and measuring Lens.org is explicitly out of scope for this
stage. Until that measurement exists, the honest thing is a bound with its
reasoning attached rather than no bound at all.

Why a bound at all, and why this one: every enabled query is a metered provider
call plus the enrichment its results pull, so an unbounded list is an unbounded
spend that one approval commits the deployment to. Ten is twice what the
automatic pipeline generates on its own (`llm.understand_invention` asks for
exactly five), which leaves a reviewer room to add without the set becoming
something nobody can meaningfully review in one sitting.

Like the element limits, it is enforced in `workflow/review.py` (binding) and
shown in the stage payload so a reviewer meets it while editing. Neither place
explains the derivation -- see this module's docstring."""

__all__ = [
    "MAX_CAPABILITIES",
    "MAX_ELEMENTS_PER_CAPABILITY",
    "MAX_ELEMENTS_TOTAL",
    "MAX_STRATEGY_QUERIES",
]
