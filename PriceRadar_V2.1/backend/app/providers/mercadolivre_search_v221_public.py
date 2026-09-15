from __future__ import annotations

from typing import Any

import httpx

from .mercadolivre import MercadoLivreProvider
from .search_intent_v218 import acceptable, query_variants, relevance_score

API = "https://api.mercadolibre.com"
_PATCHED = False


def _money(row: dict[str, Any]) -> float | None:
    sale = row.get("sale_price")
    if isinstance(sale, dict):
        value = sale.get("amount") or sale.get("regular_amount")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    value = row.get("price")
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _result(provider: MercadoLivreProvider, row: dict[str, Any]) -> dict[str, Any] | None:
    item_id = str(row.get("id") or "")
    title = str(row.get("title") or row.get("name") or "").strip()
    price = _money(row)
    if not item_id or not title or price is None:
        return None
    attrs = row.get("attributes") or []
    seller = row.get("seller") if isinstance(row.get("seller"), dict) else {}
    seller_name = seller.get("nickname") if seller else None
    if not seller_name and row.get("seller_id"):
        seller_name = f"Vendedor #{row.get('seller_id')}"
    catalog_id = row.get("catalog_product_id")
    sale = row.get("sale_price") if isinstance(row.get("sale_price"), dict) else {}
    regular = sale.get("regular_amount") if isinstance(sale, dict) else None
    original = row.get("original_price") or regular
    return {
        "external_product_id": str(catalog_id or item_id),
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
        "price_source": "public_listing_search",
    }


def enable_public_listing_search_v221() -> None:
    """Make shopper search independent from OAuth and catalog-heavy lookups.

    /sites/MLB/search is a public listing search. A single request already returns
    buyable listings with title, price and permalink, which is both broader and
    much faster for natural searches such as 'maquina de lavar 17kg'.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    previous_search = MercadoLivreProvider.search

    def search(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 12))
        merged: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        variants = query_variants(query)[:3] or [query]

        for idx, variant in enumerate(variants):
            try:
                with httpx.Client(timeout=httpx.Timeout(6.5, connect=3.5), follow_redirects=True) as client:
                    response = client.get(
                        f"{API}/sites/MLB/search",
                        params={"q": variant, "limit": 50},
                        headers={"Accept": "application/json", "User-Agent": "PriceRadar/0.2"},
                    )
                if response.status_code >= 400:
                    errors.append(f"HTTP {response.status_code}")
                    continue
                payload = response.json()
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                continue

            for raw in payload.get("results") or []:
                title = str(raw.get("title") or raw.get("name") or "")
                if not acceptable(query, title):
                    continue
                item = _result(self, raw)
                if not item:
                    continue
                score = relevance_score(query, title)
                item["_score"] = score
                key = str(item.get("item_id") or item.get("external_product_id"))
                old = merged.get(key)
                if old is None or score > float(old.get("_score") or -999):
                    merged[key] = item

            # Do not fan out needlessly once the first natural-language query worked.
            if len(merged) >= max(limit, 6) or (idx == 0 and len(merged) >= 4):
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

        # Retain the prior catalog implementation only as a last resort.
        try:
            return previous_search(self, query, limit=limit)
        except Exception:
            return []

    MercadoLivreProvider.search = search
