from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .mercadolivre_search_enhancement import (
    _device_intent,
    _family,
    _generation,
    _looks_like_accessory,
    _norm,
    _query_wants_accessory,
    _score,
    _tokens,
)

_PATCHED = False


def _acceptable(query: str, name: str | None) -> bool:
    if not name:
        return False
    q_tokens = _tokens(query)
    words = set(_norm(name).split())
    if not q_tokens:
        return True
    if not _query_wants_accessory(query) and _device_intent(query) and _looks_like_accessory(name):
        return False
    for token in q_tokens:
        if any(ch.isdigit() for ch in token) and token not in words:
            return False
    coverage = sum(1 for token in q_tokens if token in words) / len(q_tokens)
    return coverage >= (0.67 if _device_intent(query) else 0.5)


def _seller_name(row: dict[str, Any]) -> str | None:
    seller = row.get("seller")
    if isinstance(seller, dict):
        nickname = seller.get("nickname")
        if nickname:
            return str(nickname)
        sid = seller.get("id")
        if sid:
            return f"Vendedor #{sid}"
    sid = row.get("seller_id")
    return f"Vendedor #{sid}" if sid else None


def _listing_result(provider: MercadoLivreProvider, row: dict[str, Any]) -> dict[str, Any] | None:
    item_id = str(row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    price = row.get("price")
    permalink = row.get("permalink")
    if not item_id or not title or price in (None, "") or not permalink:
        return None

    attrs = row.get("attributes") or []
    catalog_id = str(row.get("catalog_product_id") or "").strip() or None
    shipping = row.get("shipping") if isinstance(row.get("shipping"), dict) else {}
    qty = row.get("available_quantity")
    status = row.get("status")
    available = status in (None, "active")
    try:
        if qty is not None and int(qty) <= 0:
            available = False
    except (TypeError, ValueError):
        pass

    return {
        "external_product_id": catalog_id or f"item:{item_id}",
        "catalog_product_id": catalog_id,
        "item_id": item_id,
        "name": title,
        "brand": provider._attr(attrs, "BRAND"),
        "model": provider._attr(attrs, "MODEL"),
        "gtin": provider._attr(attrs, "GTIN", "EAN", "UPC"),
        "image_url": row.get("thumbnail") or row.get("secure_thumbnail"),
        "url": permalink,
        "price": float(price),
        "original_price": float(row["original_price"]) if row.get("original_price") else None,
        "currency": row.get("currency_id") or "BRL",
        "seller_name": _seller_name(row),
        "shipping_free": shipping.get("free_shipping") if shipping else None,
        "available": available,
        "price_source": "search_listing",
    }


def _rank_rows(query: str, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    fam = _family(query)
    newest = None
    if fam and _generation(query, fam) is None:
        gens = [_generation(r.get("title"), fam) for r in rows]
        gens = [g for g in gens if g is not None]
        if gens:
            newest = max(gens)

    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        title = row.get("title")
        if not _acceptable(query, title):
            continue
        score = _score(query, title, newest_generation=newest)
        ranked.append((score, row))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in ranked[: max(limit * 2, limit)]]


def _catalog_fallback(provider: MercadoLivreProvider, query: str, limit: int) -> list[dict[str, Any]]:
    data = provider._request("GET", "/products/search", params={
        "status": "active", "site_id": "MLB", "q": query, "limit": 20,
    })
    raw = data.get("results") or []
    raw.sort(key=lambda item: _score(
        query,
        item.get("name"),
        brand=provider._attr(item.get("attributes") or [], "BRAND"),
        model=provider._attr(item.get("attributes") or [], "MODEL"),
    ), reverse=True)
    selected = [x for x in raw if _acceptable(query, x.get("name"))][: min(5, limit)]
    if not selected:
        return []

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {
            pool.submit(provider.product_detail, str(item.get("id"))): item
            for item in selected if item.get("id")
        }
        for future in as_completed(futures):
            try:
                detail = future.result()
            except Exception:
                continue
            if detail and _acceptable(query, detail.get("name")):
                results.append(detail)
    results.sort(key=lambda x: _score(query, x.get("name")), reverse=True)
    return results[:limit]


def enable_fast_search() -> None:
    """Search actual Mercado Livre listings first.

    `/sites/MLB/search` already returns the live listing price and permalink, so
    one API call can produce useful search cards. This avoids opening many
    catalog products sequentially, which was the main reason V2.12 timed out on
    Render. If item search is unavailable, fall back to a very small parallel
    catalog verification pass instead of freezing the whole request.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def listing_detail(self: MercadoLivreProvider, item_id: str) -> dict[str, Any]:
        raw_id = str(item_id).removeprefix("item:")
        row = self._request("GET", f"/items/{raw_id}")
        if not isinstance(row, dict):
            raise MercadoLivreError("O Mercado Livre não retornou os dados do anúncio.")
        detail = _listing_result(self, row)
        if not detail:
            raise MercadoLivreError("O anúncio não possui preço e link públicos válidos.")
        return detail

    def search_fast(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 12))
        try:
            data = self._request("GET", "/sites/MLB/search", params={
                "q": query,
                "limit": min(50, max(20, limit * 5)),
            })
            raw = data.get("results") or []
            ranked = _rank_rows(query, raw, limit)
            results: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in ranked:
                detail = _listing_result(self, row)
                if not detail or not detail.get("available"):
                    continue
                key = str(detail.get("external_product_id") or detail.get("item_id"))
                if key in seen:
                    continue
                seen.add(key)
                results.append(detail)
                if len(results) >= limit:
                    break
            if results:
                return results
        except MercadoLivreError:
            pass

        return _catalog_fallback(self, query, limit)

    MercadoLivreProvider.listing_detail = listing_detail  # type: ignore[attr-defined]
    MercadoLivreProvider.search = search_fast
