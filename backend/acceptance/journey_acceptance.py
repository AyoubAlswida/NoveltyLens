"""The inventor's journey, end to end, including the paths nothing else drives.

    cd backend && python acceptance/journey_acceptance.py

## Why this exists beside `four_gate_acceptance.py`

That script proves the workflow *advances*: every stage is reached, approved, and
the run terminates with one `result`. It approves whatever the machine proposed
and it never rejects anything, so two whole branches of the product have never
been exercised end to end by any acceptance run:

* **Rejection.** `POST /api/runs/{id}/understanding/regenerate` and
  `.../strategy/regenerate` are the "no, try again" answer at stages 1 and 2.
  Each records a request, returns **202**, and the *worker* re-enters the
  proposing node under the run's lease. A regeneration must produce a **new
  version**, must leave the rejected proposal untouched, and must approve
  nothing -- a rejection is a decision, not an approval.
* **Editing, and the rules that refuse an edit.** An inventor corrects the
  analysis and the queries before approving. The completion rules that refuse a
  bad edit (`NO_SUMMARY`, `NO_CORE_ELEMENT`, `NO_ENABLED_QUERY` /
  `UNCOVERED_CORE_ELEMENT`, `STALE_UNDERSTANDING_VERSION`) are the ones a real
  reviewer meets, and each is asserted here against the real endpoint.

It also drives the **manual-candidate loop** to its actual end. That loop is easy
to get wrong from the outside and was got wrong while this script was being
written: after the worker absorbs a manually added publication the graph returns
to **Results and evidence** so a person approves the *widened* set (case T). A
script that approves once and then waits for the next stage waits for ever. The
loop is the product behaving correctly, and this file documents it by driving it.

## Zero paid calls

Same boundaries as `four_gate_acceptance.py`: `run_mock.py --workflow --fast`
under `DEPLOYMENT_PROFILE=test`, which replaces only the four metered outbound
boundaries -- the model, discovery, EPO full-text enrichment, and embeddings.
Everything above them is real: the production graph and gate nodes, the executor,
the lease, the checkpoint, the event log, the approval transactions and every API
rule. `run_mock.py` additionally wraps `requests.Session.request` so any call to a
non-local host raises rather than silently spending quota.

**Provider roles are unchanged and untested here.** Discovery is Lens.org and
evidence is EPO OPS; both are stubbed, so this run makes no claim about either.

## What it does to the database

It creates two throwaway accounts and one run, and deletes all three in `finally`.
It touches no other row, and it never drops or migrates anything.
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
API_PORT = int(os.environ.get("JOURNEY_API_PORT", "8096"))
API = f"http://127.0.0.1:{API_PORT}"
SERVER_LOG = BACKEND / "acceptance" / "journey-server.log"

LOG: list[str] = []
FAILURES: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    LOG.append(line)


def step(text: str) -> None:
    say(f"\n--- {text}")


def check(name: str, condition: object, detail: object = "") -> bool:
    """One asserted property, recorded pass or fail.

    Collected rather than raised so one failure does not hide the twenty checks
    after it; the run still exits non-zero if anything failed.
    """
    ok = bool(condition)
    if ok:
        say(f"  PASS  {name}")
    else:
        say(f"  FAIL  {name}  :: {str(detail)[:240]}")
        FAILURES.append(name)
    return ok


def call(method: str, path: str, token: str | None = None, body: dict | None = None,
         show: bool = True) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
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
        say(f"    -> {status} {json.dumps(parsed)[:180]}")
    return status, parsed


def api_up() -> bool:
    try:
        return call("GET", "/healthz", show=False)[0] == 200
    except Exception:
        return False


def wait_for(predicate, *, what: str, timeout: float = 180.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise SystemExit(f"TIMED OUT waiting for {what}")


def wait_for_gate(token: str, run_id: str, gate: str, *, timeout: float = 180.0):
    """Wait until the executor has parked the run at `gate`.

    A FAILED run is raised immediately with the executor's traceback rather than
    waited on: the timeout would otherwise report "never reached stage 3" about a
    run that failed in the first second, which is the wrong question to debug.
    """
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
            raise SystemExit(f"the run FAILED while waiting for {gate}: {run}{detail}")
        parked_here = (run.get("status") == "WAITING_FOR_REVIEW"
                       and str(run.get("current_gate")) == gate)
        return run if parked_here else None

    run = wait_for(parked, what=f"the run to park at stage {gate}", timeout=timeout)
    say(f"    parked at {run['current_gate']} (last_event_id={run['last_event_id']})")
    return run


def read_stream(run_id: str, token: str, last_event_id: int | None) -> list[tuple[int, dict]]:
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
    reviewer = f"journey-{stamp}@example.invalid"
    intruder = f"journey-x-{stamp}@example.invalid"
    password = uuid.uuid4().hex

    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DEPLOYMENT_PROFILE", "test")
    import psycopg

    from auth import hash_password
    from config import settings

    say("=" * 78)
    say("JOURNEY ACCEPTANCE - four stages, with rejection and editing. Zero paid calls.")
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
        # Refuse rather than test a process this script did not start. A stale
        # server on this port serves older code and turns a real regression into
        # a baffling pass, or the reverse.
        if api_up():
            raise SystemExit(
                f"something is already listening on {API}. This acceptance run "
                "refuses to proceed rather than test a process it did not start. "
                "Set JOURNEY_API_PORT to a free port, or stop that process."
            )
        server = subprocess.Popen(
            [sys.executable, "run_mock.py", "--workflow", "--fast", "--port", str(API_PORT)],
            cwd=str(BACKEND), env=env,
            # NOT DEVNULL: when a run fails, the executor's traceback is in here
            # and nowhere else.
            stdout=open(SERVER_LOG, "w", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT,
        )
        say(f"    pid {server.pid}; server log -> {SERVER_LOG.name}")
        wait_for(api_up, what="the mock API to serve /healthz")
        say(f"    healthy on {API}; outbound HTTP to any non-local host is blocked")

        _, body = call("POST", "/api/auth/login", token=None,
                       body={"email": reviewer, "password": password}, show=False)
        token = body["token"]
        _, body = call("POST", "/api/auth/login", token=None,
                       body={"email": intruder, "password": password}, show=False)
        intruder_token = body["token"]
        say("    signed in (token held in memory, never printed)")

        # ---------------------------------------------------------------
        step("SUBMISSION - the three things an inventor writes")
        status, created = call("POST", "/api/runs", token, {
            "description": f"journey {stamp}: a portable water purifier that filters through a "
                           "graphene-oxide membrane and sterilises with UV-C",
            "problem_solved": "clean drinking water without mains power",
            "inventor_novelty_statement": "the membrane self-cleans using flow-rate feedback",
            "cutoff_date": "2024-01-01",
        })
        check("the run is admitted", status == 201, created)
        run_id = created["run_id"]
        check("admission records this runtime as UNQUALIFIED, and says so",
              created.get("capability_qualified") is False, created)

        # ---------------------------------------------------------------
        step("STAGE 1 - Invention understanding")
        wait_for_gate(token, run_id, "A")
        status, gate_a = call("GET", f"/api/runs/{run_id}/gates/A", token, show=False)
        check("the stage loads", status == 200, status)
        check("the inventor's own description comes back untouched",
              (gate_a.get("original_description") or "").endswith("UV-C"),
              gate_a.get("original_description"))
        check("the problem it solves is preserved",
              gate_a.get("problem_solved") == "clean drinking water without mains power",
              gate_a.get("problem_solved"))
        check("what the inventor believes is new is preserved",
              gate_a.get("inventor_novelty_statement") ==
              "the membrane self-cleans using flow-rate feedback",
              gate_a.get("inventor_novelty_statement"))
        check("the editing limits are supplied to the screen rather than restated by it",
              (gate_a.get("limits") or {}).get("max_capabilities", 0) >= 1, gate_a.get("limits"))

        status, denied = call("GET", f"/api/runs/{run_id}/gates/A", intruder_token, show=False)
        check("another account gets 404 RUN_NOT_FOUND, never 403",
              status == 404 and denied.get("code") == "RUN_NOT_FOUND", (status, denied))

        proposal = gate_a.get("candidate_decomposition") or {}
        first_version = proposal.get("version")
        say(f"    proposed understanding v{first_version}, "
            f"{len(proposal.get('features') or [])} capability/ies")

        # --- REJECTION. The path no other acceptance run drives. ---------
        key_reject = f"{stamp}-reject-understanding"
        status, rejected = call("POST", f"/api/runs/{run_id}/understanding/regenerate", token, {
            "idempotency_key": key_reject,
            "from_version": first_version,
            "feedback": "It missed the self-cleaning feedback loop entirely.",
        })
        check("rejecting the analysis returns 202 - the WORKER re-analyses, not the request",
              status == 202, (status, rejected))
        status, replayed = call("POST", f"/api/runs/{run_id}/understanding/regenerate", token, {
            "idempotency_key": key_reject,
            "from_version": first_version,
            "feedback": "It missed the self-cleaning feedback loop entirely.",
        }, show=False)
        check("an identical rejection replays the stored response, no second request",
              (status, replayed) == (202, rejected), (status, replayed))

        wait_for_gate(token, run_id, "A")
        status, gate_a2 = call("GET", f"/api/runs/{run_id}/gates/A", token, show=False)
        proposal2 = gate_a2.get("candidate_decomposition") or {}
        second_version = proposal2.get("version")
        check("the re-analysis is a NEW version", second_version == first_version + 1,
              (first_version, second_version))
        check("a rejection approved nothing", not gate_a2.get("approved_version"),
              gate_a2.get("approved_version"))
        check("the rejection is recorded and visible to the reviewer",
              len(gate_a2.get("regenerations") or []) >= 1, gate_a2.get("regenerations"))

        status, stale = call("POST", f"/api/runs/{run_id}/understanding/regenerate", token, {
            "idempotency_key": f"{stamp}-reject-stale",
            "from_version": first_version,
            "feedback": "composed against the version that has since been replaced",
        }, show=False)
        check("a rejection naming a superseded version is refused, not applied to the new one",
              status == 409, (status, stale))

        # --- EDIT, and the rules that refuse a bad edit. -----------------
        decomposition = {
            "version": second_version,
            "summary": "A portable purifier that filters through a graphene-oxide membrane "
                       "and sterilises with UV-C, cleaning itself on flow-rate feedback.",
            "features": [{
                "feature_id": "F1",
                "feature_version": "1",
                "feature_text": "filtration with a self-cleaning cycle",
                "elements": [
                    {"element_id": "E1", "text": "a graphene-oxide filtration membrane",
                     "core": True, "order": 1},
                    {"element_id": "E2", "text": "a flow-rate sensor driving a cleaning cycle",
                     "core": True, "order": 2},
                ],
                "relationships": [
                    {"relationship_id": "R1",
                     "text": "the sensor triggers the cleaning cycle", "order": 1},
                ],
            }],
        }
        status, refused = call("POST", f"/api/runs/{run_id}/gates/A/approve", token, {
            "idempotency_key": f"{stamp}-a-nosummary",
            "decomposition": {**decomposition, "summary": ""},
        }, show=False)
        check("an empty summary is refused, and names the rule it broke",
              status == 409 and "NO_SUMMARY" in json.dumps(refused), (status, refused))

        no_core = json.loads(json.dumps(decomposition))
        for element in no_core["features"][0]["elements"]:
            element["core"] = False
        status, refused = call("POST", f"/api/runs/{run_id}/gates/A/approve", token, {
            "idempotency_key": f"{stamp}-a-nocore", "decomposition": no_core,
        }, show=False)
        check("an understanding with no core element anywhere is refused",
              status == 409 and "NO_CORE_ELEMENT" in json.dumps(refused), (status, refused))

        key_a = f"{stamp}-a"
        status, approved_a = call("POST", f"/api/runs/{run_id}/gates/A/approve", token,
                                  {"idempotency_key": key_a, "decomposition": decomposition})
        check("the EDITED understanding is approved",
              status == 200 and approved_a.get("status") == "RESUMING", (status, approved_a))
        status, replayed = call("POST", f"/api/runs/{run_id}/gates/A/approve", token,
                                {"idempotency_key": key_a, "decomposition": decomposition},
                                show=False)
        check("the identical approval replays, no second mutation",
              (status, replayed) == (200, approved_a), (status, replayed))

        # ---------------------------------------------------------------
        step("STAGE 2 - Search strategy, before any provider is contacted")
        wait_for_gate(token, run_id, "STRATEGY")
        status, gate_s = call("GET", f"/api/runs/{run_id}/gates/STRATEGY", token, show=False)
        check("the stage loads", status == 200, status)
        check("the approved analysis is shown, to be read and not edited here",
              bool(gate_s.get("approved_summary")), gate_s.get("approved_summary"))
        elements = gate_s.get("elements") or []
        core_ids = [e["id"] for e in elements if e.get("core")]
        check("core elements are named, so coverage can be judged", len(core_ids) >= 1, elements)
        check("element ids are feature-qualified, so two features cannot collide",
              all("." in e["id"] for e in elements), [e.get("id") for e in elements])
        strategy = gate_s.get("candidate_search_strategy") or {}
        strategy_version = strategy.get("version")
        check("queries were proposed", len(strategy.get("queries") or []) >= 1, strategy)

        status, rejected_s = call("POST", f"/api/runs/{run_id}/strategy/regenerate", token, {
            "idempotency_key": f"{stamp}-reject-strategy",
            "from_version": strategy_version,
            "feedback": "Too narrow - nothing looks for the cleaning cycle.",
        })
        check("rejecting the queries returns 202 - the WORKER re-proposes",
              status == 202, (status, rejected_s))
        wait_for_gate(token, run_id, "STRATEGY")
        status, gate_s2 = call("GET", f"/api/runs/{run_id}/gates/STRATEGY", token, show=False)
        strategy2 = gate_s2.get("candidate_search_strategy") or {}
        check("the re-proposed strategy is a NEW version",
              strategy2.get("version") == strategy_version + 1,
              (strategy_version, strategy2.get("version")))
        check("a rejection approved no strategy", not gate_s2.get("approved_version"),
              gate_s2.get("approved_version"))

        queries = strategy2.get("queries") or []
        understanding_version = gate_s2.get("understanding_version")

        every_query_off = [{**q, "enabled": False} for q in queries]
        status, refused = call("POST", f"/api/runs/{run_id}/gates/STRATEGY/approve", token, {
            "idempotency_key": f"{stamp}-s-alloff",
            "strategy": {"version": strategy2.get("version"),
                         "understanding_version": understanding_version,
                         "queries": every_query_off},
        }, show=False)
        check("approving with every search switched off is refused", status == 409,
              (status, refused))
        check("  ...and it names why, rather than failing generically",
              any(code in json.dumps(refused)
                  for code in ("NO_ENABLED_QUERY", "UNCOVERED_CORE_ELEMENT")), refused)

        status, refused = call("POST", f"/api/runs/{run_id}/gates/STRATEGY/approve", token, {
            "idempotency_key": f"{stamp}-s-stale",
            "strategy": {"version": strategy2.get("version"),
                         "understanding_version": (understanding_version or 1) + 5,
                         "queries": queries},
        }, show=False)
        check("a strategy naming the wrong analysis version is refused as stale",
              status == 409 and "STALE_UNDERSTANDING_VERSION" in json.dumps(refused),
              (status, refused))

        # --- EDIT: rewrite one query, and switch one OFF. ----------------
        # A switched-off query is a decision about the search and must survive
        # into the record; the report has to be able to say "we deliberately did
        # not search for this". So it is sent, never filtered out by the client,
        # and the stored row is checked below.
        edited = json.loads(json.dumps(queries))
        edited[0]["query_text"] = "graphene oxide membrane self-cleaning flow rate feedback"
        edited[0]["covered_element_ids"] = list(dict.fromkeys(
            (edited[0].get("covered_element_ids") or []) + core_ids))
        disabled_query_id = None
        if len(edited) > 1:
            edited[-1]["enabled"] = False
            disabled_query_id = edited[-1]["query_id"]
        else:
            # One proposed query still has to prove the off-switch is recorded,
            # so a second is added and switched off. It covers no element, which
            # is legal: coverage is only required of *core* elements.
            disabled_query_id = "Q-OFF"
            edited.append({
                "query_id": disabled_query_id,
                "query_text": "ultraviolet sterilisation lamp housing",
                "rationale": "Deliberately not run: out of scope for this review.",
                "enabled": False,
                "covered_element_ids": [],
            })
        status, approved_s = call("POST", f"/api/runs/{run_id}/gates/STRATEGY/approve", token, {
            "idempotency_key": f"{stamp}-s",
            "strategy": {"version": strategy2.get("version"),
                         "understanding_version": understanding_version,
                         "queries": edited},
        })
        check("the EDITED strategy is approved", status == 200, (status, approved_s))

        # Read straight from the approved rows. `approved_strategy_queries` is
        # keyed by (run_id, version, query_id) -- there is no surrogate id to
        # join on -- so the newest approved version is selected explicitly.
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            rows = conn.execute(
                """SELECT query_id, enabled, query_text
                     FROM approved_strategy_queries
                    WHERE run_id = %s
                      AND version = (SELECT max(version) FROM approved_search_strategies
                                      WHERE run_id = %s AND is_approved)
                    ORDER BY ord""", (run_id, run_id)).fetchall()
        stored = {query_id: enabled for query_id, enabled, _ in rows}
        check("every approved query is stored, switched on or off",
              len(stored) == len(edited), (len(stored), len(edited)))
        check("a switched-off search is STORED, not dropped", disabled_query_id in stored, stored)
        check("  ...and stored as switched off, so the report can say so",
              stored.get(disabled_query_id) is False, stored)
        check("exactly one search is switched on, and it is the edited one",
              sum(1 for enabled in stored.values() if enabled) == 1, stored)
        check("the reviewer's edit to the query text was stored verbatim",
              any("self-cleaning flow rate feedback" in text for _, enabled, text in rows
                  if enabled), [text for _, _, text in rows])

        # ---------------------------------------------------------------
        step("STAGE 3 - Results and evidence: which publications get examined")
        wait_for_gate(token, run_id, "B")
        status, gate_b = call("GET", f"/api/runs/{run_id}/gates/B", token, show=False)
        check("the stage loads", status == 200, status)
        candidates = gate_b.get("ranked_candidates") or []
        exclusions = gate_b.get("excluded") or []
        check("the search produced candidates, reasoned exclusions, or both",
              bool(candidates or exclusions), gate_b)
        say(f"    {len(candidates)} examinable, {len(exclusions)} not examinable")
        excluded_numbers = {e.get("requested_number") for e in exclusions}
        check("an excluded publication is never also offered as a candidate",
              all(c.get("publication_number") not in excluded_numbers for c in candidates))
        for exclusion in exclusions:
            check(f"exclusion {exclusion.get('requested_number')} carries a reason",
                  bool(exclusion.get("reason")), exclusion)

        # --- THE MANUAL-CANDIDATE LOOP, driven to its real end. ----------
        #
        # **The ordering here is the whole point, and it is easy to get wrong.**
        # Adding a publication records a request and returns 202; it does NOT
        # resume the run. The run is parked at this stage waiting for a person,
        # and *approving* is what releases it -- only then does the worker claim
        # the request, re-run the real screening nodes over the widened set, and
        # bring the run back HERE for a second approval (case T).
        #
        # So a script that waits for the request to be absorbed BEFORE approving
        # waits for something that cannot happen: the worker is not running, and
        # the wait times out against a perfectly healthy run. That is exactly how
        # this was got wrong while it was being written.
        status, added = call("POST", f"/api/runs/{run_id}/candidates", token, {
            "idempotency_key": f"{stamp}-manual", "publication_number": "EP2987654",
        })
        check("adding a publication by hand returns 202 - the WORKER does the work",
              status == 202, (status, added))
        check("  ...and adding it is explicitly NOT approving it",
              added.get("accepted") is True and "not approved" in json.dumps(added).lower()
              or "same normalization" in json.dumps(added), added)

        first_set = [c["publication_number"] for c in candidates]
        status, approved_b1 = call("POST", f"/api/runs/{run_id}/gates/B/approve", token, {
            "idempotency_key": f"{stamp}-b1", "publication_numbers": first_set,
            "version": (gate_b.get("approved_version") or 0) + 1,
        })
        check("approving this stage is what releases the run to the worker",
              status == 200, (status, approved_b1))

        say("    waiting for the worker to absorb the manual candidate and loop back...")
        wait_for_gate(token, run_id, "B")
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            claimed = conn.execute(
                "SELECT requested_number, processed_at IS NOT NULL FROM manual_candidates"
                " WHERE run_id = %s", (run_id,)).fetchall()
        check("the WORKER claimed the request, under the run's lease",
              bool(claimed) and all(row[1] for row in claimed), claimed)
        check("the claimed request is KEPT as evidence a person asked and the worker acted",
              len(claimed) == 1, claimed)

        status, gate_b2 = call("GET", f"/api/runs/{run_id}/gates/B", token, show=False)
        check("the run came back to THIS stage for a second approval, not straight on",
              status == 200, status)
        widened = {c.get("publication_number") for c in (gate_b2.get("ranked_candidates") or [])}
        widened |= {e.get("requested_number") for e in (gate_b2.get("excluded") or [])}
        check("the manual publication went through the SAME screening as a discovered one "
              "- examinable, or excluded with a reason",
              "EP2987654" in widened, sorted(n for n in widened if n))
        say(f"    this stage now offers {len(gate_b2.get('ranked_candidates') or [])} "
            "publication(s) for a second approval")

        publications = [c["publication_number"] for c in (gate_b2.get("ranked_candidates") or [])]
        status, approved_b = call("POST", f"/api/runs/{run_id}/gates/B/approve", token, {
            "idempotency_key": f"{stamp}-b2", "publication_numbers": publications,
            "version": (gate_b2.get("approved_version") or 0) + 1,
        })
        check("the WIDENED set is approved, by a person, at its own version",
              status == 200, (status, approved_b))

        # ---------------------------------------------------------------
        step("STAGE 3 - Results and evidence: what the passages actually say")
        wait_for_gate(token, run_id, "C")
        status, gate_c = call("GET", f"/api/runs/{run_id}/gates/C", token, show=False)
        check("the evidence stage loads", status == 200, status)
        matrix = gate_c.get("matrix") or []
        check("comparisons were produced", len(matrix) >= 1, gate_c)
        say(f"    {len(matrix)} comparison(s), "
            f"{len(gate_c.get('blocking') or [])} blocking condition(s)")

        if matrix:
            first = matrix[0]
            status, cell = call("GET", f"/api/runs/{run_id}/cells/{first['cell_id']}",
                                token, show=False)
            check("a comparison opens with the evidence behind it", status == 200, status)
            check("the system's result and a person's decision are SEPARATE keys, never merged",
                  "machine_result" in cell and "review" in cell, sorted(cell.keys()))
            check("the passages it cited are returned, so a quotation can be checked",
                  "passages" in cell, sorted(cell.keys()))

            # A rerun SUPERSEDES; it never edits. The prior version stays.
            status, rerun = call("POST",
                                 f"/api/runs/{run_id}/cells/{first['cell_id']}/rerun", token, {
                                     "idempotency_key": f"{stamp}-rerun",
                                     "context_requests": [],
                                     "source_result_version": first.get("result_version"),
                                 })
            check("a targeted re-run returns 202 and names the version it will produce",
                  status == 202 and rerun.get("result_version"), (status, rerun))
            check("  ...and it points back at the version it supersedes",
                  rerun.get("source_result_version") == first.get("result_version"), rerun)
            status, stale = call("POST",
                                 f"/api/runs/{run_id}/cells/{first['cell_id']}/rerun", token, {
                                     "idempotency_key": f"{stamp}-rerun-stale",
                                     "context_requests": [], "source_result_version": 99,
                                 }, show=False)
            check("a re-run composed against a superseded result is refused",
                  status == 409, (status, stale))

            # **The worker carries the re-run out only once the stage is
            # released**, exactly as a manual candidate is only absorbed once
            # this stage is approved. Requesting a re-run records a request; the
            # run is still parked here waiting for a person. Completing the stage
            # is what hands it back to the worker, and the graph then returns
            # HERE so the reviewer sees the answer to the question they asked.
            #
            # Waiting on the *gate* would return instantly and prove nothing --
            # the run has not left it yet -- so this waits on the stored result.
            status, released = call("POST", f"/api/runs/{run_id}/gates/C/complete", token,
                                    {"idempotency_key": f"{stamp}-c-rerun"})
            check("completing the stage hands the re-run to the worker",
                  status == 200, (status, released))

            def reran():
                with psycopg.connect(settings.database_url, autocommit=True) as conn:
                    rows = conn.execute(
                        "SELECT result_version, source_result_version"
                        "  FROM verification_results WHERE cell_id = %s"
                        " ORDER BY result_version", (first["cell_id"],)).fetchall()
                return rows if len(rows) >= 2 else None

            versions = wait_for(reran, what="the worker to carry out the re-run")
            wait_for_gate(token, run_id, "C")
            check("the re-run produced a NEW version", len(versions) >= 2, versions)
            check("  ...that points back at the version it superseded",
                  versions[-1][1] == versions[0][0], versions)
            check("  ...and the ORIGINAL result is still there, unedited",
                  versions[0] == (1, None), versions)
            with psycopg.connect(settings.database_url, autocommit=True) as conn:
                claimed = conn.execute(
                    "SELECT processed_at IS NOT NULL FROM cell_rerun_requests"
                    " WHERE run_id = %s", (run_id,)).fetchall()
            check("the re-run request is claimed and KEPT as evidence",
                  bool(claimed) and all(row[0] for row in claimed), claimed)

        step("resolve or waive every blocking condition, then complete the stage")
        for attempt in range(6):
            status, current = call("GET", f"/api/runs/{run_id}/gates/C", token, show=False)
            blocking = current.get("blocking") or []
            if not blocking:
                break
            say(f"    {len(blocking)} blocking condition(s); recording a reviewer decision")
            for condition in blocking:
                cell_id = condition.get("cell_id")
                if not cell_id:
                    continue
                row = next((c for c in (current.get("matrix") or [])
                            if c["cell_id"] == cell_id), None)
                call("POST", f"/api/runs/{run_id}/cells/{cell_id}/waiver", token, {
                    "idempotency_key": f"{stamp}-waive-{attempt}-{cell_id[-8:]}",
                    "waiver": {
                        "source_cell_id": cell_id,
                        "source_result_version": (row or {}).get("result_version", 1),
                        "reason": "acceptance run: accepted unresolved by the reviewer",
                        "unresolved_limitation":
                            "This comparison was waived during acceptance and was not "
                            "independently re-verified.",
                    },
                }, show=False)
        status, completed = call("POST", f"/api/runs/{run_id}/gates/C/complete", token,
                                 {"idempotency_key": f"{stamp}-c"})
        check("the stage completes once nothing is left unresolved", status == 200,
              (status, completed))

        # ---------------------------------------------------------------
        step("STAGE 4 - Final report")
        wait_for_gate(token, run_id, "D")
        status, report = call("GET", f"/api/runs/{run_id}/report", token, show=False)
        check("a draft exists and is presented BEFORE it can be approved",
              status == 200, status)
        draft = report.get("draft") or {}
        check("the draft says it is a draft", report.get("status") == "DRAFT",
              report.get("status"))

        coverage = draft.get("coverage_statement") or ""
        check("the report carries its coverage statement", bool(coverage), sorted(draft.keys()))
        check("  it states the search is NOT exhaustive", "not an exhaustive" in coverage, coverage)
        check("  it states it is NOT a legal opinion", "not a legal opinion" in coverage, coverage)
        check("  it states excluded publications were NOT EVALUATED",
              "NOT" in coverage and "EVALUATED" in coverage, coverage)

        # Derived only from approved artifacts, and able to say which ones.
        check("the report names the approved understanding it verified",
              draft.get("decomposition_version") is not None, draft.get("decomposition_version"))
        check("the report names the approved publication set it verified against",
              draft.get("candidate_set_version") is not None, draft.get("candidate_set_version"))
        check("the report records the date rule it screened by",
              bool(draft.get("date_policy")), draft.get("date_policy"))
        check("the report records the text-version rule it used",
              bool(draft.get("text_version_policy")), draft.get("text_version_policy"))

        for excluded in (draft.get("excluded_publications") or []):
            check(f"an excluded publication is NAMED: {excluded.get('publication_number')!r}",
                  bool(excluded.get("publication_number")), excluded)
            check("  ...and is stated NOT EVALUATED, never 'nothing found'",
                  excluded.get("evaluated") is False
                  and "NOT EVALUATED" in (excluded.get("note") or ""), excluded)

        waived = [entry for entry in (draft.get("unresolved") or []) if entry.get("waived")]
        for entry in waived:
            check(f"a waived comparison keeps its own state ({entry.get('state')})",
                  entry.get("state") in ("VALIDATION_FAILURE", "NOT_EVALUATED"), entry)
            check("  ...and carries the limitation that remains unresolved",
                  bool(entry.get("unresolved_limitations")), entry)

        status, refused = call("POST", f"/api/runs/{run_id}/gates/D/approve", token, {
            "idempotency_key": f"{stamp}-d-nonexistent", "report_version": 999,
        }, show=False)
        check("a report version that does not exist cannot be approved into existence",
              status >= 400, (status, refused))

        key_d = f"{stamp}-d"
        status, approved_d = call("POST", f"/api/runs/{run_id}/gates/D/approve", token, {
            "idempotency_key": key_d, "report_version": report.get("version"),
            "comments": "Acceptance: each cited passage was checked against its source.",
            "caveats": "Publications with no retrievable full text were not examined.",
        })
        check("the draft is approved", status == 200, (status, approved_d))
        status, replayed = call("POST", f"/api/runs/{run_id}/gates/D/approve", token, {
            "idempotency_key": key_d, "report_version": report.get("version"),
            "comments": "Acceptance: each cited passage was checked against its source.",
            "caveats": "Publications with no retrievable full text were not examined.",
        }, show=False)
        check("the identical approval replays, no second mutation",
              (status, replayed) == (200, approved_d), (status, replayed))

        def terminal():
            _, run = call("GET", f"/api/runs/{run_id}", token, show=False)
            return run if run.get("status") in ("COMPLETED", "FAILED") else None

        final_run = wait_for(terminal, what="the run to reach a terminal state")
        check("the run COMPLETES", final_run.get("status") == "COMPLETED", final_run)

        status, final_report = call("GET", f"/api/runs/{run_id}/report", token, show=False)
        check("the final report is APPROVED", final_report.get("status") == "APPROVED",
              final_report.get("status"))
        check("it records WHO approved it", bool(final_report.get("approved_by")),
              final_report.get("approved_by"))
        check("it carries the reviewer's comments",
              "checked against its source" in (final_report.get("comments") or ""),
              final_report.get("comments"))
        check("it carries the reviewer's caveats",
              "not examined" in (final_report.get("caveats") or ""),
              final_report.get("caveats"))

        # ---------------------------------------------------------------
        step("the stream: one terminal, replayable from a cursor")
        frames = read_stream(run_id, token, 0)
        kinds = [payload.get("type") for _, payload in frames]
        check("replaying from the start delivers the whole log", len(frames) > 10, len(frames))
        check("EXACTLY ONE terminal result, and no error",
              kinds.count("result") == 1 and "error" not in kinds, kinds[-6:])
        check("every stage pause is in the log",
              kinds.count("review_required") >= 4, kinds.count("review_required"))
        check("the sequence numbers are strictly increasing",
              all(b > a for (a, _), (b, _) in zip(frames, frames[1:])),
              [sequence for sequence, _ in frames[:8]])

        terminal_sequence = max(sequence for sequence, payload in frames
                                if payload.get("type") == "result")
        tail = read_stream(run_id, token, terminal_sequence)
        check("reconnecting AT the terminal delivers nothing and closes", tail == [], tail)
        midpoint = frames[len(frames) // 2][0]
        resumed = read_stream(run_id, token, midpoint)
        check("reconnecting mid-run delivers only what came after the cursor",
              resumed and all(sequence > midpoint for sequence, _ in resumed),
              [sequence for sequence, _ in resumed[:5]])

        status, denied = call("GET", f"/api/runs/{run_id}/report", intruder_token, show=False)
        check("another account cannot read the finished report",
              status == 404 and denied.get("code") == "RUN_NOT_FOUND", (status, denied))

        say("\n" + "=" * 78)
        if FAILURES:
            say(f"RESULT: {len(FAILURES)} CHECK(S) FAILED")
            for failure in FAILURES:
                say(f"  - {failure}")
        else:
            say("RESULT: all four stages driven, including rejection and editing. "
                "Zero paid calls.")
        say("=" * 78)
        return 1 if FAILURES else 0

    finally:
        step("teardown")
        if server and server.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(server.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            try:
                server.wait(timeout=20)
            except Exception:
                pass
            say("  mock server stopped (process tree)")
        try:
            with psycopg.connect(settings.database_url, autocommit=True) as conn:
                if run_id:
                    conn.execute("DELETE FROM verification_runs WHERE id = %s", (run_id,))
                conn.execute("DELETE FROM idempotency_records WHERE actor_email = ANY(%s)",
                             ([reviewer, intruder],))
                conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                             ([reviewer, intruder],))
            say("  test data removed (this run, and its two throwaway accounts)")
        except Exception as error:  # pragma: no cover
            say(f"  cleanup warning: {type(error).__name__}")
        out = BACKEND.parent / "docs" / "acceptance" / "journey-transcript.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(LOG), encoding="utf-8")
        say("  transcript -> docs/acceptance/journey-transcript.txt")


if __name__ == "__main__":
    sys.exit(main())
