from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .mercadolivre_search_enhancement import (
    _acceptable,
    _domain_for_query,
    _family,
    _generation,
    _norm,
    _query_has_generation,
    _score,
)

_PATCHED = False


def _raw_detail(provider: MercadoLivreProvider, product_id: str) -> dict[str, Any]:
    """Fetch one catalog product without triggering the expensive fallback chain.

    Search needs to stay responsive. We therefore read /products/{id} directly
    and use its Buy Box when available. Deeper item verification remains reserved
    for tracking/refresh, where accuracy matters more than latency.
    """
    data = provider._request("GET", f"/products/{product_id}")
    attrs = data.get("attributes") or []
    winner = data.get("buy_box_winner") or {}
    seller_id = winner.get("seller_id")
    seller_name = None
    seller = winner.get("seller")
    if isinstance(seller, dict):
        seller_name = seller.get("nickname")
    if not seller_name and seller_id:
        seller_name = f"Vendedor #{seller_id}"

    pictures = data.get("pictures") or []
    image = None
    if pictures:
        image = pictures[0].get("secure_url") or pictures[0].get("url")
    image = image or data.get("thumbnail")

    return {
        "external_product_id": data.get("id") or product_id,
        "name": data.get("name") or data.get("family_name") or product_id,
        "brand": provider._attr(attrs, "BRAND"),
        "model": provider._attr(attrs, "MODEL"),
        "gtin": provider._attr(attrs, "GTIN", "EAN", "UPC"),
        "image_url": image,
        "url": data.get("permalink"),
        "item_id": winner.get("item_id"),
        "price": winner.get("price"),
        "original_price": winner.get("original_price"),
        "currency": winner.get("currency_id") or "BRL",
        "seller_name": seller_name,
        "shipping_free": (winner.get("shipping") or {}).get("free_shipping"),
        "available": bool(winner) and winner.get("price") not in (None, 0),
        "domain_id": data.get("domain_id"),
        "price_source": "search_buy_box" if winner.get("price") else "catalog_only",
    }


def enable_stable_search() -> None:
    """Override ML search with a bounded, low-call implementation.

    The previous search called the fully enriched product_detail() for many
    candidates. That routine may inspect several listings per catalog product,
    so an ordinary query could exceed the web request timeout. Here we do one
    catalog search, rank cheaply, then fetch only a few product pages in parallel.
    Even when a detail call fails, the catalog result is still returned instead
    of turning the whole marketplace into zero results.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def search_stable(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10))
        expected_domain = _domain_for_query(query)
        params: dict[str, Any] = {
            "status": "active",
            "site_id": "MLB",
            "q": query,
            "limit": 40,
        }
        if expected_domain:
            params["domain_id"] = expected_domain

        try:
            payload = self._request("GET", "/products/search", params=params)
            raw = payload.get("results") or []
            if not raw and expected_domain:
                params.pop("domain_id", None)
                payload = self._request("GET", "/products/search", params=params)
                raw = payload.get("results") or []
        except MercadoLivreError:
            raise

        fam = _family(query)
        newest_generation = None
        if fam and not _query_has_generation(query, fam):
            gens = [_generation(row.get("name"), fam) for row in raw]
            gens = [g for g in gens if g is not None]
            if gens:
                newest_generation = max(gens)

        def raw_score(row: dict[str, Any]) -> float:
            return _score(
                query,
                row.get("name"),
                domain_id=row.get("domain_id"),
                brand=self._attr(row.get("attributes") or [], "BRAND"),
                model=self._attr(row.get("attributes") or [], "MODEL"),
                newest_generation=newest_generation,
            )

        ranked = sorted(raw[:80], key=raw_score, reverse=True)
        candidates: list[dict[str, Any]] = []
        for row in ranked:
            pseudo = {
                "name": row.get("name"),
                "domain_id": row.get("domain_id"),
            }
            if _acceptable(query, pseudo, expected_domain):
                candidates.append(row)
            if len(candidates) >= max(limit, 8):
                break

        if not candidates:
            return []

        by_id = {str(row.get("id")): row for row in candidates if row.get("id")}
        details: dict[str, dict[str, Any]] = {}
        workers = min(6, len(by_id))
        if workers:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_raw_detail, self, pid): pid for pid in by_id}
                for future in as_completed(futures):
                    pid = futures[future]
                    try:
                        details[pid] = future.result()
                    except Exception:
                        pass

        results: list[dict[str, Any]] = []
        for row in candidates:
            pid = str(row.get("id") or "")
            detail = details.get(pid)
            if not detail:
                attrs = row.get("attributes") or []
                detail = {
                    "external_product_id": pid,
                    "name": row.get("name") or pid,
                    "brand": self._attr(attrs, "BRAND"),
                    "model": self._attr(attrs, "MODEL"),
                    "gtin": self._attr(attrs, "GTIN", "EAN", "UPC"),
                    "image_url": self._image(row),
                    "url": row.get("permalink"),
                    "item_id": None,
                    "price": None,
                    "original_price": None,
                    "currency": "BRL",
                    "seller_name": None,
                    "shipping_free": None,
                    "available": True,
                    "domain_id": row.get("domain_id"),
                    "price_source": "catalog_only",
                }
            detail.setdefault("domain_id", row.get("domain_id"))
            detail["_score"] = _score(
                query,
                detail.get("name"),
                domain_id=detail.get("domain_id"),
                brand=detail.get("brand"),
                model=detail.get("model"),
                newest_generation=newest_generation,
            )
            results.append(detail)

        results.sort(
            key=lambda row: (
                row.get("_score", -100),
                row.get("price") is not None,
            ),
            reverse=True,
        )
        for row in results:
            row.pop("_score", None)
        return results[:limit]

    MercadoLivreProvider.search = search_stable
