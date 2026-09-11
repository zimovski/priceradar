from __future__ import annotations

import re
import unicodedata
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


def _slug(text: str | None) -> str:
    raw = unicodedata.normalize("NFKD", text or "produto")
    ascii_text = "".join(ch for ch in raw if not unicodedata.combining(ch))
    ascii_text = ascii_text.lower()
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return ascii_text[:140] or "produto"


def _constructed_item_url(item_id: str | None, title: str | None) -> str | None:
    """Build a Mercado Livre VIP URL from a public item id.

    Mercado Livre VIP permalinks are keyed by the item id. The title portion is
    human-readable and this fallback is only used when the API omits permalink.
    """
    compact = re.sub(r"[^A-Za-z0-9]", "", str(item_id or "")).upper()
    match = re.fullmatch(r"([A-Z]{3})(\d+)", compact)
    if not match:
        return None
    site, number = match.groups()
    return f"https://produto.mercadolivre.com.br/{site}-{number}-{_slug(title)}-_JM"


def _catalog_url(product_id: str | None) -> str | None:
    compact = re.sub(r"[^A-Za-z0-9]", "", str(product_id or "")).upper()
    if not re.fullmatch(r"MLB\d+", compact):
        return None
    return f"https://www.mercadolivre.com.br/p/{compact}"


def _public_item(self: MercadoLivreProvider, item_id: str) -> dict[str, Any]:
    """Read the public item endpoint without OAuth as a permalink fallback."""
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
    """Enrich catalog products with the current marketplace offer and purchase URL."""
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
            # Even if offer discovery fails, a catalog page is a valid route to
            # continue the purchase instead of sending the user nowhere.
            detail["url"] = detail.get("url") or _catalog_url(product_id)
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
            detail["url"] = detail.get("url") or _catalog_url(product_id)
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

        # First try the authenticated item endpoint. Some app/account
        # permission combinations can return a partial item body, so we also
        # retry the same public resource without OAuth when permalink is absent.
        item_detail: dict[str, Any] = {}
        try:
            raw_item = self._request("GET", f"/items/{item_id}")
            if isinstance(raw_item, dict):
                item_detail = raw_item
        except MercadoLivreError:
            pass

        public_detail: dict[str, Any] = {}
        if not item_detail.get("permalink"):
            public_detail = _public_item(self, item_id)

        seller_id = (
            item_detail.get("seller_id")
            or public_detail.get("seller_id")
            or best.get("seller_id")
        )
        shipping = item_detail.get("shipping") if isinstance(item_detail.get("shipping"), dict) else None
        if not shipping and isinstance(public_detail.get("shipping"), dict):
            shipping = public_detail.get("shipping")
        if not shipping:
            shipping = best.get("shipping") if isinstance(best.get("shipping"), dict) else {}

        item_title = (
            item_detail.get("title")
            or public_detail.get("title")
            or best.get("title")
            or detail.get("name")
        )
        direct_url = (
            item_detail.get("permalink")
            or public_detail.get("permalink")
            or _constructed_item_url(item_id, item_title)
            or _catalog_url(product_id)
            or detail.get("url")
        )

        status = item_detail.get("status") or public_detail.get("status")
        detail.update({
            "item_id": item_id,
            "price": current_price,
            "original_price": regular_price,
            "currency": currency,
            "seller_name": f"Vendedor #{seller_id}" if seller_id else detail.get("seller_name"),
            "shipping_free": shipping.get("free_shipping") if shipping else detail.get("shipping_free"),
            "available": status in {None, "active"},
            "url": direct_url,
        })
        return detail

    MercadoLivreProvider.product_detail = product_detail_with_price
