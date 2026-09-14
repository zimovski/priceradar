from __future__ import annotations

import base64
import hashlib
import html
import os
import secrets
import time
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .main import app
from .providers.mercadolivre import MercadoLivreProvider, MercadoLivreError
from .providers.mercadolivre_db_store import enable_database_credential_store
from .providers.mercadolivre_price_enrichment import enable_price_enrichment
from .providers.mercadolivre_search_enhancement import enable_search_enhancement
from .providers.mercadolivre_verified_fallback import enable_verified_listing_fallback
from .providers.mercadolivre_fast_search import enable_fast_search
from .providers.mercadolivre_stable_search import enable_stable_search
from .providers.mercadolivre_reliable_v215 import enable_reliable_v215
from .providers.mercadolivre_search_v215_light import enable_light_search_v215
from .providers.mercadolivre_search_v218_intent import enable_intent_search_v218

enable_database_credential_store()
enable_price_enrichment()
enable_verified_listing_fallback()
enable_search_enhancement()
enable_fast_search()
enable_stable_search()
enable_reliable_v215()
enable_light_search_v215()
# Loaded last: V2.18 understands natural shopper queries and flexible formatting
# such as "máquina de lavar 17kg" versus retailer titles using "17 kg".
enable_intent_search_v218()

from . import v24_features  # noqa: E402,F401
from . import v28_features  # noqa: E402,F401
from . import v210_multistore  # noqa: E402,F401
from . import v211_features  # noqa: E402,F401
from . import v212_resilience  # noqa: E402,F401
from . import v215_ui  # noqa: E402,F401
from . import v216_magalu  # noqa: E402,F401
from . import v217_similarity  # noqa: E402,F401
from . import v218_intent  # noqa: E402,F401

AUTH_URL = "https://auth.mercadolivre.com.br/authorization"
OAUTH_STATES: dict[str, tuple[float, str | None]] = {}
STATE_TTL_SECONDS = 600


def _pkce_enabled() -> bool:
    return os.getenv("MERCADOLIVRE_PKCE", "1").lower() not in {"0", "false", "no"}


def _cleanup_states() -> None:
    now = time.time()
    expired = [key for key, (created, _) in OAUTH_STATES.items() if now - created > STATE_TTL_SECONDS]
    for key in expired:
        OAUTH_STATES.pop(key, None)


def _redirect_uri(request: Request) -> str:
    explicit = os.getenv("MERCADOLIVRE_REDIRECT_URI")
    if explicit:
        return explicit.strip()
    return str(request.url_for("mercadolivre_oauth_callback"))


@app.get("/mercadolivre/connect", include_in_schema=False)
def mercadolivre_connect(request: Request):
    ml = MercadoLivreProvider()
    app_id = ml.app_id()
    if not app_id or not ml.client_secret():
        return HTMLResponse("<h2>PriceRadar</h2><p>Configure MERCADOLIVRE_APP_ID e MERCADOLIVRE_CLIENT_SECRET no Render primeiro.</p>", status_code=500)

    _cleanup_states()
    state = secrets.token_urlsafe(32)
    verifier: str | None = None
    params = {"response_type": "code", "client_id": app_id, "redirect_uri": _redirect_uri(request), "state": state}
    if _pkce_enabled():
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    OAUTH_STATES[state] = (time.time(), verifier)
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}", status_code=302)


@app.get("/mercadolivre/oauth/callback", response_class=HTMLResponse, include_in_schema=False, name="mercadolivre_oauth_callback")
def mercadolivre_oauth_callback(request: Request, code: str | None = None, error: str | None = None, error_description: str | None = None, state: str | None = None):
    if error:
        return HTMLResponse(f"<h1>PriceRadar</h1><h2>Autorização não concluída</h2><p>{html.escape(error_description or error)}</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h1>PriceRadar</h1><h2>Retorno OAuth inválido</h2><p>Faltou o código ou o state.</p>", status_code=400)

    _cleanup_states()
    pending = OAUTH_STATES.pop(state, None)
    if not pending:
        return HTMLResponse("<h1>PriceRadar</h1><h2>Sessão expirada</h2><p>Inicie novamente a conexão com o Mercado Livre.</p>", status_code=400)

    _, verifier = pending
    ml = MercadoLivreProvider()
    try:
        ml.exchange_authorization_code(code=code, redirect_uri=_redirect_uri(request), code_verifier=verifier)
        account = ml.test_connection()
    except MercadoLivreError as exc:
        return HTMLResponse(f"<h1>PriceRadar</h1><h2>Não foi possível concluir a conexão</h2><p>{html.escape(str(exc))}</p>", status_code=400)

    nickname = html.escape(account.get("nickname") or "conta autorizada")
    return HTMLResponse(f"""
    <!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PriceRadar • Mercado Livre conectado</title></head>
    <body style="font-family:system-ui;background:#100f17;color:#f7f4f9;margin:0"><main style="max-width:680px;margin:70px auto;background:#1b1823;padding:32px;border-radius:20px;border:1px solid #3c3549;box-shadow:0 18px 45px #0008"><div style="font-weight:900;font-size:26px">PriceRadar</div><h2 style="color:#39e7a0;margin-top:24px">Mercado Livre conectado ✓</h2><p>A conta <strong>{nickname}</strong> foi autorizada com sucesso.</p><p>O Access Token e o Refresh Token ficam no banco persistente. Nas próximas visitas o PriceRadar renova a sessão automaticamente quando necessário.</p><p><a href="/" style="display:inline-block;margin-top:8px;background:#39e7a0;color:#07150f;text-decoration:none;font-weight:800;padding:11px 16px;border-radius:11px">Voltar ao PriceRadar</a></p></main></body></html>
    """)


@app.get("/api/integrations/mercadolivre/oauth-status")
def mercadolivre_oauth_status():
    ml = MercadoLivreProvider()
    if not ml.configured():
        return {"configured": False, "oauth_ready": ml.oauth_ready()}
    try:
        account = ml.test_connection()
        return {"configured": True, "oauth_ready": ml.oauth_ready(), "ok": True, "account": account, "automatic_refresh": True}
    except MercadoLivreError as exc:
        return {"configured": True, "oauth_ready": ml.oauth_ready(), "ok": False, "automatic_refresh": True, "message": str(exc)}
