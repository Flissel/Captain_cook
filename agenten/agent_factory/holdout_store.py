"""Captain-owned private holdout storage port and deterministic test adapter."""

from __future__ import annotations

from typing import Protocol


class PrivateHoldoutStore(Protocol):
    def put(self, uri: str, body: str) -> None: ...
    def put_many(self, entries: tuple[tuple[str, str], ...]) -> None: ...


class InMemoryPrivateHoldoutStore:
    def __init__(self) -> None:
        self._bodies: dict[str, str] = {}

    def put(self, uri: str, body: str) -> None:
        existing = self._bodies.get(uri)
        if existing is not None and existing != body:
            raise ValueError("holdout URI already binds different content")
        self._bodies[uri] = body

    def put_many(self, entries: tuple[tuple[str, str], ...]) -> None:
        staged = dict(self._bodies)
        for uri, body in entries:
            existing = staged.get(uri)
            if existing is not None and existing != body:
                raise ValueError("holdout URI already binds different content")
            staged[uri] = body
        self._bodies = staged

    def get(self, uri: str) -> str:
        return self._bodies[uri]

    def __len__(self) -> int:
        return len(self._bodies)
