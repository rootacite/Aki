"""Look up channel_prompt package attributes so tests can patch them."""

from __future__ import annotations


def pkg():
    import engine.channel_prompt as package

    return package
