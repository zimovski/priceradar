from __future__ import annotations

from urllib.parse import quote

import httpx

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


def _direct_mobile(provider: MagaluProvider, url: str) -> str:
    """One bounded network attempt; do not chain two 5s clients before fallback."""
    headers = provider._request_headers()
    try:
        from curl_cffi import requests as curl_requests
        response = curl_requests.get(
            url,
            headers=headers,
            timeout=4.2,
            impersonate="chrome",
            allow_redirects=True,
        )
        text = response.text or ""
        if response.status_code < 400 and len(text) >= 500:
            return text
        raise MagaluError(f"Magalu mobile respondeu {response.status_code}.")
    except ImportError:
        pass
    except Exception as exc:
        raise MagaluError(f"Magalu mobile direto falhou: {type(exc).__name__}: {exc}") from exc

    try:
        with httpx.Client(timeout=httpx.Timeout(4.2, connect=2.5), follow_redirects=True, headers=headers) as client:
            response = client.get(url)
    except httpx.RequestError as exc:
        raise MagaluError("Magalu mobile direto não respondeu.") from exc
    text = response.text or ""
    if response.status_code >= 400 or len(text) < 500:
        raise MagaluError(f"Magalu mobile respondeu {response.status_code} sem conteúdo útil.")
    return text


def magalu_search_fast_v221(query: str, limit: int = 8) -> list[dict]:
    """Fast public Magalu search with one direct attempt and one Reader fallback."""
    limit = max(1, min(int(limit), 12))
    provider = MagaluProvider()
    variants = query_variants(query)[:2] or [query]
    errors: list[str] = []

    for variant in variants:
        encoded = quote(" ".join(variant.split()).replace(" ", "+"), safe="")
        mobile_url = f"https://m.magazineluiza.com.br/busca/{encoded}/"

        try:
            html = _direct_mobile(provider, mobile_url)
            rows = _rank(query, provider._parse_search_cards(html, variant), limit)
            if rows:
                for row in rows:
                    row["_source"] = "magalu_mobile_storefront"
                return rows
        except Exception as exc:
            errors.append(f"mobile: {type(exc).__name__}: {exc}")

        # Reader supports anonymous URL reading (small rate limit). We do not use
        # s.jina.ai search here because anonymous Search is blocked without a key.
        try:
            markdown = _reader_get(mobile_url, timeout=7.2)
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
