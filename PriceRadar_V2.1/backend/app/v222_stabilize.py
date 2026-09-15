from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any, Callable

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import DATABASE_BACKEND, DATABASE_PERSISTENT, get_db
from .main import app
from .models import IntegrationCredential, PriceObservation, Product, ProductSourceLink
from .providers.magalu_fast_v221 import magalu_search_fast_v221
from .providers.mercadolivre import MercadoLivreError, MercadoLivreProvider
from .schemas import ProviderStatus, SearchResponse
from .services.collector import record_magalu_detail, record_ml_detail
from .v211_features import _append_external, _local_results
from .v217_similarity import _save_link
from .v221_fastsearch import _best_magalu_v221, v221_web_preview


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


_remove_route("/api/search", "GET")


@app.get("/api/search", response_model=SearchResponse)
async def v222_search(
    q: str = Query(..., min_length=2, max_length=240),
    limit: int = Query(12, ge=1, le=30),
    db: Session = Depends(get_db),
):
    """Stability-first search.

    Mercado Livre is the primary provider and is allowed enough time to finish
    its small verified catalog search. Magalu is best-effort only and can never
    make the whole request fail or wait indefinitely.
    """
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
    ml.timeout = 9.0
    ml_task = asyncio.create_task(_bounded(lambda: ml.search(query, limit=min(limit, 8)), 16.0))
    # Magalu remains experimental. A short deadline protects the core search.
    mg_task = asyncio.create_task(_bounded(lambda: magalu_search_fast_v221(query, min(limit, 8)), 5.0))

    ml_rows, ml_error, ml_elapsed = await ml_task
    if ml_rows:
        _append_external(results, "mercadolivre", "Mercado Livre", ml_rows, known_ml)
        statuses.append(ProviderStatus(
            slug="mercadolivre", name="Mercado Livre", configured=True, ok=True,
            message=f"{len(ml_rows)} ofertas verificadas em {ml_elapsed}s.",
        ))
    else:
        statuses.append(ProviderStatus(
            slug="mercadolivre", name="Mercado Livre", configured=ml.configured(), ok=False,
            message=("tempo limite excedido" if ml_error == "timeout" else str(ml_error) if ml_error else "nenhuma oferta útil"),
        ))

    mg_rows, mg_error, mg_elapsed = await mg_task
    if mg_rows:
        _append_external(results, "magalu", "Magazine Luiza", mg_rows, known_magalu)
        statuses.append(ProviderStatus(
            slug="magalu", name="Magazine Luiza", configured=True, ok=True,
            message=f"{len(mg_rows)} ofertas públicas em {mg_elapsed}s.",
        ))
    else:
        statuses.append(ProviderStatus(
            slug="magalu", name="Magazine Luiza", configured=False, ok=False,
            message=("consulta pública excedeu o limite" if mg_error == "timeout" else str(mg_error) if mg_error else "consulta pública sem resultado"),
        ))

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
def v222_refresh(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")

    observations = 0
    sources_ok = 0
    messages: list[str] = []

    # Mercado Livre is authoritative and refreshes first.
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

    # Magalu is allowed a hard best-effort window and never blocks ML/history.
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_best_magalu_v221, product)
    try:
        candidate, _all = future.result(timeout=5.0)
        if candidate:
            _save_link(db, product, candidate)
            clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
            level = str(candidate.get("_match_level") or "similar")
            score = int(candidate.get("_match_score") or 0)
            if record_magalu_detail(db, product, clean, source_name=f"magalu_{level}_{score}"):
                observations += 1
            sources_ok += 1
        else:
            messages.append("Magazine Luiza: nenhuma oferta pública utilizável nesta tentativa.")
    except FuturesTimeout:
        future.cancel()
        messages.append("Magazine Luiza: consulta pública excedeu 5 segundos e foi ignorada nesta atualização.")
    except Exception as exc:
        messages.append(f"Magazine Luiza: {type(exc).__name__}: {exc}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    db.commit()
    return {
        "product_id": product_id,
        "ok": sources_ok > 0,
        "observations": observations,
        "sources_ok": sources_ok,
        "message": "; ".join(messages) or None,
    }


@app.get("/api/v222/system-state")
def v222_system_state(db: Session = Depends(get_db)):
    products = int(db.scalar(select(func.count()).select_from(Product)) or 0)
    observations = int(db.scalar(select(func.count()).select_from(PriceObservation)) or 0)
    credentials = int(db.scalar(select(func.count()).select_from(IntegrationCredential)) or 0)
    return {
        "version": "2.22",
        "database": DATABASE_BACKEND,
        "database_persistent": DATABASE_PERSISTENT,
        "products": products,
        "observations": observations,
        "stored_integrations": credentials,
        "warning": None if DATABASE_PERSISTENT else (
            "O serviço está usando SQLite local. Em um novo deploy do Render, produtos e histórico podem desaparecer. "
            "Conecte o PostgreSQL ao serviço através de DATABASE_URL."
        ),
    }


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v222_web_preview():
    base = v221_web_preview()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.replace("V2.21", "V2.22")

    # Opening an already tracked product must be instant. Refresh remains an
    # explicit action instead of blocking every product view on retailer calls.
    text = text.replace(
        "try{try{await api(`/api/products/${id}/refresh`,{method:'POST'})}catch{}const [p,i,o,d,h,best]=await Promise.all",
        "try{const [p,i,o,d,h,best]=await Promise.all",
    )

    warning_html = '<div id="dbPersistWarning" class="notice hidden" style="margin-top:14px"></div>'
    text = text.replace('</header>', '</header>' + warning_html, 1)

    script = r'''
<script>
(async function(){
  try{
    const r=await fetch('/api/v222/system-state',{cache:'no-store'});
    if(!r.ok)return;
    const s=await r.json();
    const e=document.getElementById('dbPersistWarning');
    if(e && !s.database_persistent){
      e.classList.remove('hidden');
      e.innerHTML='<b>Histórico não persistente:</b> este serviço está usando SQLite local. Conecte o PostgreSQL do Render em <b>DATABASE_URL</b> antes de continuar os testes.';
    }
  }catch(_e){}
})();
</script>
'''
    text = text.replace('</body>', script + '</body>', 1)
    return HTMLResponse(text)
