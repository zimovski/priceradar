from __future__ import annotations

from typing import Any

import httpx

from .mercadolivre import MercadoLivreProvider, MercadoLivreError

_PATCHED = False
API = "https://api.mercadolibre.com"


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _public_item(self: MercadoLivreProvider, item_id: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.get(
                f"{API}/items/{item_id}",
                headers={"Accept": "application/json", "User-Agent": "PriceRadar/0.2"},
            )
        if response.status_code < 400:
            body = response.json()
            if isinstance(body, dict):
                return body
    except Exception:
        pass
    return {}


def _verified_item(self: MercadoLivreProvider, item_id: str, expected_product_id: str | None = None) -> dict[str, Any] | None:
    item: dict[str, Any] = {}
    try:
        raw = self._request("GET", f"/items/{item_id}")
        if isinstance(raw, dict):
            item = raw
    except MercadoLivreError:
        pass
    if not item:
        item = _public_item(self, item_id)
    if not item:
        return None

    status = item.get("status")
    if status not in (None, "active"):
        return None
    available = item.get("available_quantity")
    try:
        if available is not None and int(available) <= 0:
            return None
    except (TypeError, ValueError):
        pass
    price = _number(item.get("price"))
    permalink = item.get("permalink")
    if price is None or price <= 0 or not permalink:
        return None

    # When Mercado Livre exposes catalog_product_id, require it to point to the
    # exact catalog product the user selected. This prevents accidental prices
    # from a related model or accessory.
    catalog_product_id = str(item.get("catalog_product_id") or "")
    if expected_product_id and catalog_product_id and catalog_product_id != str(expected_product_id):
        return None
    return item


def _fallback_catalog_listing(self: MercadoLivreProvider, product_id: str) -> dict[str, Any] | None:
    """Find a real, active listing only when the PDP has no Buy Box winner.

    We never invent a price. Every fallback candidate is re-opened through
    /items/{item_id}; the value and permalink shown by PriceRadar therefore
    belong to the same purchasable Mercado Livre listing.
    """
    try:
        data = self._request("GET", f"/products/{product_id}/items")
    except MercadoLivreError:
        return None
    rows = data.get("results") or []
    verified: list[dict[str, Any]] = []
    for row in rows[:20]:
        item_id = str(row.get("item_id") or "")
        if not item_id:
            continue
        condition = str(row.get("condition") or "").lower()
        if condition and condition not in {"new", "novo"}:
            continue
        item = _verified_item(self, item_id, expected_product_id=product_id)
        if item:
            verified.append(item)
    if not verified:
        return None

    # This is a price comparator: when no official Buy Box exists, choose the
    # cheapest *verified* active listing and link to that exact publication.
    verified.sort(key=lambda item: float(item["price"]))
    return verified[0]


def enable_price_enrichment() -> None:
    """Resolve a real purchasable Mercado Livre listing and its exact price."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    original_product_detail = MercadoLivreProvider.product_detail

    def product_detail_with_price(self: MercadoLivreProvider, product_id: str) -> dict[str, Any]:
        detail = original_product_detail(self, product_id)
        item_id = str(detail.get("item_id") or "").strip()
        price_source = "buy_box_winner"

        # Prefer the official Buy Box winner. Some catalog products legitimately
        # have no winner; in that case use a separately verified active listing
        # rather than leaving a purchasable product with a blank price.
        item_detail = _verified_item(self, item_id, expected_product_id=product_id) if item_id else None
        if not item_detail:
            item_detail = _fallback_catalog_listing(self, product_id)
            price_source = "verified_catalog_listing"

        if not item_detail:
            detail.update({
                "price": None,
                "original_price": None,
                "available": False,
                "price_source": "no_verified_listing",
            })
            return detail

        item_id = str(item_detail.get("id") or item_id)
        price = _number(item_detail.get("price"))
        original_price = _number(item_detail.get("original_price"))
        currency = item_detail.get("currency_id") or detail.get("currency") or "BRL"
        seller_id = item_detail.get("seller_id")
        shipping = item_detail.get("shipping") if isinstance(item_detail.get("shipping"), dict) else {}

        detail.update({
            "item_id": item_id,
            "price": price,
            "original_price": original_price,
            "currency": currency,
            "seller_name": f"Vendedor #{seller_id}" if seller_id else detail.get("seller_name"),
            "shipping_free": shipping.get("free_shipping") if shipping else detail.get("shipping_free"),
            "available": price is not None,
            "url": item_detail.get("permalink") or detail.get("url"),
            "price_source": price_source,
        })
        return detail

    MercadoLivreProvider.product_detail = product_detail_with_price
