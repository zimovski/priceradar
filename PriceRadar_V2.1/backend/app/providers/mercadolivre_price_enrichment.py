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
    """Read a public marketplace item when the authenticated response is partial."""
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


def enable_price_enrichment() -> None:
    """Resolve the offer buyers actually see on the Mercado Livre product page.

    IMPORTANT: older versions selected the cheapest row returned by
    /products/{product_id}/items. That can include a competing catalog listing
    that is not the current Buy Box winner, so the number can differ from the
    price shown to shoppers on Mercado Livre. From V2.9 onward the canonical
    price is tied to /products/{product_id}.buy_box_winner and then verified
    against that exact /items/{item_id} listing.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    original_product_detail = MercadoLivreProvider.product_detail

    def product_detail_with_price(self: MercadoLivreProvider, product_id: str) -> dict[str, Any]:
        # The base provider reads /products/{product_id} and already exposes the
        # Buy Box winner's item_id and price. We deliberately do NOT scan all
        # catalog competitors looking for the absolute lowest number anymore.
        detail = original_product_detail(self, product_id)
        item_id = str(detail.get("item_id") or "").strip()

        # No active Buy Box winner means there is no single verified listing we
        # can honestly present as the current purchasable price.
        if not item_id:
            detail.update({
                "price": None,
                "original_price": None,
                "available": False,
                "price_source": "no_buy_box_winner",
            })
            return detail

        item_detail: dict[str, Any] = {}
        try:
            raw = self._request("GET", f"/items/{item_id}")
            if isinstance(raw, dict):
                item_detail = raw
        except MercadoLivreError:
            pass

        public_detail: dict[str, Any] = {}
        # Some application/account combinations receive a partial /items body.
        # Retrying the public resource is useful for permalink and visible price.
        if not item_detail.get("permalink") or _number(item_detail.get("price")) is None:
            public_detail = _public_item(self, item_id)

        def first(*values):
            for value in values:
                if value not in (None, ""):
                    return value
            return None

        # /items/{item_id}.price is the publication price documented by Mercado
        # Livre and corresponds to the exact Buy Box item. If unavailable, use
        # the Buy Box price returned by /products/{product_id}.
        verified_price = _number(first(item_detail.get("price"), public_detail.get("price"), detail.get("price")))
        original_price = _number(first(item_detail.get("original_price"), public_detail.get("original_price"), detail.get("original_price")))
        currency = first(item_detail.get("currency_id"), public_detail.get("currency_id"), detail.get("currency")) or "BRL"
        seller_id = first(item_detail.get("seller_id"), public_detail.get("seller_id"))
        status = first(item_detail.get("status"), public_detail.get("status"))
        available_qty = first(item_detail.get("available_quantity"), public_detail.get("available_quantity"))

        shipping = item_detail.get("shipping") if isinstance(item_detail.get("shipping"), dict) else None
        if not shipping and isinstance(public_detail.get("shipping"), dict):
            shipping = public_detail.get("shipping")
        shipping = shipping or {}

        # Prefer the exact publication permalink. The product/PDP permalink from
        # /products is a truthful fallback and is safer than inventing a URL.
        direct_url = first(item_detail.get("permalink"), public_detail.get("permalink"), detail.get("url"))

        available = True
        if status not in (None, "active"):
            available = False
        try:
            if available_qty is not None and int(available_qty) <= 0:
                available = False
        except (TypeError, ValueError):
            pass

        detail.update({
            "item_id": item_id,
            "price": verified_price if available else None,
            "original_price": original_price,
            "currency": currency,
            "seller_name": f"Vendedor #{seller_id}" if seller_id else detail.get("seller_name"),
            "shipping_free": shipping.get("free_shipping") if shipping else detail.get("shipping_free"),
            "available": available and verified_price is not None,
            "url": direct_url,
            "price_source": "buy_box_winner",
        })
        return detail

    MercadoLivreProvider.product_detail = product_detail_with_price
