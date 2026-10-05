import os
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./mintai.db").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("sqlite:///"):
    DATABASE_URL = DATABASE_URL.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

_engine_options: dict[str, object] = {
    "echo": os.getenv("SQL_ECHO", "false").lower() == "true",
    "pool_pre_ping": True,
}
if DATABASE_URL.startswith("postgresql+"):
    _engine_options.update(
        pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
        pool_timeout=float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "30")),
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")),
    )
elif DATABASE_URL.startswith("sqlite+"):
    sqlite_busy_timeout = float(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30"))
    _engine_options["connect_args"] = {
        "timeout": sqlite_busy_timeout
    }
else:
    sqlite_busy_timeout = 30

engine = create_async_engine(DATABASE_URL, **_engine_options)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(sqlite_busy_timeout * 1000)}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
