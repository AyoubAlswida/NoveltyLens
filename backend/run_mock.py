"""Run the real backend with every *metered* boundary replaced by a local double.

    cd backend && python run_mock.py          # serves on 127.0.0.1:8001
    cd backend && python run_mock.py --port 8002

Use this to look at the UI, click through a full run, and exercise login,
History and the error path without spending a single SerpAPI search or
HuggingFace credit. A real novelty search costs ~25 of a 250/month SerpAPI
allowance; this costs nothing.

**What is faked** - and it is exactly the three things that cost money:

    llm._llm_chat                       the LLM calls (understand/validate/analyze)
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

**Mock runs are written to the real database**, because History working for real
is half of what you came here to look at. Every mock patent carries
``MOCK DATA - no live search`` in the source field, which the report screen
prints next to each publication number, so a stored mock run is identifiable on
sight. To clear them out later:

    DELETE FROM search_runs WHERE id IN (
        SELECT run_id FROM search_run_matches WHERE source LIKE 'MOCK DATA%');

Postgres and Qdrant still need to be up (`docker compose up -d --wait`); both are
local and free. Sign in with the seeded account from
``migrations/0004.create-users.sql``.
"""

import argparse
import hashlib
import json
import re
import time
from copy import deepcopy
from unittest.mock import patch

from models import PatentRecord
from search.vector_store import QUERY_INSTRUCTION

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

SLOWDOWN = 1.0
"""Scales the artificial delays. Set to 0 by ``--fast``; see :func:`_pause`."""


def _pause(seconds: float) -> None:
    """Sleep, so the SSE progress checklist advances at a watchable pace.

    Without this the whole run finishes in about two seconds and the running
    screen is gone before you can read it -- which makes the one screen whose
    entire job is showing progress impossible to evaluate. A real run takes
    ~62s; these delays land it around 8s, close enough to judge the pacing and
    far enough from tedious.
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


MOCK_SOURCE = "MOCK DATA - no live search"
"""Stamped on every fabricated record and rendered on every report row.

The one thing this file must never do is produce output indistinguishable from
a real novelty search. The report screen prints ``source`` beside each
publication number, so this string is visible on every match, in every export,
and in every History entry a mock run leaves behind.
"""

# Written to read like real Google Patents results - long enough abstracts that
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
    offset = sum(ord(character) for character in query) % len(MOCK_CORPUS)
    rotated = MOCK_CORPUS[offset:] + MOCK_CORPUS[:offset]
    return [deepcopy(record) for record in rotated[:size]]


MOCK_SOURCES = (("Google Patents", _mock_provider),)


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


EMBED_DIM = 1024
"""Must match the real model's output width (BAAI/bge-large-en-v1.5 is 1024).

Not a free choice: a Qdrant collection fixes its vector size on creation, so a
narrower mock vector fails outright against a `patents` collection any earlier
real run already created. Indexing is currently disabled in the graph
(``graph/pipeline.py``), so nothing writes today - this is what stops that from
becoming a confusing failure the day it is switched back on.
"""

_HASH_DIM = 64
"""The width the hashing trick actually works in, before tiling up to EMBED_DIM.

Hashing a short abstract straight into 1024 buckets leaves vectors so sparse
that almost every pair is orthogonal, and the whole report would come out at 0%
- technically honest for a fake embedder, but useless for looking at how scores
render. Tiling a dense 64-dim vector 16 times leaves cosine similarity exactly
unchanged (both dot product and both norms scale by 16) while producing the
width Qdrant expects, so the spread is realistic and the dimension is correct.
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


def _mock_llm_chat(prompt: str, **kwargs: object) -> str:
    """Stand in for the HTTP call inside ``llm._llm_chat``.

    Keyed off the real prompt text, so the real prompt-building code still runs
    and a prompt change that breaks these markers shows up here rather than
    passing silently. The three branches are ordered as they are because
    ``validate_relevance``'s prompt also contains "expert patent examiner" --
    the "screening" check has to come first.
    """
    _pause(1.4)  # a real call to the HF Router is 3-12s

    if "PatentScout AI" in prompt:
        return json.dumps({
            "concepts": [
                "ultraviolet water sterilisation",
                "graphene-oxide membrane filtration",
                "flow-triggered cleaning cycle",
                "portable potable water treatment",
            ],
            "keywords": ["UV-C emitter", "graphene oxide", "flow rate sensor", "self-cleaning cap"],
            "queries": [
                "portable uv-c water sterilisation bottle",
                "graphene oxide filtration membrane replaceable cartridge",
                "flow rate sensor triggered cleaning cycle container",
                "photocatalytic self cleaning potable water conduit",
                "handheld ultraviolet steriliser power management",
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
        # `key_overlap` is not optional: as of 3dad9f6b, analyze_all_matches
        # raises if any patent comes back without tags, so omitting them here
        # would fail every mock run at the analyze phase.
        overlap_tags = [
            ["ultraviolet treatment", "in-cap emitter", "timed cycle"],
            ["membrane filtration", "replaceable cartridge", "particulate retention"],
            ["flow sensing", "automatic triggering", "cycle scheduling"],
            ["photocatalytic surface", "biofilm control", "potable water"],
        ]
        return json.dumps({
            "rationales": [
                {
                    "index": index,
                    "text": (
                        f"This record overlaps the described invention on the treatment "
                        f"mechanism and on the placement of the active element within the "
                        f"closure, but does not disclose the combination of in-line "
                        f"filtration with automatic cycle triggering. (Mock rationale "
                        f"{index} - no model was called.)"
                    ),
                    "key_overlap": overlap_tags[(index - 1) % len(overlap_tags)],
                }
                for index in range(1, count + 1)
            ],
            "recommendations": [
                "Recite the flow-rate threshold that triggers the sterilisation cycle as a "
                "numeric range rather than as a functional result. (Mock recommendation.)",
                "Claim the interlayer spacing of the graphene-oxide laminae, which the "
                "closest reference leaves unspecified. (Mock recommendation.)",
                "Add a dependent claim to the reserve-charge behaviour that guarantees an "
                "initiated cycle completes. (Mock recommendation.)",
                "Distinguish the membrane cartridge coupling from bayonet couplings already "
                "disclosed for point-of-use systems. (Mock recommendation.)",
                "Specify that sterilisation and filtration act on the same flow path in a "
                "single pass. (Mock recommendation.)",
            ],
        })

    raise AssertionError(
        f"run_mock.py has no branch for this prompt, so a real LLM call would have been "
        f"made: {prompt[:160]!r}"
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
    args = parser.parse_args()

    if args.fast:
        SLOWDOWN = 0.0

    import uvicorn

    import server

    _install_network_guard()

    print("=" * 72)
    print(" MOCK MODE - no SerpAPI searches, no HuggingFace credits will be spent.")
    print(" Outbound HTTP to any non-local host is blocked, so a gap in the")
    print(" mocking fails loudly instead of quietly spending quota.")
    print(" Patent data is fabricated and every record is stamped:")
    print(f"   {MOCK_SOURCE}")
    print(" Postgres and Qdrant are REAL; mock runs are stored in your history.")
    print("=" * 72)

    # Patched at each consuming module's import site, not at the origin, because
    # `from embeddings import embed_texts` binds a local name a patch on the
    # origin module would never reach. Same reasoning as tests/conftest.py.
    with patch("llm._llm_chat", side_effect=_mock_llm_chat), \
         patch("search.search.SOURCES", MOCK_SOURCES), \
         patch("compare.embed_texts", side_effect=_mock_embed_texts), \
         patch("search.vector_store.embed_texts", side_effect=_mock_embed_texts), \
         patch("graph.searching.enrich_with_full_text", side_effect=_mock_enrich), \
         patch("graph.searching.enrich_epo_full_text", side_effect=_mock_enrich):
        uvicorn.run(server.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
