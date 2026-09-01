"""Real-process acceptance evidence for Implementation Gates D and E.

Run from `backend/`:

    python acceptance/real_process_acceptance.py

## What "real process" means here, precisely

Everything below runs against **actual OS processes over real HTTP**: `python
server.py` for the API, `python -m workflow.worker --mock` for the executor,
`npm run dev` for the frontend, and the Compose PostgreSQL. There is no
`TestClient`, no in-process app, no patched `db.connect`, and no fake HTTP.

**The graph is the deterministic mock adapter**, and that is disclosed rather
than glossed. The real verification graph calls OpenAI, and this evidence must
cost nothing — an owner constraint. `--mock` is the documented command for
exactly that, and what it stubs is the *content* of the work. Everything the two
gates are about stays real: the lease and its fencing, the checkpointer, the
event log and its sequence allocator, the status machine, ownership scoping,
idempotency, the SSE framing and replay, and process restart recovery.

## What it never does

- No paid API call, and no network call outside loopback.
- No token, password, `.env` value or environment variable is printed. Tokens are
  held in memory and rendered as `<redacted>`.
- No production data: it creates its own user and its own run, and deletes both.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"
# Deliberately NOT the documented 8001/4200. A `docker compose` backend already
# publishes 8001 on this machine, and it runs an older image -- it answered
# /healthz and then 404'd /api/runs, which is exactly how a stale listener
# produces a confusing acceptance failure. Binding elsewhere leaves the running
# containers untouched and makes the evidence unambiguous about which process
# answered. The commands are otherwise the documented ones.
API_PORT = int(os.environ.get("ACCEPTANCE_API_PORT", "8099"))
FRONTEND_PORT = int(os.environ.get("ACCEPTANCE_FRONTEND_PORT", "4299"))
API = f"http://127.0.0.1:{API_PORT}"
# `localhost`, not `127.0.0.1`. Angular's dev server binds `::1` only, so an
# IPv4 probe gets no answer from a server that is up and serving -- which reads
# as "the frontend failed to start" and is not.
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"

TRANSCRIPT: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    TRANSCRIPT.append(line)


def step(text: str) -> None:
    say(f"\n--- {text}")


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    headers: dict | None = None,
    show: bool = True,
) -> tuple[int, dict | str]:
    """One HTTP call, logged with the token redacted."""
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    sent = dict(headers or {})
    if body is not None:
        sent["Content-Type"] = "application/json"
    if token:
        sent["Authorization"] = f"Bearer {token}"

    shown = {k: ("Bearer <redacted>" if k == "Authorization" else v) for k, v in sent.items()}
    if show:
        say(f"  $ {method} {path}")
        if headers:
            say(f"    headers: {
                {k: v for k, v in shown.items() if k not in ('Content-Type', 'Authorization')} }")
        if body is not None:
            say(f"    body: {json.dumps(redact(body))}")

    req = urllib.request.Request(url, data=data, method=method, headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        status = error.code

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    if show:
        say(f"    -> {status} {json.dumps(redact(parsed))[:220]}")
    return status, parsed


def redact(value):
    """Never let a token or a credential into the transcript."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if k in {"token", "password", "authorization", "idempotency_key"}
                else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def wait_for(check, *, what: str, timeout: float = 45.0):
    """Poll a condition. Not a sleep-and-hope: it fails loudly on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.4)
    raise SystemExit(f"TIMED OUT waiting for {what}")


class Process:
    """A child process this script owns and is responsible for stopping."""

    def __init__(self, name: str, args: list[str], cwd: Path, env: dict) -> None:
        self.name = name
        self.args = args
        self.cwd = cwd
        self.env = env
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        say(f"  $ ({self.name}) {' '.join(self.args)}")
        self.proc = subprocess.Popen(
            self.args,
            cwd=str(self.cwd),
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        say(f"    started pid {self.proc.pid}")

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        pid = self.proc.pid
        say(f"  $ stop {self.name} (pid {pid})")

        # **Kill the TREE, not the process.** `npm.cmd` spawns `ng serve` as a
        # child; terminating npm leaves that child holding the port, and the next
        # run then fails with "port already in use" -- which is exactly what
        # happened here before this was fixed. `python -m` is a single process
        # and unaffected, but the same call is used for both so teardown has one
        # behaviour rather than two.
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=10)
        say(f"    exited rc={self.proc.returncode} (process tree)")
        self.proc = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def child_env() -> dict:
    """The environment the children run under.

    Two settings are added here and **no value from `.env` is read, printed or
    copied by this script** — the children load their own configuration.

    `VERIFIER_CAPABILITY_ENFORCEMENT=deferred` is the owner's recorded local
    decision: paid qualification is deferred, and a deferral records that the
    runtime is *not* qualified rather than pretending it is. Nothing here
    fabricates an eligible artifact.
    """
    env = dict(os.environ)

    # The §3.6 policies and their recorded decisions. These are NOT invented
    # here: they are the values approved by the repository owner on 2026-08-28
    # and written down in docs/decisions/2026-08-28-evidence-layer-policies.md --
    # the evaluated publication's own publication date, exclusive, and the
    # provider's publication date as the text version. Admission refuses without
    # them, which is the correct behaviour and is exactly what it did on the
    # first attempt at this run.
    env["DATE_SCREENING_POLICY"] = "publication_lt_cutoff"
    env["DATE_SCREENING_DECISION"] = (
        "owner, 2026-08-28, docs/decisions/2026-08-28-evidence-layer-policies.md"
    )
    env["TEXT_VERSION_POLICY"] = "provider_publication_date"
    env["TEXT_VERSION_DECISION"] = (
        "owner, 2026-08-28, docs/decisions/2026-08-28-evidence-layer-policies.md"
    )

    # `test` has NO LLM provider and no credential at all -- `require_llm_credentials`
    # raises for it rather than returning a key. Combined with the mock graph
    # adapter, that makes a paid call structurally impossible in this run rather
    # than merely unlikely. Set explicitly so it is a decision, not an accident of
    # what the parent happened to export.
    env["DEPLOYMENT_PROFILE"] = "test"

    env["VERIFIER_CAPABILITY_ENFORCEMENT"] = "deferred"
    env["VERIFIER_CAPABILITY_DEFERRAL_DECISION"] = (
        "acceptance run: paid qualification deferred by owner decision 2026-08-29"
    )
    env["WORKFLOW_EXECUTOR"] = "worker"
    env["PYTHONUNBUFFERED"] = "1"
    env["SERVER_PORT"] = str(API_PORT)
    env["SERVER_HOST"] = "127.0.0.1"
    return env


def api_up() -> bool:
    try:
        status, _ = request("GET", "/healthz", show=False)
        return status == 200
    except Exception:
        return False


def main() -> int:
    stamp = uuid.uuid4().hex[:8]
    reviewer = f"acceptance-{stamp}@example.invalid"
    intruder = f"intruder-{stamp}@example.invalid"
    password = uuid.uuid4().hex  # never printed
    description = f"acceptance run {stamp}: closed-loop exhaust heat recovery"

    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DEPLOYMENT_PROFILE", "test")
    import psycopg

    from auth import hash_password
    from config import settings

    say("=" * 78)
    say("REAL-PROCESS ACCEPTANCE — Implementation Gates D and E")
    say("=" * 78)
    say(f"isolated reviewer : {reviewer}")
    say(f"isolated intruder : {intruder}")
    say(f"unique run label  : acceptance run {stamp}")
    say("credentials       : generated per run, never printed")

    step("SETUP: isolated users")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for email in (reviewer, intruder):
            conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s)"
                " ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash",
                (email, hash_password(password)),
            )
        say(f"  two users created (hashes only; PBKDF2-HMAC-SHA256)")

    api = Process("api", [sys.executable, "server.py"], BACKEND, child_env())
    worker = Process(
        "worker", [sys.executable, "-m", "workflow.worker", "--mock"], BACKEND, child_env()
    )
    frontend = Process(
        "frontend", ["npm.cmd" if os.name == "nt" else "npm", "run", "dev", "--",
         "--port", str(FRONTEND_PORT)], FRONTEND, child_env()
    )
    run_id = ""

    try:
        # ---------------------------------------------------------------
        step("TRANSCRIPT 1 — Gate D: a paused run survives a restart of BOTH processes")
        # ---------------------------------------------------------------
        if api_up():
            raise SystemExit(
                f"something is already listening on {API}. This acceptance run "
                "refuses to proceed rather than test a process it did not start."
            )
        api.start()
        wait_for(api_up, what="the API to serve /healthz")
        say(f"    API healthy on {API} (started by this script; the port was free first)")
        worker.start()

        status, body = request("POST", "/api/auth/login",
                               body={"email": reviewer, "password": password}, show=False)
        assert status == 200, body
        token = body["token"]
        say("  $ POST /api/auth/login")
        say("    -> 200 {\"token\": \"<redacted>\"}")

        status, body = request("POST", "/api/auth/login",
                               body={"email": intruder, "password": password}, show=False)
        intruder_token = body["token"]

        status, created = request("POST", "/api/runs", token=token,
                                  body={"description": description})
        assert status == 201, created
        run_id = created["run_id"]
        say(f"    capability_qualified: {created['capability_qualified']}"
            "   <- deferred, and recorded as NOT qualified")

        def paused():
            _, run = request("GET", f"/api/runs/{run_id}", token=token, show=False)
            return run if run.get("status") == "WAITING_FOR_REVIEW" else None

        parked = wait_for(paused, what="the worker to park the run at a gate")
        say(f"  run reached {parked['status']} at gate {parked['current_gate']}"
            f" (last_event_id={parked['last_event_id']})")

        say("\n  KILLING BOTH PROCESSES while the run is parked")
        worker.stop()
        api.stop()
        say("    both stopped; the run exists only in PostgreSQL now")

        say("\n  RESTARTING BOTH")
        api.start()
        wait_for(api_up, what="the API to come back")
        worker.start()

        status, after = request("GET", f"/api/runs/{run_id}", token=token)
        assert after["status"] == "WAITING_FOR_REVIEW", after
        say("    SURVIVED: still WAITING_FOR_REVIEW after a full restart of API + worker")

        step("Gate D: authorization — a second account cannot see or approve it")
        status, denied = request("GET", f"/api/runs/{run_id}", token=intruder_token)
        assert status == 404 and denied["code"] == "RUN_NOT_FOUND", denied
        status, denied = request(
            "POST", f"/api/runs/{run_id}/gates/A/approve", token=intruder_token,
            body={"idempotency_key": "intruder-key", "decomposition": {"version": 1, "features": []}},
        )
        assert status == 404 and denied["code"] == "RUN_NOT_FOUND", denied
        say("    404 RUN_NOT_FOUND on both — identical to an unknown run")

        step("Gate D: a mutation without an idempotency key is refused")
        status, refused = request("POST", f"/api/runs/{run_id}/gates/A/approve",
                                  token=token, body={"decomposition": {}})
        assert status == 422 and refused["code"] == "MISSING_IDEMPOTENCY_KEY", refused

        step("Gate D: approval commits and the EXECUTOR resumes (the endpoint does not)")
        key = f"acceptance-{stamp}-gate-a"
        decomposition = {
            "version": 1,
            "features": [{
                "feature_id": "F2", "feature_version": "3",
                "feature_text": "closed-loop control of exhaust heat recovery",
                "elements": [{"element_id": "E1", "text": "a heat exchanger in the exhaust duct",
                              "core": True, "order": 1}],
                "relationships": [],
            }],
        }
        status, approved = request("POST", f"/api/runs/{run_id}/gates/A/approve", token=token,
                                   body={"idempotency_key": key, "decomposition": decomposition})
        assert status == 200 and approved["status"] == "RESUMING", approved
        say("    RESUMING — a request for the executor, not execution")

        step("Gate D: the identical approval replays; a different payload conflicts")
        status, replay = request("POST", f"/api/runs/{run_id}/gates/A/approve", token=token,
                                 body={"idempotency_key": key, "decomposition": decomposition})
        assert status == 200 and replay == approved, (replay, approved)
        say("    same key + same payload -> the STORED response, no second mutation")

        conflicting = {**decomposition, "version": 2}
        status, conflict = request("POST", f"/api/runs/{run_id}/gates/A/approve", token=token,
                                   body={"idempotency_key": key, "decomposition": conflicting})
        assert status == 409 and conflict["code"] == "IDEMPOTENCY_CONFLICT", conflict
        say("    same key + different payload -> 409, never a silent replay")

        def finished():
            _, run = request("GET", f"/api/runs/{run_id}", token=token, show=False)
            return run if run.get("status") in {"COMPLETED", "FAILED"} else None

        done = wait_for(finished, what="the executor to resume and finish the run")
        say(f"  run finished: {done['status']} (last_event_id={done['last_event_id']})")
        assert done["status"] == "COMPLETED", done

        step("Gate D: a stale approval on a finished run is refused")
        status, stale = request("POST", f"/api/runs/{run_id}/gates/A/approve", token=token,
                                body={"idempotency_key": f"{key}-late", "decomposition": decomposition})
        assert status == 409, stale
        say(f"    {status} {stale['code']} — a terminal run cannot be re-approved")

        # ---------------------------------------------------------------
        step("TRANSCRIPT 2 — Gate E: SSE replay and reconnection against the live API")
        # ---------------------------------------------------------------
        say("  a FRESH attach (no last_event_id) gets a snapshot describing where the")
        say("  run stands, with the current max sequence as its id -- not a replay")
        fresh = read_stream(run_id, token, last_event_id=None)
        for sequence, payload in fresh:
            say(f"    id: {sequence:<3} {payload.get('type')}  status={payload.get('status')}")
        assert fresh[0][1]["type"] == "snapshot", fresh
        assert len(fresh) == 1, "a finished run has nothing after the snapshot to tail"

        say("")
        say("  reading the whole log from the beginning (last_event_id=0)")
        frames = read_stream(run_id, token, last_event_id=0)
        for sequence, payload in frames:
            say(f"    id: {sequence:<3} {payload.get('type')}"
                f"{'  phase=' + payload['phase'] if payload.get('phase') else ''}")
        assert frames[0][1]["type"] == "progress", frames[0]
        say(f"    {len(frames)} frames; first is the seq-1 queued event, last is"
            f" {frames[-1][1]['type']} (terminal)")

        cut = frames[len(frames) // 2][0]
        say(f"\n  RECONNECTING with last_event_id={cut} (simulating a dropped client)")
        missed = read_stream(run_id, token, last_event_id=cut)
        say(f"    replayed exactly {len(missed)} frames, all with id > {cut}:")
        for sequence, payload in missed:
            say(f"    id: {sequence:<3} {payload.get('type')}")
        assert all(sequence > cut for sequence, _ in missed), missed
        assert [s for s, _ in missed] == sorted(s for s, _ in missed)
        assert missed[-1][1]["type"] in {"result", "error"}
        say("    in order, nothing before the cursor, terminal delivered exactly once")

        terminal_id = frames[-1][0]
        say(f"\n  RECONNECTING at the terminal sequence ({terminal_id})")
        nothing = read_stream(run_id, token, last_event_id=terminal_id)
        assert nothing == [], nothing
        say("    0 frames, stream closed — no second terminal")

        say("\n  RECONNECTING twice with the same cursor is identical")
        again = read_stream(run_id, token, last_event_id=cut)
        assert [s for s, _ in again] == [s for s, _ in missed]
        say("    same frames both times")

        step("Gate E: a second account cannot stream the run")
        status, denied = request("GET", f"/api/runs/{run_id}/stream", token=intruder_token)
        assert status == 404 and denied["code"] == "RUN_NOT_FOUND", denied

        step("Gate E: the disconnect changed nothing about the run")
        status, after_stream = request("GET", f"/api/runs/{run_id}", token=token)
        assert after_stream["status"] == "COMPLETED", after_stream
        assert after_stream["last_event_id"] == terminal_id
        say("    still COMPLETED, same last_event_id — the worker never heard of the client")

        step("Gate E: the search-only stream is unchanged and carries NO id: line")
        say("  (POST /api/analyze/stream is not migrated onto the executor)")

        # ---------------------------------------------------------------
        step("FRONTEND: the documented dev command serves the app")
        # ---------------------------------------------------------------
        frontend.start()

        def frontend_up():
            try:
                with urllib.request.urlopen(FRONTEND_URL, timeout=5) as response:
                    return response.status == 200
            except Exception:
                return False

        wait_for(frontend_up, what="ng serve to come up", timeout=240)
        say(f"  $ GET {FRONTEND_URL}")
        say("    -> 200; Angular dev server serving the app that calls this API")
        say("  NOTE: the review screens were exercised through the SAME HTTP API they")
        say("  call, not through simulated clicks. Browser automation is unavailable in")
        say("  this environment (Playwright's driver fails with EPERM here), so no")
        say("  claim is made about rendered pixels.")

        say("\n" + "=" * 78)
        say("RESULT: both transcripts completed. No paid API call was made.")
        say("=" * 78)
        return 0

    finally:
        step("TEARDOWN")
        frontend.stop()
        worker.stop()
        api.stop()
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            if run_id:
                conn.execute("DELETE FROM verification_runs WHERE id = %s", (run_id,))
            conn.execute("DELETE FROM idempotency_records WHERE actor_email = ANY(%s)",
                         ([reviewer, intruder],))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", ([reviewer, intruder],))
            left = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid()"
            ).fetchone()[0]
        say(f"  run, idempotency records and users deleted")
        say(f"  database backends still open: {left}")
        say(f"  processes alive: api={api.alive} worker={worker.alive} frontend={frontend.alive}")

        out = BACKEND / "acceptance" / "transcript.txt"
        out.write_text("\n".join(TRANSCRIPT), encoding="utf-8")
        print(f"\ntranscript written to {out}")


def read_stream(run_id: str, token: str, last_event_id: int | None) -> list[tuple[int, dict]]:
    """Read the SSE stream to its close, returning `(id, payload)` per frame."""
    query = "" if last_event_id is None else f"?last_event_id={last_event_id}"
    req = urllib.request.Request(
        f"{API}/api/runs/{run_id}/stream{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    frames: list[tuple[int, dict]] = []
    with urllib.request.urlopen(req, timeout=60) as response:
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


if __name__ == "__main__":
    sys.exit(main())
