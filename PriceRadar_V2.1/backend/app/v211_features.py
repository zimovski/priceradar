from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Offer, PriceObservation, Product, ProductSourceLink, Retailer
from .providers.magalu_web import MagaluError, MagaluProvider
from .providers.mercadolivre import MercadoLivreError, MercadoLivreProvider
from .schemas import ProviderStatus, SearchResponse, SearchResult, TrackExternalRequest, ProductOut
from .services.collector import record_ml_detail, refresh_product

STATIC_DIR = Path(__file__).parent / "static"


def _remove_route(path: str, method: str = "GET") -> None:
    method = method.upper()
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()))
    ]


# Replace V2.1 routes with the multi-store versions below.
_remove_route("/", "GET")
_remove_route("/api/search", "GET")
_remove_route("/api/search/track-external", "POST")


def _patched_index() -> str:
    text = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    text = text.replace("<span>V2.8</span>", "<span>V2.11</span>")
    text = text.replace(
        "Mercado Livre é a fonte ativa. KaBuM, Magalu e Casas Bahia entram nas próximas etapas.",
        "Mercado Livre e Magazine Luiza já participam da comparação. KaBuM e Casas Bahia entram nas próximas etapas.",
    )
    text = text.replace("Comprar no ML ↗", "Ir para a loja ↗")
    text = text.replace(
        "${soon('KaBuM!')}${soon('Magazine Luiza')}${soon('Casas Bahia')}",
        "${soon('KaBuM!')}${storeOrSoon(o,'magalu','Magazine Luiza',p.id)}${soon('Casas Bahia')}",
    )
    helper = """
function storeOrSoon(offers,slug,name,id){
  const x=offers.find(v=>v.retailer_slug===slug);
  if(!x)return soon(name);
  const href=x.url?esc(x.url):'#';
  return `<div class=\"store active\"><div><div class=\"store-head\"><span class=\"store-name\">${esc(name)}</span><span class=\"tag\">ativo</span></div><div class=\"store-price\">${brl(x.pix_price||x.price)}</div><div class=\"muted tiny\">${esc(x.seller_name||'oferta atual')}</div></div>${x.url?`<a class=\"btn primary sm\" href=\"${href}\" target=\"_blank\" rel=\"noopener noreferrer\">Comprar agora ↗</a>`:'<span class=\"muted tiny\">link indisponível</span>'}</div>`;
}
"""
    text = text.replace("function renderProduct", helper + "\nfunction renderProduct", 1)
    # A connected ML account should not suggest that the user needs to login on
    # every visit. Token renewal is handled by the backend with the refresh token.
    text = text.replace(
        "${s.mercadolivre_connected?'Reconectar':'Conectar'}",
        "${s.mercadolivre_connected?'Conectado automaticamente':'Conectar'}",
    )
    return text


@app.get("/", include_in_schema=False)
def v211_web_preview():
    return HTMLResponse(_patched_index())


def _latest_for_product(db: Session, product_id: int) -> tuple[float | None, str | None, str | None]:
    row = db.execute(
        select(PriceObservation, Offer, Retailer)
        .join(Offer, PriceObservation.offer_id == Offer.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .where(PriceObservation.product_id == product_id, PriceObservation.available == True)
        .order_by(PriceObservation.captured_at.desc())
        .limit(1)
    ).first()
    if not row:
        return None, None, None
    obs, offer, retailer = row
    return float(obs.pix_price or obs.price), offer.url, retailer.name


def _local_results(db: Session, query: str, limit: int) -> list[SearchResult]:
    # Local products are useful to reopen history, but external store results
    # remain the authoritative source for current price.
    words = [w for w in query.lower().split() if len(w) > 1]
    conditions = []
    for word in words[:5]:
        term = f"%{word}%"
        conditions.append(or_(
            func.lower(Product.name).like(term),
            func.lower(func.coalesce(Product.brand, "")).like(term),
            func.lower(func.coalesce(Product.model, "")).like(term),
            func.lower(func.coalesce(Product.gtin, "")).like(term),
        ))
    stmt = select(Product).order_by(Product.created_at.desc()).limit(limit)
    if conditions:
        from sqlalchemy import and_
        stmt = stmt.where(and_(*conditions))
    rows = db.scalars(stmt).all()
    results: list[SearchResult] = []
    for p in rows:
        price, url, source_name = _latest_for_product(db, p.id)
        results.append(SearchResult(
            result_key=f"local:{p.id}", source="local", source_name=source_name or "PriceRadar",
            local_product_id=p.id, name=p.name, brand=p.brand, model=p.model,
            gtin=p.gtin, image_url=p.image_url, price=price, url=url, tracked=True,
        ))
    return results


def _append_external(results: list[SearchResult], source: str, source_name: str, rows: list[dict[str, Any]], known: dict[str, int]) -> None:
    for x in rows:
        ext_id = str(x.get("external_product_id") or x.get("item_id") or "")
        if not ext_id:
            continue
        tracked_id = known.get(ext_id)
        results.append(SearchResult(
            result_key=f"{source}:{ext_id}", source=source, source_name=source_name,
            local_product_id=tracked_id, external_product_id=ext_id,
            name=x.get("name") or ext_id, brand=x.get("brand"), model=x.get("model"), gtin=x.get("gtin"),
            image_url=x.get("image_url"), price=x.get("pix_price") or x.get("price"),
            original_price=x.get("original_price"), currency=x.get("currency") or "BRL",
            seller_name=x.get("seller_name"), url=x.get("url"), shipping_free=x.get("shipping_free"),
            tracked=bool(tracked_id),
        ))


@app.get("/api/search", response_model=SearchResponse)
def v211_search(
    q: str = Query(..., min_length=2, max_length=240),
    limit: int = Query(12, ge=1, le=30),
    db: Session = Depends(get_db),
):
    query = " ".join(q.strip().split())
    results = _local_results(db, query, min(limit, 6))
    statuses: list[ProviderStatus] = []

    ml = MercadoLivreProvider()
    if ml.configured():
        try:
            known = {
                str(link.external_product_id): link.product_id
                for link in db.scalars(select(ProductSourceLink).where(ProductSourceLink.provider_slug == "mercadolivre")).all()
            }
            rows = ml.search(query, limit=min(limit, 8))
            _append_external(results, "mercadolivre", "Mercado Livre", rows, known)
            statuses.append(ProviderStatus(slug="mercadolivre", name="Mercado Livre", configured=True, ok=True))
        except MercadoLivreError as exc:
            statuses.append(ProviderStatus(slug="mercadolivre", name="Mercado Livre", configured=True, ok=False, message=str(exc)))
    else:
        statuses.append(ProviderStatus(slug="mercadolivre", name="Mercado Livre", configured=False, ok=False, message="Conexão OAuth ainda não configurada."))

    magalu = MagaluProvider()
    try:
        known_magalu = {
            str(link.external_product_id): link.product_id
            for link in db.scalars(select(ProductSourceLink).where(ProductSourceLink.provider_slug == "magalu")).all()
        }
        rows = magalu.search(query, limit=min(limit, 8))
        _append_external(results, "magalu", "Magazine Luiza", rows, known_magalu)
        statuses.append(ProviderStatus(slug="magalu", name="Magazine Luiza", configured=True, ok=True))
    except MagaluError as exc:
        statuses.append(ProviderStatus(slug="magalu", name="Magazine Luiza", configured=True, ok=False, message=str(exc)))
    except Exception as exc:
        statuses.append(ProviderStatus(slug="magalu", name="Magazine Luiza", configured=True, ok=False, message="Vitrine indisponível no momento."))

    # Put store results with an actual price before local/history-only rows.
    results.sort(key=lambda r: (r.price is not None, r.source != "local"), reverse=True)
    return SearchResponse(query=query, results=results[: max(limit, 12)], providers=statuses)


@app.post("/api/search/track-external", response_model=ProductOut, status_code=201)
def v211_track_external(data: TrackExternalRequest, db: Session = Depends(get_db)):
    # Existing UI tracks the Mercado Livre catalog identity; the collector then
    # safely attaches a matching Magalu listing when it can prove the variant.
    if data.provider_slug != "mercadolivre":
        raise HTTPException(400, "Acompanhe o produto pelo resultado do Mercado Livre; o Magalu será associado automaticamente quando a variante corresponder.")

    existing_link = db.scalar(select(ProductSourceLink).where(
        ProductSourceLink.provider_slug == data.provider_slug,
        ProductSourceLink.external_product_id == data.external_product_id,
    ))
    if existing_link:
        product = db.get(Product, existing_link.product_id)
        refresh_product(db, product.id)
        return product

    ml = MercadoLivreProvider()
    try:
        detail = ml.product_detail(data.external_product_id)
    except MercadoLivreError as exc:
        raise HTTPException(400, str(exc))

    product = Product(
        name=detail["name"], brand=detail.get("brand"), model=detail.get("model"),
        gtin=detail.get("gtin"), image_url=detail.get("image_url"),
    )
    db.add(product)
    db.flush()
    db.add(ProductSourceLink(
        product_id=product.id, provider_slug="mercadolivre",
        external_product_id=str(detail["external_product_id"]), source_url=detail.get("url"),
    ))
    record_ml_detail(db, product, detail)
    db.commit()
    db.refresh(product)
    # This refresh attempts Magalu variant matching and records a second offer.
    refresh_product(db, product.id)
    db.refresh(product)
    return product
