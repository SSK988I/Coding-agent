"""agent_llm 的 auth 子系统。

模块映射:
  - types.py      (ProviderAuth、ApiKeyAuth、OAuthAuth、CredentialStore、
                   AuthContext、AuthResult、Credential、ModelsError)
  - helpers.py    (env_api_key_auth、lazy_oauth)
  - context.py    (default_auth_context)
  - credential_store.py (InMemoryCredentialStore)
  - resolve.py    (resolve_provider_auth、ModelsError 的 code)
"""
from agent_llm.auth.context import default_auth_context
from agent_llm.auth.credential_store import InMemoryCredentialStore
from agent_llm.auth.helpers import env_api_key_auth
from agent_llm.auth.resolve import resolve_provider_auth
from agent_llm.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthLoginCallbacks,
    AuthPrompt,
    AuthResult,
    Credential,
    CredentialModifier,
    CredentialStore,
    ModelAuth,
    ModelsError,
    OAuthAuth,
    OAuthCredential,
    ProviderAuth,
)

__all__ = [
    "ProviderAuth",
    "ApiKeyAuth",
    "OAuthAuth",
    "CredentialStore",
    "CredentialModifier",
    "Credential",
    "ApiKeyCredential",
    "OAuthCredential",
    "AuthContext",
    "AuthResult",
    "ModelAuth",
    "AuthPrompt",
    "AuthLoginCallbacks",
    "env_api_key_auth",
    "default_auth_context",
    "InMemoryCredentialStore",
    "resolve_provider_auth",
    "ModelsError",
]
