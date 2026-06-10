"""Phase 2 web/API layer — a thin, read-only HTTP wrapper over the M2 repositories.

This package adds *no* analytics, signal, regime, or policy logic. Every
endpoint resolves a symbol and returns rows that already exist in the
database via :mod:`orvixa.db.repository` (queries only — no writes, no schema
changes). The core pipeline (`signal_validation.py`, `regime_validation.py`,
`policy_validation.py`) and the database schema are untouched.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
