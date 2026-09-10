from __future__ import annotations

from typing import Any

from .mercadolivre import MercadoLivreProvider, MercadoLivreError

_PATCHED = False


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def enable_price_enrichment() -> None:
    """Enriquece produtos de catálogo com a melhor oferta atual exposta pelo marketplace."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    original_product_detail = MercadoLivreProvider.product_detail

    def product_detail_with_price(self: MercadoLivreProvider, product_id: str) -> dict[str, Any]:
        detail = original_product_detail(self, product_id)
        try:
            data = self._request("GET", f"/products/{product_id}/items")
        except MercadoLivreError:
            return detail

        rows = data.get("results") or []
        candidates: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item_id = row.get("item_id")
            price = _number(row.get("price"))
            if not item_id or price is None or price <= 0:
                continue
            condition = str(row.get("condition") or "").lower()
            if condition and condition not in {"new", "novo"}:
                continue
            candidates.append((price, row))
        if not candidates:
            return detail

        candidates.sort(key=lambda pair: pair[0])
        listed_price, best = candidates[0]
        item_id = str(best["item_id"])
        current_price = listed_price
        regular_price = None
        currency = best.get("currency_id") or "BRL"

        try:
            sale = self._request("GET", f"/items/{item_id}/sale_price", params={"context": "channel_marketplace"})
            sale_amount = _number(sale.get("amount"))
            if sale_amount is not None and sale_amount > 0:
                current_price = sale_amount
            regular_price = _number(sale.get("regular_amount"))
            currency = sale.get("currency_id") or currency
        except MercadoLivreError:
            pass

        item_detail: dict[str, Any] = {}
        try:
            raw_item = self._request("GET", f"/items/{item_id}")
            if isinstance(raw_item, dict):
                item_detail = raw_item
        except MercadoLivreError:
            pass

        seller_id = item_detail.get("seller_id") or best.get("seller_id")
        shipping = item_detail.get("shipping") if isinstance(item_detail.get("shipping"), dict) else None
        if not shipping:
            shipping = best.get("shipping") if isinstance(best.get("shipping"), dict) else {}

        detail.update({
            "item_id": item_id,
            "price": current_price,
            "original_price": regular_price,
            "currency": currency,
            "seller_name": f"Vendedor #{seller_id}" if seller_id else detail.get("seller_name"),
            "shipping_free": shipping.get("free_shipping") if shipping else detail.get("shipping_free"),
            "available": item_detail.get("status") in {None, "active"},
            "url": item_detail.get("permalink") or detail.get("url"),
        })
        return detail

    MercadoLivreProvider.product_detail = product_detail_with_price
