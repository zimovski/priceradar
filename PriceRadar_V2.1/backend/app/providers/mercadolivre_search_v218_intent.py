from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .mercadolivre import MercadoLivreError, MercadoLivreProvider
from .mercadolivre_search_enhancement import _domain_for_query, _family, _generation, _query_has_generation
from .mercadolivre_search_v215_light import _search_detail
from .search_intent_v218 import acceptable, query_variants, relevance_score

_PATCHED = False


def enable_intent_search_v218() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def search(self: MercadoLivreProvider, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10))
        variants = query_variants(query)
        merged: dict[str, dict[str, Any]] = {}

        for variant in variants:
            expected_domain = _domain_for_query(variant)
            params: dict[str, Any] = {
                "status": "active",
                "site_id": "MLB",
                "q": variant,
                "limit": 50,
            }
            if expected_domain:
                params["domain_id"] = expected_domain
            try:
                payload = self._request("GET", "/products/search", params=params)
                rows = payload.get("results") or []
                if not rows and expected_domain:
                    params.pop("domain_id", None)
                    rows = (self._request("GET", "/products/search", params=params).get("results") or [])
            except MercadoLivreError:
                continue

            for row in rows[:80]:
                pid = str(row.get("id") or "")
                if not pid:
                    continue
                current = merged.get(pid)
                score = relevance_score(query, row.get("name"))
                if score <= -400:
                    continue
                item = dict(row)
                item["_intent_score"] = score
                if not current or score > float(current.get("_intent_score") or -999):
                    merged[pid] = item
            if len(merged) >= 25:
                break

        if not merged:
            return []

        raw = list(merged.values())
        fam = _family(query)
        newest = None
        if fam and not _query_has_generation(query, fam):
            gens = [_generation(row.get("name"), fam) for row in raw]
            gens = [g for g in gens if g is not None]
            if gens:
                newest = max(gens)

        def rank(row: dict[str, Any]) -> float:
            score = float(row.get("_intent_score") or relevance_score(query, row.get("name")))
            if fam and newest is not None and not _query_has_generation(query, fam):
                gen = _generation(row.get("name"), fam)
                if gen is not None:
                    score += (gen - newest) * 3.0
            return score

        ranked = sorted(raw, key=rank, reverse=True)
        candidates = [row for row in ranked if acceptable(query, row.get("name"))][: max(limit + 2, 8)]
        if not candidates:
            return []

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
                    if detail:
                        details[pid] = detail
                except Exception:
                    pass

        results: list[dict[str, Any]] = []
        for row in candidates:
            pid = str(row.get("id") or "")
            detail = details.get(pid)
            if not detail or detail.get("price") is None:
                continue
            if not acceptable(query, detail.get("name")):
                continue
            detail["_intent_score"] = relevance_score(query, detail.get("name"))
            results.append(detail)

        results.sort(
            key=lambda row: (float(row.get("_intent_score") or -999), -(float(row.get("price") or 10**18))),
            reverse=True,
        )
        for row in results:
            row.pop("_intent_score", None)
        return results[:limit]

    MercadoLivreProvider.search = search
