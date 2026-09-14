from __future__ import annotations

from fastapi.responses import HTMLResponse

from .main import app
from .v220_reliability import v220_web_preview


def _remove_route(path: str, method: str = "GET") -> None:
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()))
    ]


_remove_route("/", "GET")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def v220_truthful_ui():
    base = v220_web_preview()
    body = getattr(base, "body", b"")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    # Magalu is not OAuth-connected like Mercado Livre. We collect its public
    # storefront and rendered fallbacks, so the badge should not imply a formal
    # API connection when no offer has been obtained yet.
    text = text.replace("conector ativo", "consulta pública")
    text = text.replace(
        "Buscando a alternativa mais próxima nesta loja.",
        "Consultando ofertas públicas desta loja.",
    )
    return HTMLResponse(text)
