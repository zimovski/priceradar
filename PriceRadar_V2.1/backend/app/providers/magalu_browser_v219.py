from __future__ import annotations

from .magalu_web import MagaluError, MagaluProvider

_PATCHED = False


def enable_magalu_browser_v219() -> None:
    """Fetch Magalu storefront pages with a real browser TLS fingerprint first.

    Render/cloud datacenter requests are frequently treated differently from a
    normal browser. curl_cffi impersonates Chrome at the TLS/HTTP2 layer, which
    is substantially more reliable than merely changing the User-Agent header.
    The original httpx implementation remains as a fallback.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    original_get_html = MagaluProvider._get_html

    def _get_html(self: MagaluProvider, url: str) -> str:
        browser_error: Exception | None = None
        try:
            from curl_cffi import requests as curl_requests

            response = curl_requests.get(
                url,
                headers=self._request_headers(),
                timeout=max(5.0, float(self.timeout)),
                impersonate="chrome",
                allow_redirects=True,
            )
            text = response.text or ""
            if response.status_code < 400 and len(text) >= 500:
                return text
            browser_error = MagaluError(
                f"Magalu respondeu {response.status_code} ao navegador do coletor."
            )
        except Exception as exc:  # pragma: no cover - environment/network dependent
            browser_error = exc

        try:
            return original_get_html(self, url)
        except Exception as fallback_error:
            if browser_error:
                raise MagaluError(
                    f"Não consegui ler a vitrine do Magalu: {type(browser_error).__name__}; "
                    f"fallback: {fallback_error}"
                ) from fallback_error
            raise

    MagaluProvider._get_html = _get_html
