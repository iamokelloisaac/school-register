"""
app/db/queries/enrollments.py
=============================

Raw SQL for the `enrollments` join table, plus the joined read view for
"programs a student is enrolled in."

The table itself is minimal — three columns, composite primary key, no
surrogate id. That minimalism means the interesting work happens in the
*join* between `enrollments` and `programs`, which is what the student-
facing endpoints actually need.

## The two categories of query here

1. **Link management** — insert and delete rows in `enrollments`. This
   is the pure join-table work.

2. **Joined reads** — return program details combined with the
   enrollment timestamp, so a client can render "my programs" without a
   second query per program.

Both categories live in this module because they both concern the
relationship. Splitting "raw enrollment rows" and "joined program
views" into separate files would mean callers import from two places
for one concern, which is worse than one slightly larger file.

## On the join

The `EnrolledProgram` read is a two-table join:

    enrollments  ──►  programs
    (student_id,      (id, name, code,
     program_id)       description, ...)

We select the program columns the client needs plus `enrollments.enrolled_at`.
We do not select `programs.created_by` or `programs.updated_at` — the
schema doesn't expose them, and fetching unused columns is a small waste
that adds up. The query and the schema are kept in sync by hand; the
alternative (a generic "fetch all program columns plus enrolled_at")
would leak the schema into the query's SELECT list, which is worse.

## On ordering

Every list query orders by something stable. For enrollments, the
natural order is `enrolled_at DESC` — most recently enrolled first. This
matches the UI expectation ("my newest program at the top") and, because
`enrolled_at` is set with `NOW()`, it's effectively unique per student
in practice. If you ever enrolled the same student in two programs in
the same millisecond, ties would break unpredictably — we add `id` as a
tiebreaker to make the order fully deterministic.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection


# ---------------------------------------------------------------------------
# Column list
# ---------------------------------------------------------------------------
#
# The joined select is the one place where the column list crosses two
# tables. We spell out which table each column comes from (`programs.name`,
# `enrollments.enrolled_at`) so that a future reader can tell at a glance
# what's coming from where, and so that adding a column to `programs`
# doesn't silently change this query's shape.

_ENROLLED_PROGRAM_COLUMNS = """
    programs.id,
    programs.name,
    programs.code,
    programs.description,
    enrollments.enrolled_at
"""


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def enroll(
    conn: AsyncConnection,
    *,
    student_id: int,
    program_id: int,
) -> dict[str, Any]:
    """
    Insert an enrollment row and return it.

    The composite primary key `(student_id, program_id)` means a
    duplicate insert raises `UniqueViolation`. We let it bubble up —
    the service layer decides whether that means "already enrolled"
    (a 409) or "concurrent request beat us" (also a 409, or possibly a
    silent success depending on the semantics we want).

    ## On the `ON CONFLICT` alternative

    Postgres supports `INSERT ... ON CONFLICT DO NOTHING`, which would
    turn a duplicate enroll into a silent no-op instead of an exception.
    We're deliberately *not* using it here because:

      - "Already enrolled" is meaningful information the client probably
        wants to hear about (a distinct response from "just enrolled").
      - `ON CONFLICT DO NOTHING` returning no rows would make it harder
        to distinguish "just enrolled" from "already enrolled."
      - Exceptions are the DB's way of saying "your write violated a
        rule." Swallowing them by default is a habit that hides bugs.

    If we later decide enrollment should be idempotent — a POST that
    succeeds whether the student was already enrolled or not — we'd
    change this function's contract explicitly, and probably rename it
    to something like `ensure_enrolled`.

    Returns:
        The created enrollment row.

    Raises:
        psycopg.errors.UniqueViolation: if already enrolled.
        psycopg.errors.ForeignKeyViolation: if either id doesn't exist.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO enrollments (student_id, program_id)
            VALUES (%(student_id)s, %(program_id)s)
            RETURNING student_id, program_id, enrolled_at
            """,
            {"student_id": student_id, "program_id": program_id},
        )
        row = await cur.fetchone()

    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def leave(
    conn: AsyncConnection,
    *,
    student_id: int,
    program_id: int,
) -> bool:
    """
    Delete an enrollment row. Returns True if a row was removed.

    Returns a boolean because the *service* needs to know whether the
    student was actually enrolled. "Leave a program I wasn't in" is a
    different situation from "just left," and the service may want to
    404 or 204 accordingly. The query layer reports the fact; the
    service decides.

    No cascade concerns — deleting an enrollment doesn't affect any
    other table. That's the beauty of the join table having no dependent
    children.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM enrollments
            WHERE student_id = %(student_id)s
              AND program_id = %(program_id)s
            """,
            {"student_id": student_id, "program_id": program_id},
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Read — check
# ---------------------------------------------------------------------------


async def is_enrolled(
    conn: AsyncConnection,
    *,
    student_id: int,
    program_id: int,
) -> bool:
    """
    Check whether a specific enrollment exists.

    Cheaper than fetching the whole row when you only need a boolean.
    `SELECT EXISTS(...)` short-circuits at the first match and can use
    the composite PK index directly, so it's essentially O(1).

    Used by the enrollment service before attempting an insert in some
    flows, and by read endpoints that want to include "are you enrolled
    in this?" in a program detail response. Note that the service still
    has to handle the UniqueViolation race even if it checks first —
    check-then-insert is never atomic. This function is for the
    non-racing uses.

    Returns True if the enrollment exists, False otherwise.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM enrollments
                WHERE student_id = %(student_id)s
                  AND program_id = %(program_id)s
            ) AS exists
            """,
            {"student_id": student_id, "program_id": program_id},
        )
        row = await cur.fetchone()

    assert row is not None
    return bool(row["exists"])


# ---------------------------------------------------------------------------
# Read — joined views
# ---------------------------------------------------------------------------


async def list_enrolled_programs(
    conn: AsyncConnection,
    *,
    student_id: int,
) -> list[dict[str, Any]]:
    """
    Return every program the given student is enrolled in.

    This is the query behind `GET /me/programs`. It joins `enrollments`
    to `programs` and returns the flattened shape the schema expects.

    ## Why no pagination

    A student's enrollment list is bounded by the number of programs the
    school offers, which for a school register is realistically small
    (tens, not thousands). Paginating it would be ceremony without
    benefit, and it would force the client into a pagination loop for
    what is fundamentally one call's worth of data.

    If we ever hit a case where a single student has thousands of
    enrollments, we'd add `limit`/`offset` here and the schema would
    grow accordingly. Until then, no pagination is the honest shape.

    ## Why not filter by `programs.is_active`

    Because the student's enrollment history is *theirs*. If a program
    they enrolled in gets archived, we don't want their "my programs"
    list to suddenly lose it — that would look like a bug from the
    student's point of view. The public program *catalog* filters by
    active; the student's *enrollment list* does not. Two different
    lists, two different rules, and both are correct for their purpose.

    If we later want to show "archived" as a badge on the row, we'd add
    `programs.is_active` to the SELECT and the schema, and the client
    would render it. That's a schema change, not a query change here.

    ## Ordering

    Most recently enrolled first. Ties broken by `programs.id` to make
    the order fully deterministic — see the module docstring.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_ENROLLED_PROGRAM_COLUMNS}
            FROM enrollments
            JOIN programs ON programs.id = enrollments.program_id
            WHERE enrollments.student_id = %(student_id)s
            ORDER BY enrollments.enrolled_at DESC, programs.id
            """,
            {"student_id": student_id},
        )
        rows = await cur.fetchall()

    return list(rows)


async def list_students_in_program(
    conn: AsyncConnection,
    *,
    program_id: int,
) -> list[dict[str, Any]]:
    """
    Return every student enrolled in a given program.

    Same shape idea as `list_enrolled_programs`, but in the other
    direction: join `enrollments` to `users` and return minimal student
    fields plus the enrollment timestamp.

    Deliberately excludes `users.hashed_password` — this is a
    student-facing-looking query (admin UI, or a future "who's in this
    program" view) and the hash has no business being in the result.

    Not currently used by any endpoint, but included because:
      - the admin view of "who's in program X" is the obvious next
        feature and would otherwise write raw SQL outside this module
      - it's the mirror of `list_enrolled_programs` and belongs next to
        it for discoverability
      - it's the cheapest possible example of a users+join query

    Returns:
        A list of dicts with `id`, `full_name`, `email`, and
        `enrolled_at`. The `id` is the user's id — named `id` in the
        projection because that's what the client will key on, even
        though it's `users.id` under the hood.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                users.id,
                users.full_name,
                users.email,
                enrollments.enrolled_at
            FROM enrollments
            JOIN users ON users.id = enrollments.student_id
            WHERE enrollments.program_id = %(program_id)s
            ORDER BY enrollments.enrolled_at DESC, users.id
            """,
            {"program_id": program_id},
        )
        rows = await cur.fetchall()

    return list(rows)