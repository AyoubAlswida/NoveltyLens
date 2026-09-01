"""Gate acceptance: the real workflow, all the way through, at zero cost.

Named for the four gates it was written against. It now drives **five**
reviewed stages -- the SEARCH STRATEGY stage sits between the invention
understanding and the candidate review -- and the filename is kept so the
transcript path and every reference to it stay stable.

    cd backend && python acceptance/four_gate_acceptance.py

## What makes this different from a mock graph

**There is no mock graph.** This drives `graph/pipeline.py` -- the production
topology, the production gate nodes, the production evidence layer and the
production verifier -- hosted by the production executor. The only things
replaced are the **outbound provider boundaries** that would otherwise cost
money or need credentials:

| Boundary | Stood in for by | Why it is a boundary and not logic |
|---|---|---|
| `llm._llm_chat` | `run_mock._mock_llm_chat` | the one HTTP call to the model |
| `search.search.SOURCES` | `run_mock.MOCK_SOURCES` | discovery |
| `search.enrichment.enrich_publication` | `run_mock._mock_enrich_publication` | EPO OPS full text |
| `compare.embed_texts` | `run_mock._mock_embed_texts` | the embedding endpoint |

Everything above those lines is real: normalization, date screening, passage
segmentation, locator construction, eligibility derivation, the §9 checks, the
approval transactions, the lease and its fencing, the LangGraph checkpoint and
resume, the event log, the SSE contract and every API rule.

**No gate is bypassed and nothing is auto-approved.** Each approval goes through
the real `POST /api/runs/{id}/gates/{gate}/approve` with an idempotency key, and
`workflow/approvals.py` remains the only writer of an `approved_*` field. This
script never touches an approval table.
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
API_PORT = int(os.environ.get("ACCEPTANCE_API_PORT", "8097"))
API = f"http://127.0.0.1:{API_PORT}"
SERVER_LOG = BACKEND / "acceptance" / "four-gate-server.log"

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


def main() -> int:
    stamp = uuid.uuid4().hex[:8]
    reviewer = f"fourgate-{stamp}@example.invalid"
    intruder = f"other-{stamp}@example.invalid"
    password = uuid.uuid4().hex

    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DEPLOYMENT_PROFILE", "test")
    import psycopg

    from auth import hash_password
    from config import settings

    say("=" * 78)
    say("GATE ACCEPTANCE — real graph, real gates, mocked providers only")
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
            [sys.executable, "run_mock.py", "--workflow", "--fast",
             "--port", str(API_PORT)],
            cwd=str(BACKEND), env=env,
            # NOT DEVNULL. When the run fails, the executor's traceback is in
            # here and nowhere else -- discarding it turns "the run FAILED" into
            # an unanswerable question.
            stdout=open(SERVER_LOG, "w", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT,
        )
        say(f"    pid {server.pid}; server log -> {SERVER_LOG.name}")
        def healthy() -> bool:
            # Connection-refused while the server is still binding is expected,
            # not a failure. Letting it escape turned a slow start into a crash.
            try:
                return call("GET", "/healthz", show=False)[0] == 200
            except Exception:
                return False

        wait_for(healthy, what="the mock API to serve /healthz")
        say(f"    healthy on {API}; outbound HTTP to any non-local host is blocked")

        _, body = call("POST", "/api/auth/login", body={"email": reviewer, "password": password},
                       show=False)
        token = body["token"]
        _, body = call("POST", "/api/auth/login", body={"email": intruder, "password": password},
                       show=False)
        intruder_token = body["token"]
        say("    signed in (token held in memory, never printed)")

        # ---------------------------------------------------------------
        step("CREATE the run")
        status, created = call("POST", "/api/runs", token, {
            "description": f"four-gate acceptance {stamp}: closed-loop exhaust heat recovery",
            "cutoff_date": "2024-01-01",
        })
        assert status == 201, created
        run_id = created["run_id"]
        assert created["capability_qualified"] is False, created
        say("    capability_qualified: false  <- deferred and recorded as UNQUALIFIED")

        # ---------------------------------------------------------------
        step("INVENTION UNDERSTANDING")
        wait_for_gate(token, run_id, "A")
        status, gate_a = call("GET", f"/api/runs/{run_id}/gates/A", token)
        assert status == 200
        say(f"    editable={gate_a['editable']}, rules={len(gate_a['completion_rules'])}")
        # The inventor's own submission comes back untouched beside the proposal.
        assert gate_a["original_description"], "the original description was not returned"
        assert gate_a["limits"]["max_capabilities"] >= 1, gate_a["limits"]
        say(f"    original description preserved ({len(gate_a['original_description'])} chars)")

        status, denied = call("GET", f"/api/runs/{run_id}/gates/A", intruder_token)
        assert status == 404 and denied["code"] == "RUN_NOT_FOUND"
        say("    a second account gets 404 RUN_NOT_FOUND")

        # A REVISED understanding: the reviewer edits before approving.
        decomposition = {
            "version": 1,
            # Approved together with the capabilities, at one version. An empty
            # summary is refused before anything is written.
            "summary": "A closed-loop controller recovers exhaust heat through a "
                       "duct-mounted exchanger by varying coolant flow.",
            "features": [{
                "feature_id": "F2",
                "feature_version": "1",
                "feature_text": "closed-loop control of exhaust heat recovery",
                "elements": [
                    {"element_id": "E1", "text": "a heat exchanger disposed within an exhaust duct",
                     "core": True, "order": 1},
                    {"element_id": "E2", "text": "a controller that varies coolant flow rate",
                     "core": True, "order": 2},
                ],
                "relationships": [
                    {"relationship_id": "R1",
                     "text": "the controller varies coolant flow in response to sensor output",
                     "order": 1},
                ],
            }],
        }
        # An understanding with no summary is refused with the rule it broke,
        # and writes nothing -- checked before the real approval so a failure
        # here cannot be mistaken for the approval below having worked.
        status, refused = call(
            "POST", f"/api/runs/{run_id}/gates/A/approve", token,
            {"idempotency_key": f"{stamp}-gate-a-empty",
             "decomposition": {**decomposition, "summary": ""}},
        )
        assert status == 409, (status, refused)
        assert "NO_SUMMARY" in json.dumps(refused), refused
        say("    an empty summary is refused with NO_SUMMARY")

        key_a = f"{stamp}-gate-a"
        status, approved = call("POST", f"/api/runs/{run_id}/gates/A/approve", token,
                                {"idempotency_key": key_a, "decomposition": decomposition})
        assert status == 200 and approved["status"] == "RESUMING", approved
        say(f"    approved as understanding_version={approved['understanding_version']}")

        status, replay = call("POST", f"/api/runs/{run_id}/gates/A/approve", token,
                              {"idempotency_key": key_a, "decomposition": decomposition})
        assert (status, replay) == (200, approved), (status, replay)
        say("    the identical approval replayed the stored response (no second mutation)")

        # ---------------------------------------------------------------
        step("SEARCH STRATEGY - the queries, before any provider is called")
        wait_for_gate(token, run_id, "STRATEGY")
        status, gate_s = call("GET", f"/api/runs/{run_id}/gates/STRATEGY", token)
        assert status == 200
        proposed = (gate_s.get("candidate_search_strategy") or {}).get("queries") or []
        elements = gate_s.get("elements") or []
        core = [element for element in elements if element.get("core")]
        say(f"    {len(proposed)} proposed quer(ies), {len(core)} core element(s) to cover")
        assert proposed, "the strategy stage proposed no queries"
        assert core, "the approved understanding has no core element to cover"

        status, denied = call("GET", f"/api/runs/{run_id}/gates/STRATEGY", intruder_token)
        assert status == 404 and denied["code"] == "RUN_NOT_FOUND"
        say("    a second account gets 404 RUN_NOT_FOUND")

        understanding_version = int(gate_s.get("understanding_version") or 0)
        strategy_version = int((gate_s.get("candidate_search_strategy") or {}).get("version") or 0)
        approved_queries = [
            {
                "query_id": query["query_id"],
                "query_text": query["query_text"],
                "rationale": query.get("rationale", ""),
                "enabled": True,
                "covered_element_ids": query.get("covered_element_ids", []),
                "order": index + 1,
            }
            for index, query in enumerate(proposed)
        ]

        # A strategy that leaves a CORE element unsearched is refused with the
        # elements it left uncovered -- checked before the real approval, so a
        # failure here cannot be mistaken for the approval below having worked.
        uncovered = [
            dict(query, enabled=False) if index == 0 else query
            for index, query in enumerate(approved_queries)
        ]
        if any(
            element["id"] in (approved_queries[0].get("covered_element_ids") or [])
            for element in core
        ):
            status, refused = call(
                "POST", f"/api/runs/{run_id}/gates/STRATEGY/approve", token,
                {"idempotency_key": f"{stamp}-strategy-uncovered",
                 "strategy": {"version": strategy_version,
                              "understanding_version": understanding_version,
                              "queries": uncovered}},
            )
            assert status == 409, (status, refused)
            assert "UNCOVERED_CORE_ELEMENT" in json.dumps(refused), refused
            say("    switching off a search that covers a core element is refused")

        key_s = f"{stamp}-strategy"
        status, approved_s = call(
            "POST", f"/api/runs/{run_id}/gates/STRATEGY/approve", token,
            {"idempotency_key": key_s,
             "strategy": {"version": strategy_version,
                          "understanding_version": understanding_version,
                          "queries": approved_queries}},
        )
        assert status == 200 and approved_s["status"] == "RESUMING", approved_s
        say(f"    approved strategy v{approved_s['strategy_version']}: "
            f"{approved_s['queries_enabled']} of {approved_s['queries_approved']} searches will run")

        status, replay_s = call(
            "POST", f"/api/runs/{run_id}/gates/STRATEGY/approve", token,
            {"idempotency_key": key_s,
             "strategy": {"version": strategy_version,
                          "understanding_version": understanding_version,
                          "queries": approved_queries}},
        )
        assert (status, replay_s) == (200, approved_s), (status, replay_s)
        say("    the identical approval replayed the stored response (no second mutation)")

        # Only what was approved is dispatched. Read straight out of the
        # approved table rather than inferred from the report, because "the
        # search ran the approved queries" is the claim this stage makes.
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            dispatched = [
                row[0] for row in conn.execute(
                    "SELECT query_text FROM approved_strategy_queries "
                    "WHERE run_id = %s AND enabled ORDER BY ord", (run_id,)
                ).fetchall()
            ]
        say(f"    {len(dispatched)} enabled quer(ies) recorded as approved")
        assert dispatched, "no enabled query was recorded"

        # ---------------------------------------------------------------
        step("WORKFLOW GATE B — candidates and exclusions")
        wait_for_gate(token, run_id, "B")
        status, gate_b = call("GET", f"/api/runs/{run_id}/gates/B", token)
        ranked = gate_b.get("ranked_candidates") or []
        excluded = gate_b.get("excluded") or []
        say(f"    {len(ranked)} eligible candidate(s), {len(excluded)} exclusion(s)")
        for item in excluded[:3]:
            say(f"      excluded {item.get('requested_number')}: {item.get('state')}")

        step("MANUAL CANDIDATE — added, then processed BY THE WORKER")
        status, accepted = call("POST", f"/api/runs/{run_id}/candidates", token,
                                {"idempotency_key": f"{stamp}-cand",
                                 "publication_number": "EP3218319"})
        assert status == 202, accepted
        say("    202 accepted — queued for the SAME pipeline, not approved")

        # Approving Gate B is what lets the worker pick the request up: the graph
        # routes back through normalize -> screen -> enrich -> passages ->
        # eligibility and returns HERE, to Gate B, for a second approval.
        key_b1 = f"{stamp}-gate-b-1"
        status, approved_b = call("POST", f"/api/runs/{run_id}/gates/B/approve", token,
                                  {"idempotency_key": key_b1,
                                   "publication_numbers": [c["publication_number"] if isinstance(c, dict) else c
                                                           for c in ranked],
                                   "version": 1})
        assert status == 200, approved_b

        say("    waiting for the worker to consume the manual candidate…")
        wait_for_gate(token, run_id, "B")
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            processed = conn.execute(
                "SELECT requested_number, processed_at IS NOT NULL FROM manual_candidates"
                " WHERE run_id = %s", (run_id,)
            ).fetchall()
        say(f"    manual_candidates rows: {processed}")
        assert processed and all(row[1] for row in processed), processed
        say("    the WORKER claimed and processed it (processed_at stamped)")

        status, gate_b2 = call("GET", f"/api/runs/{run_id}/gates/B", token, show=False)
        ranked2 = gate_b2.get("ranked_candidates") or []
        say(f"    Gate B now offers {len(ranked2)} candidate(s) for a second approval")

        key_b2 = f"{stamp}-gate-b-2"
        status, approved_b2 = call("POST", f"/api/runs/{run_id}/gates/B/approve", token,
                                   {"idempotency_key": key_b2,
                                    "publication_numbers": [c["publication_number"] if isinstance(c, dict) else c
                                                            for c in ranked2],
                                    "version": 2})
        assert status == 200, approved_b2
        say(f"    approved as candidate_set_version={approved_b2['candidate_set_version']}")

        # ---------------------------------------------------------------
        step("WORKFLOW GATE C — evidence and verification cells")
        wait_for_gate(token, run_id, "C")
        status, gate_c = call("GET", f"/api/runs/{run_id}/gates/C", token, show=False)
        matrix = gate_c.get("matrix") or []
        say(f"    {len(matrix)} cell(s); blocking={len(gate_c.get('blocking') or [])}")
        for cell in matrix[:4]:
            say(f"      {cell['cell_id']}: state={cell['state']} v{cell['result_version']}")

        if matrix:
            target = matrix[0]
            status, detail = call("GET",
                                  f"/api/runs/{run_id}/cells/{target['cell_id']}", token,
                                  show=False)
            passages = detail.get("passages") or []
            say(f"    cell detail: {len(passages)} passage(s) with locators")
            for passage in passages[:2]:
                say(f"      {passage.get('locator')} [{passage.get('locator_kind')}]")

            step("CELL RERUN — requested, then processed BY THE WORKER")
            status, rerun = call("POST",
                                 f"/api/runs/{run_id}/cells/{target['cell_id']}/rerun", token,
                                 {"idempotency_key": f"{stamp}-rerun",
                                  "context_requests": [],
                                  "source_result_version": target["result_version"]})
            assert status == 202, rerun
            say(f"    202 accepted — will produce v{rerun['result_version']}, "
                f"superseding v{rerun['source_result_version']}")

            key_c1 = f"{stamp}-gate-c-1"
            status, _ = call("POST", f"/api/runs/{run_id}/gates/C/complete", token,
                             {"idempotency_key": key_c1}, show=False)
            say(f"    Gate C completion -> {status} (the rerun loop takes over)")

            say("    waiting for the worker to re-run the cell…")
            wait_for_gate(token, run_id, "C")
            with psycopg.connect(settings.database_url, autocommit=True) as conn:
                versions = conn.execute(
                    "SELECT result_version, source_result_version FROM verification_results"
                    " WHERE cell_id = %s ORDER BY result_version", (target["cell_id"],)
                ).fetchall()
                claimed = conn.execute(
                    "SELECT processed_at IS NOT NULL FROM cell_rerun_requests WHERE run_id = %s",
                    (run_id,)
                ).fetchall()
            say(f"    verification_results for that cell: {versions}")
            say(f"    cell_rerun_requests claimed: {claimed}")
            assert len(versions) >= 2, versions
            assert versions[-1][1] == versions[0][0], (
                f"lineage broken: v{versions[-1][0]} does not point back to v{versions[0][0]}"
            )
            say("    LINEAGE: the new version points back at the one it superseded, "
                "and the old row is still there")

        step("resolve or waive every blocking cell, then complete Gate C")
        for attempt in range(6):
            status, gate_c = call("GET", f"/api/runs/{run_id}/gates/C", token, show=False)
            blocking = gate_c.get("blocking") or []
            if not blocking:
                break
            say(f"    {len(blocking)} blocking condition(s); waiving them")
            for condition in blocking:
                cell_id = condition.get("cell_id")
                if not cell_id:
                    continue
                current = next((c for c in (gate_c.get("matrix") or [])
                                if c["cell_id"] == cell_id), None)
                call("POST", f"/api/runs/{run_id}/cells/{cell_id}/waiver", token, {
                    "idempotency_key": f"{stamp}-waive-{attempt}-{cell_id[-8:]}",
                    "waiver": {
                        "source_cell_id": cell_id,
                        "source_result_version": (current or {}).get("result_version", 1),
                        "reason": "acceptance run: resolved by reviewer decision",
                        "unresolved_limitation":
                            "This cell was waived during acceptance and was not "
                            "independently re-verified.",
                    },
                }, show=False)

        key_c2 = f"{stamp}-gate-c-final"
        status, completed_c = call("POST", f"/api/runs/{run_id}/gates/C/complete", token,
                                   {"idempotency_key": key_c2})
        assert status == 200, completed_c

        # ---------------------------------------------------------------
        step("WORKFLOW GATE D — the draft report")
        wait_for_gate(token, run_id, "D")
        status, report = call("GET", f"/api/runs/{run_id}/report", token, show=False)
        assert status == 200, report
        say(f"    report version {report.get('version')} status={report.get('status')}")
        assert str(report.get("status")).upper() != "APPROVED", (
            "the draft must NOT be APPROVED before Gate D"
        )
        say("    the draft is NOT approved before Gate D  <- case AC")

        draft = report.get("draft") or {}
        say(f"    draft carries: matrix={len(draft.get('matrix') or [])}, "
            f"exclusions={len(draft.get('excluded_publications') or [])}, "
            f"waivers={len(draft.get('waivers') or [])}")

        key_d = f"{stamp}-gate-d"
        status, approved_d = call("POST", f"/api/runs/{run_id}/gates/D/approve", token,
                                  {"idempotency_key": key_d,
                                   "report_version": report["version"],
                                   "comments": "Acceptance approval."})
        assert status == 200, approved_d

        # ---------------------------------------------------------------
        step("TERMINAL — exactly one result, and the report is APPROVED")

        def finished():
            _, run = call("GET", f"/api/runs/{run_id}", token, show=False)
            return run if run.get("status") in {"COMPLETED", "FAILED"} else None

        done = wait_for(finished, what="the run to reach a terminal state")
        say(f"    run is {done['status']} (last_event_id={done['last_event_id']})")
        assert done["status"] == "COMPLETED", done

        frames = stream(run_id, token, 0)
        types = [payload["type"] for _, payload in frames]
        say(f"    {len(frames)} events: {types}")
        assert types.count("result") == 1, f"expected exactly one result, got {types}"
        assert "error" not in types
        say("    EXACTLY ONE terminal `result` event, and no `error`")

        gate_events = [t for t in types if t in {"review_required", "paused", "resumed"}]
        say(f"    human-gate events in the log: {gate_events.count('review_required')} "
            f"review_required, {gate_events.count('paused')} paused, "
            f"{gate_events.count('resumed')} resumed")

        status, final = call("GET", f"/api/runs/{run_id}/report", token, show=False)
        say(f"    final report: version={final.get('version')} status={final.get('status')}")
        assert str(final.get("status")).upper() == "APPROVED", final

        step("SSE replay after the fact")
        cut = frames[len(frames) // 2][0]
        missed = stream(run_id, token, cut)
        say(f"    reconnect at {cut} -> {[s for s, _ in missed]}")
        assert all(s > cut for s, _ in missed)
        assert stream(run_id, token, frames[-1][0]) == []
        say("    reconnect at the terminal delivers nothing and closes")

        say("\n" + "=" * 78)
        say("RESULT: every gate driven through the real workflow. Zero paid calls.")
        say("=" * 78)
        return 0

    finally:
        step("teardown")
        if server and server.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(server.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            server.wait(timeout=20)
            say("  mock server stopped (process tree)")
        try:
            with psycopg.connect(settings.database_url, autocommit=True) as conn:
                if run_id:
                    conn.execute("DELETE FROM verification_runs WHERE id = %s", (run_id,))
                conn.execute("DELETE FROM idempotency_records WHERE actor_email = ANY(%s)",
                             ([reviewer, intruder],))
                conn.execute("DELETE FROM users WHERE email = ANY(%s)", ([reviewer, intruder],))
                left = conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND pid <> pg_backend_pid()").fetchone()[0]
            say(f"  test data removed; database backends still open: {left}")
        except Exception as error:  # pragma: no cover
            say(f"  cleanup warning: {type(error).__name__}")
        out = BACKEND.parent / "docs" / "acceptance" / "four-gate-transcript.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(LOG), encoding="utf-8")
        say("  transcript -> docs/acceptance/four-gate-transcript.txt")


if __name__ == "__main__":
    sys.exit(main())
