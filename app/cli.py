import argparse
import asyncio
from typing import Optional

from sqlalchemy import select

from app.auth import ApiKeyService
from app.config import get_settings
from app.database import Database
from app.models import Tenant


async def create_tenant(
    database: Database,
    name: str,
    token_quota_limit: Optional[int] = None,
) -> None:
    service = ApiKeyService(database.sessions)
    tenant = await service.create_tenant(name, token_quota_limit)
    print(f"tenant_id={tenant.id}")


async def create_key(database: Database, tenant_id: str, name: str) -> None:
    service = ApiKeyService(database.sessions)
    created = await service.create_api_key(tenant_id, name)
    print(f"api_key_id={created.id}")
    print(f"api_key={created.plaintext}")
    print("Store this key now. It cannot be recovered from the database.")


async def set_tenant_quota(database: Database, tenant_id: str, tokens: int) -> None:
    service = ApiKeyService(database.sessions)
    tenant = await service.set_tenant_quota(tenant_id, tokens)
    print(f"tenant_id={tenant.id}")
    print(f"token_quota_limit={tenant.token_quota_limit}")
    print(f"quota_version={tenant.quota_version}")


async def list_tenants(database: Database) -> None:
    async with database.sessions() as session:
        result = await session.execute(select(Tenant).order_by(Tenant.created_at))
        for tenant in result.scalars():
            print(f"{tenant.id}\t{tenant.status}\t{tenant.name}")


async def run(args: argparse.Namespace) -> None:
    database = Database(get_settings().database_url)
    try:
        if args.command == "create-tenant":
            await create_tenant(database, args.name, args.token_quota_limit)
        elif args.command == "create-key":
            await create_key(database, args.tenant_id, args.name)
        elif args.command == "set-tenant-quota":
            await set_tenant_quota(database, args.tenant_id, args.tokens)
        elif args.command == "list-tenants":
            await list_tenants(database)
    finally:
        await database.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-gateway")
    commands = parser.add_subparsers(dest="command", required=True)

    tenant_parser = commands.add_parser("create-tenant", help="Create a tenant")
    tenant_parser.add_argument("--name", required=True)
    tenant_parser.add_argument("--token-quota-limit", type=int)

    key_parser = commands.add_parser("create-key", help="Create a tenant API key")
    key_parser.add_argument("--tenant-id", required=True)
    key_parser.add_argument("--name", required=True)

    quota_parser = commands.add_parser(
        "set-tenant-quota",
        help="Set a tenant monthly token quota",
    )
    quota_parser.add_argument("--tenant-id", required=True)
    quota_parser.add_argument("--tokens", required=True, type=int)

    commands.add_parser("list-tenants", help="List tenants")
    return parser


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
