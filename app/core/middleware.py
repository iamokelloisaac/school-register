"""
app/core/middleware.py
======================

Request-scoped middleware: request ID generation and per-request logging.

Middleware sits between the ASGI server and the route handler. Every
request passes through it (in declaration order), every response passes
back through it (in reverse). It's the right place for concerns that:

  - apply to *every* request, not one route
  - operate on the HTTP envelope, not the domain
  - need to run before routing has happened

Our two concerns fit all three:

  1. **Request ID** — generate a UUID per request, stash it in a context
     variable, echo it back in a response header. Every log line emitted
     during the request picks it up automatically.

  2. **Access logging** — one line per request with method, path, status,
     duration. This is the "who called what, when, and how did it go"
     trail that every production service has.

## Middleware vs dependency — the line we drew earlier

Recall the rule: middleware is for HTTP-envelope concerns; dependencies
are for domain-aware per-route concerns. The two things here are
envelope concerns:

  - The request ID is not domain data. It's a correlation token for
    logs. The route doesn't need to know it exists.
  - The access log line happens *after* the route returns and covers
    *all* routes uniformly. A dependency would have to be declared on
    every route, or on the router, and would need to wrap the handler
    to time it — which is what middleware is for.

Auth, by contrast, is a dependency. It needs the DB, it needs to be
optional per route, and it needs to *reject* requests, which middleware
shouldn't do (auth failures deserve the domain error shape, and only
dependencies have access to the exception machinery cleanly).

## On the request ID being set before logging

The order of operations inside `__call__` is deliberate:

  1. Set the context var.
  2. Call the next app (runs the route).
  3. In the `finally`, log the access line.

This means every log record emitted by anything downstream — the route,
services, queries — sees the request ID in its context, because the
context var is set before we call downstream. If we set it after, only
the access log line would have it.

## Why `finally` around the log line

Because the route might raise. If the exception propagates, we still
want an access log line saying "this request failed." The exception
handler (from `core/errors.py`) will produce the response, but by the
time middleware sees it, it's a normal response object — we don't need
to distinguish. `finally` guarantees the log happens either way.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.logging import request_id_var

logger = logging.getLogger("app.access")


# ---------------------------------------------------------------------------
# Header names
# ---------------------------------------------------------------------------
#
# Two names, both worth thinking about:
#
#   REQUEST_ID_HEADER — the response header we set. Clients and support
#     engineers can quote this back to us to correlate a specific report
#     with a specific log entry.
#
#   INCOMING_REQUEST_ID_HEADER — the header we *read* if a client (or an
#     upstream proxy) has already set one. This is a pattern worth
#     internalizing: **propagate IDs across services** rather than
#     generating fresh ones at every hop. If our app is ever behind a
#     gateway that assigns request IDs, we want to use those, so the
#     same ID appears in the gateway's logs and ours.
#
# We use the same string for both, which is the common convention
# (`X-Request-ID`). Some shops use different names, but the important
# thing is consistency.

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Middleware that assigns a request ID and logs one line per request.

    `BaseHTTPMiddleware` is Starlette's helper for HTTP-shaped middleware.
    The alternative — writing raw ASGI middleware — gives more control
    but requires more boilerplate and careful handling of the ASGI
    message protocol. For our two concerns, `BaseHTTPMiddleware` is the
    right level.

    The middleware is added to the app in `main.py` via
    `app.add_middleware(RequestContextMiddleware)`. Order of middleware
    matters — the first added is the outermost. See the note in
    `main.py` if we ever add more.

    ## Why a class and not a function

    A middleware *function* works fine with `@app.middleware("http")`.
    We use a class here for two reasons:

      1. It's greppable. `RequestContextMiddleware` is a name you can
         search for. A decorated function isn't.
      2. It carries configuration (the header names, the logger name)
         as class-level attributes, which makes the intent more
         visible than constants scoped to a function body.

    Neither is decisive. Consistency is what matters; we picked classes.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Response],
    ) -> Response:
        """
        Wrap one request/response cycle.

        `call_next` is the rest of the stack — routing, dependencies,
        the route handler, the response serialization. When we `await`
        it, all of that runs. What we do before and after is our
        middleware's contribution.

        ## Request ID sources, in priority order

          1. The `X-Request-ID` header on the incoming request, if
             present. This preserves the ID assigned by an upstream
             service so the same request is traceable end to end.
          2. A freshly-generated UUID4 otherwise.

        `uuid4` is random, not sequential. The alternative, `uuid1`,
        embeds the host's MAC address and timestamp — a small
        information leak and a worse distribution. `uuid4` is the right
        choice for correlation IDs. (`uuid7` would be even better —
        time-ordered — but the stdlib doesn't ship it yet as of this
        writing.)

        ## Setting the context var

        `request_id_var.set(...)` puts the ID into the current async
        task's context. Everything that logs after this point — the
        route, services, queries, and finally this middleware's own
        access log — reads it from the same place. We don't need to
        pass it around.

        ## Resetting the context var

        `ContextVar.set` returns a token we can use to reset the value.
        Strictly, since each request runs in its own task and the
        context doesn't leak across requests, we could skip the reset.
        But resetting is the correct discipline: it means the var's
        scope is exactly one request, and any code running after the
        request (a background task, a shutdown hook) starts with the
        default rather than inheriting the last request's ID.
        """
        # Extract or generate the request id.
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        token = request_id_var.set(request_id)

        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # If the route raised and no exception handler caught it,
            # we still want an access log line. Log at ERROR here so
            # that the line stands out; the exception handler downstream
            # will also log (via the catch-all), but this line has the
            # duration and status that the handler doesn't.
            #
            # We re-raise so the exception continues to the normal
            # handler chain. Middleware should never swallow exceptions
            # silently — that would make bugs invisible.
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "%s %s -> 500 (%.2fms)",
                request.method,
                request.url.path,
                duration_ms,
            )
            raise
        finally:
            # Reset the context var regardless of outcome. See docstring.
            request_id_var.reset(token)

        duration_ms = (time.perf_counter() - start) * 1000

        # Attach the request id to the response so a client can quote
        # it back. This is the single most useful debugging affordance
        # a web service can offer — "tell me the ID from the response
        # header" is how support tickets get resolved.
        response.headers[REQUEST_ID_HEADER] = request_id

        # One access log line per request. Level depends on the status
        # class:
        #
        #   2xx, 3xx  → INFO
        #   4xx       → INFO  (client errors are normal traffic — a
        #                      401 or 404 is not a server problem)
        #   5xx       → ERROR (we broke; this is worth alerting on)
        #
        # Logging 4xx at ERROR (a common mistake) makes the error rate
        # meaningless — a broken link would look like an outage.
        status_code = response.status_code
        log = logger.info if status_code < 500 else logger.error

        log(
            "%s %s -> %d (%.2fms)",
            request.method,
            request.url.path,
            status_code,
            duration_ms,
        )

        return response