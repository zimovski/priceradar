from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import select, or_, func

from .database import Base, engine, get_db, SessionLocal
from .models import Product, ProductSourceLink, Retailer, Offer, PriceObservation
from .schemas import (
    ProductCreate, ProductOut, SearchResult, SearchResponse, ProviderStatus,
    TrackSearchRequest, TrackExternalRequest, MercadoLivreCredentials,
    ObservationCreate, HistoryImportPoint, HistoryPoint, PriceSummary, LatestOffer,
)
from .services.analytics import product_summary
from .services.collector import record_ml_detail, refresh_product
from .providers.mercadolivre import MercadoLivreProvider, MercadoLivreError

Base.metadata.create_all(bind=engine)

COLLECTION_INTERVAL_HOURS = max(1, int(os.getenv("COLLECTION_INTERVAL_HOURS", "6")))
AUTO_COLLECTION = os.getenv("AUTO_COLLECTION", "1").lower() not in {"0", "false", "no"}


async def _collector_loop():
    while True:
        await asyncio.sleep(COLLECTION_INTERVAL_HOURS * 3600)
        if not AUTO_COLLECTION:
            continue
        db = SessionLocal()
        try:
            ids = db.scalars(select(Product.id)).all()
            for product_id in ids:
                try:
                    refresh_product(db, product_id)
                except Exception:
                    db.rollback()
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_collector_loop()) if AUTO_COLLECTION else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="PriceRadar API", version="0.2.1", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def web_preview():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "app": "priceradar", "version": "0.2.1"}


@app.get("/mercadolivre/callback", response_class=HTMLResponse, include_in_schema=False)
def mercadolivre_callback(code: str | None = None, error: str | None = None, error_description: str | None = None, state: str | None = None):
    """URI pública de retorno do OAuth do Mercado Livre.

    Na V2.1 ela confirma que o redirect HTTPS está funcional. A próxima etapa
    fará a troca automática do `code` por Access/Refresh Token no servidor.
    """
    if error:
        detail = error_description or error
        return HTMLResponse(f"""
        <!doctype html><html lang='pt-br'><head><meta charset='utf-8'><title>PriceRadar</title></head>
        <body style='font-family:system-ui;max-width:720px;margin:60px auto;padding:0 20px'>
          <h1>PriceRadar</h1><h2>Autorização não concluída</h2>
          <p>O Mercado Livre retornou: <strong>{detail}</strong></p>
          <p>Você pode fechar esta aba e voltar ao PriceRadar.</p>
        </body></html>
        """, status_code=400)
    status = "Callback HTTPS funcionando." if not code else "Autorização recebida do Mercado Livre."
    return HTMLResponse(f"""
    <!doctype html><html lang='pt-br'><head><meta charset='utf-8'><title>PriceRadar</title></head>
    <body style='font-family:system-ui;max-width:720px;margin:60px auto;padding:0 20px'>
      <h1>PriceRadar</h1><h2>{status}</h2>
      <p>Esta URL já pode ser usada como URI de redirect da aplicação.</p>
      <p>Na próxima etapa vamos completar a troca automática do código OAuth por tokens.</p>
    </body></html>
    """)


@app.get("/api/integrations")
def integrations():
    ml = MercadoLivreProvider()
    return [{
        "slug": "mercadolivre",
        "name": "Mercado Livre",
        "configured": ml.configured(),
        "auto_refresh_supported": True,
        "collection_interval_hours": COLLECTION_INTERVAL_HOURS,
    }]


@app.post("/api/integrations/mercadolivre/credentials")
def save_ml_credentials(data: MercadoLivreCredentials):
    ml = MercadoLivreProvider()
    try:
        ml.save_credentials(data.access_token, data.refresh_token, data.app_id, data.client_secret)
        me = ml.test_connection()
        return {"ok": True, "account": me}
    except MercadoLivreError as exc:
        ml.clear_credentials()
        raise HTTPException(400, str(exc))


@app.delete("/api/integrations/mercadolivre/credentials")
def delete_ml_credentials():
    MercadoLivreProvider().clear_credentials()
    return {"ok": True}


@app.get("/api/products", response_model=list[ProductOut])
def list_products(db: Session = Depends(get_db)):
    return db.scalars(select(Product).order_by(Product.created_at.desc())).all()


@app.get("/api/search", response_model=SearchResponse)
def search_products(
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

    results = [SearchResult(
        result_key=f"local:{p.id}", source="local", source_name="PriceRadar",
        local_product_id=p.id, name=p.name, brand=p.brand, model=p.model,
        gtin=p.gtin, image_url=p.image_url, tracked=True,
    ) for p in rows]

    statuses = []
    ml = MercadoLivreProvider()
    if ml.configured():
        try:
            known_links = {
                link.external_product_id: link.product_id
                for link in db.scalars(select(ProductSourceLink).where(ProductSourceLink.provider_slug == "mercadolivre")).all()
            }
            external = ml.search(query, limit=min(limit, 8))
            for x in external:
                ext_id = str(x["external_product_id"])
                tracked_id = known_links.get(ext_id)
                results.append(SearchResult(
                    result_key=f"mercadolivre:{ext_id}", source="mercadolivre", source_name="Mercado Livre",
                    local_product_id=tracked_id, external_product_id=ext_id,
                    name=x["name"], brand=x.get("brand"), model=x.get("model"), gtin=x.get("gtin"),
                    image_url=x.get("image_url"), price=x.get("price"), original_price=x.get("original_price"),
                    currency=x.get("currency") or "BRL", seller_name=x.get("seller_name"), url=x.get("url"),
                    shipping_free=x.get("shipping_free"), tracked=bool(tracked_id),
                ))
            statuses.append(ProviderStatus(slug="mercadolivre", name="Mercado Livre", configured=True, ok=True))
        except MercadoLivreError as exc:
            statuses.append(ProviderStatus(slug="mercadolivre", name="Mercado Livre", configured=True, ok=False, message=str(exc)))
    else:
        statuses.append(ProviderStatus(
            slug="mercadolivre", name="Mercado Livre", configured=False, ok=False,
            message="Configure a API do Mercado Livre para receber resultados reais.",
        ))
    return SearchResponse(query=query, results=results, providers=statuses)


@app.post("/api/search/track", response_model=ProductOut, status_code=201)
def track_search(data: TrackSearchRequest, db: Session = Depends(get_db)):
    query = " ".join(data.query.strip().split())
    existing = db.scalar(select(Product).where(func.lower(Product.name) == query.lower()).limit(1))
    if existing:
        return existing
    product = Product(name=query)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.post("/api/search/track-external", response_model=ProductOut, status_code=201)
def track_external(data: TrackExternalRequest, db: Session = Depends(get_db)):
    if data.provider_slug != "mercadolivre":
        raise HTTPException(400, "Provedor ainda não implementado nesta V2.")
    existing_link = db.scalar(select(ProductSourceLink).where(
        ProductSourceLink.provider_slug == data.provider_slug,
        ProductSourceLink.external_product_id == data.external_product_id,
    ))
    if existing_link:
        return db.get(Product, existing_link.product_id)

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
    return product


@app.post("/api/products/{product_id}/refresh")
def refresh_product_endpoint(product_id: int, db: Session = Depends(get_db)):
    if not db.get(Product, product_id):
        raise HTTPException(404, "Produto não encontrado")
    result = refresh_product(db, product_id)
    if not result["ok"] and result["observations"] == 0:
        raise HTTPException(400, result["message"] or "Falha ao atualizar")
    return result


@app.post("/api/collect/refresh-all")
def refresh_all(db: Session = Depends(get_db)):
    ids = db.scalars(select(Product.id)).all()
    results = [refresh_product(db, pid) for pid in ids]
    return {"products": len(ids), "results": results}


@app.post("/api/products", response_model=ProductOut, status_code=201)
def create_product(data: ProductCreate, db: Session = Depends(get_db)):
    product = Product(**data.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.get("/api/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produto não encontrado")
    return product


@app.post("/api/products/{product_id}/observations", status_code=201)
def add_observation(product_id: int, data: ObservationCreate, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produto não encontrado")
    retailer = db.scalar(select(Retailer).where(Retailer.slug == data.retailer_slug))
    if not retailer:
        retailer = Retailer(slug=data.retailer_slug, name=data.retailer_name)
        db.add(retailer); db.flush()
    offer = db.scalar(select(Offer).where(Offer.retailer_id == retailer.id, Offer.external_id == data.external_id))
    if not offer:
        offer = Offer(product_id=product_id, retailer_id=retailer.id, external_id=data.external_id,
                      title=data.offer_title, seller_name=data.seller_name, url=data.url)
        db.add(offer); db.flush()
    obs = PriceObservation(product_id=product_id, offer_id=offer.id, price=data.price,
        pix_price=data.pix_price, shipping_price=data.shipping_price, available=data.available,
        captured_at=data.captured_at or datetime.utcnow(), source_kind=data.source_kind, source_name=data.source_name)
    db.add(obs); db.commit()
    return {"id": obs.id, "status": "created"}


@app.post("/api/products/{product_id}/history/import", status_code=201)
def import_external_history(product_id: int, points: list[HistoryImportPoint], db: Session = Depends(get_db)):
    if not db.get(Product, product_id):
        raise HTTPException(404, "Produto não encontrado")
    inserted = 0
    for p in points:
        retailer = db.scalar(select(Retailer).where(Retailer.slug == p.retailer_slug))
        if not retailer:
            retailer = Retailer(slug=p.retailer_slug, name=p.retailer_name); db.add(retailer); db.flush()
        external_id = p.external_id or f"external:{p.source_name}:{p.retailer_slug}"
        offer = db.scalar(select(Offer).where(Offer.retailer_id == retailer.id, Offer.external_id == external_id))
        if not offer:
            offer = Offer(product_id=product_id, retailer_id=retailer.id, external_id=external_id,
                          title=f"Histórico externo - {p.retailer_name}")
            db.add(offer); db.flush()
        db.add(PriceObservation(product_id=product_id, offer_id=offer.id, price=p.price,
                                captured_at=p.captured_at, source_kind="external", source_name=p.source_name))
        inserted += 1
    db.commit(); return {"inserted": inserted}


@app.get("/api/products/{product_id}/latest", response_model=list[LatestOffer])
def latest_offers(product_id: int, db: Session = Depends(get_db)):
    if not db.get(Product, product_id): raise HTTPException(404, "Produto não encontrado")
    rows = db.execute(select(PriceObservation, Offer, Retailer)
        .join(Offer, PriceObservation.offer_id == Offer.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .where(PriceObservation.product_id == product_id, PriceObservation.available == True)
        .order_by(Retailer.slug, PriceObservation.captured_at.desc())).all()
    latest = {}
    for obs, offer, retailer in rows: latest.setdefault(retailer.slug, (obs, offer, retailer))
    return [LatestOffer(retailer=r.name, retailer_slug=r.slug, price=o.price, pix_price=o.pix_price,
        shipping_price=o.shipping_price, seller_name=offer.seller_name, url=offer.url,
        captured_at=o.captured_at, source_kind=o.source_kind, source_name=o.source_name)
        for o, offer, r in latest.values()]


@app.get("/api/products/{product_id}/history", response_model=list[HistoryPoint])
def product_history(product_id: int, days: int = Query(730, ge=1, le=7300), db: Session = Depends(get_db)):
    if not db.get(Product, product_id): raise HTTPException(404, "Produto não encontrado")
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(select(PriceObservation, Retailer)
        .join(Offer, PriceObservation.offer_id == Offer.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .where(PriceObservation.product_id == product_id, PriceObservation.captured_at >= since)
        .order_by(PriceObservation.captured_at)).all()
    return [HistoryPoint(date=obs.captured_at, retailer=retailer.name, retailer_slug=retailer.slug,
        price=obs.price, pix_price=obs.pix_price, shipping_price=obs.shipping_price,
        source_kind=obs.source_kind, source_name=obs.source_name) for obs, retailer in rows]


@app.get("/api/products/{product_id}/summary", response_model=PriceSummary)
def summary(product_id: int, db: Session = Depends(get_db)):
    if not db.get(Product, product_id): raise HTTPException(404, "Produto não encontrado")
    return product_summary(db, product_id)
