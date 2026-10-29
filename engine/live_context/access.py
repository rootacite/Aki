"""Look up live_context package attributes so tests can patch them."""

from __future__ import annotations


def pkg():
    import engine.live_context as package

    return package
