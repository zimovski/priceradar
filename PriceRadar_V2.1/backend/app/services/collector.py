from __future__ import annotations

from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, ProductSourceLink, Retailer, Offer, PriceObservation
from ..providers.mercadolivre import MercadoLivreProvider, MercadoLivreError


RETAILERS = {
    "mercadolivre": ("mercadolivre", "Mercado Livre"),
}


def _retailer(db: Session, slug: str, name: str) -> Retailer:
    row = db.scalar(select(Retailer).where(Retailer.slug == slug))
    if not row:
        row = Retailer(slug=slug, name=name)
        db.add(row)
        db.flush()
    return row


def record_ml_detail(db: Session, product: Product, detail: dict, *, source_name: str = "mercadolivre_api") -> int | None:
    if not detail.get("price") or not detail.get("item_id"):
        return None
    retailer = _retailer(db, "mercadolivre", "Mercado Livre")
    offer = db.scalar(select(Offer).where(
        Offer.retailer_id == retailer.id,
        Offer.external_id == str(detail["item_id"]),
    ))
    if not offer:
        offer = Offer(
            product_id=product.id,
            retailer_id=retailer.id,
            external_id=str(detail["item_id"]),
            title=detail.get("name") or product.name,
            seller_name=detail.get("seller_name"),
            url=detail.get("url"),
        )
        db.add(offer)
        db.flush()
    else:
        offer.product_id = product.id
        offer.title = detail.get("name") or offer.title
        offer.seller_name = detail.get("seller_name") or offer.seller_name
        offer.url = detail.get("url") or offer.url
        offer.active = True

    obs = PriceObservation(
        product_id=product.id,
        offer_id=offer.id,
        price=float(detail["price"]),
        available=bool(detail.get("available", True)),
        currency=detail.get("currency") or "BRL",
        captured_at=datetime.utcnow(),
        source_kind="observed",
        source_name=source_name,
    )
    db.add(obs)
    db.flush()
    return obs.id


def refresh_product(db: Session, product_id: int) -> dict:
    product = db.get(Product, product_id)
    if not product:
        return {"product_id": product_id, "ok": False, "message": "Produto não encontrado", "observations": 0}
    links = db.scalars(select(ProductSourceLink).where(ProductSourceLink.product_id == product_id)).all()
    observations = 0
    errors = []
    for link in links:
        if link.provider_slug == "mercadolivre":
            try:
                provider = MercadoLivreProvider()
                detail = provider.product_detail(link.external_product_id)
                if record_ml_detail(db, product, detail):
                    observations += 1
            except MercadoLivreError as exc:
                errors.append(str(exc))
    db.commit()
    return {"product_id": product_id, "ok": not errors, "message": "; ".join(errors) or None, "observations": observations}
