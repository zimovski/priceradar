from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Product, ProductSourceLink
from .providers.magalu_reader_v220 import magalu_reader_search
from .providers.magalu_web import MagaluError
from .providers.mercadolivre import MercadoLivreError, MercadoLivreProvider
from .providers.search_intent_v218 import broad_product_query, consumer_similarity
from .schemas import ProviderStatus, SearchResponse
from .services.collector import record_magalu_detail, record_ml_detail
from .v211_features import _append_external, _local_results
from .v217_similarity import _save_link
from .v219_accuracy import _magalu_search_v219, v219_web_preview


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


def _magalu_search_v220(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Race direct Magalu storefront parsing with a rendered public-page fallback."""
    limit = max(1, min(int(limit), 12))
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def direct():
        return _magalu_search_v219(query, limit=max(limit, 10))

    def rendered():
        return magalu_reader_search(query, limit=max(limit, 10))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(direct): "direct", pool.submit(rendered): "rendered"}
        for future in as_completed(futures):
            source = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
                continue
            for row in rows or []:
                key = str(row.get("external_product_id") or row.get("url") or row.get("name") or "")
                if not key:
                    continue
                old = merged.get(key)
                # Prefer direct storefront data when both paths find the same item;
                # otherwise keep any real offer with price + direct product URL.
                if old is None or (source == "direct" and old.get("_source") == "magalu_reader_rendered"):
                    merged[key] = dict(row)
            if len(merged) >= limit:
                # We do not cancel the other future abruptly; leaving the context
                # manager lets it finish cleanly while results are already merged.
                pass

    rows = [
        row for row in merged.values()
        if (row.get("pix_price") or row.get("price")) and row.get("url")
    ]
    rows.sort(key=lambda row: float(row.get("pix_price") or row.get("price") or 10**18))
    if rows:
        return rows[:limit]
    if errors:
        raise MagaluError(" | ".join(errors[-2:]))
    return []


def _best_magalu_v220(product: Product) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    source = " ".join(x for x in (product.brand, product.name, product.model) if x)
    broad = broad_product_query(source)
    queries = [broad]
    if product.name and broad.lower() != product.name.lower():
        queries.append(product.name)

    found: dict[str, dict[str, Any]] = {}
    for query in queries:
        try:
            rows = _magalu_search_v220(query, limit=12)
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
        if any(int(x.get("_match_score") or 0) >= 60 for x in found.values()):
            break

    rows = list(found.values())
    purchasable = [
        row for row in rows
        if (row.get("pix_price") or row.get("price"))
        and row.get("url")
        and int(row.get("_match_score") or 0) >= 30
    ]
    if not purchasable:
        return None, rows

    # Similarity first, price second inside a close score band.
    purchasable.sort(key=lambda row: int(row.get("_match_score") or 0), reverse=True)
    best_score = int(purchasable[0].get("_match_score") or 0)
    band = [row for row in purchasable if int(row.get("_match_score") or 0) >= best_score - 10]
    band.sort(key=lambda row: float(row.get("pix_price") or row.get("price") or 10**18))
    return band[0], rows


_remove_route("/api/search", "GET")


@app.get("/api/search", response_model=SearchResponse)
async def v220_search(
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
    ml.timeout = 8.5
    if ml.configured():
        tasks.append((
            "mercadolivre",
            asyncio.create_task(_bounded(lambda: ml.search(query, limit=min(limit, 10)), 16.0)),
        ))
    else:
        statuses.append(ProviderStatus(
            slug="mercadolivre", name="Mercado Livre", configured=False, ok=False,
            message="Conexão OAuth ainda não configurada.",
        ))

    tasks.append((
        "magalu",
        asyncio.create_task(_bounded(lambda: _magalu_search_v220(query, min(limit, 10)), 18.0)),
    ))

    for slug, task in tasks:
        rows, error = await task
        if slug == "mercadolivre":
            if rows is not None:
                _append_external(results, "mercadolivre", "Mercado Livre", rows, known_ml)
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=bool(rows),
                    message=f"{len(rows)} ofertas reais retornadas." if rows else "A API respondeu, mas não trouxe oferta útil para esta consulta.",
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
                    slug="magalu", name="Magazine Luiza", configured=True, ok=bool(rows),
                    message=f"{len(rows)} ofertas reais retornadas." if rows else "A vitrine respondeu, mas não trouxe oferta útil para esta consulta.",
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
def v220_refresh(product_id: int, db: Session = Depends(get_db)):
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
        candidate, _all = _best_magalu_v220(product)
        if candidate:
            _save_link(db, product, candidate)
            clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
            level = str(candidate.get("_match_level") or "similar")
            score = int(candidate.get("_match_score") or 0)
            if record_magalu_detail(db, product, clean, source_name=f"magalu_{level}_{score}"):
                observations += 1
            sources_ok += 1
        else:
            messages.append("Magazine Luiza: nenhum produto comprável suficientemente próximo foi localizado.")
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


@app.get("/api/v220/provider-diagnostics")
async def v220_provider_diagnostics(q: str = Query("maquina de lavar 17kg", min_length=2, max_length=160)):
    query = " ".join(q.strip().split())
    output: dict[str, Any] = {"version": "2.20", "query": query, "providers": {}}

    ml = MercadoLivreProvider()
    rows, error = await _bounded(lambda: ml.search(query, limit=4), 16.0)
    output["providers"]["mercadolivre"] = {
        "ok": bool(rows),
        "count": len(rows or []),
        "error": None if rows else ("timeout" if error == "timeout" else str(error) if error else "zero useful results"),
        "examples": [
            {"name": x.get("name"), "price": x.get("price"), "url": x.get("url")}
            for x in (rows or [])[:4]
        ],
    }

    rows, error = await _bounded(lambda: _magalu_search_v220(query, 4), 18.0)
    output["providers"]["magalu"] = {
        "ok": bool(rows),
        "count": len(rows or []),
        "error": None if rows else ("timeout" if error == "timeout" else str(error) if error else "zero useful results"),
        "examples": [
            {"name": x.get("name"), "price": x.get("price"), "pix_price": x.get("pix_price"), "url": x.get("url"), "source": x.get("_source")}
            for x in (rows or [])[:4]
        ],
    }
    return output


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v220_web_preview():
    base = v219_web_preview()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.replace("V2.19", "V2.20")
    text = text.replace(
        "Digite como você pesquisaria normalmente. Ex.: máquina de lavar 17kg, iPhone 17 ou RTX 5070.",
        "Pesquise do seu jeito: máquina de lavar 17kg, geladeira 400L, iPhone 17, RTX 5070...",
    )
    return HTMLResponse(text)
