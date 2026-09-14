from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from .magalu_web import MagaluError, MagaluProvider
from .search_intent_v218 import acceptable, query_variants, relevance_score

READER_BASE = "https://r.jina.ai/"
SEARCH_BASE = "https://s.jina.ai/"


def _clean_md(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")
    text = re.sub(r"[`*_#>]", " ", text)
    return " ".join(text.split())


def _price_from_window(provider: MagaluProvider, text: str) -> tuple[float | None, float | None]:
    pix_matches = re.findall(
        r"R\$\s*([0-9][0-9\.]*,[0-9]{2})\s*(?:no\s+Pix|à\s+vista\s+no\s+Pix)",
        text,
        flags=re.I,
    )
    pix = provider._money(pix_matches[-1]) if pix_matches else None
    values = provider._money_values(text)
    values = [v for v in values if v and v > 0]
    if not values:
        return None, None
    if pix is None:
        pix = min(values)
    regular_candidates = [v for v in values if v >= pix]
    regular = max(regular_candidates[:3]) if regular_candidates else pix
    if regular > pix * 2.2:
        regular = pix
    return regular, pix


def _reader_get(target_url: str, timeout: float = 14.0) -> str:
    url = f"{READER_BASE}{target_url}"
    headers = {
        "Accept": "text/markdown",
        "X-Engine": "browser",
        "X-Timeout": "9",
        "X-Cache-Tolerance": "120",
        "User-Agent": "PriceRadar/0.2 (+price-comparison prototype)",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise MagaluError("O fallback renderizado do Magalu não respondeu.") from exc
    if response.status_code >= 400:
        raise MagaluError(f"Fallback renderizado do Magalu respondeu {response.status_code}.")
    text = response.text or ""
    if len(text) < 300:
        raise MagaluError("Fallback renderizado do Magalu respondeu sem conteúdo útil.")
    return text


def _site_search_get(query: str, timeout: float = 14.0) -> str:
    encoded = quote(query, safe="")
    url = f"{SEARCH_BASE}{encoded}?site=magazineluiza.com.br"
    headers = {
        "Accept": "text/markdown",
        "User-Agent": "PriceRadar/0.2 (+price-comparison prototype)",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise MagaluError("A busca renderizada do Magalu não respondeu.") from exc
    if response.status_code >= 400:
        raise MagaluError(f"Busca renderizada do Magalu respondeu {response.status_code}.")
    text = response.text or ""
    if len(text) < 300:
        raise MagaluError("Busca renderizada do Magalu respondeu sem conteúdo útil.")
    return text


def _parse_reader_markdown(markdown: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
    provider = MagaluProvider()
    link_re = re.compile(
        r"\[([^\]]{4,500})\]\((https?://(?:www\.|m\.)?magazineluiza\.com\.br/[^)\s]*?/p/[^)\s]+)\)",
        flags=re.I,
    )
    matches = list(link_re.finditer(markdown))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for idx, match in enumerate(matches):
        title = provider._clean_title(_clean_md(match.group(1)))
        href = match.group(2).replace("&amp;", "&")
        product_id = provider._product_id(href)
        if not product_id or product_id in seen or len(title) < 4:
            continue
        if not acceptable(query, title):
            continue

        # Price should belong to this card/result, not the next one.
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(markdown), match.end() + 1400)
        start = max(0, match.start() - 160)
        end = min(next_start, match.end() + 1400)
        window = _clean_md(markdown[start:end])
        regular, pix = _price_from_window(provider, window)
        if regular is None and pix is None:
            continue
        price = regular or pix
        if price is None or price <= 0:
            continue

        seen.add(product_id)
        rows.append({
            "external_product_id": product_id,
            "name": title,
            "brand": None,
            "model": None,
            "gtin": None,
            "image_url": None,
            "url": href,
            "item_id": product_id,
            "price": float(price),
            "pix_price": float(pix or price),
            "original_price": float(regular) if regular and pix and regular > pix else None,
            "currency": "BRL",
            "seller_name": None,
            "shipping_free": None,
            "available": True,
            "_score": relevance_score(query, title),
            "_source": "magalu_reader_rendered",
        })
        if len(rows) >= max(limit * 3, 20):
            break

    rows.sort(
        key=lambda row: (
            float(row.get("_score") or -999),
            -(float(row.get("pix_price") or row.get("price") or 10**18)),
        ),
        reverse=True,
    )
    for row in rows:
        row.pop("_score", None)
    return rows[:limit]


def magalu_reader_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Rendered fallback for Magalu via Reader, then site-scoped web search.

    Render/datacenter requests can receive challenge or incomplete storefront
    markup. Reader renders the public Magalu page in a browser. If a search page
    still yields no direct product cards, the site-scoped Search API locates
    public Magalu product pages and their rendered price text.
    """
    provider = MagaluProvider()
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for variant in query_variants(query)[:3]:
        target = provider._search_urls(variant)[0]
        try:
            markdown = _reader_get(target)
            rows = _parse_reader_markdown(markdown, query, limit=max(limit, 10))
        except MagaluError as exc:
            errors.append(str(exc))
            rows = []
        for row in rows:
            key = str(row.get("external_product_id") or row.get("url") or "")
            if key and key not in merged:
                merged[key] = row
        if len(merged) >= limit:
            break

    if len(merged) < limit:
        try:
            markdown = _site_search_get(query)
            rows = _parse_reader_markdown(markdown, query, limit=max(limit, 10))
            for row in rows:
                row["_source"] = "magalu_reader_site_search"
                key = str(row.get("external_product_id") or row.get("url") or "")
                if key and key not in merged:
                    merged[key] = row
        except MagaluError as exc:
            errors.append(str(exc))

    if not merged and errors:
        raise MagaluError(errors[-1])
    return list(merged.values())[:limit]
