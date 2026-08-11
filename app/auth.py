import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.exceptions import UnauthorizedError
from app.models import ApiKey, Tenant


KEY_PREFIX = "llmgw_"
DISPLAY_PREFIX_LENGTH = 14


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    api_key_id: str
    token_quota_limit: Optional[int] = None
    quota_version: int = 1


@dataclass(frozen=True)
class CreatedApiKey:
    id: str
    tenant_id: str
    name: str
    key_prefix: str
    plaintext: str


class ApiKeyService:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def create_tenant(
        self,
        name: str,
        token_quota_limit: Optional[int] = None,
    ) -> Tenant:
        if token_quota_limit is not None and token_quota_limit <= 0:
            raise ValueError("token_quota_limit must be greater than zero")
        tenant = Tenant(
            name=name.strip(),
            token_quota_limit=token_quota_limit,
        )
        async with self._sessions() as session:
            session.add(tenant)
            await session.commit()
            await session.refresh(tenant)
        return tenant

    async def create_api_key(
        self,
        tenant_id: str,
        name: str,
        expires_at: Optional[datetime] = None,
    ) -> CreatedApiKey:
        plaintext = generate_api_key()
        api_key = ApiKey(
            tenant_id=tenant_id,
            name=name.strip(),
            key_prefix=plaintext[:DISPLAY_PREFIX_LENGTH],
            key_hash=hash_api_key(plaintext),
            expires_at=expires_at,
        )
        async with self._sessions() as session:
            session.add(api_key)
            await session.commit()
            await session.refresh(api_key)
        return CreatedApiKey(
            id=api_key.id,
            tenant_id=api_key.tenant_id,
            name=api_key.name,
            key_prefix=api_key.key_prefix,
            plaintext=plaintext,
        )

    async def set_tenant_quota(self, tenant_id: str, token_quota_limit: int) -> Tenant:
        if token_quota_limit <= 0:
            raise ValueError("token_quota_limit must be greater than zero")
        async with self._sessions() as session:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                raise ValueError(f"Tenant {tenant_id} does not exist")
            tenant.token_quota_limit = token_quota_limit
            tenant.quota_version += 1
            await session.commit()
            await session.refresh(tenant)
            return tenant

    async def authenticate(self, raw_key: str) -> Principal:
        if not raw_key.startswith(KEY_PREFIX):
            raise UnauthorizedError()

        prefix = raw_key[:DISPLAY_PREFIX_LENGTH]
        expected_hash = hash_api_key(raw_key)
        now = datetime.now(timezone.utc)

        async with self._sessions() as session:
            result = await session.execute(
                select(ApiKey, Tenant)
                .join(Tenant, ApiKey.tenant_id == Tenant.id)
                .where(ApiKey.key_prefix == prefix)
            )
            for api_key, tenant in result.all():
                if not secrets.compare_digest(api_key.key_hash, expected_hash):
                    continue
                if api_key.status != "active" or tenant.status != "active":
                    raise UnauthorizedError()
                if api_key.expires_at is not None:
                    expires_at = api_key.expires_at
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    if expires_at <= now:
                        raise UnauthorizedError()
                api_key.last_used_at = now
                await session.commit()
                return Principal(
                    tenant_id=tenant.id,
                    api_key_id=api_key.id,
                    token_quota_limit=tenant.token_quota_limit,
                    quota_version=tenant.quota_version,
                )

        raise UnauthorizedError()
