from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Product, ProductSourceLink, Retailer, Offer, PriceObservation
from ..providers.magalu_web import MagaluError, MagaluProvider
from ..providers.mercadolivre import MercadoLivreProvider, MercadoLivreError
from ..providers.mercadolivre_search_enhancement import _family, _generation, _norm, _score, _tokens

RETAILERS = {
    "mercadolivre": ("mercadolivre", "Mercado Livre"),
    "magalu": ("magalu", "Magazine Luiza"),
}
TRUSTED_ML_SOURCE = "mercadolivre_buy_box"
LEGACY_ML_SOURCES = {"mercadolivre_api"}


def _retailer(db: Session, slug: str, name: str) -> Retailer:
    row = db.scalar(select(Retailer).where(Retailer.slug == slug))
    if not row:
        row = Retailer(slug=slug, name=name)
        db.add(row)
        db.flush()
    return row


def _remove_legacy_ml_observations_once(db: Session, product: Product) -> int:
    already_trusted = db.scalar(
        select(PriceObservation.id)
        .where(
            PriceObservation.product_id == product.id,
            PriceObservation.source_name.in_({TRUSTED_ML_SOURCE, "mercadolivre_verified_listing"}),
        )
        .limit(1)
    )
    if already_trusted:
        return 0
    result = db.execute(
        delete(PriceObservation).where(
            PriceObservation.product_id == product.id,
            PriceObservation.source_name.in_(LEGACY_ML_SOURCES),
        )
    )
    return int(result.rowcount or 0)


def _record_detail(
    db: Session,
    product: Product,
    detail: dict,
    *,
    retailer_slug: str,
    retailer_name: str,
    source_name: str,
) -> int | None:
    price_value = detail.get("price")
    external_id = detail.get("item_id") or detail.get("external_product_id")
    if not price_value or not external_id or not detail.get("available", True):
        return None

    retailer = _retailer(db, retailer_slug, retailer_name)
    offer = db.scalar(select(Offer).where(
        Offer.retailer_id == retailer.id,
        Offer.external_id == str(external_id),
    ))
    if not offer:
        offer = Offer(
            product_id=product.id,
            retailer_id=retailer.id,
            external_id=str(external_id),
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

    now = datetime.utcnow()
    price = float(price_value)
    pix_price = detail.get("pix_price")
    pix_price = float(pix_price) if pix_price else None
    available = bool(detail.get("available", True))
    last = db.scalar(
        select(PriceObservation)
        .where(PriceObservation.offer_id == offer.id)
        .order_by(PriceObservation.captured_at.desc())
        .limit(1)
    )
    if (
        last
        and last.price == price
        and last.pix_price == pix_price
        and last.available == available
        and last.source_name == source_name
        and now - last.captured_at < timedelta(minutes=15)
    ):
        return None

    obs = PriceObservation(
        product_id=product.id,
        offer_id=offer.id,
        price=price,
        pix_price=pix_price,
        available=available,
        currency=detail.get("currency") or "BRL",
        captured_at=now,
        source_kind="observed",
        source_name=source_name,
    )
    db.add(obs)
    db.flush()
    return obs.id


def record_ml_detail(db: Session, product: Product, detail: dict, *, source_name: str | None = None) -> int | None:
    price_source = detail.get("price_source")
    if price_source == "buy_box_winner":
        source_name = source_name or TRUSTED_ML_SOURCE
        _remove_legacy_ml_observations_once(db, product)
    else:
        source_name = source_name or "mercadolivre_verified_listing"
    return _record_detail(
        db,
        product,
        detail,
        retailer_slug="mercadolivre",
        retailer_name="Mercado Livre",
        source_name=source_name,
    )


def record_magalu_detail(db: Session, product: Product, detail: dict) -> int | None:
    return _record_detail(
        db,
        product,
        detail,
        retailer_slug="magalu",
        retailer_name="Magazine Luiza",
        source_name="magalu_public_storefront",
    )


def _capacity(text: str | None) -> str | None:
    n = _norm(text)
    match = re.search(r"\b(\d{2,4})\s*(gb|tb)\b", n)
    return f"{match.group(1)}{match.group(2)}" if match else None


def _variant_words(text: str | None) -> set[str]:
    n = _norm(text)
    phrases = {
        "pro max": "pro max",
        "pro": "pro",
        "plus": "plus",
        "ultra": "ultra",
        "air": "air",
        "slim": "slim",
        "digital": "digital",
        "disc": "disc",
        "ti": "ti",
        "super": "super",
    }
    found: set[str] = set()
    for phrase, key in phrases.items():
        if re.search(rf"\b{re.escape(phrase)}\b", n):
            found.add(key)
    return found


def _identity_tokens(text: str | None) -> set[str]:
    tokens = set(_tokens(text))
    ignore = {
        "apple", "sony", "samsung", "branco", "preto", "azul", "verde",
        "cinza", "prata", "prateado", "dourado", "lavanda", "salvia",
        "gb", "tb", "nvidia", "geforce", "gddr7", "gddr6", "bits", "bit",
    }
    cleaned: set[str] = set()
    for token in tokens:
        if token in ignore:
            continue
        if token.isdigit() and len(token) >= 2:
            continue
        if re.fullmatch(r"\d{2,4}(?:gb|tb)", token):
            continue
        cleaned.add(token)
    return cleaned


def _strong_variant_match(product: Product, candidate: dict) -> bool:
    source_name = " ".join(x for x in (product.brand, product.name, product.model) if x)
    target_name = str(candidate.get("name") or "")
    if not target_name:
        return False

    fam = _family(source_name)
    if fam:
        source_gen = _generation(source_name, fam)
        target_gen = _generation(target_name, fam)
        if source_gen is not None and target_gen != source_gen:
            return False

    source_capacity = _capacity(source_name)
    target_capacity = _capacity(target_name)
    if source_capacity and target_capacity != source_capacity:
        return False

    required_variants = _variant_words(source_name)
    target_variants = _variant_words(target_name)
    if required_variants and not required_variants.issubset(target_variants):
        return False

    meaningful = _identity_tokens(source_name)
    target_tokens = _identity_tokens(target_name)
    if meaningful:
        coverage = len(meaningful & target_tokens) / len(meaningful)
        if coverage < 0.60:
            return False

    return _score(source_name, target_name) >= 18.0


def _magalu_search_query(product: Product) -> str:
    """Build a compact identity query instead of pasting a long catalog title.

    Marketplace search engines often perform worse when a title includes every
    technical attribute and internal model suffix. The strict matcher below is
    still responsible for proving that the returned variant is the same item.
    """
    source = " ".join(x for x in (product.brand, product.name, product.model) if x)
    fam = _family(source)
    parts: list[str] = []
    if product.brand:
        parts.append(product.brand)

    generation = _generation(source, fam) if fam else None
    if fam == "iphone":
        parts.append("iPhone")
        if generation is not None:
            parts.append(str(generation))
    elif fam == "playstation":
        parts.append("PlayStation")
        if generation is not None:
            parts.append(str(generation))
    elif fam == "rtx":
        parts.append("RTX")
        if generation is not None:
            parts.append(str(generation))
    elif fam == "radeon":
        parts.append("Radeon RX")
        if generation is not None:
            parts.append(str(generation))
    else:
        parts.extend(_tokens(product.name)[:5])

    variants = _variant_words(source)
    for variant in ("ti", "super", "pro max", "pro", "plus", "ultra", "slim", "digital"):
        if variant in variants:
            parts.append(variant)

    capacity = _capacity(source)
    if capacity:
        parts.append(capacity.upper())

    # Keep one distinctive model/SKU token when the catalog gives us one. This
    # greatly improves GPU and notebook matching without making the query huge.
    tokens = _tokens(source)
    modelish = [
        token for token in tokens
        if len(token) >= 7 and any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)
    ]
    if modelish:
        parts.append(modelish[-1])

    compact: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = _norm(part)
        if key and key not in seen:
            seen.add(key)
            compact.append(str(part))
    return " ".join(compact) or product.name


def _ensure_magalu_link(db: Session, product: Product) -> ProductSourceLink | None:
    existing = db.scalar(select(ProductSourceLink).where(
        ProductSourceLink.product_id == product.id,
        ProductSourceLink.provider_slug == "magalu",
    ))
    if existing:
        return existing

    provider = MagaluProvider()
    query = _magalu_search_query(product)
    candidates = provider.search(query, limit=10)
    matches = [row for row in candidates if _strong_variant_match(product, row)]
    if not matches and query != product.name:
        # A second, narrower attempt is useful for unusual catalog titles.
        candidates = provider.search(product.name, limit=10)
        matches = [row for row in candidates if _strong_variant_match(product, row)]
    if not matches:
        return None

    source_name = " ".join(x for x in (product.brand, product.name, product.model) if x)
    matches.sort(key=lambda row: _score(source_name, row.get("name")), reverse=True)
    best = matches[0]
    url = best.get("url")
    external_id = str(best.get("external_product_id") or best.get("item_id") or "")
    if not external_id or not url:
        return None

    link = ProductSourceLink(
        product_id=product.id,
        provider_slug="magalu",
        external_product_id=external_id,
        source_url=url,
    )
    db.add(link)
    db.flush()
    return link


def refresh_product(db: Session, product_id: int) -> dict:
    product = db.get(Product, product_id)
    if not product:
        return {"product_id": product_id, "ok": False, "message": "Produto não encontrado", "observations": 0}

    observations = 0
    errors: list[str] = []
    links_updated = 0
    successful_sources = 0

    ml_links = db.scalars(select(ProductSourceLink).where(
        ProductSourceLink.product_id == product_id,
        ProductSourceLink.provider_slug == "mercadolivre",
    )).all()
    for link in ml_links:
        try:
            provider = MercadoLivreProvider()
            detail = provider.product_detail(link.external_product_id)
            direct_url = detail.get("url")
            if direct_url and link.source_url != direct_url:
                link.source_url = direct_url
                links_updated += 1
            if record_ml_detail(db, product, detail):
                observations += 1
            successful_sources += 1
        except MercadoLivreError as exc:
            errors.append(f"Mercado Livre: {exc}")

    try:
        magalu_link = _ensure_magalu_link(db, product)
        if magalu_link:
            provider = MagaluProvider()
            detail = provider.product_detail(magalu_link.source_url or "", magalu_link.external_product_id)
            if detail.get("url") and magalu_link.source_url != detail.get("url"):
                magalu_link.source_url = detail.get("url")
                links_updated += 1
            if record_magalu_detail(db, product, detail):
                observations += 1
            successful_sources += 1
    except MagaluError as exc:
        errors.append(f"Magazine Luiza: {exc}")
    except Exception:
        errors.append("Magazine Luiza: não foi possível confirmar esta variante agora.")

    db.commit()
    return {
        "product_id": product_id,
        "ok": successful_sources > 0,
        "message": "; ".join(errors) or None,
        "observations": observations,
        "links_updated": links_updated,
        "sources_ok": successful_sources,
    }
