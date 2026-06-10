"""Symbol registry seeding — turns ``settings.all_symbols`` into ``symbols`` rows.

The registry (``symbols`` table) is the FK target for every other table, so
it must be seeded before any candle/indicator/signal write. Classification
is a simple bootstrap heuristic per the architecture's ``core | alt | meme``
tiers: configured ``core_symbols`` are ``"core"``, a curated set of
high-profile meme coins is ``"meme"``, everything else is ``"alt"``.
"""

from __future__ import annotations

from ..config import Settings
from ..db.models import SymbolRow
from ..db.repository import SymbolRepository
from ..feeds.normalize import normalize_symbol


def build_symbol_rows(settings: Settings) -> list[SymbolRow]:
    """Build one :class:`SymbolRow` per configured symbol (core + seed).

    Classification: configured ``core_symbols`` are ``"core"`` (Tier 0), the
    curated ``meme_symbols`` set is ``"meme"`` (Tier 1), everything else is
    ``"alt"`` (Tier 1) — the Symbol Manager (M3) refines tiers/classes for the
    full discovered universe from here.
    """
    core = {normalize_symbol(s) for s in settings.core_symbols}
    meme = {normalize_symbol(s) for s in settings.meme_symbols}
    rows: list[SymbolRow] = []
    for pair in settings.all_symbols:
        base = normalize_symbol(pair)
        if base in core:
            klass, tier = "core", 0
        elif base in meme:
            klass, tier = "meme", 1
        else:
            klass, tier = "alt", 1
        rows.append(
            SymbolRow(
                symbol=pair.upper(),
                base=base,
                klass=klass,
                tier=tier,
                tags=[klass],
            )
        )
    return rows


async def seed_symbols(symbol_repo: SymbolRepository, settings: Settings) -> dict[str, int]:
    """Upsert every configured symbol, returning a ``base -> symbols.id`` map."""
    return await symbol_repo.ensure_seeded(build_symbol_rows(settings))
