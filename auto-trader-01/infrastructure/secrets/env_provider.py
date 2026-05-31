import os

from .provider import SecretsProvider


class EnvSecretsProvider(SecretsProvider):
    """Reads secrets from environment variables / .env file.
    python-dotenv must be loaded before first call (done at app startup)."""

    async def get(self, key: str) -> str:
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"Secret '{key}' not found in environment")
        return value
