from __future__ import annotations

from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .search_intent_v218 import acceptable, query_variants, relevance_score

_PATCHED = False


def _sale_price(row: dict[str, Any]) -> float | None:
    sale = row.get("sale_price")
    if isinstance(sale, dict):
        amount = sale.get("amount") or sale.get("regular_amount")
        if isinstance(amount, (int, float)) and amount > 0:
            return float(amount)
    value = row.get("price")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _listing_to_result(provider: MercadoLivreProvider, row: dict[str, Any]) -> dict[str, Any] | None:
    item_id = str(row.get("id") or "")
    title = str(row.get("title") or row.get("name") or "").strip()
    price = _sale_price(row)
    if not item_id or not title or price is None:
        return None

    attrs = row.get("attributes") or []
    seller = row.get("seller") if isinstance(row.get("seller"), dict) else {}
    seller_name = seller.get("nickname") if seller else None
    if not seller_name and row.get("seller_id"):
        seller_name = f"Vendedor #{row.get('seller_id')}"

    catalog_id = row.get("catalog_product_id")
    external_id = str(catalog_id or item_id)
    sale = row.get("sale_price") if isinstance(row.get("sale_price"), dict) else {}
    regular = sale.get("regular_amount") if isinstance(sale, dict) else None
    original = row.get("original_price") or regular

    return {
        "external_product_id": external_id,
        "catalog_product_id": str(catalog_id) if catalog_id else None,
        "item_id": item_id,
        "name": title,
        "brand": provider._attr(attrs, "BRAND"),
        "model": provider._attr(attrs, "MODEL"),
        "gtin": provider._attr(attrs, "GTIN", "EAN", "UPC"),
        "image_url": row.get("secure_thumbnail") or row.get("thumbnail"),
        "url": row.get("permalink"),
        "price": price,
        "pix_price": price,
        "original_price": float(original) if isinstance(original, (int, float)) and original > price else None,
        "currency": row.get("currency_id") or "BRL",
        "seller_name": seller_name,
        "shipping_free": (row.get("shipping") or {}).get("free_shipping") if isinstance(row.get("shipping"), dict) else None,
        "available": True,
        "price_source": "marketplace_listing_search",
    }


def enable_listing_search_v219() -> None:
    """Prefer real marketplace listings for shopper searches.

    Catalog search is excellent for identity, but for broad consumer language it
    can return no buyable offer. The marketplace listing search is broader and
    already contains a real listing URL and displayed price, so terms like
    'máquina de lavar 17kg' work much more naturally.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    previous_search = MercadoLivreProvider.search

    def search(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 12))
        merged: dict[str, dict[str, Any]] = {}
        had_successful_call = False

        for variant in query_variants(query)[:4]:
            try:
                payload = self._request(
                    "GET",
                    "/sites/MLB/search",
                    params={"q": variant, "limit": 50},
                )
                had_successful_call = True
            except MercadoLivreError:
                continue

            for raw in payload.get("results") or []:
                title = str(raw.get("title") or raw.get("name") or "")
                if not acceptable(query, title):
                    continue
                detail = _listing_to_result(self, raw)
                if not detail:
                    continue
                score = relevance_score(query, title)
                key = str(detail.get("catalog_product_id") or detail.get("item_id") or detail.get("external_product_id"))
                previous = merged.get(key)
                if previous is None:
                    detail["_score"] = score
                    merged[key] = detail
                    continue
                old_score = float(previous.get("_score") or -999)
                old_price = float(previous.get("price") or 10**18)
                if score > old_score or (score >= old_score - 3 and float(detail.get("price") or 10**18) < old_price):
                    detail["_score"] = score
                    merged[key] = detail

            if len(merged) >= max(limit, 8):
                break

        rows = list(merged.values())
        rows.sort(
            key=lambda x: (float(x.get("_score") or -999), -(float(x.get("price") or 10**18))),
            reverse=True,
        )
        for row in rows:
            row.pop("_score", None)
        if rows:
            return rows[:limit]

        # Keep the catalog-based implementation as a fallback in case Mercado
        # Livre changes the generic listing search behavior for this account.
        try:
            return previous_search(self, query, limit=limit)
        except Exception:
            if had_successful_call:
                return []
            raise

    MercadoLivreProvider.search = search
