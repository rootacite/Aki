"""Look up akictl package attributes so tests can patch them."""

from __future__ import annotations


def pkg():
    import engine.akictl as package

    return package
