from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from .mercadolivre import MercadoLivreError, MercadoLivreProvider

_PATCHED = False
API = "https://api.mercadolibre.com"


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        value = float(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _valid_ml_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br")


def _public_item(provider: MercadoLivreProvider, item_id: str) -> dict[str, Any]:
    try:
        timeout = httpx.Timeout(5.0, connect=3.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
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


def enable_verified_listing_fallback() -> None:
    """Fill missing catalog price only with a real, active, linkable listing.

    Some Mercado Livre catalog products have no current Buy Box winner. In that
    case the previous implementation returned no price at all. This fallback
    inspects a small number of catalog listings, opens the exact /items/{id}
    resource and only accepts a candidate when price, active status and a real
    Mercado Livre permalink all belong to the same listing.

    The displayed value is therefore never a guessed catalog number: clicking
    the saved URL leads to the same verified listing used to record the price.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    original = MercadoLivreProvider.product_detail

    def product_detail_verified(self: MercadoLivreProvider, product_id: str) -> dict[str, Any]:
        detail = original(self, product_id)
        if detail.get("price") and detail.get("available") and _valid_ml_url(detail.get("url")):
            return detail

        try:
            payload = self._request("GET", f"/products/{product_id}/items")
        except MercadoLivreError:
            return detail

        rows = payload.get("results") or []
        candidates: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item_id = str(row.get("item_id") or "").strip()
            listed = _num(row.get("price"))
            condition = str(row.get("condition") or "").lower()
            if not item_id or listed is None:
                continue
            if condition and condition not in {"new", "novo"}:
                continue
            candidates.append((listed, row))

        # Look only at a small set. Search performance matters and every price
        # still has to be verified against the exact item endpoint.
        candidates.sort(key=lambda x: x[0])
        for _, row in candidates[:6]:
            item_id = str(row.get("item_id"))
            item: dict[str, Any] = {}
            try:
                raw = self._request("GET", f"/items/{item_id}")
                if isinstance(raw, dict):
                    item = raw
            except MercadoLivreError:
                pass
            if not item.get("permalink") or _num(item.get("price")) is None:
                public = _public_item(self, item_id)
                if public:
                    merged = dict(public)
                    merged.update({k: v for k, v in item.items() if v not in (None, "")})
                    item = merged

            price = _num(item.get("price"))
            permalink = item.get("permalink")
            status = item.get("status")
            qty = item.get("available_quantity")
            if price is None or not _valid_ml_url(permalink):
                continue
            if status not in (None, "active"):
                continue
            try:
                if qty is not None and int(qty) <= 0:
                    continue
            except (TypeError, ValueError):
                pass

            shipping = item.get("shipping") if isinstance(item.get("shipping"), dict) else {}
            seller_id = item.get("seller_id") or row.get("seller_id")
            detail.update({
                "item_id": item_id,
                "price": price,
                "original_price": _num(item.get("original_price")),
                "currency": item.get("currency_id") or detail.get("currency") or "BRL",
                "seller_name": f"Vendedor #{seller_id}" if seller_id else detail.get("seller_name"),
                "shipping_free": shipping.get("free_shipping") if shipping else detail.get("shipping_free"),
                "available": True,
                "url": permalink,
                "price_source": "verified_active_listing",
            })
            return detail

        return detail

    MercadoLivreProvider.product_detail = product_detail_verified
