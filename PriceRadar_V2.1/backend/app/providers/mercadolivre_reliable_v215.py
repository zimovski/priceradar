from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .mercadolivre_search_enhancement import (
    _domain_for_query,
    _family,
    _generation,
    _norm,
    _query_has_generation,
    _query_wants_accessory,
    _looks_like_accessory,
    _score,
    _tokens,
)

_PATCHED = False


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _item_url(item_id: str | None) -> str | None:
    item_id = str(item_id or "").strip().upper()
    match = re.fullmatch(r"MLB(\d+)", item_id)
    if not match:
        return None
    # Mercado Livre accepts the canonical item-id route even without a title slug.
    return f"https://produto.mercadolivre.com.br/MLB-{match.group(1)}-_JM"


def _bulk_items(provider: MercadoLivreProvider, item_ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = [str(x).strip() for x in item_ids if x]
    if not ids:
        return {}
    ids = list(dict.fromkeys(ids))[:20]
    attrs = ",".join([
        "body.id", "body.title", "body.price", "body.original_price",
        "body.permalink", "body.status", "body.available_quantity",
        "body.catalog_product_id", "body.thumbnail", "body.seller_id",
        "body.currency_id", "body.shipping", "body.condition",
    ])
    payload = None
    try:
        payload = provider._request("GET", "/items/bulk", params={"ids": ",".join(ids), "attributes": attrs})
    except MercadoLivreError:
        # The legacy multiget coexists until Oct/2026 and is still useful as a
        # compatibility fallback for applications that have not received the
        # new bulk route yet.
        try:
            payload = provider._request("GET", "/items", params={"ids": ",".join(ids)})
        except MercadoLivreError:
            return {}

    rows = payload if isinstance(payload, list) else []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = row.get("status_code", row.get("code", 200))
        body = row.get("body") if isinstance(row.get("body"), dict) else row
        item_id = str(body.get("id") or row.get("id") or "")
        if item_id and int(status or 200) < 400:
            out[item_id] = body
    return out


def _active_item(body: dict[str, Any], product_id: str | None = None) -> bool:
    if not body:
        return False
    if body.get("status") not in (None, "active"):
        return False
    condition = str(body.get("condition") or "").lower()
    if condition and condition not in {"new", "novo"}:
        return False
    try:
        if body.get("available_quantity") is not None and int(body.get("available_quantity")) <= 0:
            return False
    except (TypeError, ValueError):
        pass
    if _num(body.get("price")) is None:
        return False
    catalog = str(body.get("catalog_product_id") or "")
    if product_id and catalog and catalog != str(product_id):
        return False
    return True


def _detail_base(provider: MercadoLivreProvider, product_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data = provider._request("GET", f"/products/{product_id}")
    attrs = data.get("attributes") or []
    pictures = data.get("pictures") or []
    image = None
    if pictures:
        image = pictures[0].get("secure_url") or pictures[0].get("url")
    image = image or data.get("thumbnail")
    detail = {
        "external_product_id": data.get("id") or product_id,
        "name": data.get("name") or data.get("family_name") or product_id,
        "brand": provider._attr(attrs, "BRAND"),
        "model": provider._attr(attrs, "MODEL"),
        "gtin": provider._attr(attrs, "GTIN", "EAN", "UPC"),
        "image_url": image,
        "url": data.get("permalink"),
        "item_id": None,
        "price": None,
        "original_price": None,
        "currency": "BRL",
        "seller_name": None,
        "shipping_free": None,
        "available": False,
        "domain_id": data.get("domain_id"),
        "price_source": "catalog_only",
    }
    return detail, data


def _from_item(detail: dict[str, Any], body: dict[str, Any], *, source: str) -> dict[str, Any]:
    item_id = str(body.get("id") or detail.get("item_id") or "")
    shipping = body.get("shipping") if isinstance(body.get("shipping"), dict) else {}
    seller_id = body.get("seller_id")
    detail.update({
        "item_id": item_id or None,
        "price": _num(body.get("price")),
        "original_price": _num(body.get("original_price")),
        "currency": body.get("currency_id") or detail.get("currency") or "BRL",
        "seller_name": f"Vendedor #{seller_id}" if seller_id else detail.get("seller_name"),
        "shipping_free": shipping.get("free_shipping") if shipping else detail.get("shipping_free"),
        "available": _num(body.get("price")) is not None,
        "url": body.get("permalink") or _item_url(item_id) or detail.get("url"),
        "price_source": source,
    })
    return detail


def _query_variant_ok(query: str, title: str | None) -> bool:
    q = _norm(query)
    t = _norm(title)
    if not t:
        return False
    if not _query_wants_accessory(query) and _looks_like_accessory(title):
        return False

    # Explicit numbers (5070, 17, 14600KF...) are hard requirements.
    t_words = set(t.split())
    for token in _tokens(query):
        if any(ch.isdigit() for ch in token) and token not in t_words:
            return False

    # Common product suffixes materially change the product. A plain RTX 5070
    # should not rank a 5070 Ti above the actual 5070, while a Ti query requires Ti.
    for variant in ("ti", "super"):
        q_has = re.search(rf"\b{variant}\b", q) is not None
        t_has = re.search(rf"\b{variant}\b", t) is not None
        if q_has != t_has and ("rtx" in q or "geforce" in q or "radeon" in q):
            return False

    cap = re.search(r"\b(\d{2,4})\s*(gb|tb)\b", q)
    if cap:
        wanted = f"{cap.group(1)}{cap.group(2)}"
        compact_title = t.replace(" ", "")
        if wanted not in compact_title:
            return False

    for word in ("slim", "digital", "pro max", "pro", "plus", "ultra"):
        if re.search(rf"\b{re.escape(word)}\b", q) and not re.search(rf"\b{re.escape(word)}\b", t):
            return False
    return True


def enable_reliable_v215() -> None:
    """Replace layered prototype patches with one predictable ML implementation.

    Search is catalog-based (the generic /sites/MLB/search endpoint is restricted
    for many third-party apps), but every displayed price comes from either the
    documented Buy Box or /products/{product_id}/items. Tracking then verifies
    the chosen publication through the current bulk item endpoint when available.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def product_detail(self: MercadoLivreProvider, product_id: str) -> dict[str, Any]:
        detail, data = _detail_base(self, product_id)
        winner = data.get("buy_box_winner") or {}
        winner_id = str(winner.get("item_id") or "")

        if winner_id:
            verified = _bulk_items(self, [winner_id]).get(winner_id)
            if verified and _active_item(verified, product_id):
                return _from_item(detail, verified, source="buy_box_verified")

            # The Buy Box itself is an official product response. If item detail
            # is restricted for this app, keep the documented winner price and a
            # deterministic item URL instead of dropping the price entirely.
            winner_price = _num(winner.get("price"))
            if winner_price is not None:
                detail.update({
                    "item_id": winner_id,
                    "price": winner_price,
                    "original_price": _num(winner.get("original_price")),
                    "currency": winner.get("currency_id") or "BRL",
                    "seller_name": f"Vendedor #{winner.get('seller_id')}" if winner.get("seller_id") else None,
                    "shipping_free": (winner.get("shipping") or {}).get("free_shipping"),
                    "available": True,
                    "url": _item_url(winner_id) or detail.get("url"),
                    "price_source": "buy_box_winner",
                })
                return detail

        try:
            payload = self._request("GET", f"/products/{product_id}/items", params={"limit": 20})
            rows = payload.get("results") or []
        except MercadoLivreError:
            rows = []

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
        candidates.sort(key=lambda row: float(row.get("price") or 10**18))

        # Verify several candidates in one official bulk request. This provides
        # the exact publication permalink/price without one HTTP call per item.
        ids = [str(row.get("item_id")) for row in candidates[:10]]
        verified = _bulk_items(self, ids)
        verified_rows = [verified[i] for i in ids if i in verified and _active_item(verified[i], product_id)]
        if verified_rows:
            verified_rows.sort(key=lambda body: float(body.get("price") or 10**18))
            return _from_item(detail, verified_rows[0], source="catalog_offer_verified")

        # Last-resort value is still a real offer returned by the documented
        # catalog competition endpoint. We keep price and item id together and
        # construct the canonical item route; no synthetic price is invented.
        if candidates:
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
                "price_source": "catalog_offer",
            })
        return detail

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
        candidates = [row for row in ranked if row.get("id") and _query_variant_ok(query, row.get("name"))][: max(limit, 6)]
        if not candidates:
            return []

        details: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            futures = {pool.submit(product_detail, self, str(row["id"])): str(row["id"]) for row in candidates}
            for future in as_completed(futures):
                pid = futures[future]
                try:
                    details[pid] = future.result()
                except Exception:
                    pass

        results: list[dict[str, Any]] = []
        for row in candidates:
            pid = str(row.get("id"))
            detail = details.get(pid)
            if not detail or detail.get("price") is None:
                continue
            if not _query_variant_ok(query, detail.get("name")):
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

        results.sort(key=lambda row: (row.get("_score", -100), -(row.get("price") or 10**18)), reverse=True)
        for row in results:
            row.pop("_score", None)
        return results[:limit]

    MercadoLivreProvider.product_detail = product_detail
    MercadoLivreProvider.search = search
