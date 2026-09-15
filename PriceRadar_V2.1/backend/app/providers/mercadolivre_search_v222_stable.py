from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .mercadolivre_search_v215_light import _search_detail
from .search_intent_v218 import acceptable, query_variants, relevance_score

_PATCHED = False


def _identity(row: dict[str, Any]) -> str:
    parts = [str(row.get("name") or row.get("title") or "")]
    for attr in row.get("attributes") or []:
        value = attr.get("value_name")
        if value not in (None, ""):
            parts.append(str(value))
            continue
        for item in (attr.get("values") or [])[:2]:
            if item.get("name"):
                parts.append(str(item["name"]))
    return " ".join(parts)


def enable_stable_natural_search_v222() -> None:
    """Final Mercado Livre search used by V2.22.

    The previous versions accumulated several monkey patches and fallbacks. That
    made the effective search path difficult to reason about and caused timeouts.
    V2.22 deliberately replaces the stack with one predictable path:
      shopper query variants -> authenticated /products/search -> a small number
      of verified catalog-offer lookups in parallel.

    The title plus product attributes are used for intent matching, so ordinary
    searches like "maquina de lavar 17kg" can match catalog entries where the
    capacity is exposed as an attribute instead of formatted exactly in the title.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def search(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10))
        merged: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        variants = query_variants(query)[:3] or [query]
        for variant in variants:
            try:
                payload = self._request(
                    "GET",
                    "/products/search",
                    params={"status": "active", "site_id": "MLB", "q": variant, "limit": 50},
                )
            except MercadoLivreError as exc:
                errors.append(str(exc))
                continue

            for row in (payload.get("results") or [])[:80]:
                product_id = str(row.get("id") or "")
                if not product_id:
                    continue
                identity = _identity(row)
                if not acceptable(query, identity):
                    continue
                item = dict(row)
                item["_score"] = relevance_score(query, identity)
                old = merged.get(product_id)
                if old is None or float(item["_score"]) > float(old.get("_score") or -999):
                    merged[product_id] = item

            if len(merged) >= max(limit * 2, 10):
                break

        if not merged:
            if errors:
                raise MercadoLivreError(errors[-1])
            return []

        ranked = sorted(
            merged.values(),
            key=lambda row: float(row.get("_score") or -999),
            reverse=True,
        )
        # Resolve only the strongest candidates. This is the expensive part.
        candidates = ranked[: max(limit + 2, 8)]
        details: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            futures = {
                pool.submit(_search_detail, self, str(row["id"])): str(row["id"])
                for row in candidates
            }
            for future in as_completed(futures):
                product_id = futures[future]
                try:
                    detail = future.result()
                    if detail and detail.get("price") is not None:
                        details[product_id] = detail
                except Exception:
                    continue

        results: list[dict[str, Any]] = []
        for row in candidates:
            product_id = str(row.get("id") or "")
            detail = details.get(product_id)
            if not detail:
                continue
            identity = _identity(row)
            detail["_score"] = relevance_score(query, identity)
            results.append(detail)

        results.sort(
            key=lambda row: (
                float(row.get("_score") or -999),
                -(float(row.get("price") or 10**18)),
            ),
            reverse=True,
        )
        for row in results:
            row.pop("_score", None)
        return results[:limit]

    MercadoLivreProvider.search = search
