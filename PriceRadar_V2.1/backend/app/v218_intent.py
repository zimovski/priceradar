from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Product, ProductSourceLink
from .providers.magalu_web import MagaluError, MagaluProvider
from .providers.mercadolivre import MercadoLivreError, MercadoLivreProvider
from .providers.search_intent_v218 import (
    acceptable,
    broad_product_query,
    consumer_similarity,
    query_variants,
    relevance_score,
)
from .schemas import ProviderStatus, SearchResponse
from .services.collector import record_magalu_detail, record_ml_detail
from .v211_features import _append_external, _local_results
from .v217_similarity import _save_link, v217_web_preview


def _remove_route(path: str, method: str = "GET") -> None:
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()))
    ]


async def _bounded(call: Callable[[], list[dict[str, Any]]], seconds: float):
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(call), timeout=seconds)
        return rows, None
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as exc:
        return None, exc


def _magalu_intent_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Search Magalu with shopper-language variants and rank against the original intent.

    We deliberately parse the public search cards before applying our own
    semantic filter. This avoids retailer wording differences such as
    'máquina de lavar' versus 'lavadora de roupas'.
    """
    provider = MagaluProvider()
    provider.timeout = 7.5
    merged: dict[str, dict[str, Any]] = {}
    last_error: Exception | None = None

    for variant in query_variants(query)[:3]:
        got_page = False
        for url in provider._search_urls(variant)[:2]:
            try:
                html = provider._get_html(url)
                rows = provider._parse_search_cards(html, variant)
                got_page = True
            except MagaluError as exc:
                last_error = exc
                continue

            for row in rows:
                if not acceptable(query, row.get("name")):
                    continue
                key = str(row.get("external_product_id") or row.get("url") or row.get("name") or "")
                if not key:
                    continue
                item = dict(row)
                item["_intent_score"] = relevance_score(query, row.get("name"))
                previous = merged.get(key)
                if not previous or float(item["_intent_score"]) > float(previous.get("_intent_score") or -999):
                    merged[key] = item
            if merged or got_page:
                break
        if len(merged) >= max(5, limit):
            break

    if not merged and last_error:
        raise MagaluError(str(last_error))

    rows = list(merged.values())
    rows.sort(
        key=lambda row: (
            float(row.get("_intent_score") or -999),
            -(float(row.get("pix_price") or row.get("price") or 10**18)),
        ),
        reverse=True,
    )
    for row in rows:
        row.pop("_intent_score", None)
    return rows[:limit]


def _best_magalu_for_product(product: Product) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    source = " ".join(x for x in (product.brand, product.name, product.model) if x)
    broad = broad_product_query(source)
    queries = [broad]
    if broad.lower() != product.name.lower():
        queries.append(product.name)

    candidates: dict[str, dict[str, Any]] = {}
    for query in queries:
        try:
            rows = _magalu_intent_search(query, limit=12)
        except Exception:
            continue
        for row in rows:
            key = str(row.get("external_product_id") or row.get("url") or row.get("name") or "")
            if not key:
                continue
            score, level, notes = consumer_similarity(source, str(row.get("name") or ""))
            item = dict(row)
            item["_match_score"] = score
            item["_match_level"] = level
            item["_match_notes"] = notes
            previous = candidates.get(key)
            if not previous or score > int(previous.get("_match_score") or 0):
                candidates[key] = item
        if any(int(x.get("_match_score") or 0) >= 68 for x in candidates.values()):
            break

    rows = list(candidates.values())
    purchasable = [
        row for row in rows
        if row.get("price") and row.get("url") and int(row.get("_match_score") or 0) >= 42
    ]
    if not purchasable:
        return None, rows

    purchasable.sort(key=lambda row: int(row.get("_match_score") or 0), reverse=True)
    best_score = int(purchasable[0].get("_match_score") or 0)
    band = [row for row in purchasable if int(row.get("_match_score") or 0) >= best_score - 7]
    band.sort(key=lambda row: float(row.get("pix_price") or row.get("price") or 10**18))
    return band[0], rows


_remove_route("/api/search", "GET")


@app.get("/api/search", response_model=SearchResponse)
async def v218_search(
    q: str = Query(..., min_length=2, max_length=240),
    limit: int = Query(12, ge=1, le=30),
    db: Session = Depends(get_db),
):
    query = " ".join(q.strip().split())
    results = _local_results(db, query, min(limit, 4))
    statuses: list[ProviderStatus] = []

    known_ml = {
        str(link.external_product_id): link.product_id
        for link in db.scalars(select(ProductSourceLink).where(ProductSourceLink.provider_slug == "mercadolivre")).all()
    }
    known_magalu = {
        str(link.external_product_id): link.product_id
        for link in db.scalars(select(ProductSourceLink).where(ProductSourceLink.provider_slug == "magalu")).all()
    }

    ml = MercadoLivreProvider()
    ml.timeout = 7.0
    tasks: list[tuple[str, asyncio.Task]] = []
    if ml.configured():
        tasks.append(("mercadolivre", asyncio.create_task(_bounded(lambda: ml.search(query, limit=min(limit, 8)), 10.5))))
    else:
        statuses.append(ProviderStatus(
            slug="mercadolivre", name="Mercado Livre", configured=False, ok=False,
            message="Conexão OAuth ainda não configurada.",
        ))

    tasks.append(("magalu", asyncio.create_task(_bounded(lambda: _magalu_intent_search(query, min(limit, 8)), 10.5))))

    for slug, task in tasks:
        rows, error = await task
        if slug == "mercadolivre":
            if rows is not None:
                _append_external(results, "mercadolivre", "Mercado Livre", rows, known_ml)
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=True,
                    message=f"{len(rows)} resultados úteis.",
                ))
            else:
                message = "timeout" if error == "timeout" else str(error)
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=False, message=message,
                ))
        else:
            if rows is not None:
                _append_external(results, "magalu", "Magazine Luiza", rows, known_magalu)
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=True,
                    message=f"{len(rows)} resultados úteis.",
                ))
            else:
                message = "timeout" if error == "timeout" else str(error)
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=False, message=message,
                ))

    # De-duplicate repeated cards while keeping separate store offers visible.
    deduped = []
    seen: set[str] = set()
    for row in sorted(results, key=lambda r: (r.price is not None, r.source != "local"), reverse=True):
        key = row.result_key
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    return SearchResponse(query=query, results=deduped[: max(limit, 12)], providers=statuses)


_remove_route("/api/products/{product_id}/refresh", "POST")


@app.post("/api/products/{product_id}/refresh")
def v218_refresh(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")

    observations = 0
    sources_ok = 0
    messages: list[str] = []

    ml_links = db.scalars(select(ProductSourceLink).where(
        ProductSourceLink.product_id == product_id,
        ProductSourceLink.provider_slug == "mercadolivre",
    )).all()
    for link in ml_links:
        try:
            detail = MercadoLivreProvider().product_detail(link.external_product_id)
            if detail.get("url"):
                link.source_url = detail.get("url")
            if record_ml_detail(db, product, detail):
                observations += 1
            if detail.get("price"):
                sources_ok += 1
        except MercadoLivreError as exc:
            messages.append(f"Mercado Livre: {exc}")

    try:
        candidate, _all = _best_magalu_for_product(product)
        if candidate:
            _save_link(db, product, candidate)
            clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
            level = str(candidate.get("_match_level") or "similar")
            score = int(candidate.get("_match_score") or 0)
            if record_magalu_detail(db, product, clean, source_name=f"magalu_{level}_{score}"):
                observations += 1
            sources_ok += 1
        else:
            messages.append("Magazine Luiza: nenhuma alternativa suficientemente próxima foi localizada nesta tentativa.")
    except Exception as exc:
        messages.append(f"Magazine Luiza: {type(exc).__name__}: {exc}")

    db.commit()
    return {
        "product_id": product_id,
        "ok": sources_ok > 0,
        "observations": observations,
        "sources_ok": sources_ok,
        "message": "; ".join(messages) or None,
    }


@app.get("/api/search-intent-diagnostics")
def search_intent_diagnostics(q: str = Query(..., min_length=2, max_length=160)):
    query = " ".join(q.strip().split())
    return {"query": query, "variants": query_variants(query), "broad": broad_product_query(query)}


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v218_web_preview():
    base = v217_web_preview()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.replace("V2.17", "V2.18")
    text = text.replace(
        "Tente marca + modelo.",
        "Digite como você pesquisaria normalmente. Ex.: máquina de lavar 17kg, iPhone 17 ou RTX 5070.",
    )
    text = text.replace(
        "Só mostramos preço real de fontes já conectadas.",
        "Comparamos a intenção de compra entre lojas. Quando não for o mesmo SKU, sinalizamos uma alternativa equivalente ou similar.",
    )
    # The Magalu connector is live; do not present it as an upcoming integration.
    text = text.replace("Magazine Luiza</span><span class=\"tag soon\">em breve", "Magazine Luiza</span><span class=\"tag\">conector ativo")
    return HTMLResponse(text)
