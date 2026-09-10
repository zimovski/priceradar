from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

@dataclass
class ExternalHistoryRecord:
    retailer_slug: str
    retailer_name: str
    price: float
    captured_at: datetime
    source_name: str

class HistoryProvider(ABC):
    name: str

    @abstractmethod
    async def fetch_history(self, product_key: str) -> list[ExternalHistoryRecord]:
        """Retorna histórico apenas quando houver uma integração permitida/estável."""
        raise NotImplementedError
