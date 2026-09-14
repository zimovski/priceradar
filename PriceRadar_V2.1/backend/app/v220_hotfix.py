from __future__ import annotations

from typing import Any

from .providers.magalu_reader_v220 import magalu_reader_search
from .providers.magalu_web import MagaluError
from .v219_accuracy import _magalu_search_v219
from . import v220_reliability as v220


def _magalu_search_fast(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Prefer rendered Magalu data and avoid waiting on a blocked storefront fetch.

    In V2.20's first implementation the direct request and rendered fallback ran
    in a ThreadPoolExecutor. Exiting that executor waits for both tasks, so a
    blocked direct Magalu request could delay or time out a perfectly good
    rendered result. This hotfix returns rendered public data immediately and
    only attempts the old direct parser if the rendered path produces nothing.
    """
    errors: list[str] = []
    try:
        rows = magalu_reader_search(query, limit=limit)
        if rows:
            return rows
    except Exception as exc:
        errors.append(f"rendered: {type(exc).__name__}: {exc}")

    try:
        rows = _magalu_search_v219(query, limit=limit)
        if rows:
            return rows
    except Exception as exc:
        errors.append(f"direct: {type(exc).__name__}: {exc}")

    if errors:
        raise MagaluError(" | ".join(errors[-2:]))
    return []


# Route functions in v220_reliability resolve this name from their module globals
# at request time, so replacing it here fixes both normal searches and tracked-
# product refreshes without duplicating the API routes.
v220._magalu_search_v220 = _magalu_search_fast
