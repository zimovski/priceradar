from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from ..database import SessionLocal
from ..models import IntegrationCredential
from .mercadolivre import _CredentialStore, MLCredentials

_PATCHED = False
_PREFIX = "enc:v1:"


def _fernet() -> Fernet | None:
    secret = os.getenv("PRICERADAR_ENCRYPTION_SECRET") or os.getenv("MERCADOLIVRE_CLIENT_SECRET")
    if not secret:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    f = _fernet()
    if not f:
        return value
    return _PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        return value
    f = _fernet()
    if not f:
        return None
    try:
        return f.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def enable_database_credential_store() -> None:
    """Persiste e cifra tokens do Mercado Livre no banco do PriceRadar."""
    global _PATCHED
    if _PATCHED or os.name == "nt":
        return
    _PATCHED = True

    original_read = _CredentialStore.read
    original_write = _CredentialStore.write
    original_delete = _CredentialStore.delete

    def read() -> MLCredentials | None:
        try:
            db = SessionLocal()
            try:
                row = db.scalar(select(IntegrationCredential).where(IntegrationCredential.provider_slug == "mercadolivre"))
                if row:
                    access = _decrypt(row.access_token)
                    refresh = _decrypt(row.refresh_token)
                    if access:
                        return MLCredentials(
                            access_token=access,
                            refresh_token=refresh,
                            app_id=os.getenv("MERCADOLIVRE_APP_ID"),
                            client_secret=os.getenv("MERCADOLIVRE_CLIENT_SECRET"),
                        )
            finally:
                db.close()
        except Exception:
            pass
        return original_read()

    def write(cred: MLCredentials) -> None:
        try:
            db = SessionLocal()
            try:
                row = db.scalar(select(IntegrationCredential).where(IntegrationCredential.provider_slug == "mercadolivre"))
                encrypted_access = _encrypt(cred.access_token) or cred.access_token
                encrypted_refresh = _encrypt(cred.refresh_token)
                if not row:
                    row = IntegrationCredential(
                        provider_slug="mercadolivre",
                        access_token=encrypted_access,
                        refresh_token=encrypted_refresh,
                    )
                    db.add(row)
                else:
                    row.access_token = encrypted_access
                    row.refresh_token = encrypted_refresh
                    row.updated_at = datetime.utcnow()
                db.commit()
                return
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        except Exception:
            original_write(cred)

    def delete() -> None:
        try:
            db = SessionLocal()
            try:
                row = db.scalar(select(IntegrationCredential).where(IntegrationCredential.provider_slug == "mercadolivre"))
                if row:
                    db.delete(row)
                    db.commit()
            finally:
                db.close()
        except Exception:
            pass
        try:
            original_delete()
        except Exception:
            pass

    _CredentialStore.read = staticmethod(read)
    _CredentialStore.write = staticmethod(write)
    _CredentialStore.delete = staticmethod(delete)
