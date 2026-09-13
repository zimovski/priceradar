from __future__ import annotations

from pathlib import Path

from fastapi import Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Product, ProductSourceLink
from .schemas import ProviderStatus, SearchResponse, SearchResult
from .providers.mercadolivre import MercadoLivreProvider, MercadoLivreError
from .providers.magalu_web import MagaluProvider, MagaluError

STATIC_DIR = Path(__file__).parent / "static"


def _remove_route(path: str, method: str = "GET") -> None:
    kept = []
    for route in app.router.routes:
        methods = getattr(route, "methods", None) or set()
        if getattr(route, "path", None) == path and method in methods:
            continue
        kept.append(route)
    app.router.routes[:] = kept


# Replace the original single-store search with a multi-store version while
# keeping the public URL stable for the existing web/mobile clients.
_remove_route("/api/search", "GET")


@app.get("/api/search", response_model=SearchResponse)
def multistore_search(
    q: str = Query(..., min_length=2, max_length=240),
    limit: int = Query(12, ge=1, le=30),
    db: Session = Depends(get_db),
):
    query = " ".join(q.strip().split())
    term = f"%{query.lower()}%"
    rows = db.scalars(
        select(Product)
        .where(or_(
            func.lower(Product.name).like(term),
            func.lower(func.coalesce(Product.brand, "")).like(term),
            func.lower(func.coalesce(Product.model, "")).like(term),
            func.lower(func.coalesce(Product.gtin, "")).like(term),
        ))
        .order_by(Product.name)
        .limit(limit)
    ).all()

    results: list[SearchResult] = [SearchResult(
        result_key=f"local:{p.id}", source="local", source_name="PriceRadar",
        local_product_id=p.id, name=p.name, brand=p.brand, model=p.model,
        gtin=p.gtin, image_url=p.image_url, tracked=True,
    ) for p in rows]
    statuses: list[ProviderStatus] = []

    ml = MercadoLivreProvider()
    if ml.configured():
        try:
            known_links = {
                link.external_product_id: link.product_id
                for link in db.scalars(
                    select(ProductSourceLink).where(ProductSourceLink.provider_slug == "mercadolivre")
                ).all()
            }
            for x in ml.search(query, limit=min(limit, 8)):
                ext_id = str(x["external_product_id"])
                tracked_id = known_links.get(ext_id)
                results.append(SearchResult(
                    result_key=f"mercadolivre:{ext_id}",
                    source="mercadolivre",
                    source_name="Mercado Livre",
                    local_product_id=tracked_id,
                    external_product_id=ext_id,
                    name=x["name"],
                    brand=x.get("brand"),
                    model=x.get("model"),
                    gtin=x.get("gtin"),
                    image_url=x.get("image_url"),
                    price=x.get("price"),
                    original_price=x.get("original_price"),
                    currency=x.get("currency") or "BRL",
                    seller_name=x.get("seller_name"),
                    url=x.get("url"),
                    shipping_free=x.get("shipping_free"),
                    tracked=bool(tracked_id),
                ))
            statuses.append(ProviderStatus(
                slug="mercadolivre", name="Mercado Livre", configured=True, ok=True
            ))
        except MercadoLivreError as exc:
            statuses.append(ProviderStatus(
                slug="mercadolivre", name="Mercado Livre", configured=True, ok=False, message=str(exc)
            ))
    else:
        statuses.append(ProviderStatus(
            slug="mercadolivre", name="Mercado Livre", configured=False, ok=False,
            message="Conecte o Mercado Livre para receber resultados reais.",
        ))

    # Magalu's official Open API is seller-oriented. This read-only connector
    # consumes only public storefront search cards; failures never create a
    # guessed price and never block Mercado Livre results.
    magalu = MagaluProvider()
    try:
        for x in magalu.search(query, limit=min(limit, 8)):
            display_price = x.get("pix_price") or x.get("price")
            regular = x.get("price")
            results.append(SearchResult(
                result_key=f"magalu:{x['external_product_id']}",
                source="magalu",
                source_name="Magazine Luiza",
                external_product_id=str(x["external_product_id"]),
                name=x["name"],
                brand=x.get("brand"),
                model=x.get("model"),
                gtin=x.get("gtin"),
                image_url=x.get("image_url"),
                price=display_price,
                original_price=regular if regular and display_price and regular > display_price else None,
                currency="BRL",
                seller_name=x.get("seller_name"),
                url=x.get("url"),
                shipping_free=x.get("shipping_free"),
                tracked=False,
            ))
        statuses.append(ProviderStatus(
            slug="magalu", name="Magazine Luiza", configured=True, ok=True,
            message="Busca pública ativa; histórico por produto entra na próxima etapa.",
        ))
    except MagaluError as exc:
        statuses.append(ProviderStatus(
            slug="magalu", name="Magazine Luiza", configured=True, ok=False, message=str(exc)
        ))

    return SearchResponse(query=query, results=results, providers=statuses)


# Serve a lightly upgraded UI without duplicating the large static HTML file.
# The static file remains the Android-friendly base client; these replacements
# only expose the second live store and version label.
_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v210_web_client():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("<span>V2.8</span>", "<span>V2.10</span>")
    html = html.replace("Consultando Mercado Livre...", "Consultando Mercado Livre e Magazine Luiza...")
    html = html.replace(
        "Mercado Livre é a fonte ativa. KaBuM, Magalu e Casas Bahia entram nas próximas etapas.",
        "Mercado Livre e Magazine Luiza já participam da busca. KaBuM e Casas Bahia são as próximas integrações.",
    )
    # This text lives inside a JavaScript template literal, so the expression is
    # evaluated in the browser for each retailer result.
    html = html.replace("Comprar no ML ↗", "Ir para ${esc(x.source_name)} ↗")
    return HTMLResponse(html)
