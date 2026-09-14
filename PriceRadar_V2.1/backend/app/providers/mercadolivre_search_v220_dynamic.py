from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .mercadolivre_search_v215_light import _search_detail
from .search_intent_v218 import acceptable, query_variants, relevance_score

_PATCHED = False


def _attribute_text(row: dict[str, Any]) -> str:
    parts: list[str] = [str(row.get("name") or row.get("title") or "")]
    for attr in row.get("attributes") or []:
        value = attr.get("value_name")
        if value not in (None, ""):
            parts.append(str(value))
        else:
            values = attr.get("values") or []
            for item in values[:2]:
                if item.get("name"):
                    parts.append(str(item["name"]))
    return " ".join(parts)


def _discover_domains(provider: MercadoLivreProvider, query: str) -> list[str]:
    try:
        payload = provider._request(
            "GET",
            "/sites/MLB/domain_discovery/search",
            params={"q": query, "limit": 3},
        )
    except MercadoLivreError:
        return []
    if not isinstance(payload, list):
        return []
    out: list[str] = []
    for row in payload:
        domain = str((row or {}).get("domain_id") or "").strip()
        if domain and domain not in out:
            out.append(domain)
    return out


def enable_dynamic_domain_search_v220() -> None:
    """Use Mercado Livre's own category predictor for shopper-language searches.

    Static domain rules work for iPhone/RTX but fail on long-tail categories such
    as washing machines. The official domain_discovery endpoint lets Mercado
    Livre decide the product family, then /products/search finds catalog products
    inside that family. Candidate attributes are included in matching so a query
    such as "máquina de lavar 17kg" can match even when the visible title formats
    capacity differently or only exposes it as an attribute.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    previous_search = MercadoLivreProvider.search

    def search(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 12))
        merged: dict[str, dict[str, Any]] = {}

        for variant in query_variants(query)[:4]:
            domains = _discover_domains(self, variant)
            domain_candidates: list[str | None] = domains[:2] + [None]
            for domain in domain_candidates:
                params: dict[str, Any] = {
                    "status": "active",
                    "site_id": "MLB",
                    "q": variant,
                    "limit": 50,
                }
                if domain:
                    params["domain_id"] = domain
                try:
                    payload = self._request("GET", "/products/search", params=params)
                    rows = payload.get("results") or []
                except MercadoLivreError:
                    continue

                for row in rows[:80]:
                    product_id = str(row.get("id") or "")
                    if not product_id:
                        continue
                    identity = _attribute_text(row)
                    if not acceptable(query, identity):
                        continue
                    score = relevance_score(query, identity)
                    item = dict(row)
                    item["_intent_score"] = score
                    old = merged.get(product_id)
                    if old is None or score > float(old.get("_intent_score") or -999):
                        merged[product_id] = item

                if len(merged) >= max(limit * 2, 12):
                    break
            if len(merged) >= max(limit * 2, 12):
                break

        if merged:
            ranked = sorted(
                merged.values(),
                key=lambda row: float(row.get("_intent_score") or -999),
                reverse=True,
            )
            candidates = ranked[: max(limit + 4, 10)]
            details: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=min(5, len(candidates))) as pool:
                futures = {
                    pool.submit(_search_detail, self, str(row["id"])): str(row["id"])
                    for row in candidates
                }
                for future in as_completed(futures):
                    pid = futures[future]
                    try:
                        detail = future.result()
                        if detail and detail.get("price") is not None:
                            details[pid] = detail
                    except Exception:
                        pass

            results: list[dict[str, Any]] = []
            for row in candidates:
                pid = str(row.get("id") or "")
                detail = details.get(pid)
                if not detail:
                    continue
                # Validate against catalog identity + attributes, not title only.
                identity = _attribute_text(row)
                if not acceptable(query, identity):
                    continue
                detail["_intent_score"] = relevance_score(query, identity)
                results.append(detail)

            results.sort(
                key=lambda x: (
                    float(x.get("_intent_score") or -999),
                    -(float(x.get("price") or 10**18)),
                ),
                reverse=True,
            )
            for row in results:
                row.pop("_intent_score", None)
            if results:
                return results[:limit]

        # Keep V2.19's real-listing search as a secondary path.
        return previous_search(self, query, limit=limit)

    MercadoLivreProvider.search = search
