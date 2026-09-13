from __future__ import annotations

import re
import time
from urllib.parse import quote, urljoin, urlparse
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .mercadolivre_search_enhancement import _family, _generation, _norm, _score, _tokens, _looks_like_accessory, _query_wants_accessory, _device_intent


class MagaluError(RuntimeError):
    pass


class MagaluProvider:
    """Read-only connector for Magalu's public storefront.

    The official Magalu Open API is aimed at sellers/integrators. PriceRadar is
    a consumer price comparator, so this connector reads only public storefront
    pages that a normal shopper can open. No login, cart or account action is
    performed. If Magalu changes its markup or blocks the request, we fail
    closed and return no invented price.
    """

    slug = "magalu"
    name = "Magazine Luiza"
    base_url = "https://www.magazineluiza.com.br"
    timeout = 20.0
    cache_ttl = 300
    _cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}

    @staticmethod
    def _money(value: str) -> float | None:
        try:
            return float(value.replace(".", "").replace(",", "."))
        except Exception:
            return None

    @classmethod
    def _money_values(cls, text: str) -> list[float]:
        values: list[float] = []
        for raw in re.findall(r"R\$\s*([0-9][0-9\.]*,[0-9]{2})", text or "", flags=re.I):
            value = cls._money(raw)
            if value is not None and value > 0:
                values.append(value)
        return values

    @staticmethod
    def _product_id(url: str) -> str | None:
        match = re.search(r"/p/([^/?#]+)/", url)
        return match.group(1) if match else None

    @staticmethod
    def _clean_title(text: str) -> str:
        title = " ".join((text or "").split())
        title = re.sub(r"^(?:Full\+?\d*\+?\s*)+", "", title, flags=re.I)
        title = re.sub(r"^(?:Patrocinado\s+)+", "", title, flags=re.I)
        title = re.sub(r"^\d+\s+mem[oó]rias?\s+", "", title, flags=re.I)
        title = re.split(r"\b(?:Preço|R\$)\b", title, maxsplit=1, flags=re.I)[0]
        # Search cards often append rating/count before the price. Trim only a
        # trailing rating expression, never model numbers inside the title.
        title = re.sub(r"\s+\d\.\d\s*\(\d+\)\s*$", "", title)
        return title.strip(" -|")[:500]

    @staticmethod
    def _request_headers() -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
            "User-Agent": "PriceRadar/0.2 (+price comparison prototype; public storefront read-only)",
        }

    def _get_html(self, url: str) -> str:
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=self._request_headers()) as client:
                response = client.get(url)
        except httpx.RequestError as exc:
            raise MagaluError("Não consegui consultar o Magazine Luiza agora.") from exc
        if response.status_code >= 400:
            raise MagaluError(f"Magazine Luiza respondeu {response.status_code}.")
        return response.text

    def _search_url(self, query: str) -> str:
        # Magalu accepts '+' separated terms in /busca/. Encode the plus itself
        # in the path so it is not accidentally treated as a literal space by
        # intermediaries.
        normalized = " ".join(query.strip().split())
        path_term = quote(normalized.replace(" ", "+"), safe="")
        return f"{self.base_url}/busca/{path_term}/"

    def _candidate_title(self, anchor) -> str:
        img = anchor.find("img")
        if img:
            alt = (img.get("alt") or "").strip()
            if len(alt) >= 5 and "image:" not in alt.lower():
                return self._clean_title(alt)
        for attr in ("aria-label", "title"):
            value = (anchor.get(attr) or "").strip()
            if len(value) >= 5:
                return self._clean_title(value)
        for selector in ("h2", "h3", "h4", "[data-testid*='title']", "[data-testid*='name']"):
            node = anchor.select_one(selector)
            if node:
                value = self._clean_title(node.get_text(" ", strip=True))
                if len(value) >= 5:
                    return value
        return self._clean_title(anchor.get_text(" ", strip=True))

    def _candidate_image(self, anchor) -> str | None:
        img = anchor.find("img")
        if not img:
            return None
        for attr in ("src", "data-src", "data-original"):
            value = img.get(attr)
            if value and str(value).startswith(("http://", "https://")):
                return str(value)
        srcset = img.get("srcset")
        if srcset:
            first = str(srcset).split(",")[0].strip().split(" ")[0]
            if first.startswith(("http://", "https://")):
                return first
        return None

    def _parse_search_cards(self, html: str, query: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(self.base_url, anchor.get("href"))
            parsed = urlparse(href)
            if not parsed.hostname or not parsed.hostname.endswith("magazineluiza.com.br"):
                continue
            product_id = self._product_id(href)
            if not product_id or product_id in seen:
                continue
            text = " ".join(anchor.stripped_strings)
            if "R$" not in text:
                continue
            title = self._candidate_title(anchor)
            if len(title) < 4:
                continue
            values = self._money_values(text)
            if not values:
                continue

            # On Magalu cards the first displayed amount is normally the Pix /
            # immediate-payment price. A later "Ou R$ ... em Nx" is the regular
            # card price. Store both when we can identify them.
            pix_price = values[0]
            regular_match = re.search(r"\bOu\s+R\$\s*([0-9][0-9\.]*,[0-9]{2})", text, flags=re.I)
            regular_price = self._money(regular_match.group(1)) if regular_match else pix_price
            if regular_price is None or regular_price <= 0:
                regular_price = pix_price

            seen.add(product_id)
            rows.append({
                "external_product_id": product_id,
                "name": title,
                "brand": None,
                "model": None,
                "gtin": None,
                "image_url": self._candidate_image(anchor),
                "url": href,
                "item_id": product_id,
                "price": regular_price,
                "pix_price": pix_price,
                "original_price": regular_price if regular_price > pix_price else None,
                "currency": "BRL",
                "seller_name": None,
                "shipping_free": None,
                "available": True,
            })
        return rows

    @staticmethod
    def _acceptable(query: str, name: str) -> bool:
        q_tokens = _tokens(query)
        words = set(_norm(name).split())
        if not q_tokens:
            return True
        if not _query_wants_accessory(query) and _device_intent(query) and _looks_like_accessory(name):
            return False
        for token in q_tokens:
            if any(ch.isdigit() for ch in token) and token not in words:
                return False
        coverage = sum(1 for token in q_tokens if token in words) / len(q_tokens)
        return coverage >= (0.67 if _device_intent(query) else 0.5)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 12))
        key = (_norm(query), limit)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return [dict(x) for x in cached[1]]

        html = self._get_html(self._search_url(query))
        rows = self._parse_search_cards(html, query)
        fam = _family(query)
        newest = None
        if fam and _generation(query, fam) is None:
            generations = [_generation(row.get("name"), fam) for row in rows]
            generations = [g for g in generations if g is not None]
            if generations:
                newest = max(generations)

        ranked: list[dict[str, Any]] = []
        for row in rows:
            if not self._acceptable(query, row["name"]):
                continue
            row = dict(row)
            row["_relevance"] = _score(query, row["name"], newest_generation=newest)
            row["_generation"] = _generation(row["name"], fam) if fam else None
            ranked.append(row)

        ranked.sort(
            key=lambda x: (x.get("_relevance", -100), x.get("_generation") or -1, -(x.get("pix_price") or x.get("price") or 10**12)),
            reverse=True,
        )
        result = ranked[:limit]
        for row in result:
            row.pop("_relevance", None)
            row.pop("_generation", None)
        self._cache[key] = (time.time(), [dict(x) for x in result])
        return result

    def product_detail(self, url: str, external_product_id: str | None = None) -> dict[str, Any]:
        html = self._get_html(url)
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        title = self._clean_title(h1.get_text(" ", strip=True) if h1 else "")
        if not title:
            title = external_product_id or "Produto Magalu"

        page_text = " ".join(soup.stripped_strings)
        # Prefer the public Pix amount and keep the regular card amount
        # separately whenever the page exposes both.
        pix_match = re.search(r"Preço\s+R\$\s*([0-9][0-9\.]*,[0-9]{2}).{0,100}?no\s+Pix", page_text, flags=re.I)
        if not pix_match:
            pix_match = re.search(r"Preço\s+R\$\s*([0-9][0-9\.]*,[0-9]{2})", page_text, flags=re.I)
        pix_price = self._money(pix_match.group(1)) if pix_match else None
        regular_match = re.search(r"\bOu\s+R\$\s*([0-9][0-9\.]*,[0-9]{2})\s+em\s+\d+x", page_text, flags=re.I)
        regular_price = self._money(regular_match.group(1)) if regular_match else pix_price
        if pix_price is None and regular_price is None:
            raise MagaluError("O Magalu abriu o produto, mas não expôs um preço público confiável.")
        if pix_price is None:
            pix_price = regular_price
        if regular_price is None:
            regular_price = pix_price

        seller_match = re.search(r"Vendido(?:\s+e\s+entregue)?\s+por\s+([^|]{2,80}?)(?=\s+Informações|\s+Preco|\s+Preço|$)", page_text, flags=re.I)
        seller = " ".join(seller_match.group(1).split()) if seller_match else None

        image = None
        meta = soup.find("meta", attrs={"property": "og:image"})
        if meta and meta.get("content"):
            image = str(meta.get("content"))

        product_id = external_product_id or self._product_id(url) or url
        return {
            "external_product_id": str(product_id),
            "name": title,
            "brand": None,
            "model": None,
            "gtin": None,
            "image_url": image,
            "url": url,
            "item_id": str(product_id),
            "price": float(regular_price),
            "pix_price": float(pix_price),
            "original_price": float(regular_price) if regular_price > pix_price else None,
            "currency": "BRL",
            "seller_name": seller,
            "shipping_free": None,
            "available": True,
        }
