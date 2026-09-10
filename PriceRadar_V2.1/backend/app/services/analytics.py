from sqlalchemy.orm import Session
from sqlalchemy import select, func
from ..models import PriceObservation


def product_summary(db: Session, product_id: int) -> dict:
    q = select(
        func.min(PriceObservation.price),
        func.max(PriceObservation.price),
        func.avg(PriceObservation.price),
        func.count(PriceObservation.id),
        func.min(PriceObservation.captured_at),
        func.max(PriceObservation.captured_at),
    ).where(PriceObservation.product_id == product_id)
    min_p, max_p, avg_p, count, first_dt, last_dt = db.execute(q).one()

    # Preço atual = menor preço entre a observação mais recente de cada oferta.
    rows = db.scalars(
        select(PriceObservation)
        .where(PriceObservation.product_id == product_id, PriceObservation.available == True)
        .order_by(PriceObservation.offer_id, PriceObservation.captured_at.desc())
    ).all()
    latest_by_offer = {}
    for obs in rows:
        latest_by_offer.setdefault(obs.offer_id, obs)
    current_min = min((o.price for o in latest_by_offer.values()), default=None)

    return {
        "current_min": current_min,
        "historical_min": min_p,
        "historical_max": max_p,
        "average": float(avg_p) if avg_p is not None else None,
        "observations": int(count or 0),
        "first_observation": first_dt,
        "last_observation": last_dt,
    }
