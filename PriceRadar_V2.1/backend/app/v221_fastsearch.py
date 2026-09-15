from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Product, ProductSourceLink
from .providers.magalu_fast_v221 import magalu_search_fast_v221
from .providers.mercadolivre import MercadoLivreError, MercadoLivreProvider
from .providers.search_intent_v218 import broad_product_query, consumer_similarity
from .schemas import ProviderStatus, SearchResponse
from .services.collector import record_magalu_detail, record_ml_detail
from .v211_features import _append_external, _local_results
from .v217_similarity import _save_link
from .v220_ui_truth import v220_truthful_ui


def _remove_route(path: str, method: str = "GET") -> None:
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()))
    ]


async def _bounded(call: Callable[[], list[dict[str, Any]]], seconds: float):
    started = time.monotonic()
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(call), timeout=seconds)
        return rows, None, round(time.monotonic() - started, 2)
    except asyncio.TimeoutError:
        return None, "timeout", round(time.monotonic() - started, 2)
    except Exception as exc:
        return None, exc, round(time.monotonic() - started, 2)


def _best_magalu_v221(product: Product) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    source = " ".join(x for x in (product.brand, product.name, product.model) if x)
    broad = broad_product_query(source)
    queries = [broad]
    if product.name and product.name.lower() != broad.lower():
        queries.append(product.name)

    found: dict[str, dict[str, Any]] = {}
    for query in queries[:2]:
        try:
            rows = magalu_search_fast_v221(query, limit=10)
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
            old = found.get(key)
            if old is None or score > int(old.get("_match_score") or 0):
                found[key] = item
        if any(int(x.get("_match_score") or 0) >= 55 for x in found.values()):
            break

    rows = list(found.values())
    purchasable = [
        row for row in rows
        if (row.get("pix_price") or row.get("price"))
        and row.get("url")
        and int(row.get("_match_score") or 0) >= 25
    ]
    if not purchasable:
        return None, rows

    purchasable.sort(key=lambda row: int(row.get("_match_score") or 0), reverse=True)
    best_score = int(purchasable[0].get("_match_score") or 0)
    band = [row for row in purchasable if int(row.get("_match_score") or 0) >= best_score - 12]
    band.sort(key=lambda row: float(row.get("pix_price") or row.get("price") or 10**18))
    return band[0], rows


_remove_route("/api/search", "GET")


@app.get("/api/search", response_model=SearchResponse)
async def v221_search(
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
    tasks = [
        ("mercadolivre", asyncio.create_task(_bounded(lambda: ml.search(query, limit=min(limit, 10)), 8.5))),
        ("magalu", asyncio.create_task(_bounded(lambda: magalu_search_fast_v221(query, min(limit, 10)), 12.0))),
    ]

    for slug, task in tasks:
        rows, error, elapsed = await task
        if slug == "mercadolivre":
            if rows:
                _append_external(results, "mercadolivre", "Mercado Livre", rows, known_ml)
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=True,
                    message=f"{len(rows)} ofertas em {elapsed}s.",
                ))
            else:
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=False,
                    message=("tempo limite excedido" if error == "timeout" else str(error) if error else "nenhuma oferta útil"),
                ))
        else:
            if rows:
                _append_external(results, "magalu", "Magazine Luiza", rows, known_magalu)
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=True,
                    message=f"{len(rows)} ofertas públicas em {elapsed}s.",
                ))
            else:
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=False,
                    message=("tempo limite excedido" if error == "timeout" else str(error) if error else "nenhuma oferta pública útil"),
                ))

    # Store offers first, and within them keep the most relevant provider order.
    deduped = []
    seen: set[str] = set()
    for row in sorted(results, key=lambda r: (r.price is not None, r.source != "local"), reverse=True):
        if row.result_key in seen:
            continue
        seen.add(row.result_key)
        deduped.append(row)

    return SearchResponse(query=query, results=deduped[: max(limit, 12)], providers=statuses)


_remove_route("/api/products/{product_id}/refresh", "POST")


@app.post("/api/products/{product_id}/refresh")
def v221_refresh(product_id: int, db: Session = Depends(get_db)):
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
        candidate, _all = _best_magalu_v221(product)
        if candidate:
            _save_link(db, product, candidate)
            clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
            level = str(candidate.get("_match_level") or "similar")
            score = int(candidate.get("_match_score") or 0)
            if record_magalu_detail(db, product, clean, source_name=f"magalu_{level}_{score}"):
                observations += 1
            sources_ok += 1
        else:
            messages.append("Magazine Luiza: nenhuma alternativa comprável foi encontrada nesta atualização.")
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


@app.get("/api/v221/provider-diagnostics")
async def v221_provider_diagnostics(q: str = Query("maquina de lavar 17kg", min_length=2, max_length=160)):
    query = " ".join(q.strip().split())
    out: dict[str, Any] = {"version": "2.21", "query": query, "providers": {}}

    ml_rows, ml_error, ml_time = await _bounded(lambda: MercadoLivreProvider().search(query, 4), 8.5)
    out["providers"]["mercadolivre"] = {
        "ok": bool(ml_rows), "seconds": ml_time, "count": len(ml_rows or []),
        "error": None if ml_rows else ("timeout" if ml_error == "timeout" else str(ml_error) if ml_error else "zero results"),
        "examples": [{"name": x.get("name"), "price": x.get("price"), "url": x.get("url")} for x in (ml_rows or [])[:4]],
    }

    mg_rows, mg_error, mg_time = await _bounded(lambda: magalu_search_fast_v221(query, 4), 12.0)
    out["providers"]["magalu"] = {
        "ok": bool(mg_rows), "seconds": mg_time, "count": len(mg_rows or []),
        "error": None if mg_rows else ("timeout" if mg_error == "timeout" else str(mg_error) if mg_error else "zero results"),
        "examples": [
            {"name": x.get("name"), "price": x.get("price"), "pix_price": x.get("pix_price"), "url": x.get("url"), "source": x.get("_source")}
            for x in (mg_rows or [])[:4]
        ],
    }
    return out


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v221_web_preview():
    base = v220_truthful_ui()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.replace("V2.20", "V2.21")
    text = text.replace(
        "Pesquise do seu jeito: máquina de lavar 17kg, geladeira 400L, iPhone 17, RTX 5070...",
        "Pesquise naturalmente: máquina de lavar 17kg, geladeira 400L, iPhone 17, RTX 5070...",
    )
    return HTMLResponse(text)
