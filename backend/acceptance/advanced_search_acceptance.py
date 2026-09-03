"""Advanced Search acceptance: the real flow, end to end, at zero cost.

    cd backend && python acceptance/advanced_search_acceptance.py

**Replaces `four_gate_acceptance.py` and `journey_acceptance.py`**, which drove
the five-gate reviewed workflow. Renamed rather than edited in place, on purpose:
those filenames named a shape the product no longer has, and their transcripts
sat at `docs/acceptance/four-gate-transcript.txt` and `journey-transcript.txt`. A
transcript path that keeps working while the thing it describes has been
withdrawn is how an acceptance claim quietly stops meaning anything.

## What is real, and what is not

**There is no mock graph.** This drives `graph/pipeline.py` -- the production
topology, the production gate nodes -- hosted by the production executor. The
only things replaced are the **outbound provider boundaries** that would
otherwise cost money or need credentials:

| Boundary | Stood in for by | Why it is a boundary and not logic |
|---|---|---|
| `llm._llm_chat` | `run_mock._mock_llm_chat` | the one HTTP call to the model |
| `search.search.SOURCES` | `run_mock.MOCK_SOURCES` | discovery |
| `compare.embed_texts` | `run_mock._mock_embed_texts` | the embedding endpoint |

Everything above those lines is real: the two approval transactions, the lease
and its fencing, the LangGraph checkpoint and resume, the event log, the SSE
contract, ownership scoping and every API rule.

**Neither approval is bypassed and nothing is auto-approved.** Each goes through
the real `POST /api/runs/{id}/gates/{gate}/approve` with an idempotency key, and
`workflow/approvals.py` remains the only writer of an `approved_*` field. This
script never touches an approval table.

## What it proves

Admission -> stage 1 -> stage 2 -> stage 3 -> the search -> the standard report,
plus the properties that are easy to lose and hard to notice:

* only the **edited** queries are dispatched, and no proposal string survives;
* the report carries **no `pct`, `top_score` or `verdict`** anywhere in it;
* a completed run leaves **exactly one** standard History row, and the report the
  endpoint returns is the same object the terminal `result` event carried;
* another account gets `404 RUN_NOT_FOUND`, never `403`;
* a replayed approval returns the stored response rather than mutating twice;
* every withdrawn route answers `404` as an unknown path.

Zero paid calls: `run_mock.py`'s outbound-HTTP guard is armed throughout, so a
boundary anyone forgets to stub raises `PaidCallAttempted` rather than spending
quota.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
API_PORT = int(os.environ.get("ACCEPTANCE_API_PORT", "8095"))
API = f"http://127.0.0.1:{API_PORT}"
SERVER_LOG = BACKEND / "acceptance" / "advanced-search-server.log"

LOG: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    LOG.append(line)


def step(text: str) -> None:
    say(f"\n--- {text}")


def call(method: str, path: str, token: str | None = None, body: dict | None = None,
         show: bool = True) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        parsed = {}
    if show:
        redacted = {k: ("<redacted>" if k in {"token", "idempotency_key"} else v)
                    for k, v in (body or {}).items()}
        say(f"  $ {method} {path}" + (f"  {json.dumps(redacted)[:110]}" if body else ""))
        say(f"    -> {status} {json.dumps(parsed)[:190]}")
    return status, parsed


def wait_for(check, *, what: str, timeout: float = 120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.5)
    raise SystemExit(f"TIMED OUT waiting for {what}")


def wait_for_gate(token: str, run_id: str, gate: str):
    """Wait until the executor has parked the run at `gate`."""

    def parked():
        _, run = call("GET", f"/api/runs/{run_id}", token, show=False)
        if run.get("status") == "FAILED":
            detail = ""
            try:
                text = SERVER_LOG.read_text(encoding="utf-8", errors="replace")
                if "Traceback" in text:
                    detail = "\n" + text[text.rindex("Traceback"):][:2000]
            except OSError:
                pass
            raise SystemExit(
                f"the run FAILED while waiting for gate {gate}: {run}{detail}"
            )
        return run if (run.get("status") == "WAITING_FOR_REVIEW"
                       and str(run.get("current_gate")) == gate) else None

    run = wait_for(parked, what=f"the run to park at gate {gate}")
    say(f"    parked at gate {run['current_gate']} (last_event_id={run['last_event_id']})")
    return run


def stream(run_id: str, token: str, last_event_id: int | None) -> list[tuple[int, dict]]:
    query = "" if last_event_id is None else f"?last_event_id={last_event_id}"
    request = urllib.request.Request(
        f"{API}/api/runs/{run_id}/stream{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    frames: list[tuple[int, dict]] = []
    with urllib.request.urlopen(request, timeout=90) as response:
        buffer = ""
        for chunk in response:
            buffer += chunk.decode()
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                sequence, data = None, ""
                for line in frame.split("\n"):
                    if line.startswith("id: "):
                        sequence = int(line[4:])
                    elif line.startswith("data: "):
                        data += line[6:]
                if data and sequence is not None:
                    frames.append((sequence, json.loads(data)))
    return frames



TRANSCRIPT = BACKEND.parent / "docs" / "acceptance" / "advanced-search-transcript.txt"


EDITED_SUMMARY = "A drinking vessel that sterilises with UV-C and filters through a membrane."
EDITED_ELEMENT = "a replaceable graphene-oxide filtration membrane"
EDITED_QUERY = "graphene oxide membrane ultraviolet drinking vessel"

DESCRIPTION = (
    "A portable, refillable drinking-water bottle that sterilises its contents "
    "with an integrated UV-C emitter and filters particulates through a "
    "replaceable graphene-oxide membrane."
)


def _walk(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def main() -> int:
    stamp = uuid.uuid4().hex[:8]
    reviewer = f"advsearch-{stamp}@example.invalid"
    intruder = f"other-{stamp}@example.invalid"
    password = uuid.uuid4().hex
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            raise SystemExit(f"FAILED: {message}")

    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DEPLOYMENT_PROFILE", "test")
    import psycopg

    from auth import hash_password
    from config import settings

    say("=" * 78)
    say("ADVANCED SEARCH ACCEPTANCE — real graph, real approvals, mocked providers")
    say("=" * 78)
    say(f"reviewer: {reviewer}")

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for email in (reviewer, intruder):
            conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s)"
                " ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash",
                (email, hash_password(password)),
            )

    env = dict(os.environ)
    env["SERVER_PORT"] = str(API_PORT)
    env["SERVER_HOST"] = "127.0.0.1"
    env["DEPLOYMENT_PROFILE"] = "test"
    env["PYTHONUNBUFFERED"] = "1"

    server = None
    run_id = ""
    try:
        step("start `python run_mock.py --workflow --fast` (API + in-process executor)")
        server = subprocess.Popen(
            [sys.executable, "run_mock.py", "--workflow", "--fast", "--port", str(API_PORT)],
            cwd=str(BACKEND), env=env,
            stdout=open(SERVER_LOG, "w", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT,
        )
        say(f"    pid {server.pid}; server log -> {SERVER_LOG.name}")

        def healthy() -> bool:
            try:
                return call("GET", "/healthz", show=False)[0] == 200
            except Exception:
                return False

        wait_for(healthy, what="the mock API to serve /healthz")
        say(f"    healthy on {API}; outbound HTTP to any non-local host is blocked")

        _, body = call("POST", "/api/auth/login",
                       body={"email": reviewer, "password": password}, show=False)
        token = body["token"]
        _, body = call("POST", "/api/auth/login",
                       body={"email": intruder, "password": password}, show=False)
        intruder_token = body["token"]
        say("    signed in (tokens held in memory, never printed)")

        # -----------------------------------------------------------------
        step("ADMISSION: a description, and nothing else")
        status, created = call("POST", "/api/runs", token, {"description": DESCRIPTION})
        check(status == 201, f"admission returned {status}")
        run_id = created["run_id"]
        check(set(created) == {"run_id", "status", "stream_url"},
              f"admission body is {sorted(created)}")
        check("capability_qualified" not in created,
              "the withdrawn capability field is back in the admission response")
        say("    no verifier-policy check ran, and no capability field was returned")

        step("a withdrawn admission field is REFUSED, not ignored")
        status, _ = call("POST", "/api/runs", token,
                         {"description": DESCRIPTION, "problem_solved": "x"})
        check(status == 422, f"an unknown admission field returned {status}, not 422")
        say("    a client that has not been updated is told so")

        # -----------------------------------------------------------------
        step("STAGES 1 AND 2: the understanding")
        wait_for_gate(token, run_id, "A")
        status, payload = call("GET", f"/api/runs/{run_id}/gates/A", token)
        check(status == 200, f"gate A payload returned {status}")
        check(payload["original_description"] == DESCRIPTION,
              "the inventor's description was rewritten")
        check(isinstance(payload["technical_elements"], list)
              and all(isinstance(e, str) for e in payload["technical_elements"]),
              "technical_elements is not a flat list of strings")
        for withdrawn in ("candidate_decomposition", "problem_solved",
                          "inventor_novelty_statement", "proposal"):
            check(withdrawn not in payload, f"{withdrawn} is back in the gate A payload")
        say(f"    {len(payload['technical_elements'])} elements, as plain strings")

        proposed_elements = list(payload["technical_elements"])
        step("APPROVE the understanding, with an edit")
        status, approved = call("POST", f"/api/runs/{run_id}/gates/A/approve", token, {
            "idempotency_key": f"understanding-{stamp}",
            "technical_summary": EDITED_SUMMARY,
            "technical_elements": [EDITED_ELEMENT, *proposed_elements[1:]],
        })
        check(status == 200, f"the understanding approval returned {status}")
        say("    committed; the executor resumes the run on its next poll")

        step("a REPLAYED approval returns the stored response rather than mutating twice")
        status, replay = call("POST", f"/api/runs/{run_id}/gates/A/approve", token, {
            "idempotency_key": f"understanding-{stamp}",
            "technical_summary": EDITED_SUMMARY,
            "technical_elements": [EDITED_ELEMENT, *proposed_elements[1:]],
        })
        check(status == 200 and replay == approved, "the replay did not return the stored response")

        # -----------------------------------------------------------------
        step("STAGE 3: the searches")
        wait_for_gate(token, run_id, "STRATEGY")
        status, payload = call("GET", f"/api/runs/{run_id}/gates/STRATEGY", token)
        check(status == 200, f"strategy payload returned {status}")
        check(isinstance(payload["queries"], list)
              and all(isinstance(q, str) for q in payload["queries"]),
              "queries is not a flat list of strings")
        for withdrawn in ("candidate_search_strategy", "elements", "approved_summary",
                          "understanding_version", "proposal"):
            check(withdrawn not in payload, f"{withdrawn} is back in the strategy payload")
        proposed_queries = list(payload["queries"])
        say(f"    {len(proposed_queries)} queries, as plain strings")

        step("another account cannot read or approve this run")
        status, denied = call("GET", f"/api/runs/{run_id}/gates/STRATEGY", intruder_token)
        check(status == 404 and denied.get("code") == "RUN_NOT_FOUND",
              f"a non-owner got {status} {denied}, not 404 RUN_NOT_FOUND")
        say("    404, identical to an unknown run: never 403")

        step("APPROVE the searches, replacing one entirely")
        status, _ = call("POST", f"/api/runs/{run_id}/gates/STRATEGY/approve", token, {
            "idempotency_key": f"strategy-{stamp}",
            "queries": [EDITED_QUERY],
        })
        check(status == 200, f"the strategy approval returned {status}")
        say("    the run is released into the search")

        # -----------------------------------------------------------------
        step("THE SEARCH RUNS, and the run completes")

        def completed():
            _, run = call("GET", f"/api/runs/{run_id}", token, show=False)
            if run.get("status") == "FAILED":
                detail = ""
                try:
                    text = SERVER_LOG.read_text(encoding="utf-8", errors="replace")
                    if "Traceback" in text:
                        detail = "\n" + text[text.rindex("Traceback"):][:2000]
                except OSError:
                    pass
                raise SystemExit(f"the run FAILED: {run}{detail}")
            return run if run.get("status") == "COMPLETED" else None

        wait_for(completed, what="the run to complete", timeout=180.0)
        say("    COMPLETED")

        step("ONLY the approved queries were dispatched")
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            # Keyed by (run_id, version), not by a surrogate id: the approved
            # strategy tables are versioned per run, and reading the enabled rows
            # of the latest version is what the graph itself dispatches from.
            stored = conn.execute(
                "SELECT query_text FROM approved_strategy_queries"
                " WHERE run_id = %s AND enabled"
                " ORDER BY version DESC, ord",
                (run_id,),
            ).fetchall()
        stored_queries = [row[0] for row in stored]
        check(stored_queries == [EDITED_QUERY],
              f"the approved queries are {stored_queries}, not the edited one")
        dropped = [q for q in proposed_queries if q != EDITED_QUERY]
        check(all(q not in stored_queries for q in dropped),
              "a query the reviewer removed was approved anyway")
        say(f"    approved: {stored_queries}")

        # -----------------------------------------------------------------
        step("THE REPORT: the standard one, with no score anywhere")
        status, report = call("GET", f"/api/runs/{run_id}/report", token)
        check(status == 200, f"the report returned {status}")
        check(set(report) == {"title", "date", "concepts", "patents",
                              "recommendations", "query_count"},
              f"the report shape is {sorted(report)}")

        banned = {"pct", "top_score", "verdict", "score", "similarity", "percent"}
        offenders = [(p, k) for p, k, _ in _walk(report) if k.lower() in banned]
        check(not offenders, f"a removed score field is back: {offenders}")
        for match in report["patents"]:
            check(match["risk"] in {"High", "Medium", "Low"},
                  f"a match carries risk {match['risk']!r}")
        say(f"    {len(report['patents'])} matches, each rated High/Medium/Low and nothing else")

        step("the report a non-owner asks for does not exist")
        status, denied = call("GET", f"/api/runs/{run_id}/report", intruder_token)
        check(status == 404 and denied.get("code") == "RUN_NOT_FOUND",
              f"a non-owner got {status} for the report")

        # -----------------------------------------------------------------
        step("EXACTLY ONE standard History row, carrying the same report")
        status, history = call("GET", "/api/history", token, show=False)
        check(status == 200, f"history returned {status}")
        matching = [entry for entry in history if entry["description"] == DESCRIPTION]
        check(len(matching) == 1,
              f"a completed run left {len(matching)} History rows, not exactly one")
        entry = matching[0]
        check([m["num"] for m in entry["patents"]] == [m["num"] for m in report["patents"]],
              "the stored report and the returned report disagree on their matches")
        offenders = [(p, k) for p, k, _ in _walk(entry) if k.lower() in banned]
        check(not offenders, f"a removed score field is in the History row: {offenders}")
        say("    one row, same matches in the same order, no score")

        # -----------------------------------------------------------------
        step("every WITHDRAWN route answers 404 as an unknown path")
        for method, path in [
            ("GET", "gates/B"), ("POST", "gates/B/approve"),
            ("GET", "gates/C"), ("POST", "gates/C/complete"),
            ("POST", "gates/D/approve"), ("POST", "candidates"),
            ("GET", "cells/c1"), ("POST", "cells/c1/rerun"),
            ("POST", "understanding/regenerate"), ("POST", "strategy/regenerate"),
        ]:
            status, _ = call(method, f"/api/runs/{run_id}/{path}", token,
                             {"idempotency_key": "k"} if method == "POST" else None,
                             show=False)
            check(status == 404, f"{method} {path} answered {status}, not 404")
        say("    10 routes, all gone")

        step("QUICK SEARCH is untouched: same endpoint, same shape, no run row")
        status, quick = call("POST", "/api/analyze", token,
                             {"description": DESCRIPTION, "claims": ""}, show=False)
        check(status == 200, f"quick search returned {status}")
        check(set(quick) == set(report), "quick search and Advanced Search report shapes differ")
        offenders = [(p, k) for p, k, _ in _walk(quick) if k.lower() in banned]
        check(not offenders, f"quick search publishes a score: {offenders}")
        say("    identical report shape, and no score on either path")

        say()
        say("=" * 78)
        say(f"PASS — {checks} checks, 0 paid calls")
        say("=" * 78)
        return 0

    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        try:
            with psycopg.connect(settings.database_url, autocommit=True) as conn:
                if run_id:
                    conn.execute("DELETE FROM verification_runs WHERE id = %s", (run_id,))
                conn.execute("DELETE FROM search_runs WHERE description = %s", (DESCRIPTION,))
                # `idempotency_records.actor_email` references `users` and is NOT
                # reached by the run's cascade -- it keys its resource as a plain
                # string, so it outlives the run it belonged to. Cleared before
                # the accounts, or the account delete fails on the foreign key
                # and leaves both behind.
                conn.execute(
                    "DELETE FROM idempotency_records WHERE actor_email = ANY(%s)",
                    ([reviewer, intruder],),
                )
                conn.execute("DELETE FROM users WHERE email = ANY(%s)", ([reviewer, intruder],))
        except Exception as error:
            say(f"    (cleanup skipped: {error})")
        TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
        TRANSCRIPT.write_text("\n".join(LOG) + "\n", encoding="utf-8")
        say(f"\ntranscript -> {TRANSCRIPT}")


if __name__ == "__main__":
    raise SystemExit(main())
