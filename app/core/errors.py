"""
app/core/errors.py
==================

Domain exceptions and the handlers that turn them into HTTP responses.

This module is the shared vocabulary for failure across the app. Every
layer that needs to signal "something went wrong" raises one of the
exceptions here, and the handlers at the bottom translate them into a
single, consistent JSON shape.

## Why a domain exception layer at all

You *could* raise `fastapi.HTTPException` directly from services and
routers. It would work. But it would couple every layer to the HTTP
framework, and it would scatter the API's error contract across the
codebase. When you later wanted to change the error JSON shape (add a
request ID, add a `docs_url`, whatever), you'd have to hunt down every
`raise` site.

Instead, we split the concern in two:

  - **Domain exceptions** (`NotFound`, `Forbidden`, `Conflict`,
    `Unauthorized`) describe *what went wrong* in the language of the
    application. They know nothing about HTTP.
  - **Exception handlers** translate those into HTTP status codes and a
    JSON body. They know nothing about the domain.

The service layer raises `Conflict(code="email_taken")`. The handler
decides that means HTTP 409 with a certain body. Neither side needs to
know about the other's world.

## The error JSON shape

Every error response — whether raised by us, by FastAPI's validation, or
by an unhandled exception — comes out looking like this::

    {
        "error": {
            "code": "email_taken",
            "message": "An account with that email already exists.",
            "details": null
        }
    }

Fields:

  - `code` — a stable, machine-readable string. Clients switch on this.
    It must not change casually; it's part of the public API contract.
  - `message` — a human-readable string, safe to display to end users.
    It should never contain stack traces, SQL errors, or internal paths.
  - `details` — optional. For validation errors, a list of per-field
    problems. `null` otherwise.

Using a top-level `error` key (rather than putting `code`/`message` at
the root) means success and error responses can never be confused: a
successful response simply doesn't have an `error` key. This is a small
convention that saves a lot of client-side conditional logic.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


# ===========================================================================
# Domain exceptions
# ===========================================================================
#
# Each exception carries a `code` (stable, machine-readable) and a
# `message` (human-readable). The HTTP status that corresponds to each is
# declared on the handler, not the exception — keeping the exception free
# of HTTP concerns.
#
# All of them inherit from a common `AppError` base so that a single
# handler can catch "any of our domain errors" if we ever want that.


class AppError(Exception):
    """
    Base class for all domain exceptions.

    Carrying `code` and `message` as attributes rather than baking them
    into the exception string means the handler can access them
    structurally — no string parsing, no regex, no brittle assumptions
    about message format.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class NotFound(AppError):
    """
    The requested resource does not exist.

    Maps to HTTP 404. Example: `GET /programs/999` where no program has
    that id.
    """


class Unauthorized(AppError):
    """
    Authentication is required, or the provided credentials are invalid.

    Maps to HTTP 401. Examples: no session token, expired session,
    wrong password at login. Note the distinction from `Forbidden` below
    — 401 means "who are you?", 403 means "I know who you are, and you
    can't do this."
    """


class Forbidden(AppError):
    """
    The authenticated user lacks permission for this action.

    Maps to HTTP 403. Example: a student calling an admin-only endpoint.
    """


class Conflict(AppError):
    """
    The request conflicts with the current state of the resource.

    Maps to HTTP 409. Example: registering with an email that's already
    in use.
    """


# ===========================================================================
# HTTP status mapping
# ===========================================================================
#
# A single dictionary that says "this exception class → this status code."
# Kept in one place so the mapping is easy to audit and change. Adding a
# new domain exception means adding one line here.

_STATUS_FOR: dict[type[AppError], int] = {
    NotFound: status.HTTP_404_NOT_FOUND,
    Unauthorized: status.HTTP_401_UNAUTHORIZED,
    Forbidden: status.HTTP_403_FORBIDDEN,
    Conflict: status.HTTP_409_CONFLICT,
}


# ===========================================================================
# JSON body builder
# ===========================================================================


def _error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    """
    Build the standard error envelope.

    Centralized here so that the shape is defined in exactly one place.
    Every handler below calls this — there is no other way to build an
    error response in this codebase.
    """
    return {"error": {"code": code, "message": message, "details": details}}


# ===========================================================================
# Handlers
# ===========================================================================
#
# Each handler is a function that FastAPI calls when a particular
# exception type escapes a route. The handlers are registered in
# `register_exception_handlers`, which `main.py` calls at startup.


async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """
    Handle any of our domain exceptions.

    Looks up the HTTP status from `_STATUS_FOR` by walking the MRO — so a
    subclass of `Conflict` would inherit Conflict's 409 unless it's
    registered explicitly. Unknown subclasses fall back to 500, which is
    the safe default: we don't pretend an unclassified error is a client
    problem.
    """
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    for cls in type(exc).__mro__:
        if cls in _STATUS_FOR:
            http_status = _STATUS_FOR[cls]
            break

    # Log 5xx at ERROR, 4xx at INFO. The former indicates a bug; the
    # latter is normal traffic.
    if http_status >= 500:
        logger.error("domain error: %s", exc.code, exc_info=exc)
    else:
        logger.info("domain error: %s %s", exc.code, exc.message)

    return JSONResponse(
        status_code=http_status,
        content=_error_body(exc.code, exc.message, exc.details),
    )


async def _handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """
    Handle HTTPExceptions raised by FastAPI/Starlette itself.

    These come from framework internals — 405 Method Not Allowed, 404 for
    unmatched routes, and anything a dependency raises as an HTTPException.
    We normalize them into our error shape so the client sees one format
    regardless of where the error originated.

    The `code` is derived from the status code, since Starlette's
    HTTPException doesn't carry a domain code. `http_404`, `http_405`,
    etc. — stable enough for clients to switch on.
    """
    code = f"http_{exc.status_code}"
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."

    if exc.status_code >= 500:
        logger.error("http error: %s %s", exc.status_code, message)
    else:
        logger.info("http error: %s %s", exc.status_code, message)

    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, message),
        headers=getattr(exc, "headers", None) or {},
    )


async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handle Pydantic validation errors (422).

    FastAPI's default validation error response has a specific shape that
    includes `loc`, `msg`, `type`, and `input` per field. Rather than
    discard that detail, we embed a simplified list into our `details`
    field:

        "details": [
            {"field": "email", "message": "value is not a valid email address"},
            ...
        ]

    Flattening `loc` into a dotted path ("body.email" → "email") keeps the
    client-side handling simple — most UIs just want to know which field
    was wrong and why.
    """
    simplified = []
    for err in exc.errors():
        # `loc` is a tuple like ("body", "email") or ("query", "limit").
        # Drop the first element ("body"/"query") and join the rest.
        loc = err.get("loc", ())
        field = ".".join(str(part) for part in loc[1:]) if len(loc) > 1 else ".".join(
            str(part) for part in loc
        )
        simplified.append({"field": field, "message": err.get("msg", "invalid value")})

    logger.info("validation error on %s: %s", request.url.path, simplified)

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_error_body(
            "validation_error",
            "One or more fields are invalid.",
            simplified,
        ),
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all for anything we didn't anticipate.

    This is the last line of defense. It logs the full traceback
    server-side (for debugging) but returns a *generic* message to the
    client. Never leak exception details to the outside world — a stack
    trace in a response body is an information disclosure vulnerability
    waiting to be exploited.

    The `exc_info=exc` on the log call is what captures the traceback.
    Without it, the log line just says "unexpected error" and you have
    no idea where it came from.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_body(
            "internal_error",
            "An unexpected error occurred.",
        ),
    )


# ===========================================================================
# Registration
# ===========================================================================


def register_exception_handlers(app: FastAPI) -> None:
    """
    Wire all handlers onto a FastAPI app. Called once from `main.py`.

    Order of registration doesn't matter for FastAPI's dispatch — it
    matches by exception type, and the most specific match wins. The
    catch-all `Exception` handler only fires if nothing more specific
    matched.

    Note that `RequestValidationError` inherits from `Exception`, and
    `StarletteHTTPException` inherits from `Exception`. FastAPI resolves
    the specific type first, so our custom handlers run before the
    catch-all.
    """
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)