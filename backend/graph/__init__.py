"""LangGraph implementation of the patent novelty-search pipeline.

Selected by ``PIPELINE=langgraph``; the legacy generator in ``backend/server.py``
remains the default and the parity reference.  See ``state.py`` for the schema
every node shares and ``events.py`` for the SSE progress contract.

Nodes in this package follow one rule above all others: **a node that cannot
produce a genuine result raises.**  No fallbacks, no placeholder output, no
partial success.  Failing the request is always preferable to answering it with
something untrue.
"""

from .events import PHASES, SSEContractError, emit_progress
from .state import GraphState

__all__ = ["PHASES", "GraphState", "SSEContractError", "emit_progress"]
