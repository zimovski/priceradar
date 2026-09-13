from __future__ import annotations

from fastapi.responses import HTMLResponse

from .main import app
from .v212_resilience import v212_web_preview


def _remove_route(path: str, method: str = "GET") -> None:
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()))
    ]


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v215_web_preview():
    base = v212_web_preview()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.replace("V2.14", "V2.15")

    # Magalu is already a live connector. When a tracked product has no matching
    # Magalu offer we should say so, not call the marketplace "em breve".
    text = text.replace(
        "if(!x)return soon(name);",
        "if(!x)return `<div class=\"store\"><div><div class=\"store-head\"><span class=\"store-name\">${esc(name)}</span><span class=\"tag\">conector ativo</span></div><div class=\"lock\">◇</div><div class=\"muted tiny\">Sem oferta compatível confirmada agora.<br>O PriceRadar não mistura modelos/variantes diferentes.</div></div></div>`;",
    )
    text = text.replace(
        "Mercado Livre e Magazine Luiza já participam da comparação. KaBuM e Casas Bahia entram nas próximas etapas.",
        "Mercado Livre e Magazine Luiza já estão conectados. KaBuM e Casas Bahia entram depois que estabilizarmos estas duas fontes.",
    )
    return HTMLResponse(text)
