"""Deterministic stand-ins for the two stateful boundaries -- the network-backed
semantic layer (embeddings and Qdrant) and Postgres -- shared by every test
module so the suite keeps making zero real network calls and needs no running
database, even though ``compare.py``, semantic search and ``persist_run`` are
all unconditionally on the pipeline's happy path.

**Every Postgres double here is a dict, not a SQL engine, and both of them pin
the call contract and nothing about the SQL.**

``FakeReportStore`` stands in at the ``reports.py`` seam
(``save_report``/``list_history``), so it exercises the *call* contract those two
functions have with the pipeline and the history endpoint -- ordering, shape,
what gets stored on an empty run -- and nothing about the SQL itself. Schema
mistakes (a CHECK constraint, a foreign key, a column that does not exist) are
invisible to it and only show up against the real Compose Postgres.

``FakeWorkflowStore`` (bottom of this file) stands in at the
``workflow.store``/``workflow.lease``/``workflow.events`` seam and is the same
kind of thing, with higher stakes: it models the *authority* rules that are part
of the call contract -- fencing, `RUNNING`-only worker events, API ownership,
terminal-only-from-the-finalizers -- but it cannot exhibit a constraint, a
trigger, a cascade, a lock-ordering bug, a lost update, a lease race or a
deadlock, because it executes one call at a time in one process. Those are
exercised by the ``postgres``-marked tests against the real Compose database, and
a green run of a *skipped* suite is a skipped suite, not a passing one. Its own
section header lists exactly what it cannot see.
"""

import hashlib
import itertools
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from search.vector_store import QUERY_INSTRUCTION

EMBED_DIM = 32

_STOPWORDS = {
    "a", "an", "the", "and", "or", "if", "with", "by", "for", "is", "that",
    "this", "using", "system", "device", "method", "process", "of", "to",
    "in", "on", "as", "are", "be", "from", "at", "it", "its", "into",
}
"""Excluded so two texts sharing only function words still get an exact zero
dot product - what the zero-similarity test needs from this deterministic
double. A real hosted embedding model has no such guarantee (dense embeddings
rarely land on an exact zero for any two pieces of English text); this stub
optimizes for test determinism, not for simulating production model output."""


def fake_epo_enrich_with_full_text(patents: list) -> list:
    """Deterministic stand-in for ``search.epo.enrich_with_full_text``.

    Unlike Google's enrichment (naturally skipped in tests because synthetic
    ``PatentRecord``s never carry a ``serpapi_id``), EPO's real function keys
    off ``source`` + ``id`` alone - both of which test fixtures do set, so
    without this stub it would make real calls to EPO's auth and full-text
    endpoints during the test suite.
    """
    for patent in patents:
        if patent.source == "European Patent Office" and not patent.full_text:
            patent.full_text = f"{patent.title}. {patent.abstract}".strip()
    return patents


def fake_google_enrich_with_full_text(patents: list) -> list:
    """Deterministic stand-in for Google's ``enrich_with_full_text``.

    The real function is naturally a no-op in tests (it only touches records
    with a ``serpapi_id``, which synthetic ``PatentRecord`` fixtures never
    set) - but ``keyword_search_node`` now drops any candidate that comes out
    of enrichment with no full text at all, which would silently gut every
    Google-sourced test fixture that was never meant to model real
    ``serpapi_id`` gating. This stub sets full text the same way the EPO one
    does, so test corpora survive the drop like real enriched data would.
    """
    for patent in patents:
        if patent.source == "Google Patents" and not patent.full_text:
            patent.full_text = f"{patent.title}. {patent.abstract}".strip()
    return patents


def fake_embed_texts(texts: list[str]) -> list[list[float]]:
    """A deterministic hashing-trick embedding.

    Texts sharing content words get overlapping, correlated vectors; texts
    sharing none get an exact zero dot product (barring a hash collision).

    ``QUERY_INSTRUCTION`` is stripped first for the same reason ``_STOPWORDS``
    exists: it makes this double behave like the model it stands in for. bge was
    *trained* with that prefix on queries, so the real model reads it as a
    retrieval instruction rather than as content. A bag-of-words double has no
    such notion - it would score the prefix's five content words as ordinary
    query terms, diluting the real ones and shrinking every similarity by
    roughly the ratio of the vector norms. That artifact belongs to the stub,
    not to production, so the stub does not simulate it.
    """
    vectors: list[list[float]] = []
    for text in texts:
        vector = [0.0] * EMBED_DIM
        for word in re.findall(r"\w+", text.removeprefix(QUERY_INSTRUCTION).lower()):
            if word in _STOPWORDS:
                continue
            digest = int(hashlib.md5(word.encode()).hexdigest(), 16)
            sign = 1.0 if (digest // EMBED_DIM) % 2 == 0 else -1.0
            vector[digest % EMBED_DIM] += sign
        vectors.append(vector)
    return vectors


class _FakePoint:
    def __init__(self, payload: dict, score: float) -> None:
        self.payload = payload
        self.score = score


class _FakeRetrievedPoint:
    """What ``retrieve`` returns - an id and nothing else, since callers pass
    ``with_payload=False``/``with_vectors=False``."""

    def __init__(self, point_id: str) -> None:
        self.id = point_id


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCollectionsResult:
    def __init__(self, names: list[str]) -> None:
        self.collections = [_FakeCollection(name) for name in names]


class _FakeQueryResponse:
    def __init__(self, points: list[_FakePoint]) -> None:
        self.points = points


class FakeQdrantClient:
    """In-memory stand-in for ``qdrant_client.QdrantClient``.

    One instance persists for the lifetime of a test (see
    ``tests/conftest.py``), so calling store then search within the same test
    proves a repeated query retrieves what an earlier one indexed - the same
    property P3.3 requires of the real Qdrant collection.
    """

    def __init__(self) -> None:
        self._points: dict[str, tuple[list[float], dict]] = {}
        self._collection_created = False

    def get_collections(self) -> _FakeCollectionsResult:
        return _FakeCollectionsResult(["patents"] if self._collection_created else [])

    def create_collection(self, collection_name: str, vectors_config: object) -> None:
        self._collection_created = True

    def upsert(self, collection_name: str, points: list) -> None:
        for point in points:
            self._points[point.id] = (point.vector, point.payload)

    def retrieve(
        self,
        collection_name: str,
        ids: list[str],
        with_payload: bool = True,
        with_vectors: bool = True,
        **kwargs: object,
    ) -> list:
        """Mirrors ``QdrantClient.retrieve``: returns only the ids that exist,
        which is exactly what ``vector_store.known_ids`` reads it for."""
        return [_FakeRetrievedPoint(point_id) for point_id in ids if point_id in self._points]

    def query_points(
        self,
        collection_name: str,
        query: list[float],
        limit: int,
        with_payload: bool = True,
        score_threshold: float | None = None,
        **kwargs: object,
    ) -> _FakeQueryResponse:
        """Mirrors ``QdrantClient.query_points`` (the ``search()`` replacement
        as of qdrant-client 1.10+, which ``search/vector_store.py`` now calls),
        including the ``score_threshold`` filter it now passes."""

        def cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = sum(x * x for x in a) ** 0.5
            norm_b = sum(y * y for y in b) ** 0.5
            return 0.0 if norm_a == 0 or norm_b == 0 else dot / (norm_a * norm_b)

        scored = [
            _FakePoint(payload, cosine(query, vector))
            for vector, payload in self._points.values()
        ]
        if score_threshold is not None:
            scored = [point for point in scored if point.score >= score_threshold]
        scored.sort(key=lambda point: point.score, reverse=True)
        return _FakeQueryResponse(scored[:limit])


class FakeReportStore:
    """In-memory stand-in for ``backend/reports.py``.

    Records runs in call order and reads them back newest-first, matching
    ``list_history``'s ``ORDER BY created_at DESC``. Timestamps are synthetic
    and strictly increasing, so ordering is deterministic rather than dependent
    on how fast the test ran.

    ``patents`` is stored as a title/url lookup only. The real ``list_history``
    JOINs ``search_run_matches`` to ``patents`` for each match's title and url,
    and a match whose patent row is missing would silently vanish from that JOIN
    -- so the fake resolves both the same way instead of echoing back whatever
    was in ``matches``.
    """

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.patent_meta: dict[str, tuple[str, str]] = {}
        self._ids = itertools.count(1)
        self._clock = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def save_report(self, conn: object, **fields: object) -> str:
        matches: list[dict] = list(fields.pop("matches"))  # type: ignore[arg-type]
        patents: list = list(fields.pop("patents"))  # type: ignore[arg-type]
        for patent in patents:
            self.patent_meta[patent.id.strip().upper()] = (patent.title, patent.url)

        run_id = f"run-{next(self._ids)}"
        self._clock += timedelta(minutes=1)
        self.runs.append({"id": run_id, "created_at": self._clock, "matches": matches, **fields})
        return run_id

    def list_history(self, conn: object, limit: int = 50) -> list[dict]:
        entries = []
        for run in reversed(self.runs[-limit:] if limit else self.runs):
            entries.append({
                "id": run["id"],
                "description": run["description"],
                "created_at": run["created_at"].isoformat(),
                "concepts": run["concepts"],
                "query_count": run["query_count"],
                "recommendations": run["recommendations"],
                **({key: run['metadata'][key] for key in ('analysis_basis','analysis_version','coverage','elements')} if run.get('metadata') else {}),
                "patents": [
                    {
                        **match,
                        "key_overlap": list(match.get("key_overlap") or []),
                        "title": self.patent_meta[str(match["num"]).strip().upper()][0],
                        "url": self.patent_meta[str(match["num"]).strip().upper()][1],
                    }
                    for match in run["matches"]
                ],
            })
        return entries

    def clear_history(self, conn: object) -> int:
        """Mirrors the real one: drops every run and the patent rows no run
        references any more. Since this clears everything, that is all of them."""
        deleted = len(self.runs)
        self.runs.clear()
        self.patent_meta.clear()
        return deleted

    @contextmanager
    def connect(self):
        """Stands in for ``db.connect()``. Yields a sentinel: nothing in the
        fake touches the connection, and handing back something that is not a
        psycopg object makes an accidental real query fail loudly."""
        yield "fake-connection"


# ===========================================================================
# The durable-workflow double
# ===========================================================================
#
# READ THIS BEFORE TRUSTING IT.
#
# `FakeWorkflowStore` is a dict, not a SQL engine. It stands in at the
# `workflow.store` / `workflow.lease` / `workflow.events` call seam, so it pins
# the **call contract** those functions have with the executor and (later) the
# API -- which arguments each writer requires, which authority each one checks,
# what each returns, and which `WorkflowError` each refuses with.
#
# **It pins nothing about the SQL, and cannot.** Every one of these is invisible
# to it and must be exercised against the real Compose PostgreSQL, which is what
# the `postgres`-marked tests do:
#
#   * every CHECK constraint (`run_leases_held_consistently`,
#     `workflow_events_terminal_matches_type`, the cell/result state CHECKs);
#   * every foreign key and every ON DELETE CASCADE, including the one that is
#     the only way a lease row ever disappears;
#   * the partial unique indexes (`workflow_events_one_terminal_idx`,
#     `node_attempts_one_committed_execution_idx`);
#   * every trigger -- the run-status insert invariant and transition table,
#     fence-token monotonicity, terminal agreement, review_audit append-only;
#   * **the mandatory lock order and everything that depends on it** -- a
#     takeover racing a validate, a heartbeat racing a sweep, a lost update, a
#     deadlock. A double that executes one call at a time cannot exhibit a race,
#     so asserting a race against it would be asserting nothing.
#   * the `expires_at = now()` equality instant, which is a property of one SQL
#     predicate and of nothing in Python.
#
# CLAUDE.md already says this of `FakeReportStore`; it is equally true here and
# more consequential, because most of what Gate A exists to guarantee is in that
# list.


def _workflow_types():
    """Imported lazily so this module stays importable by the existing tests
    without the workflow package having been imported first."""
    from workflow import types

    return types


class FakeWorkflowStore:
    """In-memory stand-in for the durable-workflow surface.

    Models the *authority* rules, because those are the call contract: a fenced
    worker is refused, a worker event outside `RUNNING` is refused, the API
    writer accepts only `resumed` and only from the owner, terminals come only
    from the finalizers, and a replay with a different payload conflicts. It
    models **no** concurrency, constraint or cascade -- see the note above.
    """

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.leases: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.attempts: dict[str, list[dict]] = {}
        self._run_ids = itertools.count(1)

    # --- admission ------------------------------------------------------

    def create_run(self, conn: object, **fields: object) -> str:
        run_id = f"00000000-0000-4000-8000-{next(self._run_ids):012d}"
        self.runs[run_id] = {
            "run_id": run_id,
            "status": "QUEUED",
            "current_gate": None,
            "thread_id": run_id,
            "next_sequence": 0,
            "events_compacted_below": 0,
            "failure_code": None,
            "failure_detail": None,
            **fields,
        }
        # Created unheld at token 0, exactly as create_run() does -- and the fake
        # has no delete path either, because neither does the application.
        self.leases[run_id] = {
            "run_id": run_id,
            "owner_id": None,
            "fence_token": 0,
            "released_at": None,
            "expires_at": None,
        }
        self.events[run_id] = []
        self.attempts[run_id] = []
        self._append(
            run_id, "progress", "queued", {"type": "progress", "phase": "queued"}, "queued"
        )
        return run_id

    # --- reads ----------------------------------------------------------

    def get_run(self, conn: object, *, run_id: str, actor_email: str) -> dict:
        run = self.runs.get(run_id)
        if run is None or run.get("created_by") != actor_email:
            raise _workflow_types().RunNotFoundError(f"run {run_id} not found")
        return dict(run)

    def list_runs(self, conn: object, *, actor_email: str, limit: int = 50) -> list[dict]:
        owned = [
            dict(run)
            for run in reversed(list(self.runs.values()))
            if run.get("created_by") == actor_email
        ]
        return owned[:limit]

    def read_events(
        self, conn: object, *, run_id: str, after_sequence: int = 0, limit: int = 500
    ) -> list[dict]:
        rows = [e for e in self.events.get(run_id, []) if e["sequence"] > after_sequence]
        return rows[:limit]

    # --- the lease ------------------------------------------------------

    def acquire(self, conn: object, *, run_id: str, owner_id: str, ttl_seconds: float):
        types = _workflow_types()
        run = self.runs.get(run_id)
        if run is None:
            raise types.RunNotFoundError(f"run {run_id} not found")
        if run["status"] not in ("QUEUED", "RUNNING", "RESUMING"):
            return None
        held = self.leases[run_id]
        if held["owner_id"] is not None and held["released_at"] is None:
            return None
        held["fence_token"] += 1  # ALWAYS up. Never reset, never reused.
        held["owner_id"] = owner_id
        held["released_at"] = None
        return held["fence_token"]

    def heartbeat(
        self, conn: object, *, run_id: str, owner_id: str, fence_token: int, ttl_seconds: float
    ) -> bool:
        held = self.leases.get(run_id)
        if held is None or held["owner_id"] != owner_id:
            return False
        if held["fence_token"] != fence_token or held["released_at"] is not None:
            return False
        return True

    def release(self, conn: object, *, run_id: str, owner_id: str, fence_token: int) -> None:
        types = _workflow_types()
        held = self.leases.get(run_id)
        if (
            held is None
            or held["owner_id"] != owner_id
            or held["fence_token"] != fence_token
            or held["released_at"] is not None
        ):
            # A stale or repeated release raises. It is not a harmless no-op: a
            # worker releasing a lease it no longer holds has been operating on a
            # false belief about its own authority.
            raise types.FencedError(f"run {run_id} is not actively leased to {owner_id}")
        held["owner_id"] = None
        held["released_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # fence_token deliberately untouched: the released token stays burned.

    def _require_lease(self, run_id: str, owner_id: str, fence_token: int) -> None:
        types = _workflow_types()
        held = self.leases.get(run_id)
        if (
            held is None
            or held["owner_id"] != owner_id
            or held["fence_token"] != fence_token
            or held["released_at"] is not None
        ):
            raise types.FencedError(
                f"run {run_id} is not leased to {owner_id} at token {fence_token}"
            )

    # --- events ---------------------------------------------------------

    def _append(
        self, run_id: str, event_type: str, phase, payload: dict, dedupe_key: str
    ) -> int:
        types = _workflow_types()
        digest = hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()
        for existing in self.events[run_id]:
            if existing["dedupe_key"] == dedupe_key:
                if existing["payload_sha256"] != digest:
                    raise types.ReplayConflictError(
                        f"dedupe key {dedupe_key} replayed with a different payload"
                    )
                return existing["sequence"]
        if any(event["is_terminal"] for event in self.events[run_id]):
            raise types.TerminalExistsError(f"run {run_id} already has a terminal event")
        self.runs[run_id]["next_sequence"] += 1
        sequence = self.runs[run_id]["next_sequence"]
        self.events[run_id].append(
            {
                "sequence": sequence,
                "event_type": event_type,
                "phase": phase,
                "payload": payload,
                "payload_sha256": digest,
                "dedupe_key": dedupe_key,
                "is_terminal": event_type in ("result", "error"),
            }
        )
        return sequence

    def append_worker_event(
        self,
        conn: object,
        *,
        run_id: str,
        owner_id: str,
        fence_token: int,
        event_type,
        phase,
        payload: dict,
        dedupe_key: str,
    ) -> int:
        types = _workflow_types()
        event_type = str(event_type)
        if event_type in ("result", "error"):
            raise types.IllegalStateError(
                f"use finalize_run_success/failure for {event_type}"
            )
        if event_type == "resumed":
            raise types.IllegalStateError("resumed is an API event, not a worker event")
        self._require_lease(run_id, owner_id, fence_token)
        if self.runs[run_id]["status"] != "RUNNING":
            raise types.IllegalStateError(
                f"run {run_id} is {self.runs[run_id]['status']}, not RUNNING"
            )
        return self._append(run_id, event_type, phase, payload, dedupe_key)

    def append_api_event(
        self,
        conn: object,
        *,
        run_id: str,
        actor_email: str,
        expected_status,
        payload: dict,
        dedupe_key: str,
    ) -> int:
        types = _workflow_types()
        run = self.runs.get(run_id)
        if run is None or run.get("created_by") != actor_email:
            raise types.RunNotFoundError(f"run {run_id} not found for {actor_email}")
        if run["status"] != str(expected_status):
            raise types.IllegalStateError(
                f"run {run_id} is {run['status']} but the caller expected {expected_status}"
            )
        return self._append(run_id, "resumed", None, payload, dedupe_key)

    def pause_run_for_review(
        self,
        conn: object,
        *,
        run_id: str,
        owner_id: str,
        fence_token: int,
        gate,
        review_payload: dict,
        pause_payload: dict,
        checkpoint_id: str,
    ) -> int:
        types = _workflow_types()
        self._require_lease(run_id, owner_id, fence_token)
        if self.runs[run_id]["status"] != "RUNNING":
            raise types.IllegalStateError(f"run {run_id} is not RUNNING; it cannot pause")
        self._append(
            run_id,
            "review_required",
            None,
            review_payload,
            f"review_required:{gate}:{checkpoint_id}",
        )
        sequence = self._append(
            run_id, "paused", None, pause_payload, f"paused:{gate}:{checkpoint_id}"
        )
        self.runs[run_id]["status"] = "WAITING_FOR_REVIEW"
        self.runs[run_id]["current_gate"] = str(gate)
        self.release(conn, run_id=run_id, owner_id=owner_id, fence_token=fence_token)
        return sequence

    def _terminal_replay(
        self, run_id: str, event_type: str, payload: dict, dedupe_key: str, failure_code
    ):
        types = _workflow_types()
        for event in self.events[run_id]:
            if not event["is_terminal"]:
                continue
            digest = hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()
            if (
                event["dedupe_key"] != dedupe_key
                or event["event_type"] != event_type
                or event["payload_sha256"] != digest
                or self.runs[run_id]["failure_code"] != failure_code
            ):
                raise types.ReplayConflictError(
                    f"run {run_id} already terminated with a different terminal event"
                )
            return event["sequence"]
        return None

    def finalize_run_success(
        self,
        conn: object,
        *,
        run_id: str,
        owner_id: str,
        fence_token: int,
        payload: dict,
        dedupe_key: str = "terminal:result",
    ) -> int:
        types = _workflow_types()
        # The replay check comes FIRST, before the lease and the status, for the
        # same reason it does in SQL: by the time a retry arrives the run is
        # already COMPLETED and the lease is usually released.
        replay = self._terminal_replay(run_id, "result", payload, dedupe_key, None)
        if replay is not None:
            return replay
        self._require_lease(run_id, owner_id, fence_token)
        if self.runs[run_id]["status"] != "RUNNING":
            raise types.IllegalStateError("only a RUNNING run can succeed")
        sequence = self._append(run_id, "result", None, payload, dedupe_key)
        self.runs[run_id]["status"] = "COMPLETED"
        return sequence

    def finalize_run_failure(
        self,
        conn: object,
        *,
        run_id: str,
        owner_id: str,
        fence_token: int,
        payload: dict,
        failure_code: str,
        dedupe_key: str = "terminal:error",
    ) -> int:
        types = _workflow_types()
        replay = self._terminal_replay(run_id, "error", payload, dedupe_key, failure_code)
        if replay is not None:
            return replay
        self._require_lease(run_id, owner_id, fence_token)
        if self.runs[run_id]["status"] not in ("QUEUED", "RUNNING", "RESUMING"):
            raise types.IllegalStateError("this run cannot be failed by its executor")
        sequence = self._append(run_id, "error", None, payload, dedupe_key)
        self.runs[run_id]["status"] = "FAILED"
        self.runs[run_id]["failure_code"] = failure_code
        return sequence

    # --- transitions ----------------------------------------------------

    def transition_status(
        self,
        conn: object,
        *,
        run_id: str,
        owner_id: str,
        fence_token: int,
        expected_from,
        to_status,
        current_gate=None,
    ):
        types = _workflow_types()
        if str(to_status) in ("COMPLETED", "FAILED"):
            raise types.IllegalStateError(
                f"{to_status} is written only by the finalizers, atomically with the event"
            )
        self._require_lease(run_id, owner_id, fence_token)
        run = self.runs[run_id]
        if run["status"] not in [str(status) for status in expected_from]:
            raise types.IllegalStateError(
                f"run {run_id} is {run['status']}, not one of {expected_from}"
            )
        permitted = {(str(a), str(b)) for a, b in types.ALLOWED_TRANSITIONS}
        if (run["status"], str(to_status)) not in permitted:
            raise types.IllegalStateError(
                f"illegal run status transition {run['status']} -> {to_status}"
            )
        run["status"] = str(to_status)
        if current_gate is not None:
            run["current_gate"] = str(current_gate)
        return run["status"]

    # --- attempts -------------------------------------------------------

    def begin_attempt(
        self,
        conn: object,
        *,
        run_id: str,
        node: str,
        execution_key: str,
        fence_token: int,
        owner_id: str,
    ) -> str:
        # Fenced, like the real one: a worker that lost the lease may not add
        # telemetry either. The fake models the *authority rule*; the lock order
        # that makes it hold under concurrency is the database's, and only the
        # postgres-marked tests can see it.
        self._require_lease(run_id, owner_id, fence_token)
        attempt_id = f"attempt-{len(self.attempts[run_id]) + 1}"
        self.attempts[run_id].append(
            {
                "attempt_id": attempt_id,
                "node": node,
                "execution_key": execution_key,
                "fence_token": fence_token,
                "owner_id": owner_id,
                "outcome": "RUNNING",
            }
        )
        return attempt_id

    def commit_attempt(
        self,
        conn: object,
        *,
        run_id: str,
        owner_id: str,
        fence_token: int,
        attempt_id: str,
        provider_calls: int = 0,
        llm_calls: int = 0,
        detail: str = "",
    ) -> None:
        types = _workflow_types()
        self._require_lease(run_id, owner_id, fence_token)
        for attempt in self.attempts[run_id]:
            if attempt["attempt_id"] != attempt_id:
                continue
            if attempt["outcome"] != "RUNNING":
                # A FENCED or FAILED attempt can never become COMMITTED.
                raise types.FencedError(f"attempt {attempt_id} cannot be committed")
            already = [
                other
                for other in self.attempts[run_id]
                if other["outcome"] == "COMMITTED"
                and other["node"] == attempt["node"]
                and other["execution_key"] == attempt["execution_key"]
            ]
            if already:
                # The fake's stand-in for node_attempts_one_committed_execution_idx.
                # The real guarantee is the partial unique index, which is the only
                # thing that holds under concurrency.
                raise types.FencedError(
                    f"execution {attempt['execution_key']} already has a committed attempt"
                )
            attempt["outcome"] = "COMMITTED"
            attempt["provider_calls"] = provider_calls
            attempt["llm_calls"] = llm_calls
            attempt["detail"] = detail
            return
        raise types.FencedError(f"attempt {attempt_id} not found for run {run_id}")

    def fail_attempt(
        self,
        conn: object,
        *,
        run_id: str,
        owner_id: str,
        fence_token: int,
        attempt_id: str,
        detail: str = "",
    ) -> None:
        types = _workflow_types()
        self._require_lease(run_id, owner_id, fence_token)
        for attempt in self.attempts[run_id]:
            if (
                attempt["attempt_id"] == attempt_id
                and attempt["owner_id"] == owner_id
                and attempt["fence_token"] == fence_token
                and attempt["outcome"] == "RUNNING"
            ):
                attempt["outcome"] = "FAILED"
                attempt["detail"] = detail
                return
        # A stale worker leaves its attempt RUNNING for reconciliation to close as
        # FENCED. FAILED and FENCED are different facts and a worker that lost the
        # run does not get to choose between them.
        raise types.FencedError(f"attempt {attempt_id} cannot be failed by {owner_id}")

    def close_superseded_attempts(
        self, conn: object, *, run_id: str, owner_id: str, fence_token: int
    ) -> int:
        self._require_lease(run_id, owner_id, fence_token)
        closed = 0
        for attempt in self.attempts[run_id]:
            if attempt["outcome"] == "RUNNING" and attempt["fence_token"] < fence_token:
                attempt["outcome"] = "FENCED"
                attempt["detail"] = "superseded by a later lease"
                closed += 1
        return closed

    def list_attempts(self, conn: object, *, run_id: str) -> list[dict]:
        return [dict(attempt) for attempt in self.attempts.get(run_id, [])]
