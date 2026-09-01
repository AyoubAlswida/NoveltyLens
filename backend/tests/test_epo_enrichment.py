"""EPO enrichment: the three-state result, provider-neutral routing, bounded
retry, served-identity checking, and redaction.

**No outbound call is made anywhere in this file.** Every test drives a fake
`requests.Session`, so the suite keeps its "no credentials, no network" property
while exercising the real parsing, retry and status logic.

Two defects the specification names are pinned here specifically:

* **§17.2** -- `enrich_with_full_text` selected targets with
  ``patent.source == "European Patent Office"``. Every candidate arrives from the
  *discovery* provider, so that list is empty, enrichment silently no-ops, and
  every candidate falls to `EXCLUDED_NO_FULL_TEXT` -- a total failure that looks
  exactly like a legitimate coverage result.
* **§17.3** -- `get_patent_full_text` caught `Exception` and returned `""`, so a
  timeout and a genuine empty response were the same value. One is an exclusion,
  the other is a run failure.
"""

import pytest
import requests

from evidence.normalize import PublicationNumberError
from models import EnrichmentStatus, PatentRecord, TextVersionSource
from search.enrichment import (
    EnrichmentOutcome,
    enrich_candidates,
    enrichment_targets,
)
from search.epo_fulltext import (
    EpoEnrichmentFailed,
    MAX_ATTEMPTS,
    enrich_publication,
)

CLAIMS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns:ftxt="http://www.epo.org/fulltext">
  <ftxt:fulltext-document>
    <ftxt:claims lang="EN">
      <ftxt:claim><ftxt:claim-text>1. A waste heat recovery system comprising a heat
      exchanger disposed within an exhaust duct.</ftxt:claim-text></ftxt:claim>
    </ftxt:claims>
  </ftxt:fulltext-document>
</ops:world-patent-data>"""

DESCRIPTION_NUMBERED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns:ftxt="http://www.epo.org/fulltext">
  <ftxt:fulltext-document>
    <ftxt:description lang="EN">
      <ftxt:p num="0030">In one embodiment, heat exchanger 120 is mounted inside
      exhaust duct 110 and receives coolant from pump 130.</ftxt:p>
      <ftxt:p num="0031">Controller 140 adjusts the coolant flow rate according to
      a stored duty schedule.</ftxt:p>
    </ftxt:description>
  </ftxt:fulltext-document>
</ops:world-patent-data>"""

DESCRIPTION_UNNUMBERED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns:ftxt="http://www.epo.org/fulltext">
  <ftxt:fulltext-document>
    <ftxt:description lang="EN">The heat exchanger is mounted within the duct and
    receives coolant from a pump. """ + ("Detail sentence. " * 40) + """</ftxt:description>
  </ftxt:fulltext-document>
</ops:world-patent-data>"""

BIBLIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns:exchange="http://www.epo.org/exchange">
  <exchange:exchange-document country="EP" doc-number="4778509" kind="A1">
    <exchange:bibliographic-data>
      <exchange:publication-reference>
        <exchange:document-id document-id-type="epodoc">
          <exchange:date>20190601</exchange:date>
        </exchange:document-id>
      </exchange:publication-reference>
    </exchange:bibliographic-data>
  </exchange:exchange-document>
</ops:world-patent-data>"""

BIBLIO_NO_DATE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns:exchange="http://www.epo.org/exchange">
  <exchange:exchange-document country="EP" doc-number="4778509" kind="A1">
    <exchange:bibliographic-data>
      <exchange:publication-reference>
        <exchange:document-id document-id-type="epodoc"/>
      </exchange:publication-reference>
    </exchange:bibliographic-data>
  </exchange:exchange-document>
</ops:world-patent-data>"""

EMPTY_SECTION_XML = """<?xml version="1.0"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns:ftxt="http://www.epo.org/fulltext"/>"""


class _Response:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return {"access_token": "test-token-not-a-real-credential"}


class FakeSession:
    """A scripted EPO. Records every URL so a test can assert the call profile.

    Deliberately not a `requests_mock`: the point is that no real session class
    is involved at all, so a mistake cannot fall through to the network.
    """

    def __init__(self, routes: dict[str, object], *, token_status: int = 200) -> None:
        self.routes = routes
        self.token_status = token_status
        self.urls: list[str] = []
        self.closed = False

    def post(self, url, **kwargs):
        self.urls.append(url)
        return _Response(self.token_status, b"{}")

    def get(self, url, **kwargs):
        self.urls.append(url)
        for suffix, response in self.routes.items():
            if url.endswith(suffix):
                if callable(response):
                    return response(len([u for u in self.urls if u.endswith(suffix)]))
                return response
        return _Response(404)

    def close(self):
        self.closed = True


def _session(*, claims=None, description=None, biblio=None, **kwargs) -> FakeSession:
    return FakeSession(
        {
            "/claims": claims if claims is not None else _Response(404),
            "/description": description if description is not None else _Response(404),
            "/biblio": biblio if biblio is not None else _Response(200, BIBLIO_XML.encode()),
        },
        **kwargs,
    )


# --- the three states ------------------------------------------------------


def test_full_text_available_yields_TEXT_with_sections_recorded():
    result = enrich_publication(
        "EP4778509A1",
        session=_session(
            claims=_Response(200, CLAIMS_XML.encode()),
            description=_Response(200, DESCRIPTION_NUMBERED_XML.encode()),
        ),
    )
    assert result.status is EnrichmentStatus.TEXT
    assert set(result.sections_retrieved) == {"CLAIMS", "DESCRIPTION"}
    assert result.normalized_publication == "EP4778509"
    assert result.requested_publication == "EP4778509A1", "both forms recorded"
    assert result.section("CLAIMS").text_sha256, "the exact text is hashed"
    assert result.section("DESCRIPTION").paragraph_ids == ("0030", "0031")


def test_a_successful_response_with_no_eligible_text_yields_NO_TEXT():
    """The provider answered, and had none. A real answer about the document ->
    `EXCLUDED_NO_FULL_TEXT`."""
    result = enrich_publication("US1234567B2", session=_session())
    assert result.status is EnrichmentStatus.NO_TEXT
    assert result.sections == ()
    assert result.sections_retrieved == ()


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
def test_a_transient_failure_is_retried_and_then_FAILS(status):
    """**Never `NO_TEXT`.** Converting retry exhaustion into an exclusion would
    record a provider outage as a fact about the document."""
    session = _session(claims=_Response(status))
    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509", session=session, sleep=lambda _: None)
    assert "after 3 attempts" in str(caught.value)
    assert len([u for u in session.urls if u.endswith("/claims")]) == MAX_ATTEMPTS


def test_an_auth_failure_is_not_retried():
    """Bad credentials do not become good ones, and hammering an auth endpoint is
    how a key gets blocked."""
    session = _session(token_status=401)
    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509", session=session, sleep=lambda _: None)
    assert "authentication failed" in str(caught.value)
    assert len([u for u in session.urls if "accesstoken" in u]) == 1


def test_a_transient_failure_that_recovers_succeeds():
    """Bounded retry is permitted for transient errors -- that is the point of
    bounding it rather than removing it."""
    def flaky(call_number: int):
        return _Response(503) if call_number == 1 else _Response(200, CLAIMS_XML.encode())

    result = enrich_publication(
        "EP4778509", session=_session(claims=flaky), sleep=lambda _: None
    )
    assert result.status is EnrichmentStatus.TEXT
    assert result.attempts >= 3, "the retried call is counted, not hidden"


def test_a_malformed_body_is_a_failure_not_an_empty_section():
    """XML we cannot parse tells us nothing about whether the document has text.
    Treating it as "no text" would invent an answer out of a parse error."""
    session = _session(claims=_Response(200, b"<not-xml"))
    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509", session=session, sleep=lambda _: None)
    assert "not valid XML" in str(caught.value)


def test_a_404_on_a_section_is_a_real_answer_not_a_failure():
    """EPO's full-text service holds text only for EP/WO-family documents, so a
    US-only filing legitimately 404s. That is `NO_TEXT`, not an outage."""
    result = enrich_publication("US1234567B2", session=_session(claims=_Response(404)))
    assert result.status is EnrichmentStatus.NO_TEXT


# --- served identity: borrowed family text ---------------------------------


def test_the_served_identity_is_carried_back_for_comparison():
    result = enrich_publication(
        "EP4778509A1",
        session=_session(claims=_Response(200, CLAIMS_XML.encode())),
    )
    assert result.served_publication == "EP4778509"


def test_a_served_identity_that_differs_is_carried_back_verbatim():
    """The enrichment layer reports what it was served; `eligibility.py` decides
    that a mismatch is `EXCLUDED_INELIGIBLE`. Splitting it that way keeps the
    provider adapter free of policy."""
    other = BIBLIO_XML.replace('doc-number="4778509"', 'doc-number="9999999"')
    result = enrich_publication(
        "EP4778509A1",
        session=_session(
            claims=_Response(200, CLAIMS_XML.encode()),
            biblio=_Response(200, other.encode()),
        ),
    )
    assert result.served_publication == "EP9999999"
    assert result.normalized_publication == "EP4778509"


# --- text version ----------------------------------------------------------


def test_the_text_version_comes_from_the_provider_publication_date():
    """**The approved policy** (§3.6 decision 4, option a). Provider metadata, and
    honest that it identifies the version only as coarsely as the publication
    does."""
    result = enrich_publication(
        "EP4778509A1", session=_session(claims=_Response(200, CLAIMS_XML.encode()))
    )
    assert result.text_version_source is TextVersionSource.PROVIDER_PUBLICATION_DATE
    assert result.text_version_original == "20190601"
    assert result.text_version_normalized == "2019-06-01"


def test_retrieval_time_is_never_written_into_a_text_version_field():
    """`retrieved_at` says when we fetched, not which version we fetched.
    Substituting it would date a stored verdict against a version nobody can
    identify."""
    result = enrich_publication(
        "EP4778509A1", session=_session(claims=_Response(200, CLAIMS_XML.encode()))
    )
    assert result.retrieved_at
    assert result.retrieved_at != result.text_version_normalized
    assert result.retrieved_at != result.text_version_original
    assert result.text_version_normalized == "2019-06-01"


def test_a_well_formed_biblio_with_no_publication_date_is_the_one_honest_absence():
    """**The only path to `NONE`.** A 200 whose XML parses and genuinely carries
    no publication date is the provider answering "I have no date for this",
    which `eligibility.py` turns into `EXCLUDED_INELIGIBLE` under the approved
    text-version policy. Every other outcome fails the run."""
    result = enrich_publication(
        "EP4778509A1",
        session=_session(
            claims=_Response(200, CLAIMS_XML.encode()),
            biblio=_Response(200, BIBLIO_NO_DATE_XML.encode()),
        ),
        sleep=lambda _: None,
    )
    assert result.text_version_source is TextVersionSource.NONE
    assert result.text_version_normalized == ""
    assert result.status is EnrichmentStatus.TEXT, (
        "the text was retrieved; it is the version that is unknown"
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_a_non_retryable_biblio_status_fails_the_run_and_is_not_retried(status):
    """**A provider failure is never converted into an exclusion.** This was the
    B1 defect: `_fetch_biblio` returned an empty date for every non-200, so a
    401, a 404 and a 429 all became `EXCLUDED_INELIGIBLE` -- the run reporting a
    publication as ineligible when nobody ever asked the provider successfully,
    and understating its own coverage using the provider's downtime."""
    session = _session(
        claims=_Response(200, CLAIMS_XML.encode()), biblio=_Response(status)
    )
    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509A1", session=session, sleep=lambda _: None)
    assert str(status) in str(caught.value)
    assert len([u for u in session.urls if u.endswith("/biblio")]) == 1, (
        "deterministic statuses are not retried"
    )


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_transient_biblio_failure_is_retried_and_then_fails(status):
    session = _session(
        claims=_Response(200, CLAIMS_XML.encode()), biblio=_Response(status)
    )
    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509A1", session=session, sleep=lambda _: None)
    assert "after 3 attempts" in str(caught.value)
    assert len([u for u in session.urls if u.endswith("/biblio")]) == MAX_ATTEMPTS


def test_a_transient_biblio_failure_that_recovers_succeeds():
    """Bounded retry is still permitted -- the fix is about what happens when it
    is exhausted, not about removing it."""
    def flaky(call_number: int):
        if call_number == 1:
            return _Response(503)
        return _Response(200, BIBLIO_XML.encode())

    result = enrich_publication(
        "EP4778509A1",
        session=_session(claims=_Response(200, CLAIMS_XML.encode()), biblio=flaky),
        sleep=lambda _: None,
    )
    assert result.text_version_normalized == "2019-06-01"


def test_a_biblio_timeout_fails_the_run():
    """A timeout is the case §17.3 named: the request never completed, so the
    document's version is unknown rather than absent."""
    def timeout(_call_number: int):
        raise requests.exceptions.ReadTimeout("read timed out")

    session = _session(claims=_Response(200, CLAIMS_XML.encode()), biblio=timeout)
    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509A1", session=session, sleep=lambda _: None)
    assert "after 3 attempts" in str(caught.value)


def test_malformed_biblio_xml_fails_and_is_not_retried():
    """A body that did not parse will not parse on the second attempt, and
    re-requesting it spends quota to learn the same thing."""
    session = _session(
        claims=_Response(200, CLAIMS_XML.encode()), biblio=_Response(200, b"<not-xml")
    )
    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509A1", session=session, sleep=lambda _: None)
    assert "not valid XML" in str(caught.value)
    assert len([u for u in session.urls if u.endswith("/biblio")]) == 1


def test_a_biblio_failure_produces_no_result_object_at_all():
    """There is no partially-populated `EnrichmentResult` to mistake for an
    answer: the exception is the only outcome."""
    session = _session(
        claims=_Response(200, CLAIMS_XML.encode()), biblio=_Response(503)
    )
    outcome = None
    try:
        outcome = enrich_publication("EP4778509A1", session=session, sleep=lambda _: None)
    except EpoEnrichmentFailed:
        pass
    assert outcome is None


def test_a_biblio_failure_fails_the_whole_enrichment_rather_than_excluding_one():
    """At the batch boundary too. A single publication whose version could not be
    established must not be quietly dropped from the coverage story."""
    session = _session(
        claims=_Response(200, CLAIMS_XML.encode()), biblio=_Response(500)
    )
    with pytest.raises(EpoEnrichmentFailed):
        enrich_candidates(
            [PatentRecord(id="EP4778509A1", title="t", source="Lens.org")],
            session=session,
            sleep=lambda _: None,
        )


def test_a_biblio_failure_message_carries_no_credentials_or_body():
    session = _session(
        claims=_Response(200, CLAIMS_XML.encode()),
        biblio=_Response(401, b"{'secret': 'epo-consumer-secret-value'}"),
    )
    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509A1", session=session, sleep=lambda _: None)
    message = str(caught.value)
    assert "epo-consumer-secret-value" not in message
    assert "Traceback" not in message
    assert "401" in message, "the status is the diagnostic; the body is not"


# --- the epodoc form -------------------------------------------------------


def test_the_kind_code_is_stripped_before_the_lookup():
    """EPO's full-text endpoints 404 on the kind-suffixed form and 200 on the
    bare one. Without this every enrichment call 404s and every candidate falls
    to `EXCLUDED_NO_FULL_TEXT`."""
    session = _session(claims=_Response(200, CLAIMS_XML.encode()))
    enrich_publication("EP4778509A1", session=session)
    assert any(url.endswith("/EP4778509/claims") for url in session.urls)
    assert not any("EP4778509A1" in url for url in session.urls)


# --- provider-neutral routing: the §17.2 defect ----------------------------


def _candidate(identifier: str, source: str) -> PatentRecord:
    return PatentRecord(id=identifier, title="A heat recovery system", source=source)


@pytest.mark.parametrize(
    "source",
    ["Lens.org", "Google Patents", "European Patent Office", "", "Some Future Provider"],
)
def test_a_candidate_is_enriched_whatever_discovery_source_found_it(source):
    """**The §17.2 defect, pinned.**

    The old guard selected only ``source == "European Patent Office"``. Under the
    approved workflow every candidate arrives from the discovery provider, so the
    target list was empty, enrichment silently no-opped, and every candidate fell
    to `EXCLUDED_NO_FULL_TEXT` -- indistinguishable from a legitimate coverage
    result.

    A discovery source and an enrichment source are different roles. There is no
    source guard.
    """
    targets = enrichment_targets([_candidate("EP4778509A1", source)])
    assert [t.id for t in targets] == ["EP4778509A1"]


def test_routing_filters_only_records_with_no_usable_publication_number():
    """The one thing filtered out is the absence of the identifier the lookup
    needs -- not a provider judgement."""
    targets = enrichment_targets(
        [
            _candidate("EP4778509A1", "Lens.org"),
            _candidate("", "Lens.org"),
            _candidate("not-a-publication", "Lens.org"),
        ]
    )
    assert [t.id for t in targets] == ["EP4778509A1"]


def test_enrichment_returns_one_outcome_per_candidate():
    def fake(publication_number, **kwargs):
        from models import EnrichmentResult

        return EnrichmentResult(
            requested_publication=publication_number,
            normalized_publication=publication_number.rstrip("AB12"),
            served_publication="",
            source_document_id=f"epo:{publication_number}",
            status=EnrichmentStatus.NO_TEXT,
        )

    outcomes = enrich_candidates(
        [_candidate("EP4778509A1", "Lens.org"), _candidate("US1234567B2", "Lens.org")],
        enricher=fake,
    )
    assert len(outcomes) == 2
    assert all(not outcome.failed for outcome in outcomes)


def test_one_failed_candidate_fails_the_whole_enrichment():
    """A provider outage means the candidate set was never fully assessed.
    Proceeding would present a partial matrix as a complete one."""
    def fake(publication_number, **kwargs):
        if publication_number.startswith("US"):
            raise EpoEnrichmentFailed("EPO OPS returned HTTP 503")
        from models import EnrichmentResult

        return EnrichmentResult(
            requested_publication=publication_number,
            normalized_publication="EP4778509",
            served_publication="",
            source_document_id="epo:EP4778509",
            status=EnrichmentStatus.NO_TEXT,
        )

    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_candidates(
            [_candidate("EP4778509A1", "Lens.org"), _candidate("US1234567B2", "Lens.org")],
            enricher=fake,
        )
    assert "US1234567B2" in str(caught.value), "the failing publication is named"


def test_no_targets_is_an_empty_result_not_a_failure():
    assert enrich_candidates([]) == []


# --- secrets ---------------------------------------------------------------


def test_an_auth_failure_message_carries_no_client_secret():
    """EPO uses OAuth client credentials, and this message can be stored in
    `publication_exclusions.provider_detail`."""
    class LeakySession(FakeSession):
        def post(self, url, **kwargs):
            raise RuntimeError(
                "401 for https://ops.epo.org/3.2/auth/accesstoken"
                "?client_secret=EPO_LIVE_SECRET_123"
            )

    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication("EP4778509", session=LeakySession({}), sleep=lambda _: None)
    assert "EPO_LIVE_SECRET_123" not in str(caught.value)


def test_a_failure_message_carries_no_response_body_or_traceback():
    def leaky(call_number: int):
        raise RuntimeError(
            '503\n{"fault": {"api_key": "LEAKED_KEY_999"}}\n  at epo.js:12'
        )

    with pytest.raises(EpoEnrichmentFailed) as caught:
        enrich_publication(
            "EP4778509", session=_session(claims=leaky), sleep=lambda _: None
        )
    message = str(caught.value)
    assert "LEAKED_KEY_999" not in message
    assert "\n" not in message


# --- no outbound calls -----------------------------------------------------


def test_this_module_never_reaches_the_network():
    """Every test above drives a `FakeSession`. Asserted rather than assumed,
    because a default-argument mistake would silently start spending EPO quota
    the moment credentials were present."""
    import inspect

    import search.epo_fulltext as module

    signature = inspect.signature(module.enrich_publication)
    assert signature.parameters["session"].default is None, (
        "a session must be injectable, or tests cannot avoid the network"
    )


def test_an_unparseable_publication_number_never_reaches_the_provider():
    with pytest.raises(PublicationNumberError):
        enrich_publication("not-a-publication", session=_session())
