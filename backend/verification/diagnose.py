"""A cost-bounded, **explicitly non-qualifying** probe of one §15.1 case.

    cd backend && python -m verification.diagnose E

## Why this exists

The full §14.2.1 gate is 39 provider calls. On 2026-08-29
`gpt-5-mini-2025-08-07` failed it on **one** case — §15.1 case E, the
conservative-ambiguity case — returning `UNCLEAR`, `PARTIALLY`, `PARTIALLY` across
three repeats. The owner then set `reasoning_effort=high`. Re-running the whole
suite to find out whether one case changed spends 38 calls learning nothing about
the other ten.

So this runs **one case, five times**, through the exact production path, and
answers exactly one question: does case E now produce the conservative answer
every time?

## It cannot qualify anything, and that is structural rather than promised

- **It writes no capability artifact.** It does not import `record_path`, does not
  know `ARTIFACT_DIRECTORY`, and its output goes to a file named
  `diagnostic-*.json` in `docs/measurements/` — a name
  `capability.load_artifacts` does not glob for.
- **Its record has no `runtime_identity`.** `capability._identity_from_record`
  returns `None` for it, so it can never match a runtime even if a copy were
  renamed into place.
- **It has no `eligible` key at all**, only `all_runs_expected`. Nothing reads
  that, and nothing can mistake it for an eligibility verdict.
- `tests/test_diagnose.py` asserts each of these properties directly.

## It is not a shortcut past the gate

A pass here is permission to spend 39 calls, nothing more. The five calls it makes
are **diagnostic** and are never counted toward a qualification — the gate reruns
every case from scratch, including E.

## What counts as a pass

All five runs must produce the §15.1 expectation for the case, pass canonical
validation, and pass every §9 check including the outcome-consistency check. A
refusal, a token ceiling, an operational failure after bounded retry, or a wrong
substantive outcome is a failure, and the failure is the answer.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config
from deployment import MissingCredential, Provider
from verification.capability_test import (
    CONSISTENCY_CHECK,
    Outcome,
    RetryPolicy,
    Sleeper,
    _requirement_report,
    call_with_retry,
    evaluate_expectation,
    is_operational,
)
from verification.regression_cases import DECOMPOSITION, case_by_id
from verification.states import CellState, VerificationError
from verification.verify import (
    TEMPERATURE,
    PublicationUnderEvaluation,
    max_tokens_for,
    verify_feature_disclosure,
)
from llm import LAST_COMPLETION, inference_config
from workflow import redaction

DIAGNOSTIC_RUNS = 5
"""Five, not three.

Three is the smallest number that can show disagreement at all, which is why the
gate uses it across eleven cases. This looks at **one** case that has already
disagreed, so it buys a slightly better estimate of whether it still does for two
extra calls."""

DIAGNOSTIC_RETRY_POLICY = RetryPolicy(
    max_attempts=1,
    base_delay=0.0,
    max_delay=0.0,
    max_total_wait=0.0,
)
"""A requested diagnostic repetition is exactly one paid provider call.

Production keeps its bounded transport retry and one malformed-output re-ask.
Those are appropriate for completing user work, but a command advertised as an
11-call cost probe must be structurally incapable of expanding to 198 calls
when the provider or local environment is unhealthy. Operational failure is
therefore measured once and counted as a failed diagnostic run.
"""

DIAGNOSTIC_DIRECTORY = Path(__file__).resolve().parent.parent.parent / "docs" / "measurements"

FILENAME_PREFIX = "diagnostic-case"
"""Deliberately **not** `structured-output-capability-`.

`capability.load_artifacts` globs `structured-output-capability-*.json`. A
diagnostic record is not in that namespace, so it is not merely rejected at
admission — it is never read."""


def run_case(
    case_id: str,
    *,
    runs: int = DIAGNOSTIC_RUNS,
    policy: RetryPolicy = DIAGNOSTIC_RETRY_POLICY,
    sleeper: Optional[Sleeper] = None,
) -> dict[str, Any]:
    """Run one §15.1 case `runs` times through the production verifier.

    Same model, same byte-pinned system prompt, same generated wire schema, same
    canonical validator, same payload builder, same `reasoning_effort`. A
    diagnostic that ran a different request would answer a different question.
    """
    case = case_by_id(case_id)
    publication = PublicationUnderEvaluation(
        publication_number=case.publication_number,
        title=case.publication_title,
        jurisdiction=case.jurisdiction,
        document_ids=case.document_ids,
    )

    records: list[dict[str, Any]] = []
    for index in range(runs):
        LAST_COMPLETION.set({})
        attempt = call_with_retry(
            lambda: verify_feature_disclosure(
                decomposition=DECOMPOSITION,
                publication=publication,
                passages=list(case.passages),
                cell_id=f"diagnostic:{DECOMPOSITION.feature_id}:{case.case_id}",
                result_version=str(index + 1),
                # §16 decision 6 is still open; never a guessed floor.
                retrieval_floor=None,
                # Cost contract: one requested run, one provider call maximum.
                # The payload, schema and validation path remain production's.
                transport_retries=0,
                parse_attempts=1,
            ),
            policy=policy,
            sleeper=sleeper,
        )
        envelope = dict(LAST_COMPLETION.get() or {})
        record: dict[str, Any] = {
            "run": index + 1,
            "served_model": str(envelope.get("served_model", "")),
            "usage": {
                key: value
                for key, value in envelope.items()
                if key in ("prompt_tokens", "completion_tokens", "total_tokens",
                           "reasoning_tokens")
            },
            "attempts": attempt.attempts,
        }

        if attempt.error is not None:
            operational = not isinstance(attempt.error, VerificationError)
            record.update(
                {
                    "passed": False,
                    "failure_category": (
                        (attempt.outcome or Outcome.PROVIDER_ERROR).value
                        if operational
                        else f"VERIFIER_ERROR:{type(attempt.error).__name__}"
                    ),
                    "operational": operational,
                    "detail": redaction.safe_detail(attempt.error),
                }
            )
            records.append(record)
            continue

        result = attempt.value
        report = _requirement_report(result.raw_response or "")
        if result.state is CellState.VALIDATION_FAILURE:
            failed = str(result.failed_check or "")
            record.update(
                {
                    "passed": False,
                    "operational": False,
                    "canonical_validation": f"REJECTED at {failed}",
                    "consistency_check": "FAILED" if failed == CONSISTENCY_CHECK else "not reached",
                    "failure_category": (
                        "OUTCOME_INCONSISTENT" if failed == CONSISTENCY_CHECK
                        else "CANONICAL_VALIDATION_FAILURE"
                    ),
                    "statuses": report.get("statuses", {}),
                    "declared_outcome": report.get("declared_outcome"),
                    "derived_outcome": report.get("derived_outcome"),
                }
            )
            records.append(record)
            continue

        machine = result.machine_result
        outcome = str(machine.outcome) if machine is not None else str(result.state)
        # The expectation is §15.1's, unchanged and transcribed rather than
        # reasoned to. This diagnostic does not get to decide what a correct
        # answer is, and it has no tolerance and no partial credit: outcome,
        # every per-requirement status §15.1 names, and the combination_basis
        # link it requires quoted must all hold.
        expectation = evaluate_expectation(case, report, machine, outcome)
        record.update(
            {
                "operational": False,
                "canonical_validation": "PASSED",
                "consistency_check": "PASSED",
                "statuses": report.get("statuses", {}),
                "declared_outcome": report.get("declared_outcome"),
                "derived_outcome": report.get("derived_outcome"),
                "outcome": outcome,
                "expected_outcome": case.expected_outcome,
                "expectation": expectation,
                "passed": expectation["matches_expected"],
                "failure_category": (
                    None
                    if expectation["matches_expected"]
                    else (
                        "WRONG_OUTCOME"
                        if not expectation["outcome_matches"]
                        else (
                            "WRONG_STATUSES"
                            if not expectation["statuses_match"]
                            else "COMBINATION_LINK_NOT_QUOTED"
                        )
                    )
                ),
            }
        )
        records.append(record)

    passed = [record for record in records if record.get("passed")]
    return {
        # No `runtime_identity` and no `eligible`, on purpose. See the module
        # docstring: this record must be structurally unable to qualify anything.
        "diagnostic": f"spec §15.1 case {case.case_id}",
        "non_qualifying": True,
        "note": (
            "DIAGNOSTIC ONLY. This record cannot qualify any runtime: it carries no "
            "runtime_identity, no eligibility verdict, and its filename is outside "
            "the namespace capability.load_artifacts reads. A pass here is "
            "permission to spend the full gate's 39 calls, and its calls are never "
            "counted toward a qualification."
        ),
        "run_at": datetime.now(timezone.utc).isoformat(),
        # The sanitized inference settings this diagnostic actually ran under.
        #
        # **Auditability, never qualification.** The 2026-08-29 case-E record
        # carried no such block, so confirming afterwards that it really had run
        # at `reasoning_effort=high` meant comparing its reasoning-token counts
        # against the default-effort gate run -- sound, but an inference rather
        # than a record. A diagnostic that cannot say what it measured is worth
        # less than one that can.
        #
        # This is `InferenceConfig.as_dict()`, not `runtime_identity`, and the
        # distinction is the whole safety property: `_identity_from_record`
        # requires a `runtime_identity` mapping and returns `None` without one,
        # so no amount of detail here can make this record qualify a runtime.
        # `tests/test_diagnose.py` pins that in both directions.
        #
        # Composed from `llm.inference_config` rather than imported from
        # `verification.capability`, so the module-level ban on importing the
        # artifact writer's home stays intact and unweakened. It is the same
        # composition `capability.inference_config_for` performs, and
        # `test_the_recorded_config_is_the_one_the_gate_would_fingerprint`
        # asserts the two are byte-equal so they cannot drift apart.
        "inference_config": inference_config(
            config.llm_target,
            max_tokens=max_tokens_for(DECOMPOSITION),
            temperature=TEMPERATURE,
        ).as_dict(),
        "case_id": case.case_id,
        "case_title": case.title,
        "expected_outcome": case.expected_outcome,
        "runs": len(records),
        "passing_runs": len(passed),
        "all_runs_expected": len(passed) == len(records) == runs,
        "operational_failures": sum(1 for record in records if record.get("operational")),
        "results": records,
    }


def record_path(case_id: str, when: Optional[datetime] = None) -> Path:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d-%H%M%S")
    return DIAGNOSTIC_DIRECTORY / f"{FILENAME_PREFIX}-{case_id}-{stamp}.json"


def parse_spec(spec: str) -> tuple[str, int]:
    """``"E"`` -> ``("E", DIAGNOSTIC_RUNS)``; ``"I:3"`` -> ``("I", 3)``.

    Raises:
        ValueError: an unparseable or non-positive repetition count. A spec that
            cannot be read is never silently defaulted to one run -- a diagnostic
            that quietly did less work than asked is worse than one that refuses.
    """
    case_id, _, runs = spec.strip().partition(":")
    case_id = case_id.strip().upper()
    if not runs:
        return case_id, DIAGNOSTIC_RUNS
    try:
        count = int(runs)
    except ValueError:
        raise ValueError(
            f"{spec!r}: repetition count {runs!r} is not an integer. Use CASE or "
            "CASE:RUNS, e.g. E:5."
        ) from None
    if count < 1:
        raise ValueError(f"{spec!r}: repetition count must be at least 1.")
    return case_id, count


def main(argv: Optional[list[str]] = None) -> int:
    """Run one or more cases. Exit 0 only if **every** run met §15.1's expectation.

    ``python -m verification.diagnose E`` keeps its original meaning -- case E,
    five runs. ``python -m verification.diagnose E:5 I:3 G:3`` runs a targeted
    set, each case written to its **own** record file, because one file per case
    is what keeps a diagnostic record readable as a statement about that case.

    **Every requested case is run, and then the exit code reports.** A failing
    case does not abandon the ones after it. The instruction a failure carries is
    "stop -- do not spend the full gate", and the full gate is 39 calls; the
    remaining targeted cases are a handful, they were already budgeted, and each
    one answers a different question. Abandoning them would throw away two thirds
    of the evidence to save a cost nobody objected to. Exit 1 still means "do not
    proceed", and it means it whichever case failed.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    try:
        specs = [parse_spec(spec) for spec in (args or ["E"])]
    except ValueError as bad:
        print(f"REFUSED: {bad}", file=sys.stderr)
        return 2

    if config.llm_target.provider is Provider.STUB:
        print(
            "REFUSED: DEPLOYMENT_PROFILE=test has no provider to probe.",
            file=sys.stderr,
        )
        return 2

    # Resolved before ANY call is made, so an unknown case id costs nothing.
    for case_id, _ in specs:
        try:
            case_by_id(case_id)
        except KeyError as unknown:
            print(f"REFUSED: {unknown}", file=sys.stderr)
            return 2

    failed: list[str] = []
    for case_id, runs in specs:
        try:
            record = run_case(case_id, runs=runs)
        except MissingCredential as missing:
            print(f"NOT RUN: {missing}", file=sys.stderr)
            return 2

        destination = record_path(case_id)
        destination.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps(record, indent=2))
        print(f"\nwritten to {destination}")

        if not record["all_runs_expected"]:
            failures = sorted(
                {
                    str(result.get("failure_category"))
                    for result in record["results"]
                    if not result.get("passed")
                }
            )
            print(
                f"\nCASE {case_id} DID NOT PASS "
                f"{record['passing_runs']}/{record['runs']}: "
                + ", ".join(failures)
                + ".\nDo not run the full qualification, and do not select another "
                "model automatically.",
                file=sys.stderr,
            )
            failed.append(case_id)
            continue

        print(f"\nCASE {case_id} PASSED {record['runs']}/{record['runs']}.")

    if failed:
        print(
            "\nDIAGNOSTIC FAILED on: " + ", ".join(failed) + ".\nDo NOT run "
            "`python -m verification.capability_test`, do NOT begin Gate D or "
            "Gate E, and do NOT change the model, the prompt, the schema, any "
            "check or any acceptance criterion to make it pass.",
            file=sys.stderr,
        )
        return 1
    print("\nALL TARGETED CASES PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
