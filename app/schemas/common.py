"""
app/schemas/common.py
=====================

Shared Pydantic models used across many endpoints.

This module defines the *envelope* shapes: pagination and error. Any
endpoint that returns a list or fails with a structured error will reuse
these, which means:

  - clients parse one shape for all list endpoints
  - clients parse one shape for all error responses
  - a change to either shape happens here, once, and propagates

Pydantic v2 is what we're using throughout. The important bits for a
beginner:

  - `BaseModel` is the parent class. Subclassing it gives you validation,
    serialization, and JSON schema generation for free.
  - Fields are declared with type annotations. `name: str` means "a string
    is required and must be present."
  - `Field(...)` lets you add metadata: default values, constraints,
    descriptions, examples. FastAPI reads these and puts them into the
    auto-generated OpenAPI docs.
  - `model_config` is where you tune behavior (aliasing, extra fields,
    etc.). It replaces the older `class Config` style from Pydantic v1.

## On camelCase vs snake_case

We use snake_case throughout (`full_name`, `created_at`). This is the
Python convention and it keeps request/response keys identical to the
model attribute names — no aliasing, no surprises. If a future JS client
wants camelCase, they can map it on their side, or we can add Pydantic
aliases later. Consistency first.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ===========================================================================
# Error envelope
# ===========================================================================


class ErrorDetail(BaseModel):
    """
    The inner object of every error response.

    Having this as a named model (rather than an inline dict) means:

      - it shows up properly in the OpenAPI schema
      - it can be referenced from multiple places
      - changes to its shape are visible to type checkers

    Field notes:

      - `code` is stable and machine-readable. Clients switch on it. It
        must not change casually — it's part of the API contract.
      - `message` is safe to show a user.
      - `details` is optional and shape-agnostic. For validation errors it
        will be a list of `{field, message}` objects; for most other
        errors it's `null`. `Any` is the honest type here because the
        contents really are error-specific.
    """

    code: str = Field(
        ...,
        description="Stable machine-readable error identifier.",
        examples=["email_taken", "invalid_credentials", "validation_error"],
    )
    message: str = Field(
        ...,
        description="Human-readable error message, safe to display.",
    )
    details: Any | None = Field(
        default=None,
        description="Optional structured details, shape depends on `code`.",
    )


class ErrorResponse(BaseModel):
    """
    The outer envelope for every error, on every endpoint.

    The `error` key is always present in a failure response and always
    absent in a success response. That single rule lets clients distinguish
    success from failure without inspecting the HTTP status alone.
    """

    error: ErrorDetail


# ===========================================================================
# Pagination envelope
# ===========================================================================
#
# The pattern: a list endpoint never returns a bare JSON array. It returns
# an object with `items` and metadata. Reasons:
#
#   1. Extension without breakage. If we later add `next_cursor`, or
#      `total_pages`, existing clients don't need to change — the top
#      level stays an object.
#   2. Self-describing. The client can tell what the page size was and
#      how many results exist, without parsing query strings.
#   3. Consistency. Every list endpoint returns the same shape, so the
#      client-side pagination code is written once.
#
# The generic `Page[T]` means the schema for `Page[ProgramRead]` is
# generated automatically, with the correct item type, and FastAPI's
# OpenAPI output reflects it.


class Page(BaseModel, Generic[T]):
    """
    A single page of a paginated list.

    Type parameter `T` is the item type — e.g. `Page[UserRead]` or
    `Page[ProgramRead]`. FastAPI uses this to generate correct OpenAPI
    schemas per endpoint.

    Fields:

      - `items`  — the page's rows, in the order the query returned them.
      - `total`  — the total count of matching rows across all pages.
                    Not the length of `items`. A client can compute
                    "how many pages are there?" as `ceil(total / limit)`.
      - `limit`  — the requested page size (echoed back for clarity).
      - `offset` — the requested offset (echoed back for clarity).
    """

    items: list[T] = Field(
        ...,
        description="The rows on this page.",
    )
    total: int = Field(
        ...,
        description="Total number of matching rows across all pages.",
        examples=[123],
    )
    limit: int = Field(
        ...,
        description="Requested page size.",
        examples=[20],
    )
    offset: int = Field(
        ...,
        description="Requested offset into the full result set.",
        examples=[0],
    )


# ===========================================================================
# Pagination query parameters
# ===========================================================================
#
# FastAPI treats a Pydantic model used as a parameter type specially: it
# reads the model's fields from the query string. So a route can declare
# `pagination: Pagination = Depends()` and FastAPI will parse `?limit=&offset=`
# for you.
#
# This replaces the pattern of writing `limit: int = Query(20, ...)` on
# every list endpoint. One model, reused, with the bounds in one place.


class Pagination(BaseModel):
    """
    Standard `limit`/`offset` query parameters.

    Bounds:

      - `limit` must be between 1 and 100. The lower bound prevents
        `?limit=0` (which returns nothing and confuses users). The upper
        bound prevents `?limit=1000000` from being used as a cheap DoS.
      - `offset` must be non-negative.

    Defaults are chosen for typical browser pagination: 20 rows per page,
    first page by default.
    """

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of items to return.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of items to skip before starting to collect the result set.",
    )