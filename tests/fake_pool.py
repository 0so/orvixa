"""A fake :class:`~orvixa.db.pool.DBPool` for repository tests — no database.

Records every call and lets tests script ``fetch``/``fetchrow``/``fetchval``
return values, mirroring the in-memory fake pattern M1 used for the WebSocket
connector.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class FakePool:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.executemany_calls: list[tuple[str, list[Sequence[Any]]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []

        self.fetch_return: list[dict[str, Any]] = []
        self.fetch_routes: dict[str, list[dict[str, Any]]] = {}
        self.fetchrow_return: dict[str, Any] | None = None
        self.fetchval_return: Any = None

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "OK"

    async def executemany(self, query: str, args: Sequence[Sequence[Any]]) -> None:
        self.executemany_calls.append((query, list(args)))

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        for substring, result in self.fetch_routes.items():
            if substring in query:
                return result
        return self.fetch_return

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_return

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.fetchval_calls.append((query, args))
        return self.fetchval_return

    async def close(self) -> None:
        pass
