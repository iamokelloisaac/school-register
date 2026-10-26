"""
app/main.py
===========

Application entry point.

Creates the FastAPI app, wires up lifecycle, exception handlers,
middleware, and routers. Nothing else — no business logic, no route
definitions, no direct DB access.

## Startup order

The order of operations in `lifespan` matters:

  1. Configure logging. Must happen first — everything after it might
     want to log, and we want those logs in our format, not Python's
     default.
  2. Open the connection pool. Any later step might need the DB, and
     an open pool means the first request doesn't pay the cost.
  3. Yield — the app runs.

On shutdown, the reverse: close the pool, then nothing else. Logging
doesn't need explicit teardown; the process exiting handles it.

## Middleware order

`add_middleware` in FastAPI/Starlette wraps the app in the order calls
are made — the *first* added is the outermost layer. With only one
middleware this doesn't matter, but when we add more (CORS, for
instance), the ordering becomes load-bearing. Rule of thumb:

  - Request ID / logging go outermost — they should see every request
    and every response, including ones that later middleware rejects.
  - CORS goes outside of everything except logging.
  - Domain-specific middleware goes innermost.

## Why `lifespan` and not `@app.on_event("startup")`

FastAPI's older `on_event` decorators still work but are deprecated in
favor of the `lifespan` context manager. The context manager version is
cleaner: startup and shutdown live in one function, and teardown runs
even if startup raises partway through (which is not true of
`on_event`). Use the modern form.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.pool import close_pool, open_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan: startup and shutdown.

    The `try/finally` guarantees the pool is closed even if the app
    crashes mid-request or the process receives a signal. Without the
    `finally`, an unhandled exception during the `yield` would skip
    cleanup and leave connections dangling. Not a big deal on process
    exit, but it matters in test suites and in graceful-shutdown
    scenarios.
    """
    configure_logging()
    await open_pool()
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(
    title="School Register",
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware must be added before the app starts handling requests.
# Order matters — see module docstring.
app.add_middleware(RequestContextMiddleware)

# Exception handlers are registered on the app instance directly; they
# don't go through the middleware stack, but they do run *inside* it
# (the middleware's try/except sees the exception first, then re-raises
# and the handler produces the response).
register_exception_handlers(app)


@app.get("/")
async def root() -> dict[str, str]:
    """
    Trivial root endpoint. Useful for "is this thing on?" checks and for
    confirming which environment you're pointed at.
    """
    return {"app": "school-register", "env": settings.ENV}


app.include_router(api_router, prefix="/api/v1")