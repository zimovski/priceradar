from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from statistics import mean

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Offer, PriceObservation, Product, Retailer

VERSION = "0.2.4"
app.version = VERSION
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)


def _rate_allowed(key: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    bucket = _RATE_BUCKETS[key]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


@app.middleware("http")
async def priceradar_v24_security(request: Request, call_next):
    path = request.url.path
    client_ip = request.client.host if request.client else "unknown"

    if path == "/api/search" and not _rate_allowed(f"search:{client_ip}", 40, 300):
        return JSONResponse({"detail": "Muitas buscas em pouco tempo. Aguarde alguns minutos."}, status_code=429)
    if path.endswith("/refresh") and request.method == "POST" and not _rate_allowed(f"refresh:{client_ip}", 20, 300):
        return JSONResponse({"detail": "Muitas atualizações em pouco tempo. Aguarde alguns minutos."}, status_code=429)

    sensitive = (
        path == "/api/integrations/mercadolivre/credentials"
        or (path == "/api/products" and request.method == "POST")
        or (path.startswith("/api/products/") and (path.endswith("/observations") or path.endswith("/history/import")))
    )
    if sensitive and request.method in {"POST", "DELETE"}:
        expected_admin = os.getenv("ADMIN_API_SECRET")
        supplied_admin = request.headers.get("x-admin-secret")
        if not expected_admin or supplied_admin != expected_admin:
            return JSONResponse({"detail": "Endpoint administrativo indisponível."}, status_code=403)

    if path == "/api/search/track" and request.method == "POST":
        return JSONResponse({"detail": "Acompanhe um produto real retornado por uma loja."}, status_code=400)
    if path == "/api/search/track-external" and request.method == "POST" and not _rate_allowed(f"track:{client_ip}", 20, 300):
        return JSONResponse({"detail": "Muitos produtos adicionados em pouco tempo."}, status_code=429)

    if path == "/api/collect/refresh-all" and request.method == "POST":
        expected = os.getenv("COLLECTOR_SECRET")
        supplied = request.headers.get("x-collector-secret")
        if not expected:
            return JSONResponse({"detail": "Coleta agendada ainda não foi configurada."}, status_code=503)
        if not supplied or supplied != expected:
            return JSONResponse({"detail": "Não autorizado."}, status_code=401)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _product_observations(db: Session, product_id: int, days: int | None = None) -> list[PriceObservation]:
    q = select(PriceObservation).where(PriceObservation.product_id == product_id)
    if days is not None:
        q = q.where(PriceObservation.captured_at >= datetime.utcnow() - timedelta(days=days))
    return db.scalars(q.order_by(PriceObservation.captured_at)).all()


def _latest_by_retailer(db: Session, product_id: int) -> list[tuple[PriceObservation, Offer, Retailer]]:
    rows = db.execute(
        select(PriceObservation, Offer, Retailer)
        .join(Offer, PriceObservation.offer_id == Offer.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .where(PriceObservation.product_id == product_id, PriceObservation.available == True)
        .order_by(Retailer.slug, PriceObservation.captured_at.desc())
    ).all()
    latest: dict[str, tuple[PriceObservation, Offer, Retailer]] = {}
    for obs, offer, retailer in rows:
        latest.setdefault(retailer.slug, (obs, offer, retailer))
    return list(latest.values())


def _daily_minima(observations: list[PriceObservation]) -> list[tuple[object, float]]:
    daily = {}
    for obs in observations:
        if not obs.available or obs.price <= 0:
            continue
        day = obs.captured_at.date()
        daily[day] = min(daily.get(day, obs.price), obs.price)
    return sorted(daily.items())


def _insights_for(db: Session, product_id: int) -> dict:
    all_obs = _product_observations(db, product_id)
    latest = _latest_by_retailer(db, product_id)
    current = min((row[0].price for row in latest), default=None)
    valid_prices = [o.price for o in all_obs if o.price > 0]
    daily_all = _daily_minima(all_obs)
    recent_30 = _daily_minima([o for o in all_obs if o.captured_at >= datetime.utcnow() - timedelta(days=30)])
    recent_90 = _daily_minima([o for o in all_obs if o.captured_at >= datetime.utcnow() - timedelta(days=90)])

    hist_min = min(valid_prices) if valid_prices else None
    hist_max = max(valid_prices) if valid_prices else None
    avg_30 = mean([p for _, p in recent_30]) if recent_30 else None
    avg_90 = mean([p for _, p in recent_90]) if recent_90 else None

    previous = None
    chronological = [o for o in all_obs if o.available and o.price > 0]
    if len(chronological) >= 2:
        previous = chronological[-2].price

    change_pct = ((current / previous) - 1) * 100 if current and previous else None
    vs_avg_90_pct = ((current / avg_90) - 1) * 100 if current and avg_90 else None
    above_min_pct = ((current / hist_min) - 1) * 100 if current and hist_min else None

    score = None
    label = "Histórico insuficiente"
    explanation = "Continue acompanhando para o PriceRadar aprender a faixa normal deste produto."
    if current is not None and len(daily_all) >= 3:
        values = [p for _, p in daily_all]
        lower_or_equal = sum(1 for p in values if p <= current)
        percentile = lower_or_equal / len(values)
        score = round(max(0.0, min(100.0, (1.0 - percentile) * 100.0)))
        if score >= 80:
            label = "Excelente preço"
        elif score >= 60:
            label = "Bom preço"
        elif score >= 35:
            label = "Preço normal"
        else:
            label = "Preço alto"
        if avg_90:
            direction = "abaixo" if current < avg_90 else "acima"
            explanation = f"O preço atual está {abs(vs_avg_90_pct or 0):.1f}% {direction} da média diária dos últimos 90 dias."
        elif hist_min:
            explanation = f"O preço atual está {above_min_pct or 0:.1f}% acima do menor valor registrado."

    return {
        "current_min": current,
        "historical_min": hist_min,
        "historical_max": hist_max,
        "average_30": avg_30,
        "average_90": avg_90,
        "change_from_previous_pct": change_pct,
        "vs_average_90_pct": vs_avg_90_pct,
        "above_historical_min_pct": above_min_pct,
        "deal_score": score,
        "deal_label": label,
        "deal_explanation": explanation,
        "observations": len(all_obs),
        "days_observed": len(daily_all),
        "retailers": len(latest),
        "first_observation": all_obs[0].captured_at if all_obs else None,
        "last_observation": all_obs[-1].captured_at if all_obs else None,
    }


@app.get("/api/v2/dashboard")
def dashboard(db: Session = Depends(get_db)):
    products = db.scalars(select(Product).order_by(Product.created_at.desc())).all()
    cards = []
    for product in products:
        insights = _insights_for(db, product.id)
        latest = _latest_by_retailer(db, product.id)
        best = min(latest, key=lambda x: x[0].price) if latest else None
        cards.append({
            "id": product.id, "name": product.name, "brand": product.brand, "model": product.model,
            "image_url": product.image_url, "created_at": product.created_at,
            "current_min": insights["current_min"], "historical_min": insights["historical_min"],
            "average_90": insights["average_90"], "deal_score": insights["deal_score"],
            "deal_label": insights["deal_label"], "observations": insights["observations"],
            "last_observation": insights["last_observation"],
            "best_retailer": best[2].name if best else None, "best_url": best[1].url if best else None,
        })
    return {"version": VERSION, "products": cards, "count": len(cards)}


@app.get("/api/v2/products/{product_id}/insights")
def product_insights(product_id: int, db: Session = Depends(get_db)):
    if not db.get(Product, product_id):
        raise HTTPException(404, "Produto não encontrado")
    return _insights_for(db, product_id)


@app.get("/api/v2/products/{product_id}/daily")
def daily_history(product_id: int, days: int = Query(730, ge=1, le=7300), db: Session = Depends(get_db)):
    if not db.get(Product, product_id):
        raise HTTPException(404, "Produto não encontrado")
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        select(PriceObservation, Retailer)
        .join(Offer, PriceObservation.offer_id == Offer.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .where(PriceObservation.product_id == product_id, PriceObservation.captured_at >= since)
        .order_by(PriceObservation.captured_at)
    ).all()
    groups = defaultdict(list)
    sources = defaultdict(set)
    names = {}
    for obs, retailer in rows:
        key = (obs.captured_at.date().isoformat(), retailer.slug)
        groups[key].append(obs.price)
        sources[key].add(obs.source_kind)
        names[retailer.slug] = retailer.name
    result = []
    for (date, slug), prices in sorted(groups.items()):
        result.append({
            "date": date, "retailer_slug": slug, "retailer": names.get(slug, slug),
            "min": min(prices), "max": max(prices), "average": mean(prices),
            "observations": len(prices), "source_kinds": sorted(sources[(date, slug)]),
        })
    return result


@app.get("/api/v2/system")
def system_status():
    from .providers.mercadolivre import MercadoLivreProvider
    ml = MercadoLivreProvider()
    db_url = os.getenv("DATABASE_URL", "sqlite")
    return {
        "version": VERSION,
        "database": "postgresql" if db_url.startswith("postgres") else "sqlite",
        "mercadolivre_connected": ml.configured(),
        "collector_configured": bool(os.getenv("COLLECTOR_SECRET")),
        "server_time": datetime.now(timezone.utc),
    }
