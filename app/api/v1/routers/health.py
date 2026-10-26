from fastapi import APIRouter, Depends
from psycopg import AsyncConnection

from app.core.deps import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/db")
async def health_db(db: AsyncConnection = Depends(get_db)) -> dict[str, str]:
    async with db.cursor() as cur:
        await cur.execute("SELECT 1 AS one")
        row = await cur.fetchone()
    return {"status": "ok", "db": "ok" if row and row["one"] == 1 else "error"}