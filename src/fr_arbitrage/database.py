"""Async SQLite database layer via SQLAlchemy + aiosqlite.

Provides session management and CRUD helpers for Position persistence.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fr_arbitrage.models import Base, Position

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Engine & session factory (module-level singletons)
# ---------------------------------------------------------------------------

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


async def init_db(db_url: str) -> None:
    """Create engine, session factory, and ensure tables exist."""
    global _engine, _session_factory

    _engine = create_async_engine(db_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("database_initialized", url=db_url)


async def close_db() -> None:
    """Dispose engine connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_closed")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session with automatic commit / rollback."""
    assert _session_factory is not None, "Call init_db() first"
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

async def upsert_position(pos: Position) -> None:
    """Insert or update a position row (keyed by symbol)."""
    async with get_session() as session:
        await session.merge(pos)
    logger.debug("position_upserted", symbol=pos.symbol, state=pos.state)


async def get_open_positions() -> Sequence[Position]:
    """Return all positions with state != CLOSED."""
    async with get_session() as session:
        result = await session.execute(
            select(Position).where(Position.state != "CLOSED")
        )
        return result.scalars().all()


async def get_position(symbol: str) -> Optional[Position]:
    """Fetch a single position by symbol."""
    async with get_session() as session:
        return await session.get(Position, symbol)


async def update_position_state(symbol: str, new_state: str) -> None:
    """Update the state column for a position."""
    async with get_session() as session:
        pos = await session.get(Position, symbol)
        if pos is not None:
            pos.state = new_state
    logger.info("position_state_updated", symbol=symbol, state=new_state)
