"""Generate `docs/acceptance/matrix-traceability.md` from the enforced mapping.

Run it with `python -m acceptance.matrix_traceability` from `backend/`.

The table is generated rather than written because a hand-maintained one goes
stale silently, and a stale traceability table is worse than none: it is a claim
of coverage nobody re-checked. `tests/test_matrix_traceability.py` owns the
mapping, resolves every name against the real suite, and asserts that the file
this module writes is the file on disk.
"""

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

OUT = BACKEND.parent / "docs" / "acceptance" / "matrix-traceability.md"


def render() -> str:
    from tests.test_matrix_traceability import COVERAGE, spec_rows

    rows = spec_rows()
    script_only = sorted(
        row
        for row, covers in COVERAGE.items()
        if all(name.startswith("acceptance:") for name in covers)
    )

    lines = [
        "# Section 6 test matrix — traceability",
        "",
        "**Generated.** Do not edit by hand: run",
        "`python -m acceptance.matrix_traceability` from `backend/`.",
        "",
        "Every row of section 6 of",
        "`docs/prompts/integrated-feature-verification-workflow-implementation.md`,",
        "and the named test that asserts it. The mapping lives in",
        "`backend/tests/test_matrix_traceability.py`, which parses section 6 out of the",
        "specification itself and resolves every name below against the real suite — so",
        "a row added to the spec, a test renamed, or a test deleted fails the suite",
        "rather than leaving this table quietly wrong.",
        "",
        "A **script** entry is an acceptance script under `backend/acceptance/`, not a",
        "pytest function. Those are properties of running processes that no in-process",
        "test can exhibit, and they are labelled differently on purpose: evidence that",
        "someone has to run by hand is weaker than evidence that runs with the suite.",
        "",
        f"Rows: **{len(rows)}**. Mapped: **{len(COVERAGE)}**. "
        f"Covered only by a script: **{len(script_only)}** "
        f"({', '.join(str(row) for row in script_only)}).",
        "",
        "| # | Case | Gate | Must assert | Covered by |",
        "|---|---|---|---|---|",
    ]

    for number in sorted(rows):
        case, gate, assertion = rows[number]
        covers = []
        for name in COVERAGE.get(number, ()):
            if name.startswith("acceptance:"):
                covers.append(f"script `{name.split(':', 1)[1]}`")
            else:
                covers.append(f"`{name}`")
        lines.append(
            f"| {number} | {case} | {gate} | {assertion} | " + "<br>".join(covers) + " |"
        )

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(BACKEND.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
