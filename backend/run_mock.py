"""Run the real backend with every *metered* boundary replaced by a local double.

    docker compose -f docker-compose.yml -f docker-compose.mock.yml up -d

Use this to look at the UI, click through a full run, and exercise login,
History and the error path without spending a single SerpAPI search or
HuggingFace credit. A real novelty search costs ~25 of a 250/month SerpAPI
allowance; this costs nothing.

**What is faked** - and it is exactly the three things that cost money:

    llm._llm_chat                       the LLM calls (understand/validate/analyze,
                                        plus feature-disclosure verification)
    compare.embed_texts                 ranking embeddings
    search.vector_store.embed_texts     indexing embeddings
    search.search.SOURCES               the patent providers
    graph.searching.enrich_*            full-text fetch (2 HTTP calls per patent)

**What is real** - everything else, which is the point. The FastAPI app, the auth
gate, the LangGraph graph and its routing, the SSE framing, prompt construction,
JSON parsing, dedup, chunking, cosine scoring, verdict thresholds, Postgres
persistence, and GET /api/history all run exactly as they do in production. This
is a stub at the network boundary, not a mock of the pipeline.

**This is a developer tool and must never become a runtime mode.** It patches
from the outside and is reachable only by running this file. Nothing in
``server.py`` or ``graph/`` knows it exists, and no flag in the shipped app
selects it. The zero-tolerance policy forbids the product substituting invented
output for a failed step -- a separate script that fakes the *inputs* so you can
look at the layout is a different thing, and it stays different only as long as
it stays out of the application.

Presentation runs are stored only in the overlay's separate Postgres database.
For the dry-pet-food dispenser example, the terminal report and History use the
matches and recommendations in ``mock_pet_food_report.json``. Quick Search shows
the first eight matches; Advanced Search shows all nine.
No live patent or model call supplies those values. The browser shows the
normal report UI; operator logs still identify this process as a mock.
The script refuses to start against the regular Postgres or Qdrant services.
"""

import argparse
import hashlib
import json
import re
import time
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import sys

# --- declare workflow intent before anything imports `config` ---------------
#
# `config.Settings` reads the environment in its class body.  The local .env is
# deliberately authoritative for ordinary configuration and may replace these
# values while imports run, so `main()` also reapplies this explicit mock-only
# topology to the shared settings object before importing the server.
#
# `sys.argv` is read directly rather than through argparse because argparse runs
# in `main()`, which is after the imports this has to precede.
if "--workflow" in sys.argv:
    # **No verifier policy is set here, and none is needed.** This block used to
    # supply `DATE_SCREENING_POLICY`, `TEXT_VERSION_POLICY` and a capability
    # deferral, because admission refused a run without them. Advanced Search
    # screens no dates, selects no text version and runs no verifier, so
    # admission consults none of it (spec section 10.4) -- setting them would be
    # configuring a feature the product no longer has.
    os.environ["WORKFLOW_EXECUTOR"] = "inprocess"
    os.environ["WORKFLOW_GRAPH_ADAPTER"] = "graph.adapter:ADAPTER"


from contextlib import ExitStack
from unittest.mock import patch

from models import PatentRecord
from search.vector_store import QUERY_INSTRUCTION
from llm import (
    DECOMPOSITION_PROMPT_MARKER,
    LIMITATIONS_PROMPT_MARKER,
    _RELATION_MARKERS,
    SUMMARY_PROMPT_MARKER,
    STRATEGY_PROMPT_MARKER,
)
from verification.prompt import PROMPT_MARKER as VERIFIER_MARKER

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

SLOWDOWN = 1.0
"""Scales the artificial delays. Set to 0 by ``--fast``, or to any multiplier by
``--pace``; see :func:`_pause`."""


def _pause(seconds: float) -> None:
    """Sleep, so the SSE progress checklist advances at a watchable pace.

    Without this the whole run finishes in about two seconds and the running
    screen is gone before you can read it -- which makes the one screen whose
    entire job is showing progress impossible to evaluate. A real run takes
    ~62s; these delays land it around 8s, close enough to judge the pacing and
    far enough from tedious.

    ``--pace`` scales every delay at once, so ``--pace 1.3`` stretches the run to
    roughly 10s. It scales only the sleeps: the mocked work itself, and every
    real part of the pipeline around it, take whatever they take. Pushing it high
    does not make the mock more faithful -- it just adds dead time.
    """
    if SLOWDOWN:
        time.sleep(seconds * SLOWDOWN)


class PaidCallAttempted(RuntimeError):
    """A mocked-out boundary tried to reach the network anyway."""


def _install_network_guard() -> None:
    """Make "this spends nothing" enforced rather than promised.

    Every metered boundary in this project (``llm.py``, ``embeddings.py``,
    ``search/google.py``, ``search/epo.py``) calls out through ``requests``, so
    wrapping ``Session.request`` and refusing any non-local host means a missed
    patch fails loudly instead of quietly charging a SerpAPI search or a
    HuggingFace credit to the account.

    Only ``requests`` is guarded, deliberately: ``qdrant-client`` talks over
    ``httpx`` and Postgres over ``psycopg``, both to localhost, and both are
    free and meant to stay real.
    """
    import requests
    from urllib.parse import urlparse

    original = requests.sessions.Session.request

    def guarded(self, method: str, url: str, *args: object, **kwargs: object):
        host = urlparse(str(url)).hostname
        if host not in LOCAL_HOSTS:
            raise PaidCallAttempted(
                f"run_mock.py blocked an outbound {method} to {host!r}. Something on the "
                f"pipeline's paid path is not mocked -- fix the patch rather than "
                f"removing this guard, which exists so a gap cannot silently spend quota."
            )
        return original(self, method, url, *args, **kwargs)

    requests.sessions.Session.request = guarded


MOCK_SOURCE = "Lens.org"
"""Source text shown for the isolated presentation report and its History row."""

MOCK_TECHNICAL_SUMMARY = (
    "An automatic dry-pet-food dispenser includes a rotating body having several "
    "equal-sized compartments, each closed by a spring-loaded trap door at the "
    "bottom. A drive mechanism periodically withdraws a support bar holding a trap "
    "door shut, allowing that compartment’s food to fall by gravity through a "
    "directional trough into the pet’s bowl below and dispense one measured portion "
    "at a time on a schedule. The bowl contains a small load cell that weighs how "
    "much food the pet actually ate versus how much was dispensed, and if a portion "
    "goes mostly uneaten for several consecutive feedings, the dispenser automatically "
    "reduces future portion sizes and sends a low-appetite alert to the owner’s phone."
)
"""Stage-1 system understanding shown by the isolated presentation flow."""

# Earlier water-bottle corpus, retained for local scoring reference. The
# presentation provider below serves PET_CORPUS instead. Long enough abstracts that
# chunking, embedding and the title/abstract pre-filter all do real work, and
# varied enough that scoring produces a spread rather than one flat number.
# The last two are deliberately off-topic: they exercise the low-similarity end
# of the scale and the "Low" risk styling, which a corpus of near-matches never
# would.
MOCK_CORPUS = [
    PatentRecord(
        id="US10442712B2",
        title="Portable ultraviolet water purification vessel",
        abstract=(
            "A portable drinking vessel incorporating an ultraviolet-C light emitting diode "
            "array positioned within the cap assembly. The array irradiates the contained "
            "liquid on a timed cycle, inactivating bacteria and viral particles without "
            "chemical additives. A rechargeable cell powers the emitter and a status "
            "indicator reports cycle completion to the user."
        ),
        assignee="Clearstream Devices Inc.",
        publication_date="2019-10-15",
        source=MOCK_SOURCE,
        url="https://patents.example/US10442712B2",
    ),
    PatentRecord(
        id="EP3218319A1",
        title="Graphene oxide membrane for particulate filtration",
        abstract=(
            "A replaceable filtration membrane formed from stacked graphene oxide laminae "
            "with controlled interlayer spacing. The membrane retains particulates above a "
            "nominal threshold while maintaining volumetric flow suitable for gravity-fed "
            "and hand-pressurised systems. A cartridge housing permits replacement without "
            "tools."
        ),
        assignee="Nanolayer Filtration GmbH",
        publication_date="2017-09-20",
        source=MOCK_SOURCE,
        url="https://patents.example/EP3218319A1",
    ),
    PatentRecord(
        id="US9789456B1",
        title="Flow-triggered sanitisation cycle for beverage containers",
        abstract=(
            "A container cap containing a flow rate sensor that detects consumption events "
            "and initiates a sanitisation cycle without manual activation. Cycle scheduling "
            "is derived from cumulative flow volume rather than elapsed time, so an unused "
            "container does not consume stored energy."
        ),
        assignee="Hydrologic Systems LLC",
        publication_date="2017-10-17",
        source=MOCK_SOURCE,
        url="https://patents.example/US9789456B1",
    ),
    PatentRecord(
        id="WO2020115432A1",
        title="Self-cleaning fluid conduit with photocatalytic coating",
        abstract=(
            "A fluid conduit bearing a titanium dioxide photocatalytic coating activated by "
            "an integrated light source. Activation degrades organic films on the conduit "
            "wall, reducing biofouling in intermittently used potable water lines."
        ),
        assignee="Photolytic Water AB",
        publication_date="2020-06-11",
        source=MOCK_SOURCE,
        url="https://patents.example/WO2020115432A1",
    ),
    PatentRecord(
        id="US8123999B2",
        title="Insulated vacuum flask with rotating closure",
        abstract=(
            "A double-walled vacuum-insulated flask with a rotating closure providing "
            "single-handed opening. Thermal retention is improved by a getter material in "
            "the evacuated cavity."
        ),
        assignee="Thermal Goods Co.",
        publication_date="2012-02-28",
        source=MOCK_SOURCE,
        url="https://patents.example/US8123999B2",
    ),
    PatentRecord(
        id="US7654321B2",
        title="Agricultural gearbox power take-off assembly",
        abstract=(
            "A gearbox assembly transmitting rotational power from a tractor engine to a "
            "towed implement through a splined shaft, with an overrunning clutch preventing "
            "reverse torque transmission during deceleration."
        ),
        assignee="Fieldworks Machinery Ltd.",
        publication_date="2010-02-02",
        source=MOCK_SOURCE,
        url="https://patents.example/US7654321B2",
    ),
    PatentRecord(
        id="EP2987654A1",
        title="Modular cartridge coupling for point-of-use water treatment",
        abstract=(
            "A bayonet coupling allowing treatment cartridges to be exchanged in point-of-use "
            "water systems. Keyed geometry prevents installation of an incompatible cartridge "
            "and a seal ring maintains integrity across repeated exchange cycles."
        ),
        assignee="Aquacouple SA",
        publication_date="2016-02-24",
        source=MOCK_SOURCE,
        url="https://patents.example/EP2987654A1",
    ),
    PatentRecord(
        id="US11002233B2",
        title="Battery management for handheld sterilisation devices",
        abstract=(
            "A power management circuit for handheld ultraviolet sterilisation devices that "
            "reserves sufficient charge to complete an initiated cycle, preventing partial "
            "irradiation that would leave a load incompletely treated."
        ),
        assignee="Clearstream Devices Inc.",
        publication_date="2021-05-11",
        source=MOCK_SOURCE,
        url="https://patents.example/US11002233B2",
    ),
]

PET_REPORT = json.loads((Path(__file__).with_name("mock_pet_food_report.json")).read_text(encoding="utf-8"))
QUICK_PET_RESULT_COUNT = 8
ADVANCED_PET_RESULT_COUNT = 9
PET_CORPUS = [
    PatentRecord(
        id=match["num"],
        title=match["title"],
        abstract=match["rationale"],
        source=match["source"],
        url=match["url"],
    )
    for match in PET_REPORT["patents"]
]


def _is_pet_example(description: str) -> bool:
    normalized = " ".join(description.casefold().split())
    return "dry-pet-food dispenser" in normalized and "load cell" in normalized


def _mock_provider(query: str, size: int) -> list[PatentRecord]:
    """Stand in for ``search_google``.

    Rotates the corpus by a hash of the query so different generated queries
    return overlapping-but-different sets. That is what makes ``merge_patents``
    dedupe for real and gives ``search_all_providers_strict`` a genuine
    candidates/index_only split, rather than handing the same list back five
    times.

    Returns deep copies: ``PatentRecord`` is mutable and enrichment writes
    ``full_text`` in place, so a shared instance would accumulate state across
    requests and across queries within one request. ``deepcopy`` rather than
    ``vars()`` because the dataclass is ``slots=True`` and so has no ``__dict__``,
    and rather than ``dataclasses.replace`` because that would share the
    ``inventors`` list between every copy.
    """
    _pause(0.9)  # a real provider call is 0.5-2s; these run concurrently
    offset = sum(ord(character) for character in query) % len(PET_CORPUS)
    rotated = PET_CORPUS[offset:] + PET_CORPUS[:offset]
    return [deepcopy(record) for record in rotated[:size]]


MOCK_SOURCES = (("Lens.org", _mock_provider),)


def _mock_enrich(patents: list[PatentRecord]) -> list[PatentRecord]:
    """Stand in for both providers' ``enrich_with_full_text``.

    Not optional: ``keyword_search_node`` drops every candidate that comes out
    of enrichment with an empty ``full_text``, and the real Google enrichment
    only touches records carrying a ``serpapi_id``. Without this the mock corpus
    would be discarded in full and every run would return an empty report.

    The body is padded to a few thousand characters so ``chunk_text`` produces
    more than one chunk and the chunked-cosine path in ``compare.py`` is
    genuinely exercised.
    """
    # The real thing is two HTTP calls per patent, 8-wide: 15-20s for a full set.
    _pause(0.12 * len(patents))
    for patent in patents:
        if not patent.full_text:
            body = f"{patent.title}. {patent.abstract} "
            patent.full_text = (
                f"DESCRIPTION. {body * 6}"
                f"CLAIMS. 1. Apparatus substantially as described, comprising the elements "
                f"recited above. 2. The apparatus of claim 1 wherein the housing is "
                f"user-replaceable. {body * 4}"
            )
    return patents


# --- the evidence layer's provider boundary (verified runs) -----------------
#
# `search/enrichment.py`'s `enrich_publication` is the ONE outbound call the
# evidence branch makes: EPO OPS, over HTTP, for full text. Standing in for it
# here is the same discipline as `_mock_provider` -- mock at the provider
# boundary, and let every rule above it run for real. Normalization, date
# screening, passage segmentation, locator construction and eligibility are all
# the production code paths.
#
# The text below is SYNTHETIC and says so. It is not patent text: it is written
# for this file, it names an invented publication, and the quotes the verifier
# double cites are substrings of it -- which is what makes §9 check 10 (every
# quotation is a verbatim substring of a supplied passage) pass honestly rather
# than by being switched off.

MOCK_CLAIMS_TEXT = (
    "1. A waste heat recovery system comprising a heat exchanger disposed within "
    "an exhaust duct, and a controller configured to vary a coolant flow rate "
    "through the heat exchanger. "
    "2. The system of claim 1, wherein a temperature sensor is positioned "
    "upstream of the heat exchanger and the controller varies the coolant flow "
    "rate in response to an output of the temperature sensor."
)

MOCK_DESCRIPTION_TEXT = (
    "[0031] In one embodiment, heat exchanger 120 is mounted inside exhaust duct "
    "110 and receives coolant from pump 130. Controller 140 adjusts the coolant "
    "flow rate according to a stored duty schedule. "
    "[0032] The synthetic example above exists only to exercise the passage "
    "builder and the locator types; it describes no real product."
)

# One publication in the mock corpus is deliberately un-enrichable, so a verified
# run always produces at least one EXCLUDED_NO_FULL_TEXT and the exclusion table,
# the "not evaluated" wording and the report's coverage note are all exercised by
# a real exclusion rather than by an empty list.
MOCK_NO_TEXT_PUBLICATION = "US0000002"


def _mock_enrich_publication(publication: str, **kwargs: object):
    """Stand in for `search/enrichment.py`'s EPO full-text call.

    Returns a `TEXT` result for every publication except one, which returns
    `NO_TEXT` -- the provider answering that it has none, which is a real answer
    about the document and becomes `EXCLUDED_NO_FULL_TEXT`. A provider *failure*
    is deliberately not simulated here: that fails the run, and this path exists
    to let a run succeed.
    """
    from evidence.normalize import normalize
    from models import (
        EnrichmentResult,
        EnrichmentStatus,
        ProviderClaim,
        SectionText,
        TextVersionSource,
    )

    # `normalize` returns a `PublicationNumber`, not a string: `.normalized` is
    # the epodoc form (country + number, no kind code) the provider is asked for.
    parsed = normalize(publication)
    normalized = parsed.normalized
    now = _now_iso()

    if normalized.upper().startswith(MOCK_NO_TEXT_PUBLICATION):
        return EnrichmentResult(
            requested_publication=publication,
            normalized_publication=normalized,
            served_publication=normalized,
            source_document_id=parsed.source_document_id,
            status=EnrichmentStatus.NO_TEXT,
            sections_available=("ABSTRACT",),
            sections_retrieved=(),
            language_attempted="en",
            retrieved_at=now,
            attempts=1,
        )

    claims = SectionText(
        section="CLAIMS",
        text=MOCK_CLAIMS_TEXT,
        text_sha256=_sha256(MOCK_CLAIMS_TEXT),
        language="en",
        claims=(
            ProviderClaim(
                number=1,
                text=MOCK_CLAIMS_TEXT.split("2. ")[0].strip(),
                char_start=0,
                char_end=len(MOCK_CLAIMS_TEXT.split("2. ")[0].strip()),
            ),
            ProviderClaim(
                number=2,
                text="2. " + MOCK_CLAIMS_TEXT.split("2. ")[1],
                char_start=MOCK_CLAIMS_TEXT.index("2. "),
                char_end=len(MOCK_CLAIMS_TEXT),
            ),
        ),
    )
    description = SectionText(
        section="DESCRIPTION",
        text=MOCK_DESCRIPTION_TEXT,
        text_sha256=_sha256(MOCK_DESCRIPTION_TEXT),
        language="en",
    )

    return EnrichmentResult(
        requested_publication=publication,
        normalized_publication=normalized,
        served_publication=normalized,
        source_document_id=parsed.source_document_id,
        status=EnrichmentStatus.TEXT,
        sections=(claims, description),
        sections_available=("CLAIMS", "DESCRIPTION"),
        sections_retrieved=("CLAIMS", "DESCRIPTION"),
        language_attempted="en",
        # A real date, so date screening is exercised rather than skipped. Well
        # before any plausible cutoff, so the run is not accidentally excluded.
        text_version_original="2019-03-14",
        text_version_normalized="2019-03-14",
        text_version_source=TextVersionSource.PROVIDER_PUBLICATION_DATE,
        retrieved_at=now,
        attempts=1,
    )


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


EMBED_DIM = 1024
"""Must match the real model's output width (bge-large-en-v1.5 is 1024).

Not a free choice: a Qdrant collection fixes its vector size on creation, so a
mock vector of the wrong width fails outright against a `patents` collection any
earlier real run already created. Indexing is currently disabled in the graph
(``graph/pipeline.py``), so nothing writes today - this is what stops that from
becoming a confusing failure the day it is switched back on. It was 1024 while
the project embedded with BAAI/bge-large-en-v1.5; a collection built then is
unusable now and has to be dropped, not migrated.
"""

_HASH_DIM = 64
"""The width the hashing trick actually works in, before tiling up to EMBED_DIM.

Hashing a short abstract straight into 1024 buckets leaves vectors so sparse
that almost every pair is orthogonal, and the whole report would come out at 0%
- technically honest for a fake embedder, but useless for looking at how scores
render. Tiling a dense 64-dim vector 40 times leaves cosine similarity exactly
unchanged (both dot product and both norms scale by 40) while producing the
width Qdrant expects, so the spread is realistic and the dimension is correct.
EMBED_DIM must stay an exact multiple of this.
"""

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "as", "at", "by",
    "for", "from", "is", "are", "be", "that", "this", "with", "without",
    "it", "its", "into", "which", "wherein", "comprising", "apparatus",
    "device", "system", "method", "assembly",
}
"""Dropped so similarity reflects subject matter rather than patent boilerplate.
Without this every record scores high against every other one, because patent
prose shares most of its vocabulary."""


def _mock_embed_texts(texts: list[str], batch_size: int | None = None) -> list[list[float]]:
    """Deterministic local stand-in for ``embeddings.embed_texts``.

    A hashing-trick bag of words: texts sharing content words get correlated
    vectors, texts sharing none get near-zero cosine. No network, no credits.

    ``QUERY_INSTRUCTION`` is stripped first for the same reason ``tests/fakes.py``
    strips it - bge was trained to read that prefix as a retrieval instruction
    rather than as content, and a bag-of-words double would otherwise score its
    five words as ordinary query terms and dilute the real ones.
    """
    vectors: list[list[float]] = []
    for text in texts:
        base = [0.0] * _HASH_DIM
        for word in re.findall(r"\w+", text.removeprefix(QUERY_INSTRUCTION).lower()):
            if word in _STOPWORDS or len(word) < 3:
                continue
            digest = int(hashlib.md5(word.encode()).hexdigest(), 16)
            base[digest % _HASH_DIM] += 1.0 if (digest // _HASH_DIM) % 2 == 0 else -1.0
        vectors.append(base * (EMBED_DIM // _HASH_DIM))
    return vectors


def _verification_requirement_ids(prompt: str) -> tuple[list[str], list[str]]:
    """Read the approved element and relationship IDs back out of a verifier prompt.

    The blocks are sliced rather than pattern-matched across the whole prompt
    because passage text sits in the same string. Canonicalization (spec section
    8) collapses every passage to a single line, so a passage cannot introduce a
    line that looks like a requirement - but slicing means the double does not
    have to rely on that.
    """
    elements = prompt.split("Required technical elements:\n", 1)[1].split("\n\nRequired relationships:", 1)[0]
    relationships = prompt.split("Required relationships:\n", 1)[1].split("\n</approved_feature>", 1)[0]
    element_ids = [line.split(":", 1)[0] for line in elements.splitlines() if line.strip()]
    relationship_ids = (
        [] if relationships.strip() == "NONE"
        else [line.split(":", 1)[0] for line in relationships.splitlines() if line.strip()]
    )
    return element_ids, relationship_ids


def _mock_verification(prompt: str) -> str:
    """A valid, honest feature-disclosure verification response.

    Every requirement comes back ``NOT_FOUND_IN_SUPPLIED_PASSAGES`` with an empty
    evidence array. That is not laziness: a double that fabricated a ``FOUND``
    would have to fabricate a verbatim quotation to go with it, and mock mode's
    whole claim is that it fakes *inputs* rather than manufacturing findings. This
    response passes all eighteen section 9 checks, which is what the double owes.
    """
    element_ids, relationship_ids = _verification_requirement_ids(prompt)

    def entry(key: str, requirement_id: str) -> dict:
        return {
            key: requirement_id,
            "status": "NOT_FOUND_IN_SUPPLIED_PASSAGES",
            "evidence": [],
            "combination_basis": [],
            "note": "No direct disclosure was identified in the supplied passages.",
        }

    return json.dumps({
        "feature_id": re.search(r"^Feature ID: (.+)$", prompt, re.MULTILINE).group(1),
        "feature_version": re.search(r"^Feature version: (.+)$", prompt, re.MULTILINE).group(1),
        "publication_number": re.search(r"^Publication number: (.+)$", prompt, re.MULTILINE).group(1),
        "verification_scope": "SUPPLIED_PASSAGES_ONLY",
        "outcome": "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES",
        "elements": [entry("element_id", value) for value in element_ids],
        "relationships": [entry("relationship_id", value) for value in relationship_ids],
        "missing_element_ids": element_ids,
        "missing_relationship_ids": relationship_ids,
        "ambiguities": [],
        "requests_for_more_context": [],
    })


def _mock_decomposition() -> str:
    """A **candidate** decomposition, and nothing more.

    Deliberately a plausible decomposition of the same fabricated invention the
    rest of this file describes, so the Workflow Gate A panel has something real
    to render -- and deliberately *unapproved*, because mock mode fakes inputs
    and an approval is not an input. Nothing here reaches `verify_features`
    without a human, or `tests/harness.py`, approving it first.
    """
    return json.dumps({
        # The technical summary the understanding stage shows as the system's
        # interpretation. Present here because `llm.propose_understanding` raises
        # on an empty one: a stage whose whole purpose is showing the inventor
        # what the machine understood cannot render "nothing".
        "summary": MOCK_TECHNICAL_SUMMARY,
        "features": [
            {
                "feature_id": "F1",
                "feature_version": "1",
                "feature_text": "searchable technical limitations",
                "elements": [
                    {"element_id": "E1", "text": "a rotating body with equal-sized compartments and spring-loaded bottom trap doors", "core": True, "order": 1},
                    {"element_id": "E2", "text": "a scheduled drive withdrawing a support bar to release one measured portion by gravity through a directional trough", "core": True, "order": 2},
                    {"element_id": "E3", "text": "a bowl load cell measuring consumed food against the amount dispensed", "core": True, "order": 3},
                    {"element_id": "E4", "text": "reduction of future portions after repeated leftovers and a low-appetite alert to an owner's phone", "core": True, "order": 4},
                ],
                "relationships": [],
            },
        ]
    })


def _mock_summary() -> str:
    return json.dumps({"summary": json.loads(_mock_decomposition())["summary"]})


def _mock_limitations() -> str:
    return json.dumps({"features": json.loads(_mock_decomposition())["features"]})


def _mock_strategy(prompt: str) -> str:
    """Stand in for `llm.propose_search_strategy`, keyed off the real prompt.

    **The element ids are read out of the prompt rather than hardcoded**, for the
    same reason the screening double reads its candidate count out of its own
    prompt: the reviewer edits the understanding, so the elements this stage is
    asked to cover are whatever they approved, and a fixed list would either miss
    a core element (failing an honest coverage check for the wrong reason) or
    claim coverage of ids the run does not have.

    Every element is covered, grouped by feature, so a mock run passes the
    coverage rule the way a real proposal would rather than by the rule being
    skipped. It is a stub at the network boundary, not a bypass of the stage.
    """
    grouped: dict[str, list[str]] = {}
    relational_ids: set[str] = set()
    for line in prompt.splitlines():
        match = re.match(r"^\s+((F[^.\s]+)\.[^:\s]+)(\s+\[core\])?:", line)
        if match:
            element_id = match.group(1)
            grouped.setdefault(match.group(2), []).append(element_id)
            element_text = line.split(":", 1)[1]
            if set(re.findall(r"[a-z0-9]+", element_text.casefold())) & _RELATION_MARKERS:
                relational_ids.add(element_id)

    query_groups: list[tuple[str, list[str]]] = []
    for feature_id, element_ids in sorted(grouped.items()):
        # The production validator requires most queries to combine two or
        # three requirements. If a relationship-bearing requirement exists,
        # place it in two independently worded windows as the real prompt asks;
        # then add windows for anything those two did not cover.
        if len(element_ids) == 1:
            query_groups.append((feature_id, element_ids))
        elif feature_relations := [item for item in element_ids if item in relational_ids]:
            anchor = feature_relations[0]
            context = [item for item in element_ids if item != anchor]
            first = [anchor, *context[:2]]
            second_context = context[2:4] or list(reversed(context[:2]))
            second = [anchor, *second_context[:2]]
            query_groups.extend(((feature_id, first), (feature_id, second)))
            covered = set(first) | set(second)
            for offset in range(0, len([x for x in context if x not in covered]), 2):
                remaining = [x for x in context if x not in covered]
                query_groups.append((feature_id, [anchor, *remaining[offset : offset + 2]]))
        else:
            for offset in range(0, len(element_ids), 2):
                window = element_ids[offset : offset + 3]
                if len(window) == 1:
                    window = element_ids[-2:]
                query_groups.append((feature_id, window))
            if len(query_groups) == 1:
                query_groups.append((feature_id, list(reversed(element_ids))))

    queries = []
    for index, (feature_id, element_ids) in enumerate(query_groups, start=1):
        queries.append({
            "query_id": f"Q{index}",
            "query_text": f"rotating pet feeder compartments bowl intake query {index} for {feature_id}",
            "rationale": (
                f"Retrieves prior art for the elements of {feature_id}. "
                "The query combines related dispensing and intake-control requirements."
            ),
            "covered_element_ids": element_ids,
        })
    if not queries:
        # No elements in the prompt at all. Returning an empty list is the honest
        # answer and `propose_search_strategy` refuses it loudly, which is what
        # should happen: a strategy over nothing is not a strategy.
        return json.dumps({"domain_anchor": "automatic dry-food pet feeder", "queries": []})
    for query in queries:
        query["query_text"] = "automatic dry-food pet feeder " + str(query["query_text"])
    return json.dumps({"domain_anchor": "automatic dry-food pet feeder", "queries": queries})


def _mock_llm_chat(prompt: str, **kwargs: object) -> str:
    """Stand in for the HTTP call inside ``llm._llm_chat``.

    Keyed off the real prompt text, so the real prompt-building code still runs
    and a prompt change that breaks these markers shows up here rather than
    passing silently. The three novelty-search branches are ordered as they are
    because ``validate_relevance``'s prompt also contains "expert patent examiner"
    -- the "screening" check has to come first.

    The feature-disclosure verifier is checked first and separately, because its
    marker lives in the *system* message rather than the user prompt (spec section
    17.5's split). Without this branch a verification call would fall through to
    the AssertionError below, or - worse, if the double were ever loosened - reach
    the outbound-HTTP guard.
    """
    from abstract_mock import response
    value = response(prompt)
    if value is not None:
        return value
    _pause(1.4)  # a real call to the HF Router is 3-12s

    if VERIFIER_MARKER in str(kwargs.get("system", "")):
        return _mock_verification(prompt)

    # There is one decomposition prompt now, so the ordering rule that used to
    # sit here is gone with the relationship prompt it disambiguated: the two
    # were the same component doing the same job and both opened with this
    # marker, so matching on it alone answered a relationship request with a
    # whole decomposition. Nothing builds that prompt any more.
    if SUMMARY_PROMPT_MARKER in prompt:
        return _mock_summary()

    if LIMITATIONS_PROMPT_MARKER in prompt:
        return _mock_limitations()

    if DECOMPOSITION_PROMPT_MARKER in prompt:
        return _mock_decomposition()

    if STRATEGY_PROMPT_MARKER in prompt:
        return _mock_strategy(prompt)

    if "PatentScout AI" in prompt:
        return json.dumps({
            "concepts": [
                "rotating dry-food compartments",
                "scheduled trap-door dispensing",
                "bowl load-cell intake measurement",
                "adaptive pet feeding and appetite alert",
            ],
            "keywords": ["rotating compartments", "spring-loaded trap door", "support bar", "bowl load cell"],
            "queries": [
                "automatic dry pet food dispenser rotating compartments trap doors",
                "scheduled measured pet food portion gravity directional trough",
                "spring loaded trap door withdrawing support bar pet feeder",
                "pet food bowl load cell consumed dispensed intake measurement",
                "automatic portion reduction repeated uneaten feedings phone alert",
            ],
        })

    if "screening" in prompt:
        count = int(re.search(r"screening (\d+) candidate", prompt).group(1))
        # Every candidate kept. Dropping some would exercise the validation
        # filter, but it also makes the report's match count vary run to run for
        # reasons that have nothing to do with the UI you are looking at.
        return json.dumps({
            "verdicts": [{"index": index, "relevant": True} for index in range(1, count + 1)],
        })

    if "expert patent examiner" in prompt:
        count = int(re.search(r"against (\d+) prior-art patents", prompt).group(1))
        limitation_ids = re.findall(r"^(L\d+):", prompt, flags=re.MULTILINE)
        if not limitation_ids:
            raise AssertionError("mock analysis prompt has no reviewed limitation ids")
        covered_ids = limitation_ids[: max(1, len(limitation_ids) // 2)]
        missing_ids = [item for item in limitation_ids if item not in covered_ids]
        # `key_overlap` is not optional: as of 3dad9f6b, analyze_all_matches
        # raises if any patent comes back without tags, so omitting them here
        # would fail every mock run at the analyze phase.
        overlap_tags = [
            ["rotating compartments", "trap-door release", "timed dispensing"],
            ["dry-food release", "directional trough", "measured portion"],
            ["bowl load cell", "food intake", "adaptive portion control"],
            ["feeding schedule", "low-appetite alert", "owner phone"],
        ]
        return json.dumps({
            "rationales": [
                {
                    "index": index,
                    "text": (
                        f"This publication shares part of the scheduled dry-food "
                        f"dispensing mechanism but does not disclose the complete "
                        f"combination of bowl intake measurement, adaptive portion "
                        f"reduction, and a low-appetite phone alert."
                    ),
                    "key_overlap": overlap_tags[(index - 1) % len(overlap_tags)],
                    "covered_limitation_ids": covered_ids,
                    "missing_limitation_ids": missing_ids,
                }
                for index in range(1, count + 1)
            ],
            "recommendations": [
                "Define how the bowl load cell separates food delivery from actual consumption.",
                "Set a multi-feeding threshold before reducing future portion sizes.",
                "Confirm trap-door release before recording a portion as dispensed.",
                "Calibrate delivered mass for different kibble densities.",
                "Include recent intake measurements in the low-appetite phone alert.",
            ],
        })

    raise AssertionError(
        f"run_mock.py has no branch for this prompt, so a real LLM call would have been "
        f"made: {prompt[:160]!r}"
    )


def _pet_result(
    description: str, *, result_count: int = QUICK_PET_RESULT_COUNT
) -> dict[str, object]:
    return {
        "type": "result",
        "data": {
            "title": description[:45] + "...",
            "date": "Today",
            "concepts": list(PET_REPORT["concepts"]),
            "patents": deepcopy(PET_REPORT["patents"][:result_count]),
            "query_count": PET_REPORT["query_count"],
            "recommendations": list(PET_REPORT["recommendations"]),
        },
    }


def _save_pet_result(conn: object, description: str, claims: str | None, data: dict) -> None:
    from reports import save_report

    save_report(
        conn,
        description=description,
        claims=claims,
        concepts=data["concepts"],
        query_count=data["query_count"],
        recommendations=data["recommendations"],
        matches=data["patents"],
        patents=[deepcopy(record) for record in PET_CORPUS],
    )


def main() -> None:
    global SLOWDOWN

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8001,
                        help="default 8001, which is where the frontend already looks")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--fast", action="store_true",
                        help="drop the artificial delays; a run finishes in ~2s and the "
                             "running screen is gone before you can read it")
    parser.add_argument("--pace", type=float, default=1.0,
                        help="scale the artificial delays (default 1.0, a ~8s run). "
                             "1.3 gives roughly 10s. Ignored when --fast is passed.")
    parser.add_argument(
        "--workflow",
        action="store_true",
        help="also serve the VERIFIED workflow: host the durable executor in "
             "this process and mock the evidence layer's provider boundary, so "
             "a run pauses at all four workflow gates with no credentials and "
             "no outbound HTTP. Without this flag the behaviour is exactly what "
             "it has always been -- the search workflow only.",
    )
    args = parser.parse_args()

    # --fast wins: it is the "I do not want to wait" switch, so a stale --pace in
    # a shell alias must not quietly reintroduce the wait it exists to remove.
    SLOWDOWN = 0.0 if args.fast else max(args.pace, 0.0)

    from config import settings
    from urllib.parse import urlsplit

    if (
        urlsplit(settings.database_url).hostname != "mock-postgres"
        or urlsplit(settings.qdrant_url).hostname != "mock-qdrant"
    ):
        raise SystemExit(
            "The presentation mock requires its isolated mock-postgres and "
            "mock-qdrant services. Start it with docker-compose.mock.yml."
        )

    if args.workflow:
        # ``config`` deliberately lets backend/.env override ordinary process
        # variables.  That is correct for the real services, but this explicit
        # mock mode must host the executor itself even when the developer's
        # compose configuration says ``worker``.  Modules imported above all
        # share this one frozen settings instance, so changing the two
        # process-topology fields here updates every consumer without weakening
        # config.py's .env precedence for production code.
        object.__setattr__(settings, "workflow_executor", "inprocess")
        object.__setattr__(settings, "workflow_graph_adapter", "graph.adapter:ADAPTER")


    import uvicorn

    import server
    import db
    from graph import pipeline as graph_pipeline

    original_build_result = graph_pipeline.build_result_event
    original_persist_run = graph_pipeline.persist_run

    def presentation_result(state: dict) -> dict:
        if _is_pet_example(state["description"]):
            return _pet_result(state["description"])
        return original_build_result(state)

    def presentation_persist(state: dict, result: dict) -> None:
        if _is_pet_example(state["description"]):
            with db.connect() as conn:
                _save_pet_result(conn, state["description"], state.get("claims"), result["data"])
            return
        original_persist_run(state, result)

    _install_network_guard()

    print("=" * 72)
    print(" MOCK MODE - no SerpAPI searches, no HuggingFace credits will be spent.")
    print(" Outbound HTTP to any non-local host is blocked, so a gap in the")
    print(" mocking fails loudly instead of quietly spending quota.")
    print(" Patent data is fabricated and every record is stamped:")
    print(f"   {MOCK_SOURCE}")
    print(" Postgres and Qdrant are REAL; mock runs are stored in your history.")
    if args.workflow:
        print("")
        print(" WORKFLOW MODE: Advanced Search is served too.")
        print("   * the durable executor runs in THIS process (inprocess mode)")
        print("   * the real graph, gates, lease, checkpointer and event log")
        print("   * a new run pauses TWICE for a person, shown as two stages:")
        print("       1/2 review the summary")
        print("       2/2 edit Invention Requirements (Claims)")
        print("   * search queries are then generated and executed automatically")
        print("   * no verifier, no evidence matrix, no numeric score")
    print("=" * 72)

    # Patched at each consuming module's import site, not at the origin, because
    # `from embeddings import embed_texts` binds a local name a patch on the
    # origin module would never reach. Same reasoning as tests/conftest.py.
    with ExitStack() as stack:
        stack.enter_context(patch("graph.pipeline.build_result_event", side_effect=presentation_result))
        stack.enter_context(patch("graph.pipeline.persist_run", side_effect=presentation_persist))

        if args.workflow:
            from graph import adapter as graph_adapter

            original_adapter = graph_adapter.ADAPTER

            def adapter_result(run: dict, state: dict) -> dict:
                if _is_pet_example(run["description"]):
                    return _pet_result(
                        run["description"], result_count=ADVANCED_PET_RESULT_COUNT
                    )["data"]
                return original_adapter.result_payload(run, state)

            def adapter_persist(conn: object, run: dict, state: dict, data: dict) -> None:
                if _is_pet_example(run["description"]):
                    _save_pet_result(conn, run["description"], run.get("claims"), data)
                    return
                original_adapter.persist_result(conn, run, state, data)

            presentation_adapter = replace(
                original_adapter,
                result_payload=adapter_result,
                persist_result=adapter_persist,
            )
            stack.enter_context(patch("graph.adapter.ADAPTER", presentation_adapter))

        for target, replacement in (
            ("llm._llm_chat", _mock_llm_chat),
            # `verification/verify.py` does `from llm import _llm_chat`, which
            # binds a LOCAL name at import; patching the origin never reaches it.
            # Without this the verifier called the real provider and the run
            # failed with `MissingCredential` -- loudly, which is right, but for
            # a boundary that was supposed to be mocked.
            ("verification.verify._llm_chat", _mock_llm_chat),
            ("compare.embed_texts", _mock_embed_texts),
            ("search.vector_store.embed_texts", _mock_embed_texts),
            ("graph.searching.enrich_with_full_text", _mock_enrich),
            ("graph.searching.enrich_epo_full_text", _mock_enrich),
        ):
            stack.enter_context(patch(target, side_effect=replacement))
        stack.enter_context(patch("search.search.SOURCES", MOCK_SOURCES))
        from search.epo_abstracts import Page
        stack.enter_context(patch("graph.abstract_search.search_page",
            side_effect=lambda query, start, deadline: Page(_mock_provider(query,50), len(_mock_provider(query,50)), len(_mock_provider(query,50)))))
        stack.enter_context(patch("graph.abstract_search.family_publications", return_value=[]))

        if args.workflow:
            # The evidence layer's own provider boundary. Patched only in
            # workflow mode so the search-only behaviour is byte-identical to
            # what it has always been.
            stack.enter_context(
                patch("search.enrichment.enrich_publication",
                      side_effect=_mock_enrich_publication)
            )
        uvicorn.run(server.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
