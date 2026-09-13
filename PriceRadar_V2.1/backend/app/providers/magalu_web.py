from __future__ import annotations

import json
import re
import time
from urllib.parse import quote, urljoin, urlparse
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .mercadolivre_search_enhancement import (
    _family, _generation, _norm, _score, _tokens, _looks_like_accessory,
    _query_wants_accessory, _device_intent,
)


class MagaluError(RuntimeError):
    pass


class MagaluProvider:
    slug = "magalu"
    name = "Magazine Luiza"
    base_url = "https://www.magazineluiza.com.br"
    timeout = 8.0
    cache_ttl = 300
    _cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}

    @staticmethod
    def _money(value: str | float | int | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value) if float(value) > 0 else None
        try:
            return float(str(value).replace(".", "").replace(",", "."))
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
        title = re.sub(r"\s+\d\.\d\s*\(\d+\)\s*$", "", title)
        return title.strip(" -|")[:500]

    @staticmethod
    def _request_headers() -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.7,en;q=0.6",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        }

    def _get_html(self, url: str) -> str:
        timeout = httpx.Timeout(self.timeout, connect=min(4.0, self.timeout))
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=self._request_headers(), http2=False) as client:
                response = client.get(url)
        except httpx.RequestError as exc:
            raise MagaluError("Não consegui alcançar a vitrine do Magazine Luiza agora.") from exc
        if response.status_code >= 400:
            raise MagaluError(f"Magazine Luiza respondeu {response.status_code}.")
        text = response.text or ""
        if len(text) < 500:
            raise MagaluError("A vitrine do Magalu respondeu sem conteúdo utilizável.")
        return text

    def _search_urls(self, query: str) -> list[str]:
        normalized = " ".join(query.strip().split())
        encoded_plus = quote(normalized.replace(" ", "+"), safe="")
        encoded_space = quote(normalized, safe="")
        # The normal SSR URL is first because it currently exposes full product
        # cards (title + price + /p/{id}/ link) to ordinary browsers. bypass is
        # kept only as a fallback because its markup is not always identical.
        return [
            f"{self.base_url}/busca/{encoded_plus}/",
            f"{self.base_url}/busca/{encoded_space}/",
            f"{self.base_url}/busca/{encoded_plus}/?bypass=true",
            f"https://m.magazineluiza.com.br/busca/{encoded_plus}/",
        ]

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

    @staticmethod
    def _find_price_container(anchor):
        node = anchor
        for _ in range(6):
            text = " ".join(node.stripped_strings)
            if "R$" in text and len(text) <= 5000:
                return node
            if not getattr(node, "parent", None):
                break
            node = node.parent
        return anchor

    @staticmethod
    def _candidate_image(node) -> str | None:
        img = node.find("img")
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

    def _parse_json_ld(self, soup: BeautifulSoup) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        def walk(value: Any):
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return
            graph = value.get("@graph")
            if graph:
                walk(graph)
            items = value.get("itemListElement")
            if items:
                walk(items)
            item = value.get("item")
            if isinstance(item, dict):
                walk(item)

            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if "Product" not in kinds:
                return
            name = self._clean_title(str(value.get("name") or ""))
            url = str(value.get("url") or "")
            product_id = self._product_id(url)
            offers = value.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}
            price = self._money(offers.get("price") or offers.get("lowPrice"))
            if not (name and product_id and price):
                return
            image = value.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            rows.append({
                "external_product_id": product_id,
                "name": name,
                "brand": (value.get("brand") or {}).get("name") if isinstance(value.get("brand"), dict) else None,
                "model": value.get("model"),
                "gtin": value.get("gtin13") or value.get("gtin14") or value.get("sku"),
                "image_url": str(image) if image else None,
                "url": urljoin(self.base_url, url),
                "item_id": product_id,
                "price": price,
                "pix_price": price,
                "original_price": None,
                "currency": offers.get("priceCurrency") or "BRL",
                "seller_name": None,
                "shipping_free": None,
                "available": True,
            })

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text("", strip=True)
            if not raw:
                continue
            try:
                walk(json.loads(raw))
            except Exception:
                continue
        return rows

    def _parse_search_cards(self, html: str, query: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []

        # Prefer explicit structured data when present.
        for row in self._parse_json_ld(soup):
            pid = str(row.get("external_product_id") or "")
            if pid and pid not in seen:
                seen.add(pid)
                rows.append(row)

        for anchor in soup.find_all("a", href=True):
            href = urljoin(self.base_url, anchor.get("href"))
            parsed = urlparse(href)
            if not parsed.hostname or not parsed.hostname.endswith("magazineluiza.com.br"):
                continue
            product_id = self._product_id(href)
            if not product_id or product_id in seen:
                continue

            container = self._find_price_container(anchor)
            text = " ".join(container.stripped_strings)
            if "R$" not in text:
                continue
            title = self._candidate_title(anchor)
            if len(title) < 4:
                for selector in ("h2", "h3", "h4"):
                    node = container.select_one(selector)
                    if node:
                        title = self._clean_title(node.get_text(" ", strip=True))
                        break
            if len(title) < 4:
                continue

            values = self._money_values(text)
            if not values:
                continue
            pix_match = re.search(r"R\$\s*([0-9][0-9\.]*,[0-9]{2})\s+no\s+Pix", text, flags=re.I)
            pix_price = self._money(pix_match.group(1)) if pix_match else values[0]
            regular_match = re.search(r"\bOu\s+R\$\s*([0-9][0-9\.]*,[0-9]{2})", text, flags=re.I)
            regular_price = self._money(regular_match.group(1)) if regular_match else values[0]
            if regular_price is None or regular_price <= 0:
                regular_price = pix_price
            if pix_price is None or pix_price <= 0:
                pix_price = regular_price

            seen.add(product_id)
            rows.append({
                "external_product_id": product_id,
                "name": title,
                "brand": None,
                "model": None,
                "gtin": None,
                "image_url": self._candidate_image(container),
                "url": href,
                "item_id": product_id,
                "price": regular_price,
                "pix_price": pix_price,
                "original_price": regular_price if regular_price and pix_price and regular_price > pix_price else None,
                "currency": "BRL",
                "seller_name": None,
                "shipping_free": None,
                "available": True,
            })

        # Markup changes sometimes move all textual card content outside the
        # anchor. As a final parser fallback, inspect a bounded chunk after every
        # /p/{id}/ URL and recover a nearby R$ amount + human-readable title.
        if not rows:
            for match in re.finditer(r'href=["\']([^"\']+/p/([^/?#"\']+)/[^"\']*)["\']', html, flags=re.I):
                href = urljoin(self.base_url, match.group(1))
                product_id = match.group(2)
                if product_id in seen:
                    continue
                chunk = re.sub(r"<[^>]+>", " ", html[match.start(): match.start() + 6000])
                chunk = " ".join(chunk.split())
                values = self._money_values(chunk)
                if not values:
                    continue
                before_price = re.split(r"\b(?:Preço|R\$)\b", chunk, maxsplit=1, flags=re.I)[0]
                title = self._clean_title(before_price[-500:])
                if len(title) < 8:
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
                    "price": values[0],
                    "pix_price": values[0],
                    "original_price": None,
                    "currency": "BRL",
                    "seller_name": None,
                    "shipping_free": None,
                    "available": True,
                })
                if len(rows) >= 30:
                    break
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
        q = _norm(query)
        n = _norm(name)
        if ("rtx" in q or "geforce" in q) and ("ti" in q) != (re.search(r"\bti\b", n) is not None):
            return False
        coverage = sum(1 for token in q_tokens if token in words) / len(q_tokens)
        return coverage >= (0.67 if _device_intent(query) else 0.5)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 12))
        key = (_norm(query), limit)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return [dict(x) for x in cached[1]]

        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for url in self._search_urls(query):
            try:
                html = self._get_html(url)
                rows = self._parse_search_cards(html, query)
                if rows:
                    break
                errors.append("página respondeu, mas nenhum card de produto foi reconhecido")
            except MagaluError as exc:
                errors.append(str(exc))
        if not rows and errors:
            raise MagaluError(errors[-1])

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
        pix_match = re.search(r"Preço\s+R\$\s*([0-9][0-9\.]*,[0-9]{2}).{0,120}?no\s+Pix", page_text, flags=re.I)
        if not pix_match:
            pix_match = re.search(r"R\$\s*([0-9][0-9\.]*,[0-9]{2})\s+no\s+Pix", page_text, flags=re.I)
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
