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

# These words usually identify an accessory or replacement part rather than the
# product itself. They are only penalized when the user did NOT ask for one of
# them explicitly.
_ACCESSORY_TERMS = {
    "capa", "capinha", "case", "cover", "pelicula", "vidro", "bumper",
    "carregador", "charger", "cabo", "cable", "adaptador", "adapter",
    "suporte", "base", "dock", "stand", "bolsa", "estojo", "carteira",
    "skin", "adesivo", "protetor", "protector", "lente", "camera lens",
    "pelicula camera", "kit pelicula", "cordao", "alca", "pop socket",
    "pop-socket", "bateria", "tampa", "display", "tela", "touch",
    "flex", "conector", "placa", "peca", "reposicao", "replacement",
}

# Tokens that strongly suggest that the user is asking for the main device.
_DEVICE_FAMILIES = {
    "iphone", "ipad", "macbook", "airpods", "playstation", "ps4", "ps5",
    "xbox", "switch", "galaxy", "pixel", "moto", "motorola", "redmi",
    "poco", "xiaomi", "notebook", "laptop", "monitor", "televisor", "tv",
    "rtx", "radeon", "geforce", "processador", "ryzen", "core", "console",
}

_DOMAIN_ACCESSORY_HINTS = {
    "accessor", "cover", "case", "screen_protector", "protector", "charger",
    "cable", "holder", "mount", "replacement", "spare", "parts", "repair",
}


def _norm(text: str | None) -> str:
    raw = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_text = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _tokens(text: str | None) -> list[str]:
    return [t for t in _norm(text).split() if len(t) > 1 and t not in _STOP]


def _query_wants_accessory(query: str) -> bool:
    q = _norm(query)
    q_words = set(q.split())
    for term in _ACCESSORY_TERMS:
        if term in q or term in q_words:
            return True
    return False


def _looks_like_accessory(name: str | None, domain_id: str | None = None) -> bool:
    n = _norm(name)
    d = _norm(domain_id)
    words = set(n.split())
    for term in _ACCESSORY_TERMS:
        normalized = _norm(term)
        if " " in normalized:
            if normalized in n:
                return True
        elif normalized in words:
            return True
    return any(hint in d for hint in _DOMAIN_ACCESSORY_HINTS)


def _device_intent(query: str) -> bool:
    q = set(_tokens(query))
    return bool(q & _DEVICE_FAMILIES)


def _score(
    query: str,
    name: str | None,
    *,
    domain_id: str | None = None,
    brand: str | None = None,
    model: str | None = None,
) -> float:
    q_tokens = _tokens(query)
    n = _norm(name)
    if not q_tokens or not n:
        return -100.0

    words = n.split()
    word_set = set(words)
    normalized_query = _norm(query)

    present = sum(1 for token in q_tokens if token in word_set)
    partial = sum(1 for token in q_tokens if token in n and token not in word_set)
    coverage = present / len(q_tokens)

    score = coverage * 22.0 + present * 2.2 + partial * 0.25

    # Exact phrase/order is particularly useful for models such as "iPhone 17"
    # and "PlayStation 5 Slim Digital".
    if normalized_query == n:
        score += 14.0
    elif normalized_query in n:
        score += 9.0

    # Prefer titles whose first words are the product family/model rather than
    # "Capa para ...", "Película ..." etc.
    if words and words[0] == q_tokens[0]:
        score += 3.5
    if len(words) >= 2 and len(q_tokens) >= 2 and words[:2] == q_tokens[:2]:
        score += 5.0

    # Numeric/model tokens are highly discriminative. A query for iPhone 17
    # should not rank iPhone 16 or a generic Apple accessory highly.
    numeric_tokens = [t for t in q_tokens if any(ch.isdigit() for ch in t)]
    for token in numeric_tokens:
        if token in word_set:
            score += 5.0
        else:
            score -= 18.0

    combined_identity = _norm(" ".join(x for x in (brand, model) if x))
    if combined_identity:
        identity_tokens = set(combined_identity.split())
        score += sum(1.5 for token in q_tokens if token in identity_tokens)

    if not _query_wants_accessory(query) and _looks_like_accessory(name, domain_id):
        # Strong penalty so accessories cannot outrank the main product just
        # because they repeat its full model name in the title.
        score -= 35.0 if _device_intent(query) else 22.0

    # For device queries, candidates missing half of the requested terms are
    # almost always noise. Keep them far below complete model matches.
    if _device_intent(query) and coverage < 0.67:
        score -= 18.0

    return score


def _acceptable(query: str, detail: dict[str, Any]) -> bool:
    name = detail.get("name")
    q_tokens = _tokens(query)
    name_words = set(_norm(name).split())
    if not q_tokens:
        return True

    # Do not return obvious accessories for a main-device search. It is better
    # for PriceRadar to show fewer results than to offer a case as an iPhone.
    if not _query_wants_accessory(query) and _device_intent(query):
        if _looks_like_accessory(name, detail.get("domain_id")):
            return False

    # Every numeric/model token typed by the user must survive in the result.
    # This avoids iPhone 16 results for "iPhone 17", RTX 5070 results for 5080,
    # etc.
    for token in q_tokens:
        if any(ch.isdigit() for ch in token) and token not in name_words:
            return False

    coverage = sum(1 for token in q_tokens if token in name_words) / len(q_tokens)
    return coverage >= (0.67 if _device_intent(query) else 0.5)


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

        # Rank a wider candidate pool cheaply before opening individual product
        # details. Accessory penalties already apply at this stage when domain_id
        # is present in the catalog response.
        ranked = sorted(
            raw[:80],
            key=lambda item: _score(
                query,
                item.get("name"),
                domain_id=item.get("domain_id"),
                brand=self._attr(item.get("attributes") or [], "BRAND"),
                model=self._attr(item.get("attributes") or [], "MODEL"),
            ),
            reverse=True,
        )
        selected = ranked[: min(len(ranked), max(limit * 4, 24))]

        detailed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in selected:
            product_id = item.get("id")
            if not product_id or str(product_id) in seen_ids:
                continue
            seen_ids.add(str(product_id))
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
                    "item_id": None,
                    "price": None,
                    "original_price": None,
                    "currency": "BRL",
                    "seller_name": None,
                    "shipping_free": None,
                    "available": True,
                    "domain_id": item.get("domain_id"),
                }

            # Keep raw catalog metadata when the detailed endpoint omits it.
            detail.setdefault("domain_id", item.get("domain_id"))
            if not _acceptable(query, detail):
                continue

            detail["_relevance"] = _score(
                query,
                detail.get("name"),
                domain_id=detail.get("domain_id"),
                brand=detail.get("brand"),
                model=detail.get("model"),
            )
            detailed.append(detail)

        # Price availability breaks ties only after semantic relevance. A cheap
        # but wrong accessory must never outrank the intended device.
        detailed.sort(
            key=lambda d: (d.get("_relevance", -100), d.get("price") is not None),
            reverse=True,
        )
        result = detailed[:limit]
        for item in result:
            item.pop("_relevance", None)

        _CACHE[key] = (time.time(), [dict(x) for x in result])
        return result

    MercadoLivreProvider.search = search_ranked
