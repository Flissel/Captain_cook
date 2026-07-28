"""Stable filesystem authority paths for production business benchmarks."""

from __future__ import annotations

from pathlib import Path


def canonical_business_benchmark_authority_root(workspace_root: Path) -> Path:
    """Return the run-independent private authority root for one workspace."""

    return (
        workspace_root.resolve()
        / ".captain-cook"
        / "private"
        / "business-benchmarks"
    )


__all__ = ["canonical_business_benchmark_authority_root"]
