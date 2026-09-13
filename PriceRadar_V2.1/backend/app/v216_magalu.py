from __future__ import annotations

import re
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .main import app
from .models import Product, ProductSourceLink
from .providers.magalu_web import MagaluError, MagaluProvider
from .providers.mercadolivre import MercadoLivreError, MercadoLivreProvider
from .providers.mercadolivre_search_enhancement import _family, _generation, _norm, _score, _tokens, _looks_like_accessory
from .services.collector import record_magalu_detail, record_ml_detail
from .v215_ui import v215_web_preview


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()))
    ]


def _capacity(text: str | None) -> str | None:
    n = _norm(text)
    m = re.search(r"\b(\d{1,4})\s*(gb|tb)\b", n)
    return f"{m.group(1)}{m.group(2)}" if m else None


def _variants(text: str | None) -> set[str]:
    n = _norm(text)
    found: set[str] = set()
    for v in ("ti", "super", "pro max", "pro", "plus", "ultra", "slim", "digital"):
        if re.search(rf"\b{re.escape(v)}\b", n):
            found.add(v)
    return found


def _brand(product: Product) -> str | None:
    raw = _norm(product.brand or "")
    if not raw:
        return None
    # These brands are often omitted from retailer titles because the family
    # already identifies them (iPhone, PlayStation, GeForce/Radeon).
    if raw in {"apple", "sony", "microsoft", "nvidia", "amd"}:
        return None
    return raw.split()[0]


def _match_reason(product: Product, candidate: dict[str, Any]) -> tuple[bool, list[str], float]:
    source = " ".join(x for x in (product.brand, product.name, product.model) if x)
    target = str(candidate.get("name") or "")
    reasons: list[str] = []
    if not target:
        return False, ["sem título"], -999.0
    if _looks_like_accessory(target):
        return False, ["acessório"], -999.0

    fam = _family(source)
    source_gen = _generation(source, fam) if fam else None
    target_gen = _generation(target, fam) if fam else None
    if source_gen is not None and target_gen != source_gen:
        reasons.append(f"geração {target_gen} != {source_gen}")

    source_cap = _capacity(source)
    target_cap = _capacity(target)
    if source_cap and fam in {"iphone", "rtx", "radeon"} and target_cap != source_cap:
        reasons.append(f"capacidade {target_cap} != {source_cap}")

    source_vars = _variants(source)
    target_vars = _variants(target)
    if fam in {"rtx", "radeon"}:
        # A 5070 and a 5070 Ti/Super are different products in price comparison.
        for v in ("ti", "super"):
            if (v in source_vars) != (v in target_vars):
                reasons.append(f"variante {v} incompatível")
    else:
        for v in source_vars:
            if v not in target_vars:
                reasons.append(f"faltou variante {v}")

    wanted_brand = _brand(product)
    if wanted_brand and wanted_brand not in set(_norm(target).split()):
        reasons.append(f"marca {wanted_brand} ausente")

    score = _score(source, target)
    return not reasons, reasons, score


def _search_queries(product: Product) -> list[str]:
    source = " ".join(x for x in (product.brand, product.name, product.model) if x)
    fam = _family(source)
    gen = _generation(source, fam) if fam else None
    cap = _capacity(source)
    brand = product.brand or ""
    variants = _variants(source)

    queries: list[str] = []
    if fam == "rtx":
        core = " ".join(x for x in (brand, "RTX", str(gen) if gen else "", "Ti" if "ti" in variants else "", "Super" if "super" in variants else "", cap.upper() if cap else "") if x)
        queries.extend([core, f"placa video {brand} RTX {gen or ''}".strip(), f"RTX {gen or ''} {cap.upper() if cap else ''}".strip()])
    elif fam == "radeon":
        core = " ".join(x for x in (brand, "Radeon RX", str(gen) if gen else "", cap.upper() if cap else "") if x)
        queries.extend([core, f"placa video {brand} Radeon {gen or ''}".strip()])
    elif fam == "iphone":
        core = " ".join(x for x in ("iPhone", str(gen) if gen else "", "Pro Max" if "pro max" in variants else "Pro" if "pro" in variants else "Plus" if "plus" in variants else "", cap.upper() if cap else "") if x)
        queries.extend([core, f"iPhone {gen or ''}".strip()])
    elif fam == "playstation":
        core = " ".join(x for x in ("PlayStation", str(gen) if gen else "", "Slim" if "slim" in variants else "", "Digital" if "digital" in variants else "") if x)
        queries.extend([core, f"PlayStation {gen or ''}".strip()])
    else:
        core_tokens = [t for t in _tokens(source) if len(t) > 2][:7]
        queries.append(" ".join(core_tokens) or product.name)

    # Last resort: original product name, but only after concise queries.
    queries.append(product.name)
    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        q = " ".join(q.split())
        k = _norm(q)
        if len(q) >= 2 and k not in seen:
            seen.add(k)
            out.append(q)
    return out


def _best_magalu_candidate(product: Product) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    provider = MagaluProvider()
    provider.timeout = 9.0
    all_candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()

    for query in _search_queries(product):
        try:
            rows = provider.search(query, limit=12)
        except MagaluError as exc:
            errors.append(f"{query}: {exc}")
            continue
        for row in rows:
            key = str(row.get("external_product_id") or row.get("url") or row.get("name"))
            if key in seen:
                continue
            seen.add(key)
            ok, reasons, score = _match_reason(product, row)
            enriched = dict(row)
            enriched["_match"] = ok
            enriched["_reasons"] = reasons
            enriched["_score"] = score
            enriched["_query"] = query
            all_candidates.append(enriched)
        if any(row.get("_match") for row in all_candidates):
            break

    matches = [row for row in all_candidates if row.get("_match") and row.get("price") and row.get("url")]
    if not matches:
        return None, all_candidates, errors

    # Identity comes first. Price only breaks ties among equivalently relevant
    # variants, so we do not accidentally choose a cheaper but less exact model.
    matches.sort(key=lambda row: (float(row.get("_score") or -999), -float(row.get("pix_price") or row.get("price") or 10**18)), reverse=True)
    top_score = float(matches[0].get("_score") or -999)
    close = [row for row in matches if float(row.get("_score") or -999) >= top_score - 4]
    close.sort(key=lambda row: float(row.get("pix_price") or row.get("price") or 10**18))
    return close[0], all_candidates, errors


def _save_magalu_link(db: Session, product: Product, candidate: dict[str, Any]) -> ProductSourceLink:
    link = db.scalar(select(ProductSourceLink).where(
        ProductSourceLink.product_id == product.id,
        ProductSourceLink.provider_slug == "magalu",
    ))
    external_id = str(candidate.get("external_product_id") or candidate.get("item_id") or "")
    url = str(candidate.get("url") or "")
    if not external_id or not url:
        raise ValueError("Candidato Magalu sem id ou URL")
    if not link:
        link = ProductSourceLink(product_id=product.id, provider_slug="magalu", external_product_id=external_id, source_url=url)
        db.add(link)
        db.flush()
    else:
        link.external_product_id = external_id
        link.source_url = url
    return link


_remove_route("/api/products/{product_id}/refresh", "POST")


@app.post("/api/products/{product_id}/refresh")
def v216_refresh_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")

    observations = 0
    messages: list[str] = []
    sources_ok = 0

    # Mercado Livre remains strict and unchanged from the V2.15 logic.
    ml_links = db.scalars(select(ProductSourceLink).where(
        ProductSourceLink.product_id == product_id,
        ProductSourceLink.provider_slug == "mercadolivre",
    )).all()
    for link in ml_links:
        try:
            detail = MercadoLivreProvider().product_detail(link.external_product_id)
            if detail.get("url"):
                link.source_url = detail.get("url")
            if record_ml_detail(db, product, detail):
                observations += 1
            if detail.get("price"):
                sources_ok += 1
        except MercadoLivreError as exc:
            messages.append(f"Mercado Livre: {exc}")

    # For Magalu the search result itself is a public storefront offer with
    # product URL + displayed price. Record that directly. Product-detail HTML
    # is optional and must not erase a valid search-card price if markup changes.
    try:
        candidate, _candidates, errors = _best_magalu_candidate(product)
        if candidate:
            _save_magalu_link(db, product, candidate)
            clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
            if record_magalu_detail(db, product, clean):
                observations += 1
            sources_ok += 1
        elif errors:
            messages.append("Magazine Luiza: " + errors[-1])
        else:
            messages.append("Magazine Luiza: nenhum anúncio da mesma variante foi confirmado.")
    except Exception as exc:
        messages.append(f"Magazine Luiza: {type(exc).__name__}: {exc}")

    db.commit()
    return {
        "product_id": product_id,
        "ok": sources_ok > 0,
        "observations": observations,
        "sources_ok": sources_ok,
        "message": "; ".join(messages) or None,
    }


@app.get("/api/products/{product_id}/magalu-diagnostics")
def v216_magalu_diagnostics(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    best, candidates, errors = _best_magalu_candidate(product)
    compact = []
    for row in candidates[:20]:
        compact.append({
            "name": row.get("name"),
            "price": row.get("price"),
            "pix_price": row.get("pix_price"),
            "url": row.get("url"),
            "match": row.get("_match"),
            "reasons": row.get("_reasons"),
            "score": row.get("_score"),
            "query": row.get("_query"),
        })
    return {
        "product": product.name,
        "queries": _search_queries(product),
        "best": None if not best else {"name": best.get("name"), "price": best.get("price"), "pix_price": best.get("pix_price"), "url": best.get("url")},
        "errors": errors,
        "candidates": compact,
    }


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v216_web_preview():
    base = v215_web_preview()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.replace("V2.15", "V2.16")
    return HTMLResponse(text)
