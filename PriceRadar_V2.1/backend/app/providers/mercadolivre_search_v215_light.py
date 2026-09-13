from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .mercadolivre_reliable_v215 import _detail_base, _item_url, _num, _query_variant_ok
from .mercadolivre_search_enhancement import (
    _domain_for_query, _family, _generation, _query_has_generation, _score,
)

_PATCHED = False


def _search_detail(provider: MercadoLivreProvider, product_id: str) -> dict[str, Any] | None:
    """Resolve a search card with at most two API requests.

    Tracking/history uses the stricter V2.15 product_detail with bulk item
    verification. Search only needs a documented real offer + item id so it can
    remain responsive on Render.
    """
    detail, data = _detail_base(provider, product_id)
    winner = data.get("buy_box_winner") or {}
    winner_price = _num(winner.get("price"))
    winner_id = str(winner.get("item_id") or "")
    if winner_id and winner_price is not None:
        detail.update({
            "item_id": winner_id,
            "price": winner_price,
            "original_price": _num(winner.get("original_price")),
            "currency": winner.get("currency_id") or "BRL",
            "seller_name": f"Vendedor #{winner.get('seller_id')}" if winner.get("seller_id") else None,
            "shipping_free": (winner.get("shipping") or {}).get("free_shipping"),
            "available": True,
            "url": _item_url(winner_id) or detail.get("url"),
            "price_source": "search_buy_box",
        })
        return detail

    try:
        payload = provider._request("GET", f"/products/{product_id}/items", params={"limit": 12})
        rows = payload.get("results") or []
    except MercadoLivreError:
        return None

    candidates: list[dict[str, Any]] = []
    for row in rows:
        item_id = str(row.get("item_id") or "")
        price = _num(row.get("price"))
        condition = str(row.get("condition") or "").lower()
        if not item_id or price is None:
            continue
        if condition and condition not in {"new", "novo"}:
            continue
        candidates.append(row)
    if not candidates:
        return None

    candidates.sort(key=lambda row: float(row.get("price") or 10**18))
    row = candidates[0]
    item_id = str(row.get("item_id"))
    detail.update({
        "item_id": item_id,
        "price": _num(row.get("price")),
        "original_price": _num(row.get("original_price")),
        "currency": row.get("currency_id") or "BRL",
        "seller_name": f"Vendedor #{row.get('seller_id')}" if row.get("seller_id") else None,
        "shipping_free": (row.get("shipping") or {}).get("free_shipping") if isinstance(row.get("shipping"), dict) else None,
        "available": True,
        "url": _item_url(item_id) or detail.get("url"),
        "price_source": "search_catalog_offer",
    })
    return detail


def enable_light_search_v215() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def search(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 8))
        expected_domain = _domain_for_query(query)
        params: dict[str, Any] = {"status": "active", "site_id": "MLB", "q": query, "limit": 40}
        if expected_domain:
            params["domain_id"] = expected_domain
        payload = self._request("GET", "/products/search", params=params)
        raw = payload.get("results") or []
        if not raw and expected_domain:
            params.pop("domain_id", None)
            raw = (self._request("GET", "/products/search", params=params).get("results") or [])

        fam = _family(query)
        newest = None
        if fam and not _query_has_generation(query, fam):
            gens = [_generation(row.get("name"), fam) for row in raw]
            gens = [g for g in gens if g is not None]
            if gens:
                newest = max(gens)

        ranked = sorted(
            raw[:80],
            key=lambda row: _score(
                query,
                row.get("name"),
                domain_id=row.get("domain_id"),
                brand=self._attr(row.get("attributes") or [], "BRAND"),
                model=self._attr(row.get("attributes") or [], "MODEL"),
                newest_generation=newest,
            ),
            reverse=True,
        )
        candidates = [
            row for row in ranked
            if row.get("id") and _query_variant_ok(query, row.get("name"))
        ][: max(limit, 6)]
        if not candidates:
            return []

        details: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            futures = {
                pool.submit(_search_detail, self, str(row["id"])): str(row["id"])
                for row in candidates
            }
            for future in as_completed(futures):
                pid = futures[future]
                try:
                    detail = future.result()
                    if detail:
                        details[pid] = detail
                except Exception:
                    pass

        results: list[dict[str, Any]] = []
        for row in candidates:
            pid = str(row.get("id"))
            detail = details.get(pid)
            if not detail or detail.get("price") is None:
                continue
            detail["_score"] = _score(
                query,
                detail.get("name"),
                domain_id=detail.get("domain_id"),
                brand=detail.get("brand"),
                model=detail.get("model"),
                newest_generation=newest,
            )
            results.append(detail)

        results.sort(
            key=lambda row: (row.get("_score", -100), -(row.get("price") or 10**18)),
            reverse=True,
        )
        for row in results:
            row.pop("_score", None)
        return results[:limit]

    MercadoLivreProvider.search = search
