"""Deterministic outcome derivation (spec section 6).

The model evaluates requirements individually; the **application** derives the
feature outcome, mechanically. The model also returns ``outcome``, and spec
section 9's check 15 recomputes it and rejects the response on mismatch - so the
returned value is a consistency check on the model's own reasoning, never the
source of truth.

The same function serves the human-reviewed outcome (spec section 13.3). One
function, two callers: a reviewed outcome that could contradict its own reviewed
statuses is structurally impossible.
"""

from collections.abc import Sequence

from .schema import Outcome, Status


def derive_outcome(statuses: Sequence[str]) -> Outcome:
    """Spec section 6.2, verbatim in behaviour.

    ``statuses`` covers every approved element **and** relationship, in approved
    order. Used for both the machine outcome and the human-reviewed outcome.

    Three consequences of the rule order are worth naming, because they are the
    properties under review rather than incidental:

    - ``FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES`` is unreachable when any
      requirement is ``NOT_FOUND_IN_SUPPLIED_PASSAGES`` or ``UNCLEAR``. It is not
      a judgment call.
    - Everything found with one ambiguity outstanding is ``UNCLEAR``, not "full".
      An unresolved ambiguity is precisely the case a human must see at Gate C, so
      it must not be rounded up.
    - Nothing found plus an ambiguity is ``UNCLEAR``, not
      ``NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES``, because the ambiguity might
      have resolved in favour of disclosure.
    """
    if not statuses:
        raise ValueError("a decomposition with no requirements cannot be verified")

    found = statuses.count(Status.FOUND)
    not_found = statuses.count(Status.NOT_FOUND_IN_SUPPLIED_PASSAGES)
    unclear = statuses.count(Status.UNCLEAR)

    if found + not_found + unclear != len(statuses):
        # Reachable only through a caller that assembled the list by hand; the
        # validated response path cannot produce it, because `status` is a closed
        # enum. Failing loudly beats deriving an outcome from a miscount.
        raise ValueError(f"unrecognized status value in {list(statuses)!r}")

    if not_found == 0 and unclear == 0:
        return Outcome.FULLY
    if found == 0 and unclear == 0:
        return Outcome.NONE_FOUND
    if found == 0 or not_found == 0:
        return Outcome.UNCLEAR
    return Outcome.PARTIALLY
