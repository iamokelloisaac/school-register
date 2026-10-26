"""
app/db/queries/users.py
=======================

Raw SQL for the `users` table.

This module is the only place in the codebase that knows the shape of the
`users` table. Routers, services, and dependencies call these functions
instead of writing SQL themselves. That discipline has three payoffs:

  1. Every query against `users` is greppable — one file, one place.
  2. Changing the schema means changing this file and nothing else.
  3. The queries are testable in isolation, without FastAPI or HTTP.

## Layering rules this file obeys

  - **Takes a connection, never opens one.** The caller supplies `conn`.
    That connection comes from the pool, via `get_db`. This module never
    imports `app.db.pool` or `app.core.deps`. Enforcing this rule is what
    makes the queries reusable and testable.

  - **Returns plain dicts, not Pydantic models.** We don't know or care
    about the API's response shape. `dict_row` gives us dicts; we hand
    them back. Whoever needs a Pydantic model can build one from the dict.

  - **No business logic.** "Email already taken" is a *service* concern.
    If the insert violates a UNIQUE constraint, we let the exception
    bubble up. The service layer decides whether that means a 409, a 400,
    or something else.

  - **No exception translation.** No `try/except` here on purpose. This
    file is dumb on purpose — it does SQL, and SQL errors are the DB's
    language. Translation happens higher up.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection


# ---------------------------------------------------------------------------
# Column list
# ---------------------------------------------------------------------------
#
# We spell out the columns we select instead of using `SELECT *`. Two
# reasons, both practical:
#
#   1. If we add a column later (say, `phone`), old code that did `SELECT *`
#      would suddenly start receiving an unexpected field — and any code
#      that constructs a Pydantic model with `extra="forbid"` would break.
#      Explicit columns mean the change is opt-in.
#
#   2. `hashed_password` is deliberately excluded from the default read
#      path. Most callers — the auth dependency, admin listings, `/me` —
#      never need to see the hash. Only the login flow does, and it uses a
#      dedicated query for that. This is defense in depth: even if a
#      response schema accidentally forgets to exclude the hash, the query
#      never fetched it.
#
# This is a small pattern worth internalizing: **shape the query around
# who consumes it**, don't fetch the union of all possible columns.
_USER_COLUMNS = """
    id,
    email,
    full_name,
    age,
    role,
    is_active,
    created_at,
    updated_at
"""


async def create_user(
    conn: AsyncConnection,
    *,
    email: str,
    hashed_password: str,
    full_name: str,
    age: int,
    role: str = "student",
) -> dict[str, Any]:
    """
    Insert a new user and return the created row.

    Uses `RETURNING` so the caller gets back the generated `id`, timestamps,
    and every default the DB filled in — all in one round trip. Without
    `RETURNING`, we'd need a second `SELECT` to fetch what we just wrote,
    which is both slower and race-prone (another request could modify the
    row in between).

    Args:
        conn:            An open psycopg connection.
        email:           The user's email. Unique across users. Because the
                         column is CITEXT, comparison is case-insensitive,
                         but we store whatever casing the caller passed.
        hashed_password: The Argon2id hash — never the plaintext.
        full_name:       Display name.
        age:             Integer. Enforced 0..150 by a CHECK constraint.
        role:            'student' or 'admin'. The enum type in the DB will
                         reject anything else.

    Returns:
        A dict with the newly-created user's columns (minus the hash).

    Raises:
        psycopg.errors.UniqueViolation: if the email already exists.
            Not caught here — the service layer translates it.
        psycopg.errors.CheckViolation: if `age` is out of range.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            INSERT INTO users (email, hashed_password, full_name, age, role)
            VALUES (%(email)s, %(hashed_password)s, %(full_name)s, %(age)s, %(role)s)
            RETURNING {_USER_COLUMNS}
            """,
            {
                "email": email,
                "hashed_password": hashed_password,
                "full_name": full_name,
                "age": age,
                "role": role,
            },
        )
        row = await cur.fetchone()

    # With RETURNING, insert always produces exactly one row.
    assert row is not None
    return row


async def get_user_by_email(
    conn: AsyncConnection,
    email: str,
) -> dict[str, Any] | None:
    """
    Fetch a user by email, *including the password hash*.

    This is the one read path that returns `hashed_password`. It exists
    solely for the login flow, which needs the hash to verify the password.
    Every other caller should use `get_user_by_id` or `list_users`, both of
    which exclude the hash.

    The lookup is case-insensitive because the column is `CITEXT` — no
    `LOWER()` wrapping needed. That's the payoff of choosing CITEXT at
    schema time.

    Returns None if no user matches. The caller decides whether that's a
    404, a login failure, or something else.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_USER_COLUMNS}, hashed_password
            FROM users
            WHERE email = %(email)s
            """,
            {"email": email},
        )
        return await cur.fetchone()


async def get_user_by_id(
    conn: AsyncConnection,
    user_id: int,
) -> dict[str, Any] | None:
    """
    Fetch a user by primary key, without the password hash.

    Used by `get_current_user` on every authenticated request, so it should
    stay lean. The password hash is deliberately not selected — nothing in
    the auth dependency needs it, and not fetching it means it cannot leak
    by accident.

    Returns None if no user matches.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_USER_COLUMNS}
            FROM users
            WHERE id = %(id)s
            """,
            {"id": user_id},
        )
        return await cur.fetchone()


async def list_users(
    conn: AsyncConnection,
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """
    Fetch a page of users plus the total count.

    Two queries in one function, on purpose. The page is what the caller
    wants to display; the total is what the pagination envelope needs.
    Separating them into two functions would force every caller to remember
    to call the count query, which is exactly the kind of thing that gets
    forgotten. Bundling them here keeps the pagination contract in one
    place.

    `ORDER BY id` is stable and deterministic — without an explicit order,
    Postgres is free to return rows in any order, and pagination across
    calls can then skip or duplicate rows. Always order when paginating.
    `id` (a BIGSERIAL) is monotonic, so ordering by it is cheap and
    consistent.

    Returns:
        (rows, total) where `rows` is the current page and `total` is the
        overall count of users in the table (ignoring limit/offset).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_USER_COLUMNS}
            FROM users
            ORDER BY id
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"limit": limit, "offset": offset},
        )
        rows = await cur.fetchall()

        await cur.execute("SELECT COUNT(*) AS total FROM users")
        count_row = await cur.fetchone()

    assert count_row is not None
    return list(rows), count_row["total"]


async def update_user_password(
    conn: AsyncConnection,
    *,
    user_id: int,
    hashed_password: str,
) -> None:
    """
    Replace a user's password hash.

    Also bumps `updated_at`, so the change is visible in audit-style
    queries. We do this manually rather than with a trigger to keep the
    SQL explicit — the tradeoff is one extra thing to remember per write
    query, the benefit is that nothing happens behind your back.

    Does nothing if the user does not exist (UPDATE with no matching rows
    is a no-op, not an error). The service layer decides whether that
    matters.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE users
            SET hashed_password = %(hashed_password)s,
                updated_at      = NOW()
            WHERE id = %(id)s
            """,
            {"id": user_id, "hashed_password": hashed_password},
        )


async def set_user_active(
    conn: AsyncConnection,
    *,
    user_id: int,
    is_active: bool,
) -> None:
    """
    Flip a user's active flag — used for suspension and reactivation.

    Same no-op-on-missing-row semantics as the other updates.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE users
            SET is_active  = %(is_active)s,
                updated_at = NOW()
            WHERE id = %(id)s
            """,
            {"id": user_id, "is_active": is_active},
        )