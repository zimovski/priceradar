from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import select

from ..database import SessionLocal
from ..models import IntegrationCredential
from .mercadolivre import _CredentialStore, MLCredentials

_PATCHED = False


def enable_database_credential_store() -> None:
    """Persiste tokens do Mercado Livre no banco do PriceRadar em servidores Linux.

    No Windows mantemos o Credential Manager nativo. No Render/Linux, o banco
    passa a ser a fonte principal; o armazenamento em arquivo da versão anterior
    fica apenas como fallback de desenvolvimento.
    """
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
                row = db.scalar(
                    select(IntegrationCredential).where(
                        IntegrationCredential.provider_slug == "mercadolivre"
                    )
                )
                if row:
                    return MLCredentials(
                        access_token=row.access_token,
                        refresh_token=row.refresh_token,
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
                row = db.scalar(
                    select(IntegrationCredential).where(
                        IntegrationCredential.provider_slug == "mercadolivre"
                    )
                )
                if not row:
                    row = IntegrationCredential(
                        provider_slug="mercadolivre",
                        access_token=cred.access_token,
                        refresh_token=cred.refresh_token,
                    )
                    db.add(row)
                else:
                    row.access_token = cred.access_token
                    row.refresh_token = cred.refresh_token
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
                row = db.scalar(
                    select(IntegrationCredential).where(
                        IntegrationCredential.provider_slug == "mercadolivre"
                    )
                )
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
