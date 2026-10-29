"""Look up akid package attributes so tests can patch them."""

from __future__ import annotations


def pkg():
    import engine.akid as package

    return package
