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
from .providers.mercadolivre_search_enhancement import (
    _family, _generation, _looks_like_accessory, _norm, _tokens,
)
from .services.collector import record_magalu_detail, record_ml_detail
from .v216_magalu import v216_web_preview


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()))
    ]


def _text(product: Product) -> str:
    return " ".join(x for x in (product.brand, product.name, product.model) if x)


def _capacity(text: str | None) -> int | None:
    n = _norm(text)
    m = re.search(r"\b(\d{1,4})\s*(gb|tb)\b", n)
    if not m:
        return None
    value = int(m.group(1))
    return value * 1024 if m.group(2) == "tb" else value


def _variants(text: str | None) -> set[str]:
    n = _norm(text)
    found: set[str] = set()
    for value in ("ti", "super", "pro max", "pro", "plus", "ultra", "air", "slim", "digital"):
        if re.search(rf"\b{re.escape(value)}\b", n):
            found.add(value)
    return found


def _core_words(text: str | None) -> set[str]:
    stop = {
        "placa", "video", "de", "da", "do", "para", "com", "sem", "nova", "novo",
        "gb", "tb", "gddr6", "gddr7", "bit", "bits", "hdmi", "displayport", "oc",
        "apple", "sony", "nvidia", "amd", "geforce", "radeon", "cor", "branco", "preto",
    }
    return {token for token in _tokens(text) if len(token) > 1 and token not in stop}


def _brand_word(product: Product) -> str | None:
    brand = _norm(product.brand or "").strip()
    if not brand:
        return None
    return brand.split()[0]


def _similarity(product: Product, candidate: dict[str, Any]) -> tuple[int, str, list[str]]:
    """Return a consumer-oriented cross-store similarity score.

    We intentionally do not require the same SKU. The goal is to compare what a
    shopper would reasonably consider the same target: e.g. any board partner's
    RTX 5070, or an iPhone 17 of another storage tier when the exact capacity is
    unavailable. Hard rejection is reserved for clearly different generations or
    accessories.
    """
    source = _text(product)
    target = str(candidate.get("name") or "")
    notes: list[str] = []
    if not target or _looks_like_accessory(target):
        return 0, "incompatível", ["acessório ou título inválido"]

    source_family = _family(source)
    target_family = _family(target)
    if source_family and target_family and source_family != target_family:
        return 0, "incompatível", ["família diferente"]

    score = 20
    if source_family and target_family == source_family:
        score += 25
        notes.append("mesma família")

    source_gen = _generation(source, source_family) if source_family else None
    target_gen = _generation(target, source_family) if source_family else None
    if source_gen is not None:
        if target_gen is not None and target_gen != source_gen:
            return 0, "incompatível", [f"geração/modelo {target_gen} diferente de {source_gen}"]
        if target_gen == source_gen:
            score += 30
            notes.append("mesma geração/modelo")

    source_vars = _variants(source)
    target_vars = _variants(target)
    if source_vars == target_vars:
        score += 10
        if source_vars:
            notes.append("mesma variante")
    else:
        mismatch = len(source_vars.symmetric_difference(target_vars))
        score -= min(16, mismatch * 8)
        if mismatch:
            notes.append("variante próxima")

    source_cap = _capacity(source)
    target_cap = _capacity(target)
    if source_cap is not None and target_cap is not None:
        if source_cap == target_cap:
            score += 8
            notes.append("mesma capacidade")
        else:
            # Storage/VRAM is useful context, not a reason to throw away a
            # comparison. A close tier gets a small penalty; a distant tier more.
            ratio = max(source_cap, target_cap) / max(1, min(source_cap, target_cap))
            score -= 4 if ratio <= 2 else 8
            notes.append("capacidade diferente")

    source_brand = _brand_word(product)
    target_words = set(_norm(target).split())
    if source_brand and source_brand in target_words:
        score += 4
        notes.append("mesma marca")
    elif source_family not in {"rtx", "radeon", "iphone", "playstation"} and source_brand:
        score -= 4

    source_words = _core_words(source)
    target_core = _core_words(target)
    if source_words:
        overlap = len(source_words & target_core) / len(source_words)
        score += round(overlap * 15)
        if overlap >= 0.5:
            notes.append("descrição semelhante")

    score = max(0, min(100, int(score)))
    if score >= 88:
        level = "muito_proximo"
    elif score >= 70:
        level = "equivalente"
    elif score >= 48:
        level = "similar"
    else:
        level = "fraco"
    return score, level, notes


def _intent_queries(product: Product) -> list[str]:
    """Broad-first search queries built from shopper intent, not catalog SKU."""
    source = _text(product)
    fam = _family(source)
    gen = _generation(source, fam) if fam else None
    variants = _variants(source)
    cap = _capacity(source)
    queries: list[str] = []

    if fam == "rtx":
        queries.append(f"RTX {gen}" if gen else "RTX")
        queries.append(f"placa de video RTX {gen}" if gen else "placa de video RTX")
        if "ti" in variants:
            queries.insert(0, f"RTX {gen} Ti")
        if "super" in variants:
            queries.insert(0, f"RTX {gen} Super")
    elif fam == "radeon":
        queries.append(f"Radeon RX {gen}" if gen else "Radeon RX")
        queries.append(f"placa de video Radeon {gen}" if gen else "placa de video Radeon")
    elif fam == "iphone":
        queries.append(f"iPhone {gen}" if gen else "iPhone")
        special = "Pro Max" if "pro max" in variants else "Pro" if "pro" in variants else "Plus" if "plus" in variants else ""
        if special and gen:
            queries.insert(0, f"iPhone {gen} {special}")
    elif fam == "playstation":
        queries.append(f"PlayStation {gen}" if gen else "PlayStation")
        queries.append(f"PS{gen}" if gen else "PS5")
        if gen and ("slim" in variants or "digital" in variants):
            suffix = " ".join(v.title() for v in ("slim", "digital") if v in variants)
            queries.insert(0, f"PlayStation {gen} {suffix}")
    else:
        core = [t for t in _tokens(source) if len(t) > 2][:5]
        if core:
            queries.append(" ".join(core))

    # More detailed queries are fallbacks, not prerequisites for users.
    brand = product.brand or ""
    if queries and brand:
        queries.append(f"{brand} {queries[0]}")
    if queries and cap:
        shown = f"{cap // 1024}TB" if cap >= 1024 and cap % 1024 == 0 else f"{cap}GB"
        queries.append(f"{queries[0]} {shown}")
    queries.append(product.name)

    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        q = " ".join(str(q).split()).strip()
        key = _norm(q)
        if len(q) >= 2 and key not in seen:
            seen.add(key)
            out.append(q)
    return out


def _best_magalu(product: Product) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    provider = MagaluProvider()
    provider.timeout = 9.0
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()

    # We deliberately collect several broad query result sets before choosing.
    # This avoids getting trapped by one exact SKU/title formulation.
    for query in _intent_queries(product):
        try:
            rows = provider.search(query, limit=12)
        except MagaluError as exc:
            errors.append(f"{query}: {exc}")
            continue
        for row in rows:
            key = str(row.get("external_product_id") or row.get("url") or row.get("name"))
            if not key or key in seen:
                continue
            seen.add(key)
            score, level, notes = _similarity(product, row)
            item = dict(row)
            item["_match_score"] = score
            item["_match_level"] = level
            item["_match_notes"] = notes
            item["_query"] = query
            candidates.append(item)
        # Once a strong equivalent exists there is no need to hammer every query.
        if any(int(x.get("_match_score") or 0) >= 78 for x in candidates):
            break

    purchasable = [
        x for x in candidates
        if x.get("price") and x.get("url") and int(x.get("_match_score") or 0) >= 48
    ]
    if not purchasable:
        return None, candidates, errors

    # Similarity dominates. Within a small confidence band, cheaper offer wins.
    purchasable.sort(key=lambda x: int(x.get("_match_score") or 0), reverse=True)
    best_score = int(purchasable[0].get("_match_score") or 0)
    band = [x for x in purchasable if int(x.get("_match_score") or 0) >= best_score - 5]
    band.sort(key=lambda x: float(x.get("pix_price") or x.get("price") or 10**18))
    return band[0], candidates, errors


def _save_link(db: Session, product: Product, candidate: dict[str, Any]) -> ProductSourceLink:
    link = db.scalar(select(ProductSourceLink).where(
        ProductSourceLink.product_id == product.id,
        ProductSourceLink.provider_slug == "magalu",
    ))
    external_id = str(candidate.get("external_product_id") or candidate.get("item_id") or "")
    url = str(candidate.get("url") or "")
    if not external_id or not url:
        raise ValueError("oferta Magalu sem id/link")
    if not link:
        link = ProductSourceLink(
            product_id=product.id,
            provider_slug="magalu",
            external_product_id=external_id,
            source_url=url,
        )
        db.add(link)
        db.flush()
    else:
        link.external_product_id = external_id
        link.source_url = url
    return link


_remove_route("/api/products/{product_id}/refresh", "POST")


@app.post("/api/products/{product_id}/refresh")
def v217_refresh(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")

    observations = 0
    sources_ok = 0
    messages: list[str] = []

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

    try:
        candidate, _all, errors = _best_magalu(product)
        if candidate:
            _save_link(db, product, candidate)
            clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
            level = str(candidate.get("_match_level") or "similar")
            score = int(candidate.get("_match_score") or 0)
            source_name = f"magalu_{level}_{score}"
            if record_magalu_detail(db, product, clean, source_name=source_name):
                observations += 1
            sources_ok += 1
        elif errors:
            messages.append("Magazine Luiza: " + errors[-1])
        else:
            messages.append("Magazine Luiza: não encontrei uma alternativa suficientemente parecida agora.")
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


@app.get("/api/products/{product_id}/similarity-diagnostics")
def similarity_diagnostics(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    best, candidates, errors = _best_magalu(product)
    return {
        "product": product.name,
        "queries": _intent_queries(product),
        "best": None if not best else {
            "name": best.get("name"), "price": best.get("price"), "url": best.get("url"),
            "score": best.get("_match_score"), "level": best.get("_match_level"),
            "notes": best.get("_match_notes"),
        },
        "errors": errors,
        "candidates": [
            {
                "name": x.get("name"), "price": x.get("price"), "url": x.get("url"),
                "score": x.get("_match_score"), "level": x.get("_match_level"),
                "notes": x.get("_match_notes"), "query": x.get("_query"),
            }
            for x in sorted(candidates, key=lambda r: int(r.get("_match_score") or 0), reverse=True)[:20]
        ],
    }


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v217_web_preview():
    base = v216_web_preview()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.replace("V2.16", "V2.17")

    helper = r'''
function matchQuality(x){
  const s=String(x?.source_name||'');
  const m=s.match(/^magalu_(muito_proximo|equivalente|similar)_(\d+)/);
  if(!m)return '';
  const label=m[1]==='muito_proximo'?'muito próximo':m[1]==='equivalente'?'equivalente':'similar';
  return `<span class="pill ${m[1]!=='similar'?'good':''}">${label} • ${m[2]}%</span>`;
}
'''
    text = text.replace("function storeOrSoon", helper + "\nfunction storeOrSoon", 1)
    text = text.replace(
        "<div class=\"muted tiny\">${esc(x.seller_name||'oferta atual')}</div>",
        "<div class=\"muted tiny\">${esc(x.seller_name||'oferta atual')}</div><div class=\"pills\">${matchQuality(x)}</div>",
    )
    text = text.replace(
        "<div class=\"muted tiny\">${esc(x.seller_name||'Oferta do marketplace')}</div>",
        "<div class=\"muted tiny\">${esc(x.seller_name||'Oferta do marketplace')}</div><div class=\"pills\">${matchQuality(x)}</div>",
    )
    text = text.replace(
        "Sem oferta compatível confirmada agora.<br>O PriceRadar não mistura modelos/variantes diferentes.",
        "Buscando a alternativa mais próxima nesta loja.<br>Quando não houver o mesmo SKU, usamos um produto equivalente ou muito similar e sinalizamos isso.",
    )
    return HTMLResponse(text)
