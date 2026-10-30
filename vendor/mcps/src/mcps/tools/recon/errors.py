"""Errors for the recon public layer."""

from __future__ import annotations


class ReconError(ValueError):
    """Caller-facing recon failure (scope, bin, exec, path)."""


class ScopeError(ReconError):
    """Target is out of configured scope."""


class BinUnavailable(ReconError):
    """Required CLI is missing, wrong identity, or not executable."""
