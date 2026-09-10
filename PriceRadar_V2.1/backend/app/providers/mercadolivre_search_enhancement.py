from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

from .mercadolivre import MercadoLivreProvider, MercadoLivreError

_PATCHED = False
_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
CACHE_TTL_SECONDS = 300
_STOP = {"de", "da", "do", "das", "dos", "para", "com", "e", "em", "a", "o"}


def _norm(text: str | None) -> str:
    raw = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_text = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _tokens(text: str | None) -> list[str]:
    return [t for t in _norm(text).split() if len(t) > 1 and t not in _STOP]


def _score(query: str, name: str | None) -> float:
    q = _tokens(query)
    n = _norm(name)
    if not q or not n:
        return 0.0
    words = n.split()
    present = sum(1 for token in q if token in words)
    partial = sum(1 for token in q if token in n and token not in words)
    coverage = present / len(q)
    phrase_bonus = 1.0 if _norm(query) in n else 0.0
    prefix_bonus = 0.25 if n.startswith(q[0]) else 0.0
    return coverage * 10 + present * 1.5 + partial * 0.35 + phrase_bonus * 3 + prefix_bonus


def enable_search_enhancement() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def search_ranked(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 12))
        key = (_norm(query), limit)
        cached = _CACHE.get(key)
        if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
            return [dict(x) for x in cached[1]]

        data = self._request("GET", "/products/search", params={
            "status": "active", "site_id": "MLB", "q": query,
        })
        raw = data.get("results") or []
        ranked = sorted(raw[:40], key=lambda item: _score(query, item.get("name")), reverse=True)
        selected = ranked[: min(len(ranked), max(limit * 2, limit))]

        detailed: list[dict[str, Any]] = []
        for item in selected:
            product_id = item.get("id")
            if not product_id:
                continue
            try:
                detail = self.product_detail(product_id)
            except MercadoLivreError:
                attrs = item.get("attributes") or []
                detail = {
                    "external_product_id": product_id,
                    "name": item.get("name") or product_id,
                    "brand": self._attr(attrs, "BRAND"),
                    "model": self._attr(attrs, "MODEL"),
                    "gtin": self._attr(attrs, "GTIN", "EAN", "UPC"),
                    "image_url": self._image(item),
                    "url": item.get("permalink"),
                    "item_id": None, "price": None, "original_price": None,
                    "currency": "BRL", "seller_name": None,
                    "shipping_free": None, "available": True,
                }
            detail["_relevance"] = _score(query, detail.get("name"))
            detailed.append(detail)

        detailed.sort(key=lambda d: (d.get("_relevance", 0), d.get("price") is not None), reverse=True)
        result = detailed[:limit]
        for item in result:
            item.pop("_relevance", None)
        _CACHE[key] = (time.time(), [dict(x) for x in result])
        return result

    MercadoLivreProvider.search = search_ranked
