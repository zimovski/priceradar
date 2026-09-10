from datetime import datetime, timedelta
from random import Random
from sqlalchemy import select
from .database import Base, engine, SessionLocal
from .models import Product, Retailer, Offer, PriceObservation, User, WatchlistItem

Base.metadata.create_all(bind=engine)

def seed():
    db = SessionLocal()
    try:
        if db.scalar(select(Product).limit(1)):
            print("Banco já possui dados; seed ignorado.")
            return

        p = Product(name="PlayStation 5 Slim Digital", brand="Sony", model="PS5 Slim Digital")
        db.add(p)
        db.flush()

        retailers = {
            "mercado-livre": Retailer(slug="mercado-livre", name="Mercado Livre"),
            "magalu": Retailer(slug="magalu", name="Magazine Luiza"),
            "casas-bahia": Retailer(slug="casas-bahia", name="Casas Bahia"),
            "kabum": Retailer(slug="kabum", name="KaBuM!"),
        }
        db.add_all(retailers.values())
        db.flush()

        offers = {}
        for slug, r in retailers.items():
            o = Offer(product_id=p.id, retailer_id=r.id, external_id=f"DEMO-{slug}",
                      title="PS5 Slim Digital - DADO DEMONSTRATIVO", seller_name=r.name)
            db.add(o); db.flush(); offers[slug] = o

        rng = Random(20260910)
        bases = {"mercado-livre": 3299, "magalu": 3349, "casas-bahia": 3379, "kabum": 3249}
        start = datetime.utcnow() - timedelta(days=89)
        for day in range(90):
            dt = start + timedelta(days=day)
            trend = -1.3 * day
            promo = -220 if day in (18, 47, 76) else 0
            for slug, base in bases.items():
                noise = rng.choice([-80,-50,-30,0,0,20,40,60])
                price = max(2799, round(base + trend + promo + noise, 2))
                db.add(PriceObservation(
                    product_id=p.id, offer_id=offers[slug].id, price=price,
                    pix_price=round(price * 0.97, 2) if slug in ("kabum","magalu") else None,
                    shipping_price=0.0,
                    captured_at=dt,
                    source_kind="demo",
                    source_name="seed_demo",
                ))

        u = User(display_name="Usuário Demo")
        db.add(u); db.flush()
        db.add(WatchlistItem(user_id=u.id, product_id=p.id, target_price=3000.0))
        db.commit()
        print("Seed criado. Produto DEMO: PS5 Slim Digital")
    finally:
        db.close()

if __name__ == "__main__":
    seed()
