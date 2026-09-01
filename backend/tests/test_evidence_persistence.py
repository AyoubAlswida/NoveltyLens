"""Evidence persistence against real PostgreSQL: fencing, ownership, atomicity,
replay safety, and the constraints migrations 0011 and 0012 declare.

Everything here is `postgres`-marked because every assertion is a property of the
SQL -- a CHECK, a foreign key, a transaction boundary, a lease -- and the
in-memory double structurally cannot exhibit any of them.

**The rule this file protects.** A fenced enrichment response must never become
a passage. The response arrived; the authority to act on it did not survive. And
a publication must never be half-written: eligible with some of its passages is a
coverage claim nobody made.
"""

import psycopg
import pytest

from evidence.dates import (
    DateCertainty,
    ScreeningStage,
    build_publication_date,
)
from evidence.eligibility import ExclusionState, PublicationExclusion, confirm_eligibility
from evidence.passages import build_passages
from models import (
    EnrichmentResult,
    EnrichmentStatus,
    ProviderClaim,
    SectionText,
    TextVersionSource,
)
from workflow import evidence_store, lease
from workflow.types import FencedError, ReplayConflictError, RunNotFoundError

pytestmark = pytest.mark.postgres

TTL = 60.0
OWNER = "evidence-worker"
ACTOR = "testnovelty@gmail.com"

PARAGRAPH = (
    "In one embodiment, heat exchanger 120 is mounted inside exhaust duct 110 and "
    "receives coolant from pump 130. Controller 140 adjusts the coolant flow rate "
    "according to a stored duty schedule, calibrated during manufacture. "
)


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


CLAIM_ONE = "A system comprising a heat exchanger disposed within an exhaust duct."
CLAIM_TWO = "The system of claim 1, wherein the duct has a circular cross-section."
CLAIMS_TEXT = CLAIM_ONE + "\n\n" + CLAIM_TWO


def _section(name: str) -> SectionText:
    """A retrieved section as EPO serves it.

    A CLAIMS section carries **structured provider claims** with the document's
    own numbers and offsets into this exact text. That is the only thing claim
    evidence is ever built from -- `PatentRecord.claims` is discovery metadata
    from Lens and is not evidence, which is why no `PatentRecord` appears
    anywhere in this module.
    """
    if name == "CLAIMS":
        return SectionText(
            section="CLAIMS",
            text=CLAIMS_TEXT,
            text_sha256=_sha(CLAIMS_TEXT),
            language="en",
            claims=(
                ProviderClaim(
                    number=1,
                    text=CLAIM_ONE,
                    char_start=0,
                    char_end=len(CLAIM_ONE),
                ),
                ProviderClaim(
                    number=2,
                    text=CLAIM_TWO,
                    char_start=len(CLAIM_ONE) + 2,
                    char_end=len(CLAIMS_TEXT),
                ),
            ),
        )
    return SectionText(
        section=name,
        text=PARAGRAPH * 2,
        text_sha256=_sha(PARAGRAPH * 2),
        language="en",
    )


def _enrichment(*, sections=("CLAIMS",), served="EP4778509") -> EnrichmentResult:
    built = tuple(_section(name) for name in sections)
    return EnrichmentResult(
        requested_publication="EP4778509A1",
        normalized_publication="EP4778509",
        served_publication=served,
        source_document_id="epo:EP4778509",
        status=EnrichmentStatus.TEXT,
        sections=built,
        sections_available=tuple(sections),
        sections_retrieved=tuple(sections),
        language_attempted="en",
        text_version_original="20190601",
        text_version_normalized="2019-06-01",
        text_version_source=TextVersionSource.PROVIDER_PUBLICATION_DATE,
        retrieved_at="2026-08-28T12:00:00+00:00",
    )


@pytest.fixture
def leased(pg, make_run):
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    return run_id, token


def _passages():
    enrichment = _enrichment()
    return build_passages(enrichment), enrichment


# --- fencing ---------------------------------------------------------------


def test_a_fenced_worker_cannot_write_passages(pg, leased):
    """**A fenced enrichment response never becomes a passage.** The response
    arrived; the authority to act on it did not survive."""
    run_id, old_token = leased
    passages, _ = _passages()

    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '120 seconds',
               expires_at  = now() - interval '60 seconds'
         WHERE run_id = %s
        """,
        (run_id,),
    )
    lease.acquire(pg, run_id=run_id, owner_id="replacement", ttl_seconds=TTL)

    with pytest.raises(FencedError):
        evidence_store.record_passages(
            pg, run_id=run_id, owner_id=OWNER, fence_token=old_token, passages=passages
        )
    assert pg.execute(
        "SELECT count(*) FROM evidence_passages WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0


def test_a_fenced_worker_cannot_write_an_exclusion(pg, leased):
    run_id, old_token = leased
    pg.execute(
        """
        UPDATE run_leases SET acquired_at = now() - interval '120 seconds',
               expires_at = now() - interval '60 seconds' WHERE run_id = %s
        """,
        (run_id,),
    )
    lease.acquire(pg, run_id=run_id, owner_id="replacement", ttl_seconds=TTL)

    with pytest.raises(FencedError):
        evidence_store.record_exclusion(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=old_token,
            exclusion=PublicationExclusion(
                requested_number="EP4778509A1",
                normalized_number="EP4778509",
                state=ExclusionState.EXCLUDED_NO_FULL_TEXT,
                reason="no eligible full text",
            ),
        )
    assert pg.execute(
        "SELECT count(*) FROM publication_exclusions WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0


def test_a_fenced_worker_cannot_write_a_publication_date(pg, leased):
    run_id, old_token = leased
    pg.execute(
        """
        UPDATE run_leases SET acquired_at = now() - interval '120 seconds',
               expires_at = now() - interval '60 seconds' WHERE run_id = %s
        """,
        (run_id,),
    )
    lease.acquire(pg, run_id=run_id, owner_id="replacement", ttl_seconds=TTL)

    with pytest.raises(FencedError):
        evidence_store.record_publication_date(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=old_token,
            publication_date=build_publication_date(
                publication_number="EP4778509",
                date_original="2019-06-01",
                date_source="lens.date_published",
                stage=ScreeningStage.DISCOVERY,
            ),
        )
    assert pg.execute(
        "SELECT count(*) FROM publication_dates WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0


# --- the happy path and replay safety --------------------------------------


def test_passages_round_trip_with_their_provenance(pg, leased):
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )

    stored = evidence_store.list_passages(pg, run_id=run_id, actor_email=ACTOR)
    assert len(stored) == len(passages)
    first = stored[0]
    assert first["section"] in ("CLAIMS", "DESCRIPTION")
    assert first["locator"]
    assert first["text_version_date"].isoformat() == "2019-06-01"
    assert first["canonical_text"]


def test_re_running_the_same_enrichment_converges_rather_than_duplicating(pg, leased):
    """Recovery re-runs a node. Chunk ids are pure functions of publication +
    section + locator, so the second attempt writes the same rows."""
    run_id, token = leased
    passages, _ = _passages()

    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    first = pg.execute(
        "SELECT count(*) FROM evidence_passages WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    second = pg.execute(
        "SELECT count(*) FROM evidence_passages WHERE run_id = %s", (run_id,)
    ).fetchone()[0]

    assert first == second, "a replayed enrichment converges, it does not duplicate"


def test_a_publication_date_upserts_on_replay(pg, leased):
    run_id, token = leased
    for original in ("2019-06-01", "2019-06-01"):
        evidence_store.record_publication_date(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            publication_date=build_publication_date(
                publication_number="EP4778509",
                date_original=original,
                date_source="lens.date_published",
                stage=ScreeningStage.DISCOVERY,
            ),
        )
    assert pg.execute(
        "SELECT count(*) FROM publication_dates WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 1


# --- atomicity -------------------------------------------------------------


def test_a_partial_persistence_failure_rolls_back_everything(pg, leased):
    """A crash between the passages and the exclusion would leave a publication
    that is neither eligible nor excluded -- invisible to both surfaces, which is
    worse than either."""
    run_id, token = leased
    passages, _ = _passages()

    try:
        with pg.transaction():
            evidence_store.record_passages(
                pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
            )
            evidence_store.record_publication_date(
                pg,
                run_id=run_id,
                owner_id=OWNER,
                fence_token=token,
                publication_date=build_publication_date(
                    publication_number="EP4778509",
                    date_original="2019-06-01",
                    date_source="lens",
                    stage=ScreeningStage.DISCOVERY,
                ),
            )
            raise RuntimeError("the node died before committing")
    except RuntimeError:
        pass

    assert pg.execute(
        "SELECT count(*) FROM evidence_passages WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0
    assert pg.execute(
        "SELECT count(*) FROM publication_dates WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0


def test_a_publication_cannot_be_both_excluded_and_carry_passages(pg, leased):
    """An exclusion with citable evidence attached is a contradiction a reviewer
    would have to resolve by guessing."""
    run_id, token = leased
    passages, _ = _passages()
    with pytest.raises(ValueError) as caught:
        evidence_store.persist_publication(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            exclusion=PublicationExclusion(
                requested_number="EP4778509A1",
                normalized_number="EP4778509",
                state=ExclusionState.EXCLUDED_NO_FULL_TEXT,
                reason="no eligible full text",
            ),
            passages=passages,
        )
    assert "cannot be both excluded and carry passages" in str(caught.value)


def test_persisting_a_whole_publication_is_one_transaction(pg, leased):
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.persist_publication(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        publication_date=build_publication_date(
            publication_number="EP4778509",
            date_original="2019-06-01",
            date_source="lens.date_published",
            stage=ScreeningStage.DISCOVERY,
        ),
        passages=passages,
    )
    assert pg.execute(
        "SELECT count(*) FROM evidence_passages WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == len(passages)
    assert pg.execute(
        "SELECT count(*) FROM publication_dates WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 1


# --- an exclusion creates no verification cell -----------------------------


def test_an_exclusion_creates_no_verification_cell(pg, leased):
    """**The scope rule.** A publication-scoped exclusion means no comparison was
    attempted, so a cell would imply one was attempted and answered negatively."""
    run_id, token = leased
    enrichment = _enrichment(sections=())
    enrichment = EnrichmentResult(
        **{
            **{f: getattr(enrichment, f) for f in enrichment.__slots__},
            "status": EnrichmentStatus.NO_TEXT,
            "sections": (),
            "sections_retrieved": (),
        }
    )
    result = confirm_eligibility(enrichment, [])
    evidence_store.persist_publication(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        exclusion=result.exclusion,
    )

    assert pg.execute(
        "SELECT count(*) FROM publication_exclusions WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 1
    assert pg.execute(
        "SELECT count(*) FROM verification_cells WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0, "an excluded publication has no cells at all"


# --- ownership -------------------------------------------------------------


def test_a_second_user_cannot_read_another_runs_evidence(pg, leased, second_user):
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )

    assert evidence_store.list_passages(pg, run_id=run_id, actor_email=ACTOR)
    with pytest.raises(RunNotFoundError):
        evidence_store.list_passages(pg, run_id=run_id, actor_email=second_user)
    with pytest.raises(RunNotFoundError):
        evidence_store.list_exclusions(pg, run_id=run_id, actor_email=second_user)


# --- the schema's own rules (migrations 0011 and 0012) ---------------------


def test_only_claims_and_description_can_be_stored_as_evidence(pg, leased):
    """The schema-level half of the eligibility rule: no application bug can
    route an abstract into a prompt, because the row cannot exist."""
    run_id, _ = leased
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            """
            INSERT INTO evidence_passages
                (run_id, chunk_id, publication_number, source_document_id, source,
                 section, locator, locator_kind, locator_source, section_text_sha256,
                 language, text_version_original, text_version_date, text_version_src,
                 retrieved_at, canonical_text, text_sha256)
            VALUES (%s, 'PAT-X-ABS-001', 'EP4778509', 'epo:EP4778509', 'EPO OPS',
                    'ABSTRACT', 'abstract', 'SOURCE_RANGE', 'DERIVED_SOURCE_OFFSET',
                    'h', 'en', '20190601', '2019-06-01', 'PROVIDER_PUBLICATION_DATE',
                    now(), 'text', 'h')
            """,
            (run_id,),
        )


def test_a_source_range_passage_must_carry_its_offsets_and_version(pg, leased):
    """Offsets with no segmenter version, or a version with no offsets, is a
    locator that cannot be reproduced."""
    run_id, _ = leased
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            """
            INSERT INTO evidence_passages
                (run_id, chunk_id, publication_number, source_document_id, source,
                 section, locator, locator_kind, locator_source, section_text_sha256,
                 language, text_version_original, text_version_date, text_version_src,
                 retrieved_at, canonical_text, text_sha256)
            VALUES (%s, 'PAT-X-DESC-R1', 'EP4778509', 'epo:EP4778509', 'EPO OPS',
                    'DESCRIPTION', 'description characters 0-10', 'SOURCE_RANGE',
                    'DERIVED_SOURCE_OFFSET', 'h', 'en', '20190601', '2019-06-01',
                    'PROVIDER_PUBLICATION_DATE', now(), 'text', 'h')
            """,
            (run_id,),
        )


def test_a_text_version_source_of_none_can_never_be_stored(pg, leased):
    """`Passage` requires a non-empty `text_version_date`, so a publication with
    no trustworthy version produces no passage and is excluded instead. The
    schema says so too."""
    run_id, _ = leased
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            """
            INSERT INTO evidence_passages
                (run_id, chunk_id, publication_number, source_document_id, source,
                 section, locator, locator_kind, locator_source, section_text_sha256,
                 language, text_version_original, text_version_date, text_version_src,
                 retrieved_at, canonical_text, text_sha256)
            VALUES (%s, 'PAT-X-CLM-001', 'EP4778509', 'epo:EP4778509', 'EPO OPS',
                    'CLAIMS', 'claim 1', 'CLAIM_NUMBER', 'PROVIDER_CLAIM_NUMBER',
                    'h', 'en', '', '2019-06-01', 'NONE', now(), 'text', 'h')
            """,
            (run_id,),
        )


def test_a_certain_date_must_have_a_normalized_value(pg, leased):
    """`CERTAIN` with no date is a claim of certainty about nothing."""
    run_id, _ = leased
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            """
            INSERT INTO publication_dates
                (run_id, publication_number, date_original, date_normalized,
                 date_source, date_certainty, screened_at_stage)
            VALUES (%s, 'EP4778509', '2019-06-01', NULL, 'lens', 'CERTAIN', 'DISCOVERY')
            """,
            (run_id,),
        )


def test_an_uncertain_date_may_have_no_normalized_value(pg, leased):
    """The other direction is legal and necessary: that is what an unreadable
    provider date looks like."""
    run_id, _ = leased
    pg.execute(
        """
        INSERT INTO publication_dates
            (run_id, publication_number, date_original, date_normalized,
             date_source, date_certainty, screened_at_stage)
        VALUES (%s, 'EP4778509', 'sometime in 2019', NULL, 'lens', 'UNCERTAIN', 'DISCOVERY')
        """,
        (run_id,),
    )


def test_an_exclusion_reason_can_never_be_empty(pg, leased):
    """An exclusion nobody can explain is not auditable."""
    run_id, _ = leased
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            """
            INSERT INTO publication_exclusions
                (run_id, requested_number, normalized_number, state, reason)
            VALUES (%s, 'EP4778509A1', 'EP4778509', 'EXCLUDED_NO_FULL_TEXT', '')
            """,
            (run_id,),
        )


def test_evidence_rows_disappear_with_their_run(pg, make_run):
    """The retention path: deleting a run takes its evidence with it, by
    cascade."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )

    pg.execute("DELETE FROM verification_runs WHERE id = %s", (run_id,))
    assert pg.execute(
        "SELECT count(*) FROM evidence_passages WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0


def test_no_module_outside_the_store_writes_the_evidence_tables():
    """The Gate A boundary, extended to the new tables."""
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    tables = ("evidence_passages", "publication_dates", "publication_exclusions")
    verbs = ("insert into ", "update ", "delete from ")
    offenders = []
    for path in backend.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests", "workflow"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8").lower()
        for table in tables:
            if any(f"{verb}{table}" in text for verb in verbs):
                offenders.append(f"{path.relative_to(backend)} -> {table}")
    assert offenders == [], f"evidence tables written outside workflow/: {offenders}"


# =========================================================================
# B3 -- exclusion replay is a no-op, a conflicting one is FN003
# =========================================================================
#
# Recovery re-runs a node, so every evidence write is replayed sooner or later.
# `record_exclusion` used to be a plain INSERT into a table with **no
# natural-key constraint at all**, so a replay did not raise -- it silently
# wrote a SECOND exclusion row for the same publication, possibly with a
# different reason, both rows claiming to be why it was excluded.


def _exclusion(
    *,
    state=ExclusionState.EXCLUDED_NO_FULL_TEXT,
    reason="no eligible full text",
    failing_check="",
    passages_built=0,
) -> PublicationExclusion:
    return PublicationExclusion(
        requested_number="EP4778509A1",
        normalized_number="EP4778509",
        state=state,
        reason=reason,
        failing_check=failing_check,
        passages_built=passages_built,
    )


def _exclusion_rows(pg, run_id) -> int:
    return pg.execute(
        "SELECT count(*) FROM publication_exclusions WHERE run_id = %s", (run_id,)
    ).fetchone()[0]


def test_an_identical_exclusion_replay_writes_nothing_and_returns_the_same_id(pg, leased):
    """The whole point of the contract: a re-run node converges."""
    run_id, token = leased
    first = evidence_store.record_exclusion(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
    )
    second = evidence_store.record_exclusion(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
    )
    assert first == second
    assert _exclusion_rows(pg, run_id) == 1


def test_a_conflicting_exclusion_replay_raises_rather_than_writing_a_second_row(pg, leased):
    """**The B3 defect, pinned.** Two rows with different reasons, both claiming
    to be why the publication was excluded, is not something a reviewer can
    resolve -- so it is refused at the point of writing."""
    run_id, token = leased
    evidence_store.record_exclusion(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
    )
    with pytest.raises(ReplayConflictError) as caught:
        evidence_store.record_exclusion(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            exclusion=_exclusion(
                state=ExclusionState.EXCLUDED_INELIGIBLE,
                reason="no trustworthy text version",
            ),
        )
    assert "conflicting replay is not a replay" in str(caught.value)
    assert _exclusion_rows(pg, run_id) == 1


def test_a_differing_reason_alone_is_still_a_conflict(pg, leased):
    """Matching states are not enough. The reason is what a reviewer reads."""
    run_id, token = leased
    evidence_store.record_exclusion(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
    )
    with pytest.raises(ReplayConflictError):
        evidence_store.record_exclusion(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            exclusion=_exclusion(reason="EPO served text for a different publication"),
        )


def test_a_stale_fence_token_cannot_replay_an_exclusion(pg, leased):
    """Fencing is checked before the replay comparison, so a superseded worker
    cannot even confirm its own earlier write."""
    run_id, token = leased
    evidence_store.record_exclusion(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
    )
    pg.execute(
        """
        UPDATE run_leases SET acquired_at = now() - interval '120 seconds',
               expires_at = now() - interval '60 seconds' WHERE run_id = %s
        """,
        (run_id,),
    )
    lease.acquire(pg, run_id=run_id, owner_id="replacement", ttl_seconds=TTL)

    with pytest.raises(FencedError):
        evidence_store.record_exclusion(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
        )
    assert _exclusion_rows(pg, run_id) == 1


def test_the_right_token_held_by_the_wrong_owner_is_still_fenced(pg, leased):
    """A token is not a bearer credential: it is valid only to the owner the
    lease actually names."""
    run_id, token = leased
    with pytest.raises(FencedError):
        evidence_store.record_exclusion(
            pg,
            run_id=run_id,
            owner_id="someone-else",
            fence_token=token,
            exclusion=_exclusion(),
        )
    assert _exclusion_rows(pg, run_id) == 0


def test_an_exclusion_write_rolled_back_leaves_no_row(pg, leased):
    """The write is ordinary transactional work, not a side channel."""
    run_id, token = leased
    try:
        with pg.transaction():
            evidence_store.record_exclusion(
                pg,
                run_id=run_id,
                owner_id=OWNER,
                fence_token=token,
                exclusion=_exclusion(),
            )
            raise RuntimeError("the node died before committing")
    except RuntimeError:
        pass
    assert _exclusion_rows(pg, run_id) == 0


def test_concurrent_identical_exclusions_produce_exactly_one_row(pg, leased, postgres_url):
    """Two workers replaying the same node at once. The row lock serializes them
    and the loser sees the winner's committed row."""
    import threading

    import psycopg as _psycopg

    run_id, token = leased
    barrier = threading.Barrier(2)
    errors: list = []

    def attempt() -> None:
        try:
            with _psycopg.connect(postgres_url, autocommit=True) as conn:
                barrier.wait(timeout=10)
                evidence_store.record_exclusion(
                    conn,
                    run_id=run_id,
                    owner_id=OWNER,
                    fence_token=token,
                    exclusion=_exclusion(),
                )
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    assert _exclusion_rows(pg, run_id) == 1


def test_concurrent_conflicting_exclusions_leave_one_row_and_one_refusal(
    pg, leased, postgres_url
):
    """One statement about the publication survives; the other is refused rather
    than silently overwriting or duplicating it."""
    import threading

    import psycopg as _psycopg

    run_id, token = leased
    barrier = threading.Barrier(2)
    errors: list = []
    reasons = ["no eligible full text", "no trustworthy text version"]

    def attempt(index: int) -> None:
        try:
            with _psycopg.connect(postgres_url, autocommit=True) as conn:
                barrier.wait(timeout=10)
                evidence_store.record_exclusion(
                    conn,
                    run_id=run_id,
                    owner_id=OWNER,
                    fence_token=token,
                    exclusion=_exclusion(reason=reasons[index]),
                )
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(errors) == 1, f"expected exactly one refusal, got {errors}"
    assert isinstance(errors[0], ReplayConflictError)
    assert _exclusion_rows(pg, run_id) == 1


def test_an_exclusion_replay_creates_no_verification_cell_either(pg, leased):
    run_id, token = leased
    for _ in range(2):
        evidence_store.record_exclusion(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
        )
    assert pg.execute(
        "SELECT count(*) FROM verification_cells WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0


# =========================================================================
# B4 -- a validated passage is immutable
# =========================================================================
#
# The old upsert's SET list touched only `canonical_text`, `text_sha256`,
# `section_text_sha256` and `retrieved_at`. A replay carrying genuinely
# different evidence overwrote the text while leaving the text-version date,
# locator, section, language and provenance from the ORIGINAL passage -- a row
# whose text says one thing and whose provenance says another, and a stored
# verdict citing text it was never validated against.


def _passage_rows(pg, run_id) -> int:
    return pg.execute(
        "SELECT count(*) FROM evidence_passages WHERE run_id = %s", (run_id,)
    ).fetchone()[0]


def _mutated(passages, **passage_fields):
    """The same chunk id carrying different content or provenance."""
    import dataclasses

    first = passages[0]
    changed = dataclasses.replace(
        first, passage=dataclasses.replace(first.passage, **passage_fields)
    )
    return [changed] + list(passages[1:])


def test_an_identical_passage_replay_inserts_nothing(pg, leased):
    run_id, token = leased
    passages, _ = _passages()
    first = evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    second = evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    assert first == len(passages)
    assert second == 0, "a replayed passage writes nothing at all"
    assert _passage_rows(pg, run_id) == len(passages)


def test_different_text_under_the_same_chunk_id_raises_rather_than_overwriting(pg, leased):
    """**The B4 defect, pinned.** The old upsert accepted this and rewrote the
    text in place, leaving the original provenance beside it."""
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    with pytest.raises(ReplayConflictError) as caught:
        evidence_store.record_passages(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            passages=_mutated(passages, text="Entirely different claim language."),
        )
    assert "immutable" in str(caught.value)

    stored = evidence_store.list_passages(pg, run_id=run_id, actor_email=ACTOR)
    assert all(
        row["canonical_text"] != "Entirely different claim language." for row in stored
    )


def test_different_provenance_under_the_same_chunk_id_raises(pg, leased):
    """A changed text-version date with unchanged text is the more dangerous
    half: the evidence looks identical and the thing it is dated against moved."""
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    with pytest.raises(ReplayConflictError):
        evidence_store.record_passages(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            passages=_mutated(passages, text_version_date="2021-01-04"),
        )
    stored = evidence_store.list_passages(pg, run_id=run_id, actor_email=ACTOR)
    assert {row["text_version_date"].isoformat() for row in stored} == {"2019-06-01"}


def test_a_changed_locator_under_the_same_chunk_id_raises(pg, leased):
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    with pytest.raises(ReplayConflictError):
        evidence_store.record_passages(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            passages=_mutated(passages, locator="claim 9"),
        )


def test_a_later_retrieval_time_alone_is_a_replay_and_never_updates_the_row(pg, leased):
    """`retrieved_at` is excluded from the identity comparison on purpose: two
    identical re-fetches legitimately differ in clock time, so including it would
    make every replay a conflict. It is also never updated -- the first retrieval
    is the one that produced this evidence, and overwriting it would misdate the
    provenance."""
    import dataclasses

    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    original = pg.execute(
        "SELECT retrieved_at FROM evidence_passages WHERE run_id = %s ORDER BY chunk_id",
        (run_id,),
    ).fetchall()

    later = [
        dataclasses.replace(built, retrieved_at="2026-09-01T09:00:00+00:00")
        for built in passages
    ]
    assert (
        evidence_store.record_passages(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=later
        )
        == 0
    )
    assert (
        pg.execute(
            "SELECT retrieved_at FROM evidence_passages WHERE run_id = %s ORDER BY chunk_id",
            (run_id,),
        ).fetchall()
        == original
    )


def test_a_publication_with_passages_can_never_then_be_excluded(pg, leased):
    """Enforced in the database, not by a hopeful `if` in the caller."""
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    with pytest.raises(ReplayConflictError) as caught:
        evidence_store.record_exclusion(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
        )
    assert "already holds evidence passages" in str(caught.value)
    assert _exclusion_rows(pg, run_id) == 0


def test_an_excluded_publication_can_never_then_receive_passages(pg, leased):
    """The same rule from the other direction, so neither write order can produce
    a publication that is both cited and excluded."""
    run_id, token = leased
    evidence_store.record_exclusion(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, exclusion=_exclusion()
    )
    passages, _ = _passages()
    with pytest.raises(ReplayConflictError) as caught:
        evidence_store.record_passages(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
        )
    assert "has no evidence" in str(caught.value)
    assert _passage_rows(pg, run_id) == 0


def test_a_conflicting_passage_replay_leaves_the_earlier_rows_intact(pg, leased):
    """A refused write is not a partial one: the rows already stored are exactly
    as they were."""
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    before = evidence_store.list_passages(pg, run_id=run_id, actor_email=ACTOR)
    with pytest.raises(ReplayConflictError):
        evidence_store.record_passages(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            passages=_mutated(passages, text="Different evidence entirely."),
        )
    assert evidence_store.list_passages(pg, run_id=run_id, actor_email=ACTOR) == before


def test_concurrent_identical_passage_writes_insert_each_row_once(pg, leased, postgres_url):
    import threading

    import psycopg as _psycopg

    run_id, token = leased
    passages, _ = _passages()
    barrier = threading.Barrier(2)
    errors: list = []
    inserted: list[int] = []

    def attempt() -> None:
        try:
            with _psycopg.connect(postgres_url, autocommit=True) as conn:
                barrier.wait(timeout=10)
                inserted.append(
                    evidence_store.record_passages(
                        conn,
                        run_id=run_id,
                        owner_id=OWNER,
                        fence_token=token,
                        passages=passages,
                    )
                )
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    assert sum(inserted) == len(passages), "every row was inserted exactly once"
    assert _passage_rows(pg, run_id) == len(passages)


def test_a_passage_row_is_never_rewritten_by_a_replay(pg, leased):
    """`xmin` is PostgreSQL's own record of the transaction that last wrote a
    row. An UPDATE would change it; a no-op replay cannot."""
    run_id, token = leased
    passages, _ = _passages()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    before = pg.execute(
        "SELECT chunk_id, xmin::text FROM evidence_passages "
        "WHERE run_id = %s ORDER BY chunk_id",
        (run_id,),
    ).fetchall()
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, passages=passages
    )
    after = pg.execute(
        "SELECT chunk_id, xmin::text FROM evidence_passages "
        "WHERE run_id = %s ORDER BY chunk_id",
        (run_id,),
    ).fetchall()
    assert before == after, "a replay must not rewrite the row in any way"
