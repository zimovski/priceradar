from __future__ import annotations

import re
from typing import Any

from .mercadolivre_search_enhancement import (
    _family,
    _generation,
    _looks_like_accessory,
    _norm,
    _query_wants_accessory,
)

_STOP = {
    "de", "da", "do", "das", "dos", "para", "com", "sem", "e", "em", "a", "o",
    "uma", "um", "por", "na", "no", "nas", "nos",
}

# Canonical consumer concepts. The user should be able to type normal Portuguese
# instead of knowing the exact title that each retailer uses.
_PHRASE_RULES: list[tuple[str, str]] = [
    (r"\bmaquinas?\s+de\s+lavar(?:\s+roupas?)?\b", "lavadora"),
    (r"\blava\s+roupas?\b", "lavadora"),
    (r"\blavadoras?(?:\s+de\s+roupas?)?\b", "lavadora"),
    (r"\bgeladeiras?\b", "refrigerador"),
    (r"\brefrigeradores?\b", "refrigerador"),
    (r"\bsmart\s+tv\b", "tv"),
    (r"\btelevis(?:ao|oes)\b", "tv"),
    (r"\bplacas?\s+de\s+video\b", "gpu"),
    (r"\bgpu\b", "gpu"),
    (r"\bsmartphones?\b", "smartphone"),
    (r"\bcelulares?\b", "smartphone"),
    (r"\bar\s+condicionado\b", "arcondicionado"),
    (r"\baspiradores?\s+de\s+po\b", "aspirador"),
    (r"\bmicro\s+ondas\b", "microondas"),
    (r"\bsecadoras?(?:\s+de\s+roupas?)?\b", "secadora"),
    (r"\blava\s+loucas?\b", "lavaloucas"),
]

_CATEGORY_WORDS = {
    "lavadora", "refrigerador", "tv", "gpu", "smartphone", "arcondicionado",
    "aspirador", "microondas", "secadora", "lavaloucas", "fogao", "freezer",
    "notebook", "monitor", "impressora", "cafeteira", "ventilador",
}

_UNIT_ALIASES = {
    "kg": "kg",
    "gb": "gb",
    "tb": "tb",
    "hz": "hz",
    "btu": "btu",
    "btus": "btu",
    "l": "l",
    "litro": "l",
    "litros": "l",
    "v": "v",
    "w": "w",
    "pol": "pol",
    "polegada": "pol",
    "polegadas": "pol",
    "mah": "mah",
}

_SPEC_RE = re.compile(
    r"\b(\d{1,5})\s*(kg|gb|tb|hz|btus?|litros?|l|v|w|pol|polegadas?|mah)\b",
    flags=re.I,
)


def canonical_text(text: str | None) -> str:
    value = _norm(text)
    for pattern, replacement in _PHRASE_RULES:
        value = re.sub(pattern, replacement, value)
    return " ".join(value.split())


def specs(text: str | None) -> dict[str, int]:
    value = canonical_text(text)
    found: dict[str, int] = {}
    for amount, raw_unit in _SPEC_RE.findall(value):
        unit = _UNIT_ALIASES.get(raw_unit.lower(), raw_unit.lower())
        number = int(amount)
        # Keep TB comparable with GB when necessary.
        if unit == "tb":
            found["storage_mb"] = number * 1024
        elif unit == "gb":
            found["storage_mb"] = number
        else:
            found[unit] = number
    return found


def semantic_tokens(text: str | None) -> list[str]:
    value = canonical_text(text)
    value = _SPEC_RE.sub(" ", value)
    return [token for token in value.split() if len(token) > 1 and token not in _STOP]


def category(text: str | None) -> str | None:
    words = set(canonical_text(text).split())
    for item in _CATEGORY_WORDS:
        if item in words:
            return item
    fam = _family(text or "")
    return fam


def _variant_tokens(text: str | None) -> set[str]:
    n = canonical_text(text)
    found: set[str] = set()
    for value in ("ti", "super", "pro max", "pro", "plus", "ultra", "air", "slim", "digital"):
        if re.search(rf"\b{re.escape(value)}\b", n):
            found.add(value)
    return found


def query_variants(query: str) -> list[str]:
    """Generate a few retailer-friendly queries from ordinary shopper language."""
    original = " ".join((query or "").split()).strip()
    canonical = canonical_text(original)
    q_specs = specs(original)

    def spec_suffix() -> str:
        parts: list[str] = []
        if "kg" in q_specs:
            parts.append(f"{q_specs['kg']}kg")
        if "storage_mb" in q_specs:
            amount = q_specs["storage_mb"]
            parts.append(f"{amount}GB")
        if "hz" in q_specs:
            parts.append(f"{q_specs['hz']}Hz")
        if "btu" in q_specs:
            parts.append(f"{q_specs['btu']} BTU")
        if "l" in q_specs:
            parts.append(f"{q_specs['l']}L")
        if "v" in q_specs:
            parts.append(f"{q_specs['v']}V")
        return " ".join(parts)

    suffix = spec_suffix()
    candidates: list[str] = [original]
    cat = category(original)
    fam = _family(original)
    gen = _generation(original, fam) if fam else None
    variants = _variant_tokens(original)

    if cat == "lavadora":
        candidates.extend([f"lavadora {suffix}".strip(), f"lavadora de roupas {suffix}".strip()])
    elif cat == "refrigerador":
        candidates.extend([f"refrigerador {suffix}".strip(), f"geladeira {suffix}".strip()])
    elif cat == "tv":
        candidates.extend([f"smart tv {suffix}".strip(), f"televisao {suffix}".strip()])
    elif cat == "arcondicionado":
        candidates.extend([f"ar condicionado {suffix}".strip(), f"split {suffix}".strip()])
    elif cat == "smartphone" and not fam:
        candidates.extend([f"smartphone {suffix}".strip(), f"celular {suffix}".strip()])
    elif fam == "rtx":
        extra = " Ti" if "ti" in variants else " Super" if "super" in variants else ""
        candidates.extend([f"RTX {gen or ''}{extra}".strip(), f"placa de video RTX {gen or ''}{extra}".strip()])
    elif fam == "radeon":
        candidates.extend([f"Radeon RX {gen or ''}".strip(), f"placa de video Radeon RX {gen or ''}".strip()])
    elif fam == "iphone":
        special = " Pro Max" if "pro max" in variants else " Pro" if "pro" in variants else " Plus" if "plus" in variants else ""
        candidates.append(f"iPhone {gen or ''}{special}".strip())
    elif fam == "playstation":
        suffix2 = " ".join(v.title() for v in ("slim", "digital") if v in variants)
        candidates.extend([f"PlayStation {gen or ''} {suffix2}".strip(), f"PS{gen or 5} {suffix2}".strip()])
    elif canonical != _norm(original):
        candidates.append(canonical)

    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        item = " ".join(item.split()).strip()
        key = canonical_text(item)
        if len(item) >= 2 and key not in seen:
            seen.add(key)
            out.append(item)
    return out[:4]


def hard_constraints_match(query: str, title: str | None) -> bool:
    if not title:
        return False
    if not _query_wants_accessory(query) and _looks_like_accessory(title):
        return False

    fam = _family(query)
    if fam:
        wanted_gen = _generation(query, fam)
        got_gen = _generation(title, fam)
        if wanted_gen is not None and got_gen is not None and wanted_gen != got_gen:
            return False
        if wanted_gen is not None and got_gen is None:
            return False

        q_variants = _variant_tokens(query)
        t_variants = _variant_tokens(title)
        if fam in {"rtx", "radeon"}:
            for variant in ("ti", "super"):
                if (variant in q_variants) != (variant in t_variants):
                    return False
        elif "pro max" in q_variants and "pro max" not in t_variants:
            return False

    # When the shopper explicitly types a measurable specification, respect it
    # even when retailers format it differently (17kg vs 17 kg, 512GB vs 512 GB).
    wanted_specs = specs(query)
    got_specs = specs(title)
    for unit, amount in wanted_specs.items():
        if unit not in got_specs or got_specs[unit] != amount:
            return False
    return True


def relevance_score(query: str, title: str | None) -> float:
    if not title:
        return -999.0
    if not hard_constraints_match(query, title):
        return -500.0

    q_tokens = semantic_tokens(query)
    t_tokens = semantic_tokens(title)
    if not q_tokens:
        return 0.0
    t_set = set(t_tokens)
    present = sum(1 for token in q_tokens if token in t_set)
    coverage = present / max(1, len(q_tokens))
    score = coverage * 55 + present * 4

    q_cat = category(query)
    t_cat = category(title)
    if q_cat and t_cat == q_cat:
        score += 24
    elif q_cat and t_cat and q_cat != t_cat:
        score -= 40

    fam = _family(query)
    if fam and _family(title) == fam:
        score += 18
        wanted_gen = _generation(query, fam)
        got_gen = _generation(title, fam)
        if wanted_gen is not None and got_gen == wanted_gen:
            score += 20

    qn = canonical_text(query)
    tn = canonical_text(title)
    if qn and qn in tn:
        score += 10
    return score


def acceptable(query: str, title: str | None) -> bool:
    if not hard_constraints_match(query, title):
        return False
    q_tokens = semantic_tokens(query)
    t_tokens = set(semantic_tokens(title))
    if not q_tokens:
        return True
    coverage = sum(1 for token in q_tokens if token in t_tokens) / len(q_tokens)
    q_cat = category(query)
    # Known categories can tolerate retailer wording differences because the
    # category itself plus explicit specs already constrain the result.
    threshold = 0.34 if q_cat else 0.50
    return coverage >= threshold


def broad_product_query(text: str | None) -> str:
    """Build a cross-store query from purchase intent, not from an exact SKU."""
    value = text or ""
    fam = _family(value)
    gen = _generation(value, fam) if fam else None
    variants = _variant_tokens(value)
    q_specs = specs(value)
    cat = category(value)

    if fam == "rtx":
        suffix = " Ti" if "ti" in variants else " Super" if "super" in variants else ""
        return f"RTX {gen or ''}{suffix}".strip()
    if fam == "radeon":
        return f"Radeon RX {gen or ''}".strip()
    if fam == "iphone":
        suffix = " Pro Max" if "pro max" in variants else " Pro" if "pro" in variants else " Plus" if "plus" in variants else ""
        return f"iPhone {gen or ''}{suffix}".strip()
    if fam == "playstation":
        suffix = " ".join(v.title() for v in ("slim", "digital") if v in variants)
        return f"PlayStation {gen or ''} {suffix}".strip()
    if cat == "lavadora":
        return f"lavadora {q_specs.get('kg', '')}kg".replace(" kg", "").strip() if q_specs.get("kg") else "lavadora"
    if cat == "refrigerador":
        return f"refrigerador {q_specs.get('l', '')}L".strip() if q_specs.get("l") else "refrigerador"
    if cat == "tv":
        return "smart tv"
    if cat == "arcondicionado":
        return f"ar condicionado {q_specs.get('btu', '')} BTU".strip() if q_specs.get("btu") else "ar condicionado"

    tokens = semantic_tokens(value)
    return " ".join(tokens[:4]) or " ".join((value or "").split())


def consumer_similarity(source: str, target: str | None) -> tuple[int, str, list[str]]:
    """Similarity for cross-store alternatives; exact SKU is not required."""
    if not target or _looks_like_accessory(target):
        return 0, "incompatível", ["acessório ou título inválido"]

    notes: list[str] = []
    source_cat = category(source)
    target_cat = category(target)
    if source_cat and target_cat and source_cat != target_cat:
        return 0, "incompatível", ["tipo de produto diferente"]

    source_fam = _family(source)
    target_fam = _family(target)
    if source_fam and target_fam and source_fam != target_fam:
        return 0, "incompatível", ["família diferente"]

    score = 28
    if source_cat and target_cat == source_cat:
        score += 24
        notes.append("mesmo tipo de produto")
    if source_fam and target_fam == source_fam:
        score += 18
        notes.append("mesma família")

    if source_fam:
        a = _generation(source, source_fam)
        b = _generation(target, source_fam)
        if a is not None and b is not None and a != b:
            return 0, "incompatível", ["geração/modelo principal diferente"]
        if a is not None and b == a:
            score += 22
            notes.append("mesma geração/modelo")

    source_specs = specs(source)
    target_specs = specs(target)
    for unit, amount in source_specs.items():
        if unit not in target_specs:
            continue
        other = target_specs[unit]
        if other == amount:
            score += 10
            notes.append(f"mesma especificação {amount}{unit if unit != 'storage_mb' else 'GB'}")
        else:
            ratio = max(amount, other) / max(1, min(amount, other))
            score -= 4 if ratio <= 1.2 else 8
            notes.append("especificação próxima")

    a_tokens = set(semantic_tokens(source))
    b_tokens = set(semantic_tokens(target))
    generic = _CATEGORY_WORDS | {"rtx", "radeon", "iphone", "playstation", "geforce", "nvidia", "amd"}
    a_meaningful = {x for x in a_tokens if x not in generic}
    if a_meaningful:
        overlap = len(a_meaningful & b_tokens) / len(a_meaningful)
        score += round(overlap * 12)
        if overlap >= 0.4:
            notes.append("descrição semelhante")

    score = max(0, min(100, int(score)))
    if score >= 86:
        level = "muito_proximo"
    elif score >= 68:
        level = "equivalente"
    elif score >= 42:
        level = "similar"
    else:
        level = "fraco"
    return score, level, notes
