"""`evidence/lens_coverage.py` must never print a credential or a provider body.

The Phase 3A probe is a **measurement tool whose output is committed**: its
artifact lands in `docs/measurements/` and its stdout is pasted into a report.
That makes it a leak surface the running application does not have -- the SSE
error path is at least ephemeral, and a markdown file in git is not.

Two things can leak here and both have leaked in this project before:

* **The token.** Lens authenticates with a bearer header, so unlike SerpAPI it
  is not in the URL -- but it is passed as an argument through every function in
  the probe, and a traceback or a repr of the wrong object would carry it.
  `tests/test_secrets_do_not_leak.py` documents the settings-repr incident that
  printed five live credentials out of a pytest failure report.
* **The response body.** `_classify_status` is the only place a body is read,
  and it exists to turn one into a category and throw it away. A provider body
  can echo the request -- headers included -- so anything derived from it that
  reaches a row is a leak with an unbounded shape.

These tests drive the probe against stubbed responses. **No network call is
made and no credential is needed**, which is also what makes them the offline
proof that the tool's parsing works: `test_a_full_record_is_measured_end_to_end`
exercises the same code a live run would.
"""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from evidence import lens_coverage
from evidence.normalize import deduplicate, normalize

SECRET = "lens-live-token-DO-NOT-LEAK-abc123"


class _StubResponse:
    """Enough of `requests.Response` for the probe, with a hostile body."""

    def __init__(self, status_code, payload=None, body="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = body
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON object could be decoded")
        return self._payload


class _StubSession:
    def __init__(self, response):
        self._response = response
        self.seen_headers = None
        self.calls = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.seen_headers = headers
        self.calls += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def close(self):
        pass


def _probe(response, publication="US11002233"):
    """Drive the probe with an injected no-op sleeper.

    Retry is bounded and real, so a test that let it sleep would pay
    `RETRY_BACKOFF_SECONDS` per retryable status -- which it did, turning a
    2-second suite into a 96-second one. The sleeper is a parameter precisely so
    no test ever waits on a backoff.
    """
    session = _StubSession(response)
    slept = []
    row = lens_coverage.probe_publication(
        normalize(publication),
        session=session,
        api_key=SECRET,
        sleep=slept.append,
    )
    session.slept = slept
    return row, session


def _rendered(row):
    """Everything about a row that can reach a human: the artifact and a repr."""
    report = lens_coverage.ProbeReport(
        sample_source="test",
        raw_count=1,
        distinct_count=1,
        collapsed=0,
        rows=[row],
        calls_made=1,
    )
    return report.to_markdown() + repr(row) + json.dumps(asdict(row), default=str)


def _lens_record(description, claims):
    return {
        "lens_id": "000-000-000-000-000",
        "jurisdiction": "US",
        "doc_number": "11002233",
        "kind": "B2",
        "date_published": "2021-05-11",
        "biblio": {"invention_title": [{"text": "A widget", "lang": "en"}]},
        "abstract": [{"text": "An abstract.", "lang": "en"}],
        "description": [{"text": description, "lang": "en"}],
        "claims": [{"lang": "en", "claims": [{"claim_text": [c]} for c in claims]}],
    }


# --------------------------------------------------------------------------
# The token
# --------------------------------------------------------------------------


def test_the_token_reaches_the_authorization_header_and_nowhere_else():
    row, session = _probe(_StubResponse(200, {"data": []}))
    assert session.seen_headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in _rendered(row)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_no_error_status_carries_the_token_into_the_row(status):
    """The body is deliberately hostile: it echoes the Authorization header, the
    way a provider that reflects the request would."""
    body = '{"message":"denied","request":{"Authorization":"Bearer %s"}}' % SECRET
    row, _ = _probe(_StubResponse(status, None, body))
    assert row.outcome == lens_coverage.Outcome.FAILED
    assert SECRET not in _rendered(row)
    assert str(status) in row.detail, "the status is what makes a failure actionable"


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.Timeout("read timeout for Bearer " + SECRET),
        requests.exceptions.ChunkedEncodingError("ended prematurely " + SECRET),
        requests.exceptions.ConnectionError("failed to connect with " + SECRET),
    ],
)
def test_no_transport_failure_carries_the_token(error):
    row, _ = _probe(error)
    assert row.outcome == lens_coverage.Outcome.FAILED
    assert SECRET not in _rendered(row)


def test_a_malformed_body_carrying_the_token_does_not_leak_it():
    row, _ = _probe(_StubResponse(200, None, "<html>" + SECRET + "</html>"))
    assert row.failure_category == lens_coverage.FailureCategory.MALFORMED_JSON
    assert SECRET not in _rendered(row)


# --------------------------------------------------------------------------
# The provider body
# --------------------------------------------------------------------------


def test_the_response_body_never_reaches_the_row():
    marker = "INTERNAL-PROVIDER-DIAGNOSTIC-STRING"
    row, _ = _probe(_StubResponse(500, None, '{"trace":"%s"}' % marker))
    assert marker not in _rendered(row)
    assert row.detail == "Lens Patent API returned HTTP 500"


def test_a_body_hint_selects_a_category_without_being_stored():
    row, _ = _probe(_StubResponse(403, None, '{"message":"monthly quota exceeded"}'))
    assert row.failure_category == lens_coverage.FailureCategory.QUOTA_EXCEEDED
    rendered = _rendered(row).lower().replace("quota_exceeded", "")
    assert "monthly quota exceeded" not in rendered, (
        "the category may name the class; the body may not survive"
    )


def test_the_module_reads_the_response_body_in_exactly_one_place():
    source = Path(lens_coverage.__file__).read_text(encoding="utf-8")
    assert source.count("response.text") == 1, (
        "a second read of the body is a second chance to store it; if one is "
        "genuinely needed, prove here that it cannot reach a row"
    )


def test_the_module_never_prints_or_logs_the_credential():
    source = Path(lens_coverage.__file__).read_text(encoding="utf-8")
    forbidden = (
        "print(settings.lens_api_key",
        "print(api_key",
        "{settings.lens_api_key}",
        "logger.info(settings.lens_api_key",
        "logger.error(settings.lens_api_key",
        "logger.warning(settings.lens_api_key",
    )
    for pattern in forbidden:
        assert pattern not in source, pattern + " would print the token"

    # The credential may appear in exactly one construction -- the Authorization
    # header -- and nowhere else. Anything else interpolating it is a leak
    # waiting for a log line, so the count is pinned rather than the absence.
    assert source.count("{api_key}") == 1
    assert 'f"Bearer {api_key}"' in source
    assert "bool(settings.lens_api_key)" in source, (
        "presence is reported as a boolean, which is the useful half"
    )


# --------------------------------------------------------------------------
# It is a measurement tool, so it must also measure correctly -- offline
# --------------------------------------------------------------------------


def test_a_full_record_is_measured_end_to_end_with_no_network_and_no_credential():
    description = ("Paragraph body text about the widget. " * 40).strip()
    record = _lens_record(description, ["1. A widget.", "2. The widget of claim 1."])
    row, _ = _probe(_StubResponse(200, {"data": [record]}))

    assert row.outcome == lens_coverage.Outcome.TEXT
    assert row.description_available and row.description_chars == len(description)
    assert row.claims_available and row.claims_count == 2
    assert row.claim_numbering == lens_coverage.ClaimNumbering.STABLE
    assert row.language_description == "en"
    assert row.text_version_normalized == "2021-05-11"
    assert row.served_publication == "US11002233B2"
    assert row.served_matches_request is True
    assert row.source_range_locators >= 1
    assert row.source_range_reproducible is True, (
        "a SOURCE_RANGE locator that does not slice back out of the exact "
        "retrieved text is not a citation"
    )
    assert not row.paragraph_ids_supplied, "Lens serves description as one blob"


def test_the_epo_only_provenance_fields_are_always_reported_missing():
    """Phase 3A finding, pinned so it cannot be quietly assumed away: a
    Lens-sourced passage cannot be built today without stamping EPO provenance
    on text EPO never served."""
    row, _ = _probe(_StubResponse(200, {"data": [_lens_record("Body. " * 200, ["1. X."])]}))
    for field_name in (
        "source",
        "source_document_id",
        "paragraph_ids",
        "claims_char_offsets",
    ):
        assert field_name in row.contract_fields_missing


def test_ambiguous_claim_numbering_is_recorded_and_never_silently_renumbered():
    record = _lens_record("Body. " * 200, ["2. A widget.", "A further widget."])
    row, _ = _probe(_StubResponse(200, {"data": [record]}))
    assert row.claim_numbering == lens_coverage.ClaimNumbering.AMBIGUOUS
    assert (
        row.failure_category == lens_coverage.FailureCategory.CLAIM_NUMBERING_AMBIGUOUS
    )
    assert SECRET not in _rendered(row)


def test_an_empty_result_is_not_matched_rather_than_no_text():
    row, _ = _probe(_StubResponse(200, {"data": []}))
    assert row.outcome == lens_coverage.Outcome.NOT_MATCHED
    assert row.answered is True, "a real answer about the index, not an outage"


def test_a_different_document_is_recorded_as_a_served_identity_mismatch():
    record = _lens_record("Body. " * 200, ["1. A widget."])
    record["doc_number"] = "99999999"
    row, _ = _probe(_StubResponse(200, {"data": [record]}))
    assert row.served_matches_request is False
    assert "MISMATCH" in _rendered(row)


def test_failed_rows_are_excluded_from_the_coverage_denominator():
    ok, _ = _probe(
        _StubResponse(200, {"data": [_lens_record("Body. " * 200, ["1. X."])]})
    )
    bad, _ = _probe(_StubResponse(503, None, "unavailable"))
    report = lens_coverage.ProbeReport(
        sample_source="test",
        raw_count=2,
        distinct_count=2,
        collapsed=0,
        rows=[ok, bad],
        calls_made=2,
    )
    assert report.answered == 1
    assert report.coverage_percent == 100.0, (
        "an outage must not be counted as absent text"
    )


def test_no_credential_means_no_artifact_is_written(tmp_path, monkeypatch):
    # `Settings` is frozen (and every credential field is `repr=False`), so the
    # module-level binding is what gets replaced, not a field on it.
    monkeypatch.setattr(
        lens_coverage, "settings", SimpleNamespace(lens_api_key="")
    )
    output = tmp_path / "should-not-exist.md"
    assert lens_coverage.main(["--output", str(output)]) == 3
    assert not output.exists(), (
        "a 0% file written with no credential is indistinguishable from a "
        "measured absence of text"
    )


def test_dry_run_makes_no_http_call(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("--dry-run must perform zero HTTP calls")

    monkeypatch.setattr(requests.Session, "request", explode)
    monkeypatch.setattr(lens_coverage, "probe_publication", explode)
    assert lens_coverage.main(["--dry-run"]) == 0


def test_the_max_calls_ceiling_is_checked_before_the_request():
    calls = []

    def counting(publication, *, session, api_key):
        calls.append(publication.normalized)
        return lens_coverage.ProbeRow(
            requested=publication.requested,
            normalized=publication.normalized,
            country=publication.country,
            outcome=lens_coverage.Outcome.NOT_MATCHED,
        )

    entries, _ = lens_coverage.sample_from_file(lens_coverage.DEFAULT_SAMPLE)
    distinct, _ = deduplicate(entries)
    rows, made = lens_coverage.run(
        distinct,
        api_key=SECRET,
        max_calls=3,
        prober=counting,
        session=_StubSession(None),
        sleep=lambda _seconds: None,
    )
    assert made == 3 and len(calls) == 3 and len(rows) == 3


def test_the_pinned_sample_is_the_one_epo_was_measured_against():
    entries, _ = lens_coverage.sample_from_file(lens_coverage.DEFAULT_SAMPLE)
    distinct, collapsed = deduplicate(entries)
    assert len(distinct) == 34 and collapsed == 0
    offices = {}
    for publication in distinct:
        offices[publication.country] = offices.get(publication.country, 0) + 1
    assert offices == {"US": 18, "KR": 6, "EP": 4, "CA": 2, "CN": 2, "DK": 1, "WO": 1}


def test_the_probe_is_not_imported_by_any_runtime_module():
    """Phase 3A changes no runtime behaviour, and this is how that stays true."""
    backend = Path(lens_coverage.__file__).resolve().parent.parent
    for package in ("graph", "workflow", "api", "search", "verification"):
        for path in (backend / package).rglob("*.py"):
            assert "lens_coverage" not in path.read_text(encoding="utf-8"), (
                str(path) + " imports the measurement tool; it must stay out of "
                "the running application"
            )
    for name in ("server.py", "run_mock.py", "llm.py"):
        assert "lens_coverage" not in (backend / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Rate limiting: the throttle must never be reported as a coverage finding
# --------------------------------------------------------------------------


def test_a_429_is_retried_a_bounded_number_of_times_and_then_fails():
    """The unpaced first live run turned the trial tier's per-minute bucket into
    23 publications with no data. A throttle is transient and is retried;
    exhaustion is still `FAILED`, never `NO_TEXT`."""
    row, session = _probe(_StubResponse(429, None, "rate limit exceeded"))
    assert session.calls == lens_coverage.MAX_ATTEMPTS
    assert row.attempts == lens_coverage.MAX_ATTEMPTS
    assert row.outcome == lens_coverage.Outcome.FAILED
    assert row.failure_category == lens_coverage.FailureCategory.RATE_LIMITED
    assert len(session.slept) == lens_coverage.MAX_ATTEMPTS - 1


@pytest.mark.parametrize("status", [401, 403, 400, 404, 422])
def test_a_deterministic_4xx_is_never_retried(status):
    """Bad credentials do not become good ones, and hammering an auth endpoint
    is how a key gets blocked."""
    _, session = _probe(_StubResponse(status, None, "denied"))
    assert session.calls == 1 and session.slept == []


def test_retry_after_is_honoured_but_never_shortens_the_backoff():
    short = _StubResponse(429, None, "slow down", headers={"Retry-After": "1"})
    _, session = _probe(short)
    assert all(s >= lens_coverage.RETRY_BACKOFF_SECONDS for s in session.slept), (
        "a provider asking for less time than a per-minute bucket needs would "
        "just produce another 429"
    )

    long = _StubResponse(429, None, "slow down", headers={"Retry-After": "30"})
    _, session = _probe(long)
    assert session.slept == [30.0, 30.0]


def test_the_quota_headings_are_recorded_from_headers_not_from_the_body():
    headers = {
        "x-rate-limit-remaining-request-per-minute": "8",
        "x-rate-limit-remaining-request-per-month": "938",
    }
    record = _lens_record("Body. " * 200, ["1. A widget."])
    row, _ = _probe(_StubResponse(200, {"data": [record]}, headers=headers))
    assert row.rate_limit_remaining_minute == 8
    assert row.rate_limit_remaining_month == 938


def test_an_absent_quota_header_is_none_and_never_zero():
    record = _lens_record("Body. " * 200, ["1. A widget."])
    row, _ = _probe(_StubResponse(200, {"data": [record]}))
    assert row.rate_limit_remaining_minute is None, (
        "'the provider did not say' and 'nothing left' are different facts"
    )


def test_the_run_paces_itself_between_requests_but_not_before_the_first():
    slept = []

    def prober(publication, *, session, api_key):
        return lens_coverage.ProbeRow(
            requested=publication.requested,
            normalized=publication.normalized,
            country=publication.country,
            outcome=lens_coverage.Outcome.NOT_MATCHED,
        )

    entries, _ = lens_coverage.sample_from_file(lens_coverage.DEFAULT_SAMPLE)
    distinct, _ = deduplicate(entries)
    _, made = lens_coverage.run(
        distinct[:4],
        api_key=SECRET,
        max_calls=40,
        prober=prober,
        session=_StubSession(None),
        sleep=slept.append,
    )
    assert made == 4
    assert slept == [lens_coverage.DEFAULT_DELAY_SECONDS] * 3


def test_the_default_delay_is_derived_from_the_measured_ceiling():
    assert lens_coverage.DEFAULT_DELAY_SECONDS > 60.0 / lens_coverage.REQUESTS_PER_MINUTE


# --------------------------------------------------------------------------
# Resuming: reuse a success, retry a throttle
# --------------------------------------------------------------------------


def test_an_already_answered_publication_is_carried_forward_not_re_requested(tmp_path):
    answered = _probe(
        _StubResponse(200, {"data": [_lens_record("Body. " * 200, ["1. X."])]})
    )[0]
    throttled, _ = _probe(_StubResponse(429, None, "rate limit"), "US7654321")

    sidecar = tmp_path / "prior.json"
    sidecar.write_text(
        json.dumps({"rows": [asdict(answered), asdict(throttled)]}), encoding="utf-8"
    )
    carried = lens_coverage.load_measured(sidecar)

    assert set(carried) == {"US11002233"}, (
        "a FAILED row is the absence of a measurement, so it must be retried"
    )
    assert carried["US11002233"] == answered, "a merged row is byte-identical"

    requested = []

    def prober(publication, *, session, api_key):
        requested.append(publication.normalized)
        return lens_coverage.ProbeRow(
            requested=publication.requested,
            normalized=publication.normalized,
            country=publication.country,
            outcome=lens_coverage.Outcome.NOT_MATCHED,
        )

    entries, _ = lens_coverage.sample_from_file(lens_coverage.DEFAULT_SAMPLE)
    distinct, _ = deduplicate(entries)
    rows, made = lens_coverage.run(
        distinct[:3],
        api_key=SECRET,
        max_calls=40,
        already_measured=carried,
        prober=prober,
        session=_StubSession(None),
        sleep=lambda _s: None,
    )
    assert "US11002233" not in requested, "no request is spent on a carried row"
    assert requested == ["US7654321", "EP3218319"]
    assert len(rows) == 3 and made == 2


def test_the_sidecar_round_trips_without_a_credential_or_a_body(tmp_path):
    marker = "INTERNAL-PROVIDER-DIAGNOSTIC-STRING"
    rows = [
        _probe(_StubResponse(200, {"data": [_lens_record("Body. " * 200, ["1. X."])]}))[0],
        _probe(_StubResponse(500, None, '{"t":"%s","a":"Bearer %s"}' % (marker, SECRET)))[0],
    ]
    payload = json.dumps({"rows": [asdict(row) for row in rows]}, indent=2)
    assert SECRET not in payload and marker not in payload
    path = tmp_path / "sidecar.json"
    path.write_text(payload, encoding="utf-8")
    assert set(lens_coverage.load_measured(path)) == {"US11002233"}
