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

# Words that normally identify accessories/replacement parts instead of the
# main product. They are ignored when the user explicitly asks for an accessory.
_ACCESSORY_TERMS = {
    "capa", "capinha", "case", "cover", "pelicula", "vidro", "bumper",
    "carregador", "charger", "cabo", "cable", "adaptador", "adapter",
    "suporte", "base", "dock", "stand", "bolsa", "estojo", "carteira",
    "skin", "adesivo", "protetor", "protector", "lente", "magsafe",
    "cordao", "alca", "pop socket", "popsocket", "bateria", "tampa",
    "display", "tela", "touch", "flex", "conector", "peca", "reposicao",
    "replacement", "controle", "joystick", "headset", "fone", "cooler",
}

_DEVICE_FAMILIES = {
    "iphone", "ipad", "macbook", "playstation", "ps4", "ps5", "xbox",
    "switch", "galaxy", "pixel", "moto", "motorola", "redmi", "poco",
    "xiaomi", "notebook", "laptop", "monitor", "televisor", "tv", "rtx",
    "radeon", "geforce", "processador", "ryzen", "core", "console",
}

_DOMAIN_ACCESSORY_HINTS = {
    "accessor", "cover", "case", "screen_protector", "protector", "charger",
    "cable", "holder", "mount", "replacement", "spare", "parts", "repair",
}

# Mercado Livre lets /products/search receive a domain_id. Using it when the
# intent is clear is much safer than trying to repair a noisy result set later.
_DOMAIN_RULES: list[tuple[set[str], str]] = [
    ({"iphone", "celular", "smartphone", "galaxy", "pixel", "motorola", "moto", "redmi", "poco", "xiaomi"}, "MLB-CELLPHONES"),
    ({"playstation", "ps4", "ps5", "xbox", "console", "switch"}, "MLB-GAME_CONSOLES"),
    ({"rtx", "geforce", "radeon", "gpu", "placa video"}, "MLB-GRAPHICS_CARDS"),
    ({"notebook", "laptop", "macbook"}, "MLB-NOTEBOOKS"),
    ({"monitor"}, "MLB-MONITORS"),
    ({"televisor", "smart tv", "tv"}, "MLB-TELEVISIONS"),
    ({"processador", "ryzen", "core i3", "core i5", "core i7", "core i9"}, "MLB-PROCESSORS"),
]


def _norm(text: str | None) -> str:
    raw = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_text = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _tokens(text: str | None) -> list[str]:
    return [t for t in _norm(text).split() if len(t) > 1 and t not in _STOP]


def _query_wants_accessory(query: str) -> bool:
    q = _norm(query)
    words = set(q.split())
    for term in _ACCESSORY_TERMS:
        normalized = _norm(term)
        if (" " in normalized and normalized in q) or normalized in words:
            return True
    return False


def _looks_like_accessory(name: str | None, domain_id: str | None = None) -> bool:
    n = _norm(name)
    d = _norm(domain_id)
    words = set(n.split())
    for term in _ACCESSORY_TERMS:
        normalized = _norm(term)
        if (" " in normalized and normalized in n) or normalized in words:
            return True
    return any(hint in d for hint in _DOMAIN_ACCESSORY_HINTS)


def _device_intent(query: str) -> bool:
    q = set(_tokens(query))
    return bool(q & _DEVICE_FAMILIES)


def _domain_for_query(query: str) -> str | None:
    if _query_wants_accessory(query):
        return None
    q = _norm(query)
    words = set(q.split())
    for hints, domain_id in _DOMAIN_RULES:
        for hint in hints:
            h = _norm(hint)
            if (" " in h and h in q) or h in words:
                return domain_id
    return None


def _family(query: str) -> str | None:
    q = _norm(query)
    if "iphone" in q:
        return "iphone"
    if "playstation" in q or re.search(r"\bps\d\b", q):
        return "playstation"
    if "rtx" in q or "geforce" in q:
        return "rtx"
    if "radeon" in q or re.search(r"\brx\s*\d", q):
        return "radeon"
    return None


def _generation(text: str | None, family: str | None) -> int | None:
    n = _norm(text)
    if family == "iphone":
        m = re.search(r"\biphone\s*(\d{1,2})\b", n)
        return int(m.group(1)) if m else None
    if family == "playstation":
        m = re.search(r"\b(?:playstation|ps)\s*(\d)\b", n)
        return int(m.group(1)) if m else None
    if family == "rtx":
        m = re.search(r"\brtx\s*(\d{3,4})\b", n)
        return int(m.group(1)) if m else None
    if family == "radeon":
        m = re.search(r"\brx\s*(\d{3,4})\b", n)
        return int(m.group(1)) if m else None
    return None


def _query_has_generation(query: str, family: str | None) -> bool:
    return _generation(query, family) is not None


def _score(
    query: str,
    name: str | None,
    *,
    domain_id: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    newest_generation: int | None = None,
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

    score = coverage * 24.0 + present * 2.5 + partial * 0.2

    if normalized_query == n:
        score += 14.0
    elif normalized_query in n:
        score += 9.0

    if words and words[0] == q_tokens[0]:
        score += 4.0
    if len(words) >= 2 and len(q_tokens) >= 2 and words[:2] == q_tokens[:2]:
        score += 5.0

    # Explicit model/generation numbers are hard requirements. This is what
    # prevents iPhone 16 from surfacing for "iPhone 17".
    numeric_tokens = [t for t in q_tokens if any(ch.isdigit() for ch in t)]
    for token in numeric_tokens:
        if token in word_set:
            score += 7.0
        else:
            score -= 24.0

    combined_identity = _norm(" ".join(x for x in (brand, model) if x))
    if combined_identity:
        identity_tokens = set(combined_identity.split())
        score += sum(1.8 for token in q_tokens if token in identity_tokens)

    if not _query_wants_accessory(query) and _looks_like_accessory(name, domain_id):
        score -= 45.0 if _device_intent(query) else 24.0

    if _device_intent(query) and coverage < 0.67:
        score -= 20.0

    # When the user searches only the family ("iPhone", "PlayStation", "RTX")
    # and does not specify a generation, prefer the newest generation found in
    # the catalog pool. This solves the old-iPhone-first behavior while keeping
    # exact model searches deterministic.
    fam = _family(query)
    if fam and newest_generation is not None and not _query_has_generation(query, fam):
        gen = _generation(name, fam)
        if gen is not None:
            score += (gen - newest_generation) * 3.0
        else:
            score -= 7.0

    return score


def _acceptable(query: str, detail: dict[str, Any], expected_domain: str | None) -> bool:
    name = detail.get("name")
    q_tokens = _tokens(query)
    name_words = set(_norm(name).split())
    if not q_tokens:
        return True

    if expected_domain and detail.get("domain_id") and detail.get("domain_id") != expected_domain:
        return False

    if not _query_wants_accessory(query) and _device_intent(query):
        if _looks_like_accessory(name, detail.get("domain_id")):
            return False

    for token in q_tokens:
        if any(ch.isdigit() for ch in token) and token not in name_words:
            return False

    coverage = sum(1 for token in q_tokens if token in name_words) / len(q_tokens)
    return coverage >= (0.67 if _device_intent(query) else 0.5)


def _catalog_search(self: MercadoLivreProvider, query: str, expected_domain: str | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "status": "active",
        "site_id": "MLB",
        "q": query,
        "limit": 50,
    }
    if expected_domain:
        params["domain_id"] = expected_domain
    data = self._request("GET", "/products/search", params=params)
    rows = data.get("results") or []
    if rows or not expected_domain:
        return rows

    # Domain names can evolve. If Mercado Livre rejects/returns nothing for a
    # domain hint, fall back to a broad search instead of returning zero items.
    params.pop("domain_id", None)
    data = self._request("GET", "/products/search", params=params)
    return data.get("results") or []


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

        expected_domain = _domain_for_query(query)
        raw = _catalog_search(self, query, expected_domain)
        fam = _family(query)
        newest_generation = None
        if fam and not _query_has_generation(query, fam):
            generations = [_generation(item.get("name"), fam) for item in raw]
            generations = [g for g in generations if g is not None]
            if generations:
                newest_generation = max(generations)

        ranked = sorted(
            raw[:100],
            key=lambda item: _score(
                query,
                item.get("name"),
                domain_id=item.get("domain_id"),
                brand=self._attr(item.get("attributes") or [], "BRAND"),
                model=self._attr(item.get("attributes") or [], "MODEL"),
                newest_generation=newest_generation,
            ),
            reverse=True,
        )
        selected = ranked[: min(len(ranked), max(limit * 5, 30))]

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

            detail.setdefault("domain_id", item.get("domain_id"))
            if not _acceptable(query, detail, expected_domain):
                continue

            detail["_relevance"] = _score(
                query,
                detail.get("name"),
                domain_id=detail.get("domain_id"),
                brand=detail.get("brand"),
                model=detail.get("model"),
                newest_generation=newest_generation,
            )
            detail["_generation"] = _generation(detail.get("name"), fam) if fam else None
            detailed.append(detail)

        detailed.sort(
            key=lambda d: (
                d.get("_relevance", -100),
                d.get("_generation") or -1,
                d.get("price") is not None,
            ),
            reverse=True,
        )
        result = detailed[:limit]
        for item in result:
            item.pop("_relevance", None)
            item.pop("_generation", None)

        _CACHE[key] = (time.time(), [dict(x) for x in result])
        return result

    MercadoLivreProvider.search = search_ranked
