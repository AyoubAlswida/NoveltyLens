"""Policy admission, and the coverage tool's cost safety.

**Admission refuses before anything is written.** No run row, no lease, no
checkpoint, no workflow event, no successful idempotency record. There is nothing
to clean up because nothing was created -- which is why `QUEUED → FAILED` is
*not* the policy-validation path: using it would create a row whose only purpose
is to record that it should never have been created.

**The coverage tool is shaped defensively** because "measure coverage on 50 real
results" reads like an instruction to go and find 50 real results, and finding
them is what spends a month's trial allowance. The sample is taken, never
generated; `--dry-run` makes zero HTTP calls; and a missing sample stops and asks
rather than running discovery "just this once".
"""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from evidence.coverage import (
    CoverageReport,
    CoverageRow,
    NoSampleAvailable,
    main as coverage_main,
    measure,
    sample_from_file,
)
from evidence.normalize import normalize
from evidence.policy import (
    EXCLUDE_WITHOUT_VERSION,
    POLICY_NOT_APPROVED,
    PROVIDER_PUBLICATION_DATE,
    PolicyNotApproved,
    validate_admission_policies,
)
from evidence.dates import PUBLICATION_LT_CUTOFF
from models import EnrichmentResult, EnrichmentStatus


@dataclass
class FakeSettings:
    date_screening_policy: str = PUBLICATION_LT_CUTOFF
    date_screening_decision: str = "approved by Ayoub, 2026-08-28: publication date, exclusive"
    text_version_policy: str = PROVIDER_PUBLICATION_DATE
    text_version_decision: str = "approved by Ayoub, 2026-08-28: provider publication date"


# --- admission -------------------------------------------------------------


def test_fully_recorded_policies_admit_a_run(capability_fixture_dir):
    approved = validate_admission_policies(
        FakeSettings(), capability_directory=capability_fixture_dir
    )
    assert approved.date_screening_policy == PUBLICATION_LT_CUTOFF
    assert approved.text_version_policy == PROVIDER_PUBLICATION_DATE


@pytest.mark.parametrize(
    "field",
    ["date_screening_policy", "text_version_policy"],
)
def test_an_unset_policy_refuses_admission(field):
    """No default. Which date screens prior art and what a text version may be
    are legal questions, and a default would answer them in code review."""
    settings = FakeSettings(**{field: ""})
    with pytest.raises(PolicyNotApproved) as caught:
        validate_admission_policies(settings)
    assert any(field.upper() in item for item in caught.value.missing)


@pytest.mark.parametrize(
    "field", ["date_screening_decision", "text_version_decision"]
)
def test_a_policy_with_no_recorded_decision_refuses_admission(field):
    """A policy set with nobody's name on it is a rule nobody approved. This is
    the same shape as `VERIFIER_GENERIC_JSON_DECISION`: one pattern for "a
    decision a human must record before the machine may act"."""
    settings = FakeSettings(**{field: ""})
    with pytest.raises(PolicyNotApproved) as caught:
        validate_admission_policies(settings)
    assert any("who approved it" in item for item in caught.value.missing)


def test_an_unsupported_policy_value_refuses_admission():
    with pytest.raises(PolicyNotApproved) as caught:
        validate_admission_policies(FakeSettings(date_screening_policy="whatever"))
    assert any("not supported" in item for item in caught.value.missing)


def test_every_missing_item_is_reported_at_once():
    """An operator setting this up deserves the whole list in one response, not
    four rounds of trial and error."""
    with pytest.raises(PolicyNotApproved) as caught:
        validate_admission_policies(
            FakeSettings(
                date_screening_policy="",
                text_version_policy="",
                date_screening_decision="",
                text_version_decision="",
            )
        )
    assert len(caught.value.missing) == 2, "one per policy, naming the policy first"


def test_the_error_carries_structured_safe_fields():
    """The API response quotes these; a free-text blob would be one more thing to
    sanitize."""
    with pytest.raises(PolicyNotApproved) as caught:
        validate_admission_policies(FakeSettings(date_screening_policy=""))
    assert caught.value.code == POLICY_NOT_APPROVED
    assert isinstance(caught.value.missing, list)
    assert all(isinstance(item, str) for item in caught.value.missing)


def test_the_shipped_default_configuration_refuses_admission():
    """**The repository ships with no policy set**, deliberately: the decision is
    recorded in `.env`, not in the code, so a fresh checkout cannot run a
    date-screened workflow until somebody writes it down."""
    from config import settings

    if settings.date_screening_policy and settings.text_version_policy:
        pytest.skip(
            "this environment has recorded policies in .env, which is the intended "
            "state for a configured deployment"
        )
    with pytest.raises(PolicyNotApproved):
        validate_admission_policies(settings)


def test_the_approved_policies_are_frozen_onto_the_run(capability_fixture_dir):
    """Copied at admission and never re-read, so changing a setting cannot
    retroactively reinterpret a finished run's screening."""
    approved = validate_admission_policies(
        FakeSettings(), capability_directory=capability_fixture_dir
    )
    with pytest.raises(Exception):
        approved.date_screening_policy = "something else"  # type: ignore[misc]


# --- coverage: cost safety -------------------------------------------------


def _result(status: EnrichmentStatus, sections=()) -> EnrichmentResult:
    return EnrichmentResult(
        requested_publication="EP4778509",
        normalized_publication="EP4778509",
        served_publication="EP4778509",
        source_document_id="epo:EP4778509",
        status=status,
        sections_retrieved=tuple(sections),
    )


def test_dry_run_makes_zero_http_calls_and_lists_the_query_order(tmp_path, capsys):
    """Run it first, every time. A preview that could itself spend quota would
    defeat its own purpose."""
    sample = tmp_path / "sample.txt"
    sample.write_text("US1234567B2\nEP4778509A1\nus 1,234,567\n# a comment\n", encoding="utf-8")

    with patch("requests.Session.request") as request, patch(
        "requests.get"
    ) as get, patch("requests.post") as post:
        assert coverage_main(["--dry-run", "--sample-file", str(sample)]) == 0
        request.assert_not_called()
        get.assert_not_called()
        post.assert_not_called()

    output = capsys.readouterr().out
    assert "US1234567" in output and "EP4778509" in output
    assert "1 duplicate(s) collapsed" in output


def test_duplicates_are_collapsed_before_querying(tmp_path, capsys):
    sample = tmp_path / "sample.txt"
    sample.write_text("US1234567B2\nUS1234567A1\nUS1234567\n", encoding="utf-8")
    coverage_main(["--dry-run", "--sample-file", str(sample)])
    output = capsys.readouterr().out
    assert "distinct          : 1" in output
    assert "2 duplicate(s) collapsed" in output


def test_a_missing_sample_stops_and_asks_rather_than_running_discovery(tmp_path, caplog):
    """**Never a fresh discovery call.** Building a sample "just this once" is
    exactly the spend this rule exists to prevent."""
    with patch(
        "evidence.coverage.sample_from_stored_patents",
        side_effect=NoSampleAvailable(
            "no publication numbers are stored in `patents`, and this tool will not "
            "run discovery to build a sample"
        ),
    ):
        assert coverage_main([]) == 2
    assert "will not run discovery" in caplog.text


def test_the_tool_never_imports_a_discovery_provider():
    """A source contract. The measurement answers "does EPO hold text for this
    number?", and no discovery provider, embedding or LLM is needed for that."""
    source = (Path(__file__).resolve().parent.parent / "evidence" / "coverage.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "search_lens",
        "search_google",
        "search_epo",
        "from search.search",
        "embed_texts",
        "_llm_chat",
        "from llm",
    ):
        assert forbidden not in source, f"coverage.py reaches for {forbidden}"


def test_failed_is_never_counted_as_no_text():
    """Counting a provider outage as absent text would understate coverage using
    the provider's own downtime -- and coverage is the number a product decision
    gets made on."""
    report = CoverageReport(
        sample_source="test",
        raw_count=4,
        distinct_count=4,
        collapsed=0,
        rows=[
            CoverageRow(normalize("EP1000000"), EnrichmentStatus.TEXT, has_claims=True),
            CoverageRow(normalize("EP2000000"), EnrichmentStatus.TEXT, has_description=True),
            CoverageRow(normalize("US3000000"), EnrichmentStatus.NO_TEXT),
            CoverageRow(normalize("CN4000000"), EnrichmentStatus.FAILED),
        ],
    )
    assert report.answered == 3, "the FAILED publication is not a data point"
    assert report.with_text == 2
    assert report.coverage_percent == 66.7

    markdown = report.to_markdown()
    assert "| `NO_TEXT` | 1 |" in markdown
    assert "| `FAILED` | 1 |" in markdown
    assert "not** counted as `NO_TEXT`" in markdown


def test_coverage_is_none_rather_than_zero_when_nothing_was_answered():
    """"We could not measure" and "EPO has nothing" are different findings, and a
    0% that actually means an outage would send a product decision the wrong
    way."""
    report = CoverageReport(
        sample_source="test",
        raw_count=1,
        distinct_count=1,
        collapsed=0,
        rows=[CoverageRow(normalize("EP1000000"), EnrichmentStatus.FAILED)],
    )
    assert report.coverage_percent is None
    assert "not measurable" in report.to_markdown()


def test_a_failed_measurement_records_a_redacted_detail():
    rows = measure(
        [normalize("EP4778509")],
        enricher=lambda _: (_ for _ in ()).throw(
            RuntimeError(
                "401 for https://ops.epo.org/auth?client_secret=COVERAGE_SECRET_9"
            )
        ),
    )
    assert rows[0].status is EnrichmentStatus.FAILED
    assert "COVERAGE_SECRET_9" not in rows[0].detail


def test_the_artifact_contains_no_credential_or_provider_body():
    report = CoverageReport(
        sample_source="stored `patents` rows",
        raw_count=1,
        distinct_count=1,
        collapsed=0,
        rows=[
            CoverageRow(
                normalize("EP4778509"),
                EnrichmentStatus.FAILED,
                detail="EPO OPS returned HTTP 503.",
            )
        ],
    )
    markdown = report.to_markdown()
    for forbidden in ("api_key=", "client_secret=", "Authorization:", "Bearer ", "postgresql://"):
        assert forbidden not in markdown


def test_the_artifact_says_it_is_not_a_cache():
    """This repository has no caching of any kind, and adding one is a fresh
    design decision rather than a side effect of a measurement tool."""
    report = CoverageReport("test", 0, 0, 0, rows=[])
    markdown = report.to_markdown()
    assert "not a cache" in markdown
    assert "never short-circuits a provider call" in markdown


def test_a_missing_sample_file_is_reported_rather_than_substituted(tmp_path):
    with pytest.raises(NoSampleAvailable):
        sample_from_file(tmp_path / "absent.txt")


def test_an_empty_sample_file_is_reported(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("# only comments\n\n", encoding="utf-8")
    with pytest.raises(NoSampleAvailable):
        sample_from_file(empty)
