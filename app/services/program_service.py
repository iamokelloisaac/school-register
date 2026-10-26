"""
app/services/program_service.py
===============================

Orchestration for admin program operations.

Currently thin: it wraps the query layer and translates DB-level
exceptions into domain ones, following the same pattern as
`auth_service` and `enrollment_service`.

## Why this exists

Without it, `POST /admin/programs` would call `programs_q.create_program`
directly and let `psycopg.errors.UniqueViolation` escape as a 500. The
translation "unique violation on a program insert means a name-or-code
collision means 409 Conflict" belongs in the service layer, not the
router. Same rule as everywhere else: **queries let DB errors bubble;
services decide what they mean.**

As more program operations accumulate (edit, unarchive, bulk-import),
they'll land here and the file will grow into a more substantial service.
For now, it's one function.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection
from psycopg import errors as pg_errors

from app.core.errors import Conflict
from app.db.queries import programs as programs_q

logger = logging.getLogger(__name__)


async def create_program(
    conn: AsyncConnection,
    *,
    name: str,
    code: str,
    description: str | None,
    created_by: int,
) -> dict[str, Any]:
    """
    Create a program, translating unique-constraint errors into Conflict.

    The DB has two unique constraints on `programs`: one on `name`, one
    on `code`. A `UniqueViolation` from the insert could be either. To
    give the client a useful message, we check the constraint name —
    psycopg exposes it via `exc.diag.constraint_name`, which Postgres
    populates with the name of the violated constraint.

    Postgres auto-generates those names as `programs_name_key` and
    `programs_code_key` (based on the column and the constraint type).
    If we ever rename a column or add a named constraint, these strings
    change, and this function needs updating. That's the price of
    giving a specific error rather than a generic "duplicate program."

    If we can't match a constraint name (Postgres changed something, or
    we're on a version that doesn't populate `diag`), we fall back to a
    generic Conflict with the code `program_duplicate`. Better to give
    a vaguer error than a 500.

    Raises:
        Conflict: on either unique-constraint violation.
    """
    try:
        return await programs_q.create_program(
            conn,
            name=name,
            code=code,
            description=description,
            created_by=created_by,
        )
    except pg_errors.UniqueViolation as exc:
        constraint = exc.diag.constraint_name if exc.diag else None

        if constraint == "programs_code_key":
            logger.info("program create rejected: code '%s' already in use", code)
            raise Conflict(
                code="program_code_taken",
                message="A program with that code already exists.",
            ) from exc

        if constraint == "programs_name_key":
            logger.info("program create rejected: name '%s' already in use", name)
            raise Conflict(
                code="program_name_taken",
                message="A program with that name already exists.",
            ) from exc

        # Fallback — we know *something* unique was violated but can't
        # tell which. Better a generic 409 than a 500.
        logger.warning(
            "program create rejected: unique violation on unrecognized constraint %r",
            constraint,
        )
        raise Conflict(
            code="program_duplicate",
            message="A program with those details already exists.",
        ) from exc