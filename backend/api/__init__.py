"""HTTP surface for verified runs (Implementation Gate E).

Kept as its own package rather than added to `server.py` for one reason that is
structural rather than tidy: **nothing in here may reach an executor-only read.**
`workflow/store.py` marks those `_system_*` because they skip ownership scoping --
no human is behind the call -- and `tests/test_run_ownership.py` asserts the HTTP
surface never reaches one. A package boundary makes that assertion something a
grep can check.

`server.py` keeps its existing endpoints untouched. `POST /api/analyze`,
`POST /api/analyze/stream`, `GET`/`DELETE /api/history` and the auth endpoints
behave exactly as they did, are not migrated onto the executor, and their SSE
frames carry no `id:` line (spec section 3.2).
"""
