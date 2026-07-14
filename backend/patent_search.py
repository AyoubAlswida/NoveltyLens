"""Backward-compatible facade for the refactored search layer.

New code should import from ``search.search`` directly.
"""

from search.search import PatentSearchError, search_patents

__all__ = ["PatentSearchError", "search_patents"]
