"""Durable workflow execution: the run state machine, the lease, the event log,
transactional idempotency, attempt telemetry, and the executor that drives them.

Read `CLAUDE.md`'s "Durable workflow execution" section before changing anything
here. Four rules govern the whole package and none of them is negotiable:

1. **At most one lease holder, and exactly one authorized writer at a time.**
   Every state transition and every event append carries a fencing token that the
   database revalidates under lock. A fenced worker persists nothing.
2. **External calls are at-least-once across recovery, never exactly-once.** A
   fenced worker's in-flight provider request still completes and is still
   billed. Guard the commit, record the attempt, disclose the duplicate read.
3. **There is no generic event writer.** Four functions write `workflow_events`,
   each declaring its own authority, and `result`/`error` come only from the
   finalizers, atomically with the status transition.
4. **No in-memory continuation.** If the lease store, the checkpointer or the
   event log is unreachable, the worker stops. It does not finish the graph in
   memory and it does not present success.

The public surface is deliberately narrow: `store` for run rows and ownership,
`events` for the log, `lease` for the fencing protocol, `idempotency` for
replay-safe mutations, `attempts` for telemetry, and `executor`/`worker` for the
process that runs it all.
"""
