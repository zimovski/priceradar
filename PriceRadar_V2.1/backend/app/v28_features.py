from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Offer, PriceObservation, Product, ProductSourceLink, Retailer
from .providers.mercadolivre import MercadoLivreProvider, MercadoLivreError
from .services.collector import refresh_product

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/static/logo.svg", include_in_schema=False)
def priceradar_logo():
    return FileResponse(STATIC_DIR / "logo.svg", media_type="image/svg+xml")


def _safe_store_url(url: str | None) -> str | None:
    """Only allow redirects to known Mercado Livre hosts."""
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        allowed = (
            host == "mercadolivre.com.br"
            or host.endswith(".mercadolivre.com.br")
            or host == "mercadolivre.com"
            or host.endswith(".mercadolivre.com")
            or host == "mercadolibre.com"
            or host.endswith(".mercadolibre.com")
        )
        if parsed.scheme in {"http", "https"} and allowed:
            return url.strip()
    except Exception:
        pass
    return None


def _catalog_url(external_product_id: str | None) -> str | None:
    compact = re.sub(r"[^A-Za-z0-9]", "", str(external_product_id or "")).upper()
    if re.fullmatch(r"MLB\d+", compact):
        return f"https://www.mercadolivre.com.br/p/{compact}"
    return None


def _latest_offer_candidates(db: Session, product_id: int):
    rows = db.execute(
        select(PriceObservation, Offer, Retailer)
        .join(Offer, PriceObservation.offer_id == Offer.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .where(
            PriceObservation.product_id == product_id,
            PriceObservation.available == True,
        )
        .order_by(PriceObservation.captured_at.desc())
    ).all()

    latest_by_offer: dict[int, tuple[PriceObservation, Offer, Retailer]] = {}
    for obs, offer, retailer in rows:
        latest_by_offer.setdefault(offer.id, (obs, offer, retailer))

    candidates = [
        row for row in latest_by_offer.values()
        if row[0].price is not None and float(row[0].price) > 0
    ]
    candidates.sort(key=lambda row: float(row[0].pix_price or row[0].price))
    return candidates


def _refresh_without_breaking(db: Session, product_id: int) -> None:
    try:
        refresh_product(db, product_id)
    except Exception:
        db.rollback()


def _best_offer(db: Session, product_id: int):
    candidates = _latest_offer_candidates(db, product_id)
    return candidates[0] if candidates else None


def _source_links(db: Session, product_id: int) -> list[ProductSourceLink]:
    return db.scalars(
        select(ProductSourceLink).where(ProductSourceLink.product_id == product_id)
    ).all()


def _resolve_ml_url(db: Session, product_id: int) -> str | None:
    # 1. Exact offer URL already stored in PostgreSQL.
    for _obs, offer, _retailer in _latest_offer_candidates(db, product_id):
        direct = _safe_store_url(offer.url)
        if direct:
            return direct

    links = _source_links(db, product_id)

    # 2. Product/source URL already stored from a prior collection.
    for link in links:
        direct = _safe_store_url(link.source_url)
        if direct:
            return direct

    # 3. Ask the provider again. The price enrichment layer now retries the
    # public /items endpoint and builds an item VIP URL when permalink is absent.
    provider = MercadoLivreProvider()
    for link in links:
        if link.provider_slug != "mercadolivre":
            continue
        try:
            detail = provider.product_detail(link.external_product_id)
            direct = _safe_store_url(detail.get("url"))
            if direct:
                link.source_url = direct
                db.commit()
                return direct
        except MercadoLivreError:
            db.rollback()

    # 4. Guaranteed Mercado Livre purchase path for a tracked catalog product.
    # This goes to the Mercado Livre PDP, where the active sellers are offered.
    for link in links:
        if link.provider_slug == "mercadolivre":
            fallback = _catalog_url(link.external_product_id)
            if fallback:
                return fallback
    return None


@app.get("/api/v28/products/{product_id}/best-offer")
def v28_best_offer(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produto não encontrado")

    best = _best_offer(db, product_id)
    direct_url = _safe_store_url(best[1].url) if best else None
    if not direct_url:
        _refresh_without_breaking(db, product_id)
        best = _best_offer(db, product_id)
        direct_url = _resolve_ml_url(db, product_id)

    if not best:
        return None

    obs, offer, retailer = best
    return {
        "retailer": retailer.name,
        "retailer_slug": retailer.slug,
        "price": obs.pix_price or obs.price,
        "regular_price": obs.price,
        "seller_name": offer.seller_name,
        "url": direct_url,
        "captured_at": obs.captured_at,
    }


@app.get("/go/product/{product_id}", include_in_schema=False)
def go_to_best_offer(product_id: int, db: Session = Depends(get_db)):
    """Refresh, resolve and redirect to a real Mercado Livre purchase path."""
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produto não encontrado")

    _refresh_without_breaking(db, product_id)
    direct = _resolve_ml_url(db, product_id)
    if direct:
        return RedirectResponse(direct, status_code=302)

    raise HTTPException(
        404,
        "Não foi possível obter uma rota de compra do Mercado Livre para este produto agora.",
    )
