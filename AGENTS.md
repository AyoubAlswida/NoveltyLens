# AGENTS.md

This file exists so Codex (and any other tool that reads the `AGENTS.md`
convention) picks up the same ground rules as Claude Code, which reads
`CLAUDE.md`.

**Read `CLAUDE.md` in full before making any change in this repository.**
It is the single source of truth for:

- what this project is, and how the two processes (backend/frontend) are run
- the current architecture, and the LangGraph migration plan in `docs/`
- the SSE event contract between `backend/server.py` and
  `frontend/src/app/App.tsx` — the most fragile cross-cutting constraint in
  this codebase
- the zero-tolerance failure policy: no fallback, no fabricated or
  degraded-but-unlabeled output, ever — a failing step must raise a clear
  error instead
- what's explicitly out of scope (USPTO — no API access)

This file intentionally stays a pointer, not a second copy of `CLAUDE.md`.
Keeping the architecture description in exactly one place is deliberate:
`CLAUDE.md` already carries an instruction to update itself in the same edit
that changes the architecture it describes, and maintaining that promise
across two full copies invites drift. If you're working from this file and
land on something `CLAUDE.md` needs to reflect, update `CLAUDE.md` itself.

## Tool-specific notes for Codex

Nothing Codex-specific yet. Add notes here only for things that are actually
about Codex's own behavior (flags, sandboxing quirks, tool-specific
conventions) — not project architecture, which belongs in `CLAUDE.md`.
