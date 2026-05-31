from .provider import SecretsProvider


class AwsSecretsProvider(SecretsProvider):
    """Reads secrets from AWS Secrets Manager. Used in Phase 3+ EC2 deployment."""

    def __init__(self, region: str = "us-east-1") -> None:
        self._region = region
        self._cache: dict[str, str] = {}

    async def get(self, key: str) -> str:
        if key in self._cache:
            return self._cache[key]
        import boto3  # noqa: PLC0415 — optional dependency, only needed in Phase 3+
        client = boto3.client("secretsmanager", region_name=self._region)
        response = client.get_secret_value(SecretId=key)
        value = response["SecretString"]
        self._cache[key] = value
        return value
