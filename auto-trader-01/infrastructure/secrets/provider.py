from abc import ABC, abstractmethod


class SecretsProvider(ABC):
    @abstractmethod
    async def get(self, key: str) -> str:
        """Return secret value for key. Raises KeyError if not found."""
        ...
