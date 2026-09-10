from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

API = "https://api.mercadolibre.com"
CRED_TARGET = "PriceRadar/MercadoLivre"


class MercadoLivreError(RuntimeError):
    pass


@dataclass
class MLCredentials:
    access_token: str
    refresh_token: str | None = None
    app_id: str | None = None
    client_secret: str | None = None


class _CredentialStore:
    """Armazena credenciais fora do navegador.

    No Windows usamos o Credential Manager. Em Linux/Render a V2.2 usa um
    arquivo privado local apenas para o protótipo. Esse arquivo é efêmero no
    plano gratuito do Render; migraremos os tokens para PostgreSQL antes de
    considerar a aplicação pronta para uso contínuo/multiusuário.
    """

    @staticmethod
    def _from_env() -> MLCredentials | None:
        token = os.getenv("MERCADOLIVRE_ACCESS_TOKEN")
        if not token:
            return None
        return MLCredentials(
            access_token=token,
            refresh_token=os.getenv("MERCADOLIVRE_REFRESH_TOKEN"),
            app_id=os.getenv("MERCADOLIVRE_APP_ID"),
            client_secret=os.getenv("MERCADOLIVRE_CLIENT_SECRET"),
        )

    @staticmethod
    def _file_path() -> Path:
        return Path(os.getenv("PRICERADAR_ML_CREDENTIAL_FILE", ".priceradar_ml_credentials.json"))

    @staticmethod
    def _read_file() -> MLCredentials | None:
        path = _CredentialStore._file_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return MLCredentials(**data)
        except Exception:
            return None

    @staticmethod
    def _write_file(cred: MLCredentials) -> None:
        path = _CredentialStore._file_path()
        path.write_text(json.dumps(cred.__dict__, separators=(",", ":")), encoding="utf-8")
        try:
            path.chmod(0o600)
        except Exception:
            pass

    @staticmethod
    def read() -> MLCredentials | None:
        env = _CredentialStore._from_env()
        if env:
            return env

        if os.name != "nt":
            return _CredentialStore._read_file()

        CRED_TYPE_GENERIC = 1

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi32 = ctypes.WinDLL("Advapi32.dll")
        cred_ptr = ctypes.POINTER(CREDENTIALW)()
        advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        ok = advapi32.CredReadW(CRED_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr))
        if not ok:
            return None
        try:
            cred = cred_ptr.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-8")
            data = json.loads(raw)
            return MLCredentials(**data)
        except Exception:
            return None
        finally:
            advapi32.CredFree(cred_ptr)

    @staticmethod
    def write(cred: MLCredentials) -> None:
        if os.name != "nt":
            _CredentialStore._write_file(cred)
            return

        CRED_TYPE_GENERIC = 1
        CRED_PERSIST_LOCAL_MACHINE = 2

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        payload = json.dumps(cred.__dict__, separators=(",", ":")).encode("utf-8")
        blob = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        c = CREDENTIALW()
        c.Flags = 0
        c.Type = CRED_TYPE_GENERIC
        c.TargetName = CRED_TARGET
        c.Comment = "Credenciais da API do Mercado Livre para o PriceRadar"
        c.CredentialBlobSize = len(payload)
        c.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        c.Persist = CRED_PERSIST_LOCAL_MACHINE
        c.AttributeCount = 0
        c.Attributes = None
        c.TargetAlias = None
        c.UserName = "PriceRadar"

        advapi32 = ctypes.WinDLL("Advapi32.dll")
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        if not advapi32.CredWriteW(ctypes.byref(c), 0):
            raise MercadoLivreError(f"Falha ao salvar no Gerenciador de Credenciais do Windows (erro {ctypes.get_last_error()}).")

    @staticmethod
    def delete() -> None:
        if os.name != "nt":
            path = _CredentialStore._file_path()
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        advapi32 = ctypes.WinDLL("Advapi32.dll")
        advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi32.CredDeleteW.restype = wintypes.BOOL
        advapi32.CredDeleteW(CRED_TARGET, 1, 0)


class MercadoLivreProvider:
    slug = "mercadolivre"
    name = "Mercado Livre"

    def __init__(self):
        self.timeout = 18.0

    @staticmethod
    def app_id() -> str | None:
        return os.getenv("MERCADOLIVRE_APP_ID")

    @staticmethod
    def client_secret() -> str | None:
        return os.getenv("MERCADOLIVRE_CLIENT_SECRET")

    def oauth_ready(self) -> bool:
        return bool(self.app_id() and self.client_secret())

    def _load_credentials(self) -> MLCredentials | None:
        return _CredentialStore.read()

    def save_credentials(self, access_token: str, refresh_token: str | None = None,
                         app_id: str | None = None, client_secret: str | None = None):
        _CredentialStore.write(MLCredentials(
            access_token=access_token.strip(),
            refresh_token=(refresh_token or "").strip() or None,
            app_id=(app_id or self.app_id() or "").strip() or None,
            client_secret=(client_secret or self.client_secret() or "").strip() or None,
        ))

    def clear_credentials(self):
        _CredentialStore.delete()

    def configured(self) -> bool:
        return self._load_credentials() is not None

    def exchange_authorization_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> MLCredentials:
        app_id = self.app_id()
        secret = self.client_secret()
        if not app_id or not secret:
            raise MercadoLivreError("APP ID ou Client Secret não configurados no Render.")
        data = {
            "grant_type": "authorization_code",
            "client_id": app_id,
            "client_secret": secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(
                    f"{API}/oauth/token",
                    data=data,
                    headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
                )
        except httpx.RequestError as exc:
            raise MercadoLivreError("Não consegui conectar ao OAuth do Mercado Livre.") from exc
        if r.status_code >= 400:
            try:
                body = r.json()
                detail = body.get("message") or body.get("error_description") or body.get("error") or str(body)
            except Exception:
                detail = r.text[:240]
            raise MercadoLivreError(f"Falha ao gerar token ({r.status_code}): {detail}")
        body = r.json()
        cred = MLCredentials(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            app_id=app_id,
            client_secret=secret,
        )
        _CredentialStore.write(cred)
        return cred

    def _refresh(self, cred: MLCredentials) -> MLCredentials:
        app_id = cred.app_id or self.app_id()
        secret = cred.client_secret or self.client_secret()
        if not (cred.refresh_token and app_id and secret):
            raise MercadoLivreError("O Access Token expirou e não há dados suficientes para renová-lo automaticamente.")
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(f"{API}/oauth/token", data={
                "grant_type": "refresh_token",
                "client_id": app_id,
                "client_secret": secret,
                "refresh_token": cred.refresh_token,
            }, headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"})
        if r.status_code >= 400:
            try:
                body = r.json()
                detail = body.get("message") or body.get("error_description") or body.get("error") or ""
            except Exception:
                detail = r.text[:200]
            raise MercadoLivreError(f"Falha ao renovar token do Mercado Livre ({r.status_code}): {detail}".strip())
        data = r.json()
        new = MLCredentials(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or cred.refresh_token,
            app_id=app_id,
            client_secret=secret,
        )
        _CredentialStore.write(new)
        return new

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        cred = self._load_credentials()
        if not cred:
            raise MercadoLivreError("Mercado Livre ainda não está conectado.")
        headers = {"Authorization": f"Bearer {cred.access_token}", "Accept": "application/json"}
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            try:
                r = client.request(method, f"{API}{path}", params=params, headers=headers)
            except httpx.RequestError as exc:
                raise MercadoLivreError("Não consegui conectar à API do Mercado Livre. Verifique sua internet.") from exc
            if r.status_code == 401:
                cred = self._refresh(cred)
                headers["Authorization"] = f"Bearer {cred.access_token}"
                r = client.request(method, f"{API}{path}", params=params, headers=headers)
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                detail = body.get("message") or body.get("error") or ""
            except Exception:
                detail = r.text[:160]
            raise MercadoLivreError(f"Mercado Livre respondeu {r.status_code}: {detail}".strip())
        return r.json()

    def test_connection(self) -> dict[str, Any]:
        data = self._request("GET", "/users/me")
        return {"id": data.get("id"), "nickname": data.get("nickname")}

    @staticmethod
    def _attr(attributes: list[dict[str, Any]] | None, *ids: str) -> str | None:
        wanted = set(ids)
        for a in attributes or []:
            if a.get("id") in wanted:
                value = a.get("value_name")
                if value not in (None, ""):
                    return str(value)
                values = a.get("values") or []
                if values and values[0].get("name"):
                    return str(values[0]["name"])
        return None

    @staticmethod
    def _image(data: dict[str, Any]) -> str | None:
        pics = data.get("pictures") or []
        if pics:
            p = pics[0]
            return p.get("secure_url") or p.get("url")
        return data.get("thumbnail")

    def product_detail(self, product_id: str) -> dict[str, Any]:
        d = self._request("GET", f"/products/{product_id}")
        attrs = d.get("attributes") or []
        winner = d.get("buy_box_winner") or {}
        item_id = winner.get("item_id")
        seller_id = winner.get("seller_id")
        seller_name = winner.get("seller", {}).get("nickname") if isinstance(winner.get("seller"), dict) else None
        if not seller_name and seller_id:
            seller_name = f"Vendedor #{seller_id}"
        return {
            "external_product_id": d.get("id") or product_id,
            "name": d.get("name") or d.get("family_name") or product_id,
            "brand": self._attr(attrs, "BRAND"),
            "model": self._attr(attrs, "MODEL"),
            "gtin": self._attr(attrs, "GTIN", "EAN", "UPC"),
            "image_url": self._image(d),
            "url": d.get("permalink"),
            "item_id": item_id,
            "price": winner.get("price"),
            "original_price": winner.get("original_price"),
            "currency": winner.get("currency_id") or "BRL",
            "seller_name": seller_name,
            "shipping_free": (winner.get("shipping") or {}).get("free_shipping"),
            "available": bool(winner) and (winner.get("available_quantity", 1) != 0),
        }

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        data = self._request("GET", "/products/search", params={
            "status": "active", "site_id": "MLB", "q": query,
        })
        raw = (data.get("results") or [])[:max(1, min(limit, 12))]
        results = []
        for item in raw:
            product_id = item.get("id")
            if not product_id:
                continue
            try:
                detail = self.product_detail(product_id)
            except MercadoLivreError:
                attrs = item.get("attributes") or []
                detail = {
                    "external_product_id": product_id,
                    "name": item.get("name") or product_id,
                    "brand": self._attr(attrs, "BRAND"),
                    "model": self._attr(attrs, "MODEL"),
                    "gtin": self._attr(attrs, "GTIN", "EAN", "UPC"),
                    "image_url": self._image(item),
                    "url": item.get("permalink"),
                    "item_id": None, "price": None, "original_price": None,
                    "currency": "BRL", "seller_name": None,
                    "shipping_free": None, "available": True,
                }
            results.append(detail)
        return results
