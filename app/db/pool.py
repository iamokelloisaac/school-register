import logging

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

logger = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    global _pool
    if _pool is not None:
        return

    _pool = AsyncConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await _pool.open()
    logger.info("db pool opened (min=%s max=%s)", settings.DB_POOL_MIN, settings.DB_POOL_MAX)


async def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("db pool closed")


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool is not open")
    return _pool