from __future__ import annotations

import time
from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .mercadolivre_search_enhancement import (
    _acceptable,
    _catalog_search,
    _domain_for_query,
    _family,
    _generation,
    _norm,
    _query_has_generation,
    _score,
)

_PATCHED = False
_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
CACHE_TTL_SECONDS = 240


def enable_fast_search() -> None:
    """Keep semantic ranking while capping expensive product-detail lookups.

    V2.10 ranked up to 100 catalog rows correctly but then opened 30-60
    detailed product endpoints sequentially. With a second marketplace this
    could leave the browser apparently stuck. Here we still rank a broad cheap
    catalog pool, but verify only the strongest handful and stop as soon as we
    have enough acceptable results.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def search_fast(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10))
        key = (_norm(query), limit)
        cached = _CACHE.get(key)
        if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
            return [dict(x) for x in cached[1]]

        expected_domain = _domain_for_query(query)
        raw = _catalog_search(self, query, expected_domain)
        fam = _family(query)
        newest_generation = None
        if fam and not _query_has_generation(query, fam):
            generations = [_generation(item.get("name"), fam) for item in raw]
            generations = [g for g in generations if g is not None]
            if generations:
                newest_generation = max(generations)

        ranked = sorted(
            raw[:100],
            key=lambda item: _score(
                query,
                item.get("name"),
                domain_id=item.get("domain_id"),
                brand=self._attr(item.get("attributes") or [], "BRAND"),
                model=self._attr(item.get("attributes") or [], "MODEL"),
                newest_generation=newest_generation,
            ),
            reverse=True,
        )

        # Usually 8-12 detail calls are enough after the cheap semantic pass.
        # The old implementation could do 30-60 calls per keystroke/search.
        candidate_cap = min(len(ranked), max(limit + 4, 10))
        detailed: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in ranked[:candidate_cap]:
            product_id = item.get("id")
            if not product_id or str(product_id) in seen:
                continue
            seen.add(str(product_id))
            try:
                detail = self.product_detail(product_id)
            except MercadoLivreError:
                attrs = item.get("attributes") or []
                detail = {
                    "external_product_id": product_id,
                    "name": item.get("name") or product_id,
                    "brand": self._attr(attrs, "BRAND"),
                    "model": self._attr(attrs, "MODEL"),
                    "gtin": self._attr(attrs, "GTIN", "EAN", "UPC"),
                    "image_url": self._image(item),
                    "url": item.get("permalink"),
                    "item_id": None,
                    "price": None,
                    "original_price": None,
                    "currency": "BRL",
                    "seller_name": None,
                    "shipping_free": None,
                    "available": True,
                    "domain_id": item.get("domain_id"),
                }

            detail.setdefault("domain_id", item.get("domain_id"))
            if not _acceptable(query, detail, expected_domain):
                continue

            detail["_relevance"] = _score(
                query,
                detail.get("name"),
                domain_id=detail.get("domain_id"),
                brand=detail.get("brand"),
                model=detail.get("model"),
                newest_generation=newest_generation,
            )
            detail["_generation"] = _generation(detail.get("name"), fam) if fam else None
            detailed.append(detail)
            if len(detailed) >= limit:
                break

        detailed.sort(
            key=lambda d: (
                d.get("_relevance", -100),
                d.get("_generation") or -1,
                d.get("price") is not None,
            ),
            reverse=True,
        )
        result = detailed[:limit]
        for item in result:
            item.pop("_relevance", None)
            item.pop("_generation", None)

        _CACHE[key] = (time.time(), [dict(x) for x in result])
        return result

    MercadoLivreProvider.search = search_fast
