"""
app/utils/pagination.py
=======================

Helpers for the limit/offset pagination pattern used across list
endpoints.

## What this module is, and what it isn't

This is *not* a pagination framework. It's the smallest thing that
supports the pattern we've standardized on:

    limit  — how many rows to return (1..100)
    offset — how many rows to skip (0..∞)

The `Pagination` Pydantic model (in `schemas/common.py`) handles input
parsing and validation. This module handles the two things that model
can't:

    1. Converting a list of DB rows into a `Page[T]` response envelope.
    2. Clamping values defensively in code paths that don't go through
       FastAPI's query-param validation (scripts, tests, direct calls).

## Why a helper at all

Because every list endpoint needs the same two lines:

    rows, total = await some_query(db, limit=p.limit, offset=p.offset)
    return Page(items=rows, total=total, limit=p.limit, offset=p.offset)

That's not enough code to justify a framework, but it *is* enough to
justify a helper that eliminates the risk of someone forgetting to echo
back `total` or `limit`. Small consistency helpers like this pay off at
scale — every list endpoint looks identical, and clients can write one
pagination handler for the whole API.

## On `total` costing an extra query

Every paginated list does two queries: one for the page, one for the
count. The count is often the expensive one on large tables, because
`COUNT(*)` has to scan — Postgres can do it with an index-only scan in
some cases, but not always.

Alternatives we're *not* using, and why:

  - **Skip the count entirely.** Return only `items` and a `has_next`
    boolean. Simpler, cheaper, but you lose the ability to show "page 3
    of 47." For most admin UIs, showing the total is worth the query.
  - **Estimate the count.** `SELECT reltuples FROM pg_class WHERE ...`
    gives a rough row count from statistics. Fast, approximate, useless
    for a paginated response because the number changes.
  - **Cursor-based pagination.** No count needed. Different UX ("next"
    without page numbers). Right for feeds, wrong for admin tables.

For a school register with a handful of programs and students, the
count query is trivially cheap. The pattern here is the right one for
the shape of this app.
"""

from __future__ import annotations

from typing import Any, TypeVar

from app.schemas.common import Page, Pagination

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------
#
# These mirror the bounds declared on `Pagination` in `schemas/common.py`.
# They exist here as constants so this module can clamp values that come
# from places other than FastAPI query-param validation — such as scripts,
# tests, or a future admin tool that calls these helpers directly.
#
# If you ever change the bounds in `Pagination`, change them here too.
# The coupling is intentional: the schema is the source of truth for API
# clients, these constants are the source of truth for internal callers.
# They must agree.

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MIN_LIMIT = 1
MIN_OFFSET = 0


def clamp_pagination(limit: int, offset: int) -> tuple[int, int]:
    """
    Clamp limit and offset into their valid ranges.

    FastAPI's Pydantic validation already does this for query parameters
    on routes — an out-of-range `?limit=0` gets a 422 before the handler
    runs. But internal callers (scripts, tests, the seed script) bypass
    that. This function is the belt to the schema's suspenders.

    Behavior on out-of-range values:

      - `limit < 1`        → clamped to 1
      - `limit > MAX_LIMIT` → clamped to MAX_LIMIT
      - `offset < 0`        → clamped to 0

    We clamp rather than raise. That's the right choice for internal
    callers, which should get *something* rather than an exception for a
    slightly wrong bound. FastAPI already raises for API clients, so the
    two paths behave appropriately for their audiences.

    Returns:
        A `(limit, offset)` tuple, both guaranteed in range.
    """
    limit = max(MIN_LIMIT, min(MAX_LIMIT, limit))
    offset = max(MIN_OFFSET, offset)
    return limit, offset


def build_page(
    *,
    items: list[T],
    total: int,
    pagination: Pagination,
) -> Page[T]:
    """
    Wrap a page of items in the standard response envelope.

    This is the one function every list endpoint calls. It takes what the
    query returned (`items`, `total`) and what the client requested
    (`pagination`), and produces the `Page[T]` response.

    Type parameter `T` is inferred from `items`. So `build_page(items=[user1, user2], ...)`
    infers `T=dict` and returns a `Page[dict]`, which FastAPI then
    serializes using the response model declared on the route.

    Note we take `pagination: Pagination` rather than separate `limit`
    and `offset` kwargs. That's deliberate: it keeps the echo-back values
    (`limit`, `offset` in the response) exactly as FastAPI parsed them,
    not as some intermediate transform. If the client asked for
    `?limit=25`, the response says `"limit": 25`, even if the underlying
    query clamped something.

    ## Why this matters for the response envelope

    A client that pages through results uses `total` and `limit` to know
    when to stop:

        pages_remaining = ceil((total - offset) / limit)

    If we echoed back a clamped or transformed value, that math would be
    wrong. Echoing the request's values exactly is what makes the client-
    side pagination math reliable.

    Args:
        items:      The rows for this page, as returned by the query.
        total:      Total number of matching rows across all pages.
        pagination: The parsed request pagination (limit, offset).

    Returns:
        A `Page` model ready to be returned from a route.
    """
    return Page(
        items=items,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )