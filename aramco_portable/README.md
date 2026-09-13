# Novelty Lens Portable

A standalone Aramco-targeted edition of this project. It uses **only Python 3.13 standard-library modules**: no Docker, Node.js, FastAPI, LangGraph, PostgreSQL, Qdrant, or pip packages.

It provides a local browser interface, uses the MetaBrain chat and embedding endpoints, calls Lens only when the network permits it, and stores completed reports in `data/novelty_lens.db` through SQLite.

## Run on the Aramco computer

Copy this entire directory, then in PowerShell from its directory set the two credentials for the current session. Do not save keys in the source tree or paste them into a terminal command history.

```powershell
$env:ARAMCO_API_KEY = Read-Host "Paste MetaBrain key"
$env:LENS_API_KEY = Read-Host "Paste Lens key"
python .\app.py
```

Open `http://127.0.0.1:8000`.

`api.lens.org` is currently unresolved from the Aramco network. Until IT permits DNS plus HTTPS access to that host (or supplies an approved proxy), a search correctly fails rather than returning an incomplete report.

## Scope deliberately excluded

This portable first release has no sign-in, Angular frontend, advanced approval workflow, LangGraph checkpoints, Qdrant semantic index, or multi-user concurrency. SQLite history is local to the machine.

The original project is unchanged. This directory is a separate deployment target.
