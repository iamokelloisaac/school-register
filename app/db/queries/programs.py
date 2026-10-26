"""
app/db/queries/programs.py
==========================

Raw SQL for the `programs` table.

Same layering rules as `users.py` and `sessions.py`:

  - Takes a connection, never opens one.
  - Returns plain dicts, never Pydantic models.
  - No business logic, no exception translation.
  - One focused statement per function where possible.

## What's different here

Unlike `users` and `sessions`, `programs` is domain data — it's what the
app is actually about. That means there are more read paths:

  - The public catalog listing (active programs only, paginated).
  - The single-program detail view.
  - A lookup by code (for imports, migrations, and future URL-friendly
    routes).
  - A "does this program exist and is it enrollable" check used by the
    enrollment service.

None of these are business decisions. They're just different shapes of
"select from programs." The business — "should students be able to
enroll in archived programs?" — lives one layer up.

## On `is_active`

The table has an `is_active` column for soft-delete. The queries here
filter on it where appropriate, but always as a *parameter*, never as a
hardcoded WHERE clause. That way:

  - The public listing can show only active programs.
  - An admin listing can show all programs (active and archived).
  - A future audit view can list only archived ones.

One query, three filters. The alternative — writing three separate
queries — duplicates the SELECT list and guarantees drift.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection


# ---------------------------------------------------------------------------
# Column list
# ---------------------------------------------------------------------------
#
# Spelled out, same as the other query modules. One place to change if
# the shape of a "program" changes.
#
# `created_by` is included even though most clients don't need it — it's
# cheap to select, and dropping it from the projection would mean a
# separate query for the admin audit view. The cost of one extra integer
# per row is negligible.

_PROGRAM_COLUMNS = """
    id,
    name,
    code,
    description,
    is_active,
    created_by,
    created_at,
    updated_at
"""


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_program(
    conn: AsyncConnection,
    *,
    name: str,
    code: str,
    description: str | None,
    created_by: int,
) -> dict[str, Any]:
    """
    Insert a new program and return it.

    `code` is expected to already be normalized (uppercased) by the
    schema layer. We don't re-normalize here — the query layer shouldn't
    quietly rewrite input; it should insert what it's given. If a caller
    bypasses the schema and inserts a lowercase code, the resulting row
    is that caller's responsibility. This is the same principle as "no
    exception translation in queries" — the query layer is dumb, on
    purpose.

    `created_by` is required and non-nullable in this insert. The DB
    column is nullable (for the `ON DELETE SET NULL` cascade), but a
    newly-created program always has a creator. Requiring the argument
    here means the caller can't forget it, and a future "system-created
    program" flow would have to make a deliberate choice to pass null.

    Raises:
        psycopg.errors.UniqueViolation: if `name` or `code` collides.
            Not caught — the service translates.
        psycopg.errors.ForeignKeyViolation: if `created_by` doesn't
            reference a real user. Also not caught.

    Returns:
        The created program row, including the generated `id` and
        timestamps.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            INSERT INTO programs (name, code, description, created_by)
            VALUES (%(name)s, %(code)s, %(description)s, %(created_by)s)
            RETURNING {_PROGRAM_COLUMNS}
            """,
            {
                "name": name,
                "code": code,
                "description": description,
                "created_by": created_by,
            },
        )
        row = await cur.fetchone()

    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Read — single row
# ---------------------------------------------------------------------------


async def get_program_by_id(
    conn: AsyncConnection,
    program_id: int,
) -> dict[str, Any] | None:
    """
    Fetch a single program by primary key.

    Returns the row regardless of `is_active` — this is used both by
    public endpoints (which will check `is_active` themselves) and by
    admin endpoints (which want to see archived programs too). Filtering
    here would force the admin path into a second query.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_PROGRAM_COLUMNS}
            FROM programs
            WHERE id = %(id)s
            """,
            {"id": program_id},
        )
        return await cur.fetchone()


async def get_program_by_code(
    conn: AsyncConnection,
    code: str,
) -> dict[str, Any] | None:
    """
    Fetch a single program by its short code.

    Not currently used by any endpoint, but useful for:
      - a future URL-friendly route (`/programs/by-code/CS`)
      - seed and migration scripts that want to upsert by code
      - tests that want to look up a program without knowing its id

    Included now because it's trivial and cheap, and leaving it out means
    the first script that needs it has to write raw SQL outside the
    query layer, which is what we're trying to avoid.

    `code` is compared exactly — no case-folding. If you want
    case-insensitive lookup, the schema layer normalizes the input first
    (which it does), so this function can assume it's already uppercase.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_PROGRAM_COLUMNS}
            FROM programs
            WHERE code = %(code)s
            """,
            {"code": code},
        )
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# Read — list
# ---------------------------------------------------------------------------


async def list_programs(
    conn: AsyncConnection,
    *,
    limit: int,
    offset: int,
    active_only: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """
    Fetch a page of programs plus the total count.

    `active_only` defaults to True because the *public* listing is the
    common case — students should only see active programs. Admin
    listings pass `active_only=False` to see everything.

    The two queries (page + count) use the same WHERE clause. That's
    important: if the page query filtered but the count didn't, `total`
    would be larger than the sum of items across pages, and the client's
    pagination math would be off. Keeping the clause in both places (via
    a small local variable) means they can't drift.

    `ORDER BY id` — stable ordering, same reasoning as `list_users`.
    Without an explicit order, Postgres may return rows in any order and
    paginated results can skip or duplicate rows.

    Returns:
        `(rows, total)` where `rows` is the current page and `total` is
        the overall count of programs matching the filter.
    """
    where_clause = "WHERE is_active = TRUE" if active_only else ""

    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_PROGRAM_COLUMNS}
            FROM programs
            {where_clause}
            ORDER BY id
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"limit": limit, "offset": offset},
        )
        rows = await cur.fetchall()

        await cur.execute(
            f"SELECT COUNT(*) AS total FROM programs {where_clause}"
        )
        count_row = await cur.fetchone()

    assert count_row is not None
    return list(rows), count_row["total"]


# ---------------------------------------------------------------------------
# Delete (hard)
# ---------------------------------------------------------------------------
#
# Two delete functions with different semantics:
#
#   - `delete_program` — hard DELETE. Removes the row and (via the
#     enrollments FK's ON DELETE CASCADE) removes every enrollment
#     pointing at it.
#   - `deactivate_program` — soft delete. Flips `is_active` to FALSE.
#     Enrollments survive; the program just stops appearing in the
#     public listing.
#
# Both are legitimate, and the choice between them is a *service-layer*
# decision based on what the caller asked for. The query layer offers
# both primitives; the service picks one.


async def delete_program(
    conn: AsyncConnection,
    *,
    program_id: int,
) -> bool:
    """
    Hard-delete a program. Returns True if a row was removed.

    Because `enrollments.program_id` has `ON DELETE CASCADE`, deleting a
    program also deletes every enrollment pointing at it. That's a
    significant side effect, and it's the reason we expose both hard
    delete and soft delete: an admin who wants to preserve enrollment
    history should archive, not delete.

    Returns a boolean rather than void because the *service* needs to
    know whether the row existed. If it didn't, the service probably
    wants to raise `NotFound` — a 204 on a delete that removed nothing
    is fine for idempotent APIs, but for an explicit admin action it's
    more honest to 404. The service decides; this function just reports
    what happened.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM programs WHERE id = %(id)s",
            {"id": program_id},
        )
        return cur.rowcount > 0


async def deactivate_program(
    conn: AsyncConnection,
    *,
    program_id: int,
) -> bool:
    """
    Soft-delete a program by flipping `is_active` to FALSE.

    Enrollments are unaffected. The program disappears from the public
    listing but remains visible to admin queries and can be reactivated
    later.

    Returns True if a row was updated, False if no program matched the
    id. Same reasoning as `delete_program`.

    Note this sets `is_active = FALSE` unconditionally — deactivating an
    already-inactive program is a no-op that still reports True (the row
    was found, even if nothing meaningfully changed). If you needed to
    distinguish "was already inactive" from "just deactivated," you'd
    add a WHERE clause or return the row. We don't need that distinction
    yet.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE programs
            SET is_active  = FALSE,
                updated_at = NOW()
            WHERE id = %(id)s
            """,
            {"id": program_id},
        )
        return cur.rowcount > 0