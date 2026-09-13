from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from fastapi import Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import ProductSourceLink
from .providers.magalu_web import MagaluError, MagaluProvider
from .providers.mercadolivre import MercadoLivreError, MercadoLivreProvider
from .schemas import ProviderStatus, SearchResponse
from .v211_features import _append_external, _local_results, _patched_index


def _remove_route(path: str, method: str = "GET") -> None:
    method = method.upper()
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()))
    ]


_remove_route("/", "GET")
_remove_route("/api/search", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v212_web_preview():
    text = _patched_index()
    text = text.replace("V2.11", "V2.14")
    text = text.replace("V2.12", "V2.14")
    text = text.replace("V2.13", "V2.14")
    text = text.replace("Consultando Mercado Livre...", "Consultando lojas...")
    text = text.replace("Consultando Mercado Livre e Magazine Luiza...", "Consultando lojas...")

    old_refresh = "try{try{await api(`/api/products/${id}/refresh`,{method:'POST'})}catch{}const [p,i,o,d,h,best]=await Promise.all"
    new_refresh = "try{api(`/api/products/${id}/refresh`,{method:'POST'}).catch(()=>{});const [p,i,o,d,h,best]=await Promise.all"
    text = text.replace(old_refresh, new_refresh)

    old_action = "<a class=\"btn secondary sm\" href=\"/mercadolivre/connect\">${s.mercadolivre_connected?'Conectado automaticamente':'Conectar'}</a>"
    new_action = "${s.mercadolivre_connected?'<span class=\"chip ok\">sessão persistente</span>':'<a class=\"btn secondary sm\" href=\"/mercadolivre/connect\">Conectar</a>'}"
    text = text.replace(old_action, new_action)

    old_count = "document.querySelector('#searchSub').textContent=`${d.results.length} resultados encontrados`;"
    new_count = "const falhas=(d.providers||[]).filter(p=>!p.ok).map(p=>p.name);document.querySelector('#searchSub').textContent=`${d.results.length} resultados encontrados${falhas.length?' • indisponível agora: '+falhas.join(', '):''}`;"
    text = text.replace(old_count, new_count)

    old_empty = "r.innerHTML=d.results.length?d.results.map(result).join(''):'<div class=\"card empty\"><b>Nenhum resultado</b>Tente marca + modelo.</div>'"
    new_empty = "r.innerHTML=d.results.length?d.results.map(result).join(''):(falhas.length?'<div class=\"card empty\"><b>As lojas não responderam nesta tentativa</b>Tente novamente em alguns segundos. O PriceRadar não inventa preços quando uma fonte falha.</div>':'<div class=\"card empty\"><b>Nenhum resultado</b>Tente marca + modelo.</div>')"
    text = text.replace(old_empty, new_empty)
    return HTMLResponse(text)


async def _bounded(call: Callable[[], list[dict[str, Any]]], seconds: float):
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(call), timeout=seconds)
        return rows, None
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as exc:
        return None, exc


@app.get("/api/search", response_model=SearchResponse)
async def v212_search(
    q: str = Query(..., min_length=2, max_length=240),
    limit: int = Query(12, ge=1, le=30),
    db: Session = Depends(get_db),
):
    query = " ".join(q.strip().split())
    results = _local_results(db, query, min(limit, 5))
    statuses: list[ProviderStatus] = []

    known_ml = {
        str(link.external_product_id): link.product_id
        for link in db.scalars(
            select(ProductSourceLink).where(ProductSourceLink.provider_slug == "mercadolivre")
        ).all()
    }
    known_magalu = {
        str(link.external_product_id): link.product_id
        for link in db.scalars(
            select(ProductSourceLink).where(ProductSourceLink.provider_slug == "magalu")
        ).all()
    }

    ml = MercadoLivreProvider()
    # V2.14 uses one catalog call plus a handful of parallel product calls.
    # A slightly larger per-call timeout is safe now because there is no large
    # sequential fan-out anymore.
    ml.timeout = 7.5
    magalu = MagaluProvider()
    magalu.timeout = 8.0

    tasks: list[tuple[str, asyncio.Task]] = []
    if ml.configured():
        tasks.append((
            "mercadolivre",
            asyncio.create_task(_bounded(lambda: ml.search(query, limit=min(limit, 8)), 9.5)),
        ))
    else:
        statuses.append(ProviderStatus(
            slug="mercadolivre",
            name="Mercado Livre",
            configured=False,
            ok=False,
            message="Conexão OAuth ainda não configurada.",
        ))

    tasks.append((
        "magalu",
        asyncio.create_task(_bounded(lambda: magalu.search(query, limit=min(limit, 8)), 9.5)),
    ))

    for slug, task in tasks:
        rows, error = await task
        if slug == "mercadolivre":
            if rows is not None:
                _append_external(results, "mercadolivre", "Mercado Livre", rows, known_ml)
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=True,
                    message=f"{len(rows)} produtos retornados.",
                ))
            elif error == "timeout":
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=False,
                    message="A API não respondeu dentro de 9 segundos.",
                ))
            else:
                message = str(error) if isinstance(error, MercadoLivreError) else f"Mercado Livre indisponível: {type(error).__name__}."
                statuses.append(ProviderStatus(
                    slug="mercadolivre", name="Mercado Livre", configured=True, ok=False, message=message
                ))
        else:
            if rows is not None:
                _append_external(results, "magalu", "Magazine Luiza", rows, known_magalu)
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=True,
                    message=f"{len(rows)} produtos retornados.",
                ))
            elif error == "timeout":
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=False,
                    message="A vitrine não respondeu dentro de 9 segundos.",
                ))
            else:
                message = str(error) if isinstance(error, MagaluError) else f"Vitrine indisponível: {type(error).__name__}."
                statuses.append(ProviderStatus(
                    slug="magalu", name="Magazine Luiza", configured=True, ok=False, message=message
                ))

    results.sort(
        key=lambda r: (r.price is not None, r.source != "local"),
        reverse=True,
    )
    return SearchResponse(query=query, results=results[: max(limit, 12)], providers=statuses)


@app.get("/api/provider-diagnostics")
async def provider_diagnostics(q: str = Query("rtx 5070", min_length=2, max_length=120)):
    """Non-secret health probe for debugging outbound retailer connectivity."""
    query = " ".join(q.strip().split())
    output: dict[str, Any] = {"query": query, "version": "2.14", "providers": {}}

    ml = MercadoLivreProvider()
    ml.timeout = 7.5
    start = time.perf_counter()
    try:
        rows, error = await _bounded(lambda: ml.search(query, limit=2), 9.5)
        output["providers"]["mercadolivre"] = {
            "configured": ml.configured(),
            "ok": rows is not None,
            "count": len(rows or []),
            "seconds": round(time.perf_counter() - start, 2),
            "error": None if rows is not None else ("timeout" if error == "timeout" else str(error)),
        }
    except Exception as exc:
        output["providers"]["mercadolivre"] = {
            "configured": ml.configured(), "ok": False, "count": 0,
            "seconds": round(time.perf_counter() - start, 2), "error": f"{type(exc).__name__}: {exc}",
        }

    magalu = MagaluProvider()
    magalu.timeout = 8.0
    start = time.perf_counter()
    try:
        rows, error = await _bounded(lambda: magalu.search(query, limit=2), 9.5)
        output["providers"]["magalu"] = {
            "ok": rows is not None,
            "count": len(rows or []),
            "seconds": round(time.perf_counter() - start, 2),
            "error": None if rows is not None else ("timeout" if error == "timeout" else str(error)),
        }
    except Exception as exc:
        output["providers"]["magalu"] = {
            "ok": False, "count": 0,
            "seconds": round(time.perf_counter() - start, 2), "error": f"{type(exc).__name__}: {exc}",
        }
    return output
