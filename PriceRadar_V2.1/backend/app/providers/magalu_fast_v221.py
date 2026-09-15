from __future__ import annotations

from urllib.parse import quote

from .magalu_reader_v220 import _parse_reader_markdown, _reader_get
from .magalu_web import MagaluError, MagaluProvider
from .search_intent_v218 import acceptable, query_variants, relevance_score


def _rank(query: str, rows: list[dict], limit: int) -> list[dict]:
    out: dict[str, dict] = {}
    for row in rows or []:
        title = str(row.get("name") or "")
        if not acceptable(query, title):
            continue
        if not (row.get("price") or row.get("pix_price")) or not row.get("url"):
            continue
        item = dict(row)
        item["_score"] = relevance_score(query, title)
        key = str(item.get("external_product_id") or item.get("url") or title)
        old = out.get(key)
        if old is None or float(item["_score"]) > float(old.get("_score") or -999):
            out[key] = item
    rows2 = list(out.values())
    rows2.sort(
        key=lambda x: (float(x.get("_score") or -999), -(float(x.get("pix_price") or x.get("price") or 10**18))),
        reverse=True,
    )
    for row in rows2:
        row.pop("_score", None)
    return rows2[:limit]


def magalu_search_fast_v221(query: str, limit: int = 8) -> list[dict]:
    """Bounded Magalu public-store search.

    The old connector could try many URLs and then wait for all threads, causing
    18-second timeouts. This version tries at most two retailer-friendly query
    variants and, per variant, only one mobile storefront request plus one
    browser-rendered Reader fallback. It never uses Jina Search, whose anonymous
    endpoint is blocked without an API key.
    """
    limit = max(1, min(int(limit), 12))
    provider = MagaluProvider()
    provider.timeout = 5.5
    variants = query_variants(query)[:2] or [query]
    errors: list[str] = []

    for variant in variants:
        encoded = quote(" ".join(variant.split()).replace(" ", "+"), safe="+")
        mobile_url = f"https://m.magazineluiza.com.br/busca/{encoded}/"

        # 1) Real mobile storefront. The existing provider is patched with a
        # browser-like TLS fingerprint and falls back to ordinary HTTP.
        try:
            html = provider._get_html(mobile_url)
            rows = _rank(query, provider._parse_search_cards(html, variant), limit)
            if rows:
                for row in rows:
                    row["_source"] = "magalu_mobile_storefront"
                return rows
        except Exception as exc:
            errors.append(f"mobile: {type(exc).__name__}: {exc}")

        # 2) Browser-rendered public search page. Reader is available without a
        # key at a small rate limit and avoids the datacenter HTML challenge.
        try:
            markdown = _reader_get(mobile_url, timeout=8.5)
            rows = _rank(query, _parse_reader_markdown(markdown, query, max(limit, 10)), limit)
            if rows:
                for row in rows:
                    row["_source"] = "magalu_reader"
                return rows
        except Exception as exc:
            errors.append(f"reader: {type(exc).__name__}: {exc}")

    if errors:
        raise MagaluError(" | ".join(errors[-2:]))
    return []
