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
from .v217_similarity import _save_link
from .v218_intent import v218_web_preview


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


def _magalu_search_v219(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Search every useful Magalu storefront variant before giving up.

    V2.18 stopped after the first HTML page even when that page contained zero
    useful cards. That made a generic/anti-bot page look like a valid empty
    search. V2.19 only stops once actual matching products were parsed.
    """
    provider = MagaluProvider()
    provider.timeout = 8.5
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    parsed_any_page = False

    for variant in query_variants(query)[:4]:
        for url in provider._search_urls(variant):
            try:
                html = provider._get_html(url)
                parsed_any_page = True
                rows = provider._parse_search_cards(html, variant)
            except MagaluError as exc:
                errors.append(str(exc))
                continue

            for row in rows:
                title = str(row.get("name") or "")
                if not acceptable(query, title):
                    continue
                key = str(row.get("external_product_id") or row.get("url") or title)
                if not key:
                    continue
                item = dict(row)
                item["_score"] = relevance_score(query, title)
                old = merged.get(key)
                if old is None or float(item["_score"]) > float(old.get("_score") or -999):
                    merged[key] = item

            if len(merged) >= max(5, limit):
                break
        if len(merged) >= max(5, limit):
            break

    if not merged and errors and not parsed_any_page:
        raise MagaluError(errors[-1])

    rows = list(merged.values())
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


def _best_magalu_v219(product: Product) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    source = " ".join(x for x in (product.brand, product.name, product.model) if x)
    broad = broad_product_query(source)
    queries = [broad]
    if broad.lower() != product.name.lower():
        queries.append(product.name)

    found: dict[str, dict[str, Any]] = {}
    for query in queries:
        try:
            rows = _magalu_search_v219(query, limit=14)
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
            previous = found.get(key)
            if previous is None or score > int(previous.get("_match_score") or 0):
                found[key] = item
        if any(int(x.get("_match_score") or 0) >= 65 for x in found.values()):
            break

    rows = list(found.values())
    purchasable = [
        x for x in rows
        if x.get("price") and x.get("url") and int(x.get("_match_score") or 0) >= 35
    ]
    if not purchasable:
        return None, rows

    purchasable.sort(key=lambda x: int(x.get("_match_score") or 0), reverse=True)
    best_score = int(purchasable[0].get("_match_score") or 0)
    band = [x for x in purchasable if int(x.get("_match_score") or 0) >= best_score - 8]
    band.sort(key=lambda x: float(x.get("pix_price") or x.get("price") or 10**18))
    return band[0], rows


_remove_route("/api/search", "GET")


@app.get("/api/search", response_model=SearchResponse)
async def v219_search(
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

    tasks: list[tuple[str, asyncio.Task]] = []
    ml = MercadoLivreProvider()
    ml.timeout = 8.0
    if ml.configured():
        tasks.append((
            "mercadolivre",
            asyncio.create_task(_bounded(lambda: ml.search(query, limit=min(limit, 10)), 12.0)),
        ))
    else:
        statuses.append(ProviderStatus(
            slug="mercadolivre", name="Mercado Livre", configured=False, ok=False,
            message="Conexão OAuth ainda não configurada.",
        ))

    tasks.append((
        "magalu",
        asyncio.create_task(_bounded(lambda: _magalu_search_v219(query, min(limit, 10)), 13.0)),
    ))

    for slug, task in tasks:
        rows, error = await task
        if slug == "mercadolivre":
            if rows is not None:
                _append_external(results, "mercadolivre", "Mercado Livre", rows, known_ml)
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=True,
                    message=f"{len(rows)} ofertas reais retornadas.",
                ))
            else:
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=False,
                    message="timeout" if error == "timeout" else str(error),
                ))
        else:
            if rows is not None:
                _append_external(results, "magalu", "Magazine Luiza", rows, known_magalu)
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=True,
                    message=f"{len(rows)} ofertas reais retornadas.",
                ))
            else:
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=False,
                    message="timeout" if error == "timeout" else str(error),
                ))

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
def v219_refresh(product_id: int, db: Session = Depends(get_db)):
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
        candidate, _all = _best_magalu_v219(product)
        if candidate:
            _save_link(db, product, candidate)
            clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
            level = str(candidate.get("_match_level") or "similar")
            score = int(candidate.get("_match_score") or 0)
            if record_magalu_detail(db, product, clean, source_name=f"magalu_{level}_{score}"):
                observations += 1
            sources_ok += 1
        else:
            messages.append("Magazine Luiza: não encontrei uma oferta próxima com preço e link nesta tentativa.")
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


@app.get("/api/v219/provider-diagnostics")
async def v219_provider_diagnostics(q: str = Query("maquina de lavar 17kg", min_length=2, max_length=160)):
    query = " ".join(q.strip().split())
    output: dict[str, Any] = {"version": "2.19", "query": query, "variants": query_variants(query), "providers": {}}

    ml = MercadoLivreProvider()
    try:
        rows, error = await _bounded(lambda: ml.search(query, limit=3), 12.0)
        output["providers"]["mercadolivre"] = {
            "ok": rows is not None,
            "count": len(rows or []),
            "error": None if rows is not None else ("timeout" if error == "timeout" else str(error)),
            "examples": [{"name": x.get("name"), "price": x.get("price"), "url": x.get("url")} for x in (rows or [])[:3]],
        }
    except Exception as exc:
        output["providers"]["mercadolivre"] = {"ok": False, "count": 0, "error": f"{type(exc).__name__}: {exc}"}

    try:
        rows, error = await _bounded(lambda: _magalu_search_v219(query, 3), 13.0)
        output["providers"]["magalu"] = {
            "ok": rows is not None,
            "count": len(rows or []),
            "error": None if rows is not None else ("timeout" if error == "timeout" else str(error)),
            "examples": [{"name": x.get("name"), "price": x.get("price"), "url": x.get("url")} for x in (rows or [])[:3]],
        }
    except Exception as exc:
        output["providers"]["magalu"] = {"ok": False, "count": 0, "error": f"{type(exc).__name__}: {exc}"}
    return output


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v219_web_preview():
    base = v218_web_preview()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.replace("V2.18", "V2.19")
    text = text.replace("Consultando lojas...", "Buscando ofertas reais nas lojas...")
    text = text.replace("Consultando Mercado Livre...", "Buscando ofertas reais nas lojas...")
    text = text.replace(
        "Mercado Livre é a fonte ativa. KaBuM, Magalu e Casas Bahia entram nas próximas etapas.",
        "Mercado Livre e Magazine Luiza estão ativos. KaBuM e Casas Bahia são as próximas integrações.",
    )
    return HTMLResponse(text)
