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
    """Enriquece produtos de catálogo com uma oferta/preço real.

    /products/{product_id} descreve a PDP e nem sempre traz buy_box_winner.
    Para comparação de preços, consultamos /products/{product_id}/items e
    escolhemos a menor publicação válida. Em seguida tentamos /sale_price para
    obter o preço vigente (incluindo promoção, quando a API o disponibiliza).
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    original_product_detail = MercadoLivreProvider.product_detail

    def product_detail_with_price(self: MercadoLivreProvider, product_id: str) -> dict[str, Any]:
        detail = original_product_detail(self, product_id)

        # Se o endpoint de produto já trouxe uma oferta válida, preservamos.
        if detail.get("item_id") and _number(detail.get("price")) is not None:
            return detail

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
            # Quando o campo existir, não misturamos usados com produto novo.
            condition = str(row.get("condition") or "").lower()
            if condition and condition not in {"new", "novo"}:
                continue
            candidates.append((price, row))

        if not candidates:
            return detail

        # O objetivo do PriceRadar é mostrar o melhor preço atualmente encontrado
        # para o mesmo produto de catálogo.
        candidates.sort(key=lambda pair: pair[0])
        listed_price, best = candidates[0]
        item_id = str(best["item_id"])

        current_price = listed_price
        regular_price = None
        currency = best.get("currency_id") or "BRL"

        # O Mercado Livre recomenda sale_price para o preço de venda atual.
        try:
            sale = self._request(
                "GET",
                f"/items/{item_id}/sale_price",
                params={"context": "channel_marketplace"},
            )
            sale_amount = _number(sale.get("amount"))
            if sale_amount is not None and sale_amount > 0:
                current_price = sale_amount
            regular_price = _number(sale.get("regular_amount"))
            currency = sale.get("currency_id") or currency
        except MercadoLivreError:
            # O preço da listagem PDP continua sendo um fallback útil.
            pass

        seller_id = best.get("seller_id")
        shipping = best.get("shipping") if isinstance(best.get("shipping"), dict) else {}

        detail.update({
            "item_id": item_id,
            "price": current_price,
            "original_price": regular_price,
            "currency": currency,
            "seller_name": f"Vendedor #{seller_id}" if seller_id else detail.get("seller_name"),
            "shipping_free": shipping.get("free_shipping") if shipping else detail.get("shipping_free"),
            "available": True,
        })
        return detail

    MercadoLivreProvider.product_detail = product_detail_with_price
