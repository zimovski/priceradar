from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Offer, PriceObservation, Product, ProductSourceLink, Retailer
from .services.collector import refresh_product


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


@app.get("/api/v28/products/{product_id}/best-offer")
def v28_best_offer(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produto não encontrado")

    best = _best_offer(db, product_id)
    if not best or not _safe_store_url(best[1].url):
        # Old tracked products may have been saved before the direct permalink
        # was persisted. Refreshing once repairs the Offer URL in PostgreSQL.
        _refresh_without_breaking(db, product_id)
        best = _best_offer(db, product_id)

    if not best:
        return None

    obs, offer, retailer = best
    return {
        "retailer": retailer.name,
        "retailer_slug": retailer.slug,
        "price": obs.pix_price or obs.price,
        "regular_price": obs.price,
        "seller_name": offer.seller_name,
        "url": _safe_store_url(offer.url),
        "captured_at": obs.captured_at,
    }


@app.get("/go/product/{product_id}", include_in_schema=False)
def go_to_best_offer(product_id: int, db: Session = Depends(get_db)):
    """Resolve the current listing server-side and redirect to the store.

    This route intentionally does not redirect back to PriceRadar when the URL
    is missing: a failure is shown as an error instead of creating a confusing
    loop to the top of the same page.
    """
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produto não encontrado")

    # Always refresh before purchase so the user receives a current listing and
    # old products get their Mercado Livre permalink repaired automatically.
    _refresh_without_breaking(db, product_id)

    for _obs, offer, _retailer in _latest_offer_candidates(db, product_id):
        direct = _safe_store_url(offer.url)
        if direct:
            return RedirectResponse(direct, status_code=302)

    # Last-resort fallback: an older ProductSourceLink can already contain a
    # valid product/listing URL even if the Offer row predates URL persistence.
    links = db.scalars(
        select(ProductSourceLink).where(ProductSourceLink.product_id == product_id)
    ).all()
    for link in links:
        direct = _safe_store_url(link.source_url)
        if direct:
            return RedirectResponse(direct, status_code=302)

    raise HTTPException(
        404,
        "O Mercado Livre retornou o preço, mas ainda não forneceu um link de compra válido para esta oferta. Atualize o produto e tente novamente.",
    )
