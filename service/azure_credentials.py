"""Cached, refreshable Azure AD credentials; never fall back to an API key."""

from functools import lru_cache

from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)

OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"


@lru_cache(maxsize=4)
def build_credential(client_id: str | None, managed_identity_only: bool):
    """Use only managed identity in Azure; permit `az login` for local development."""
    return (
        ManagedIdentityCredential(client_id=client_id)
        if managed_identity_only
        else DefaultAzureCredential(managed_identity_client_id=client_id)
    )


@lru_cache(maxsize=4)
def openai_token_provider(client_id: str | None, managed_identity_only: bool):
    credential = build_credential(client_id, managed_identity_only)
    return get_bearer_token_provider(credential, OPENAI_SCOPE)
