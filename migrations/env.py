from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import Column, MetaData, String, Table, inspect, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from database import DATABASE_URL
from models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    if connection.dialect.name == "postgresql":
        inspector = inspect(connection)
        if not inspector.has_table("alembic_version"):
            Table(
                "alembic_version",
                MetaData(),
                Column("version_num", String(128), primary_key=True),
            ).create(connection)
        else:
            version_column = next(
                (
                    column
                    for column in inspector.get_columns("alembic_version")
                    if column["name"] == "version_num"
                ),
                None,
            )
            version_length = (
                getattr(version_column["type"], "length", None)
                if version_column
                else None
            )
            if version_length is not None and version_length < 128:
                connection.exec_driver_sql(
                    "ALTER TABLE alembic_version "
                    "ALTER COLUMN version_num TYPE VARCHAR(128)"
                )
        connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_migrations_online())
