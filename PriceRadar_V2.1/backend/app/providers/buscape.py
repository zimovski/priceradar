from .base import HistoryProvider, ExternalHistoryRecord

class BuscapeHistoryProvider(HistoryProvider):
    name = "buscape"

    async def fetch_history(self, product_key: str) -> list[ExternalHistoryRecord]:
        # Intencionalmente vazio na V1.
        # O Buscapé exibe histórico ao usuário, mas esta implementação só deve
        # ser ativada quando houver uma forma autorizada/estável de obter os dados.
        return []
