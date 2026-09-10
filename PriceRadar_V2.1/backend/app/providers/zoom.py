from .base import HistoryProvider, ExternalHistoryRecord

class ZoomHistoryProvider(HistoryProvider):
    name = "zoom"

    async def fetch_history(self, product_key: str) -> list[ExternalHistoryRecord]:
        # Intencionalmente vazio na V1; mesma política do adaptador Buscapé.
        return []
