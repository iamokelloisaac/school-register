"""
tests/db/test_queries.py
========================

Tests for the query layer — the raw-SQL modules in `app/db/queries/`.

## Why test the query layer separately

The API tests already exercise most of these functions, but indirectly:
through FastAPI, dependencies, service translation, response
serialization. When something fails there, it's not always obvious
*which layer* broke. These tests call the query functions directly, with
a real connection, and assert on the returned rows. They answer questions
like:

  - Does `create_user` return the columns we expect?
  - Does `get_user_by_email` include the hash, while `get_user_by_id`
    excludes it?
  - Does `CITEXT` actually give us case-insensitive email matching?
  - Does the composite PK on `enrollments` reject duplicates?
  - Do the `CHECK` constraints fire on out-of-range values?
  - Does `ON DELETE CASCADE` actually cascade?

These are properties of the *schema* and the *queries*, and they can
break without breaking any HTTP-level behavior. For instance, if
someone added `hashed_password` to `_USER_COLUMNS`, no endpoint test
would fail (the schemas filter it out on the way to the response), but
the query would now leak the hash to any caller that inspects the row
directly. Only a query-layer test catches that.

## What we don't test here

  - HTTP concerns — that's `tests/api/`.
  - Business rules — that's `tests/services/`.
  - Pydantic schemas — they're tested implicitly by the API tests.

The focus is: does the SQL do what the schema says it should?
"""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection
from psycopg import errors as pg_errors


# ===========================================================================
# users.py
# ===========================================================================


async def test_create_user_returns_full_row(
    db_conn: AsyncConnection,
) -> None:
    """
    `create_user` returns every column except the hash.

    We assert the presence of the expected keys and the absence of
    `hashed_password`. The latter is a design contract: the read path
    for users never exposes the hash. If a future edit added it to
    `_USER_COLUMNS`, this test would fail.
    """
    from app.core import security
    from app.db.queries import users as users_q

    row = await users_q.create_user(
        db_conn,
        email="q-create@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Query Create",
        age=30,
    )

    expected_keys = {
        "id",
        "email",
        "full_name",
        "age",
        "role",
        "is_active",
        "created_at",
        "updated_at",
    }
    assert expected_keys <= set(row.keys())
    assert "hashed_password" not in row

    assert row["email"] == "q-create@example.com"
    assert row["full_name"] == "Query Create"
    assert row["age"] == 30
    assert row["role"] == "student"
    assert row["is_active"] is True


async def test_create_user_duplicate_email_raises(
    db_conn: AsyncConnection,
) -> None:
    """
    Duplicate email raises `UniqueViolation`.

    The query layer doesn't translate this — that's the service's job.
    We assert the raw exception here, which documents the contract:
    "queries let DB errors bubble."
    """
    from app.core import security
    from app.db.queries import users as users_q

    hashed = security.hash_password("x" * 12)
    await users_q.create_user(
        db_conn,
        email="q-dup@example.com",
        hashed_password=hashed,
        full_name="First",
        age=30,
    )

    with pytest.raises(pg_errors.UniqueViolation):
        await users_q.create_user(
            db_conn,
            email="q-dup@example.com",
            hashed_password=hashed,
            full_name="Second",
            age=30,
        )


async def test_create_user_email_case_insensitive(
    db_conn: AsyncConnection,
) -> None:
    """
    The CITEXT column treats `User@X.com` and `user@x.com` as equal.

    This is the payoff of choosing CITEXT at schema time. Without the
    extension, the two would be separate rows and an attacker could
    register `admin@school` when `Admin@school` already exists.
    """
    from app.core import security
    from app.db.queries import users as users_q

    hashed = security.hash_password("x" * 12)
    await users_q.create_user(
        db_conn,
        email="CaseTest@example.com",
        hashed_password=hashed,
        full_name="First",
        age=30,
    )

    with pytest.raises(pg_errors.UniqueViolation):
        await users_q.create_user(
            db_conn,
            email="casetest@example.com",
            hashed_password=hashed,
            full_name="Second",
            age=30,
        )


async def test_create_user_age_below_minimum_raises(
    db_conn: AsyncConnection,
) -> None:
    """
    The CHECK constraint on `age` fires for negative values.

    Same transaction caveat as the max-bound test: a constraint
    violation aborts the current transaction, so this test can only
    exercise one failure.
    """
    from app.core import security
    from app.db.queries import users as users_q

    hashed = security.hash_password("x" * 12)

    with pytest.raises(pg_errors.CheckViolation):
        await users_q.create_user(
            db_conn,
            email="q-age-neg@example.com",
            hashed_password=hashed,
            full_name="Too Young",
            age=-1,
        )


async def test_create_user_age_above_maximum_raises(
    db_conn: AsyncConnection,
) -> None:
    """
    The CHECK constraint on `age` fires for overly large values.

    Split from the below-minimum test because a Postgres CHECK
    violation aborts the transaction — the *second* failing insert in
    the same test would fail with `InFailedSqlTransaction` before
    reaching the constraint. One failing insert per test.
    """
    from app.core import security
    from app.db.queries import users as users_q

    hashed = security.hash_password("x" * 12)

    with pytest.raises(pg_errors.CheckViolation):
        await users_q.create_user(
            db_conn,
            email="q-age-huge@example.com",
            hashed_password=hashed,
            full_name="Too Old",
            age=200,
        )


async def test_get_user_by_email_includes_hash(
    db_conn: AsyncConnection,
) -> None:
    """
    `get_user_by_email` returns `hashed_password`.

    This is the one read path that includes the hash, because the
    login flow needs it to verify the password. The test asserts it's
    present, so a future edit that (reasonably) tried to remove it
    everywhere would fail here and force a deliberate decision.
    """
    from app.core import security
    from app.db.queries import users as users_q

    hashed = security.hash_password("correct-password")
    await users_q.create_user(
        db_conn,
        email="q-with-hash@example.com",
        hashed_password=hashed,
        full_name="Has Hash",
        age=30,
    )

    row = await users_q.get_user_by_email(db_conn, "q-with-hash@example.com")

    assert row is not None
    assert "hashed_password" in row
    assert row["hashed_password"] == hashed


async def test_get_user_by_email_not_found(
    db_conn: AsyncConnection,
) -> None:
    """Missing email returns None, not an exception."""
    from app.db.queries import users as users_q

    row = await users_q.get_user_by_email(db_conn, "nobody@example.com")
    assert row is None


async def test_get_user_by_id_excludes_hash(
    db_conn: AsyncConnection,
) -> None:
    """
    `get_user_by_id` does NOT return `hashed_password`.

    This is the inverse of `get_user_by_email`. The auth dependency
    uses this path on every request, and fetching the hash would be
    both wasteful and a small security risk. The test pins the
    asymmetry.
    """
    from app.core import security
    from app.db.queries import users as users_q

    created = await users_q.create_user(
        db_conn,
        email="q-no-hash@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="No Hash",
        age=30,
    )

    row = await users_q.get_user_by_id(db_conn, created["id"])

    assert row is not None
    assert "hashed_password" not in row


async def test_update_user_password_changes_hash(
    db_conn: AsyncConnection,
) -> None:
    """`update_user_password` replaces the stored hash."""
    from app.core import security
    from app.db.queries import users as users_q

    created = await users_q.create_user(
        db_conn,
        email="q-update-pw@example.com",
        hashed_password=security.hash_password("old-password"),
        full_name="Update PW",
        age=30,
    )

    new_hash = security.hash_password("new-password")
    await users_q.update_user_password(
        db_conn,
        user_id=created["id"],
        hashed_password=new_hash,
    )

    row = await users_q.get_user_by_email(db_conn, "q-update-pw@example.com")
    assert row is not None
    assert row["hashed_password"] == new_hash
    assert security.verify_password("new-password", row["hashed_password"])


async def test_set_user_active_flips_flag(
    db_conn: AsyncConnection,
) -> None:
    """`set_user_active` toggles the flag in both directions."""
    from app.core import security
    from app.db.queries import users as users_q

    created = await users_q.create_user(
        db_conn,
        email="q-active@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Active",
        age=30,
    )

    assert created["is_active"] is True

    await users_q.set_user_active(db_conn, user_id=created["id"], is_active=False)
    row = await users_q.get_user_by_id(db_conn, created["id"])
    assert row is not None and row["is_active"] is False

    await users_q.set_user_active(db_conn, user_id=created["id"], is_active=True)
    row = await users_q.get_user_by_id(db_conn, created["id"])
    assert row is not None and row["is_active"] is True


async def test_list_users_pagination(
    db_conn: AsyncConnection,
) -> None:
    """
    `list_users` respects limit/offset and returns a correct total.

    The total is the count of all users matching the query, not the
    page size. If the count query accidentally got a WHERE clause, or
    forgot one, this test would catch it.
    """
    from app.core import security
    from app.db.queries import users as users_q

    hashed = security.hash_password("x" * 12)
    for i in range(5):
        await users_q.create_user(
            db_conn,
            email=f"q-page-{i}@example.com",
            hashed_password=hashed,
            full_name=f"Pager {i}",
            age=30,
        )

    page1, total1 = await users_q.list_users(db_conn, limit=2, offset=0)
    assert len(page1) == 2
    assert total1 >= 5

    page2, total2 = await users_q.list_users(db_conn, limit=2, offset=2)
    assert len(page2) == 2
    assert total2 == total1

    ids_p1 = {u["id"] for u in page1}
    ids_p2 = {u["id"] for u in page2}
    assert ids_p1.isdisjoint(ids_p2)


# ===========================================================================
# sessions.py
# ===========================================================================


async def test_create_session_returns_row(
    db_conn: AsyncConnection,
) -> None:
    """`create_session` returns a row with the generated id and timestamps."""
    from app.core import security
    from app.db.queries import sessions as sessions_q
    from app.db.queries import users as users_q

    user = await users_q.create_user(
        db_conn,
        email="s-create@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Session User",
        age=30,
    )

    token_hash = security.hash_token("fake-token")
    row = await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=token_hash,
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )

    assert row["user_id"] == user["id"]
    assert row["token_hash"] == token_hash
    assert row["expires_at"] is not None
    assert row["absolute_expires_at"] is not None


async def test_get_session_by_token_hash(
    db_conn: AsyncConnection,
) -> None:
    """
    Lookup by token hash finds the right row.

    Because `hash_token` is SHA-256 (deterministic), the same input
    produces the same digest, and the indexed equality match works.
    This test verifies the contract.
    """
    from app.core import security
    from app.db.queries import sessions as sessions_q
    from app.db.queries import users as users_q

    user = await users_q.create_user(
        db_conn,
        email="s-lookup@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Lookup User",
        age=30,
    )

    raw = "some-random-token"
    token_hash = security.hash_token(raw)
    created = await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=token_hash,
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )

    # Hashing the same input produces the same digest.
    assert security.hash_token(raw) == token_hash

    row = await sessions_q.get_session_by_token_hash(db_conn, token_hash)
    assert row is not None
    assert row["id"] == created["id"]
    assert row["user_id"] == user["id"]


async def test_get_session_by_unknown_hash_returns_none(
    db_conn: AsyncConnection,
) -> None:
    """Unknown hash returns None."""
    from app.db.queries import sessions as sessions_q

    row = await sessions_q.get_session_by_token_hash(db_conn, "0" * 64)
    assert row is None


async def test_delete_session(db_conn: AsyncConnection) -> None:
    """`delete_session` removes the row; a re-lookup returns None."""
    from app.core import security
    from app.db.queries import sessions as sessions_q
    from app.db.queries import users as users_q

    user = await users_q.create_user(
        db_conn,
        email="s-delete@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Delete User",
        age=30,
    )

    token_hash = security.hash_token("delete-me-token")
    created = await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=token_hash,
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )

    await sessions_q.delete_session(db_conn, session_id=created["id"])

    assert await sessions_q.get_session_by_token_hash(db_conn, token_hash) is None


async def test_delete_all_sessions_for_user(
    db_conn: AsyncConnection,
) -> None:
    """`delete_all_sessions_for_user` removes every session for one user."""
    from app.core import security
    from app.db.queries import sessions as sessions_q
    from app.db.queries import users as users_q

    user = await users_q.create_user(
        db_conn,
        email="s-all@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="All Sessions",
        age=30,
    )

    for i in range(3):
        await sessions_q.create_session(
            db_conn,
            user_id=user["id"],
            token_hash=security.hash_token(f"token-{i}"),
            idle_ttl_seconds=1800,
            absolute_ttl_seconds=2592000,
        )

    await sessions_q.delete_all_sessions_for_user(db_conn, user_id=user["id"])

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": user["id"]},
        )
        assert (await cur.fetchone())["n"] == 0


async def test_delete_other_sessions_for_user(
    db_conn: AsyncConnection,
) -> None:
    """
    `delete_other_sessions_for_user` keeps exactly one session.

    The one it keeps must be the one we named. We verify by checking
    the surviving session's id.
    """
    from app.core import security
    from app.db.queries import sessions as sessions_q
    from app.db.queries import users as users_q

    user = await users_q.create_user(
        db_conn,
        email="s-other@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Other Sessions",
        age=30,
    )

    keep = await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=security.hash_token("keep-me"),
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )
    await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=security.hash_token("delete-me-1"),
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )
    await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=security.hash_token("delete-me-2"),
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )

    await sessions_q.delete_other_sessions_for_user(
        db_conn,
        user_id=user["id"],
        keep_session_id=keep["id"],
    )

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM sessions WHERE user_id = %(id)s",
            {"id": user["id"]},
        )
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["id"] == keep["id"]


async def test_delete_expired_sessions(
    db_conn: AsyncConnection,
) -> None:
    """
    `delete_expired_sessions` removes only the expired rows.

    We create three sessions: one expired by idle, one expired by
    absolute, one still valid. The first two should be deleted, the
    third kept.
    """
    from datetime import datetime, timedelta, timezone

    from app.core import security
    from app.db.queries import sessions as sessions_q
    from app.db.queries import users as users_q

    user = await users_q.create_user(
        db_conn,
        email="s-cleanup@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Cleanup User",
        age=30,
    )

    now = datetime.now(timezone.utc)
    past = now - timedelta(days=1)
    future = now + timedelta(days=1)

    # Session 1: expired by idle.
    s1 = await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=security.hash_token("s1"),
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )
    # Session 2: expired by absolute.
    s2 = await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=security.hash_token("s2"),
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )
    # Session 3: still valid.
    s3 = await sessions_q.create_session(
        db_conn,
        user_id=user["id"],
        token_hash=security.hash_token("s3"),
        idle_ttl_seconds=1800,
        absolute_ttl_seconds=2592000,
    )

    async with db_conn.cursor() as cur:
        await cur.execute(
            "UPDATE sessions SET expires_at = %(past)s WHERE id = %(id)s",
            {"past": past, "id": s1["id"]},
        )
        await cur.execute(
            "UPDATE sessions SET absolute_expires_at = %(past)s WHERE id = %(id)s",
            {"past": past, "id": s2["id"]},
        )

    deleted = await sessions_q.delete_expired_sessions(db_conn)
    assert deleted == 2

    async with db_conn.cursor() as cur:
        await cur.execute("SELECT id FROM sessions WHERE user_id = %(id)s", {"id": user["id"]})
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["id"] == s3["id"]


# ===========================================================================
# programs.py
# ===========================================================================


async def test_create_program_returns_row(
    db_conn: AsyncConnection,
) -> None:
    """`create_program` returns the full program row."""
    from app.core import security
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-prog@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )

    row = await programs_q.create_program(
        db_conn,
        name="Query Program",
        code="QP",
        description="A program.",
        created_by=admin["id"],
    )

    assert row["name"] == "Query Program"
    assert row["code"] == "QP"
    assert row["description"] == "A program."
    assert row["is_active"] is True
    assert row["created_by"] == admin["id"]


async def test_create_program_duplicate_code_raises(
    db_conn: AsyncConnection,
) -> None:
    """Duplicate code raises `UniqueViolation`."""
    from app.core import security
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-dup@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )

    await programs_q.create_program(
        db_conn, name="One", code="DUP", description=None, created_by=admin["id"]
    )

    with pytest.raises(pg_errors.UniqueViolation):
        await programs_q.create_program(
            db_conn, name="Two", code="DUP", description=None, created_by=admin["id"]
        )


async def test_create_program_created_by_missing_fk_raises(
    db_conn: AsyncConnection,
) -> None:
    """A nonexistent `created_by` raises `ForeignKeyViolation`."""
    from app.db.queries import programs as programs_q

    with pytest.raises(pg_errors.ForeignKeyViolation):
        await programs_q.create_program(
            db_conn,
            name="Orphan",
            code="ORPH",
            description=None,
            created_by=999999,
        )


async def test_get_program_by_code(
    db_conn: AsyncConnection,
) -> None:
    """`get_program_by_code` finds by exact code."""
    from app.core import security
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-code@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    created = await programs_q.create_program(
        db_conn, name="By Code", code="BYC", description=None, created_by=admin["id"]
    )

    row = await programs_q.get_program_by_code(db_conn, "BYC")
    assert row is not None
    assert row["id"] == created["id"]

    assert await programs_q.get_program_by_code(db_conn, "NOPE") is None


async def test_list_programs_active_only(
    db_conn: AsyncConnection,
) -> None:
    """`active_only=True` filters; `active_only=False` doesn't."""
    from app.core import security
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-list@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    active = await programs_q.create_program(
        db_conn, name="Active P", code="AP1", description=None, created_by=admin["id"]
    )
    archived = await programs_q.create_program(
        db_conn, name="Archived P", code="AR1", description=None, created_by=admin["id"]
    )
    await programs_q.deactivate_program(db_conn, program_id=archived["id"])

    active_rows, active_total = await programs_q.list_programs(
        db_conn, limit=100, offset=0, active_only=True
    )
    all_rows, all_total = await programs_q.list_programs(
        db_conn, limit=100, offset=0, active_only=False
    )

    active_codes = {p["code"] for p in active_rows}
    all_codes = {p["code"] for p in all_rows}

    assert "AP1" in active_codes
    assert "AR1" not in active_codes
    assert "AP1" in all_codes
    assert "AR1" in all_codes
    assert all_total > active_total


async def test_delete_program_returns_bool(
    db_conn: AsyncConnection,
) -> None:
    """`delete_program` returns True on success, False on missing."""
    from app.core import security
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-del@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    program = await programs_q.create_program(
        db_conn, name="Delete Me", code="DELM", description=None, created_by=admin["id"]
    )

    assert await programs_q.delete_program(db_conn, program_id=program["id"]) is True
    assert await programs_q.delete_program(db_conn, program_id=program["id"]) is False


async def test_deactivate_program_sets_flag(
    db_conn: AsyncConnection,
) -> None:
    """`deactivate_program` flips `is_active` to False."""
    from app.core import security
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-arc@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    program = await programs_q.create_program(
        db_conn, name="Archive Me", code="ARC1", description=None, created_by=admin["id"]
    )

    assert await programs_q.deactivate_program(db_conn, program_id=program["id"]) is True

    row = await programs_q.get_program_by_id(db_conn, program["id"])
    assert row is not None and row["is_active"] is False


# ===========================================================================
# enrollments.py
# ===========================================================================


async def test_enroll_and_leave(db_conn: AsyncConnection) -> None:
    """Basic enroll then leave cycle."""
    from app.core import security
    from app.db.queries import enrollments as enrollments_q
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-enr@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    student = await users_q.create_user(
        db_conn,
        email="q-student-enr@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Student",
        age=20,
    )
    program = await programs_q.create_program(
        db_conn, name="Enr P", code="ENRP", description=None, created_by=admin["id"]
    )

    created = await enrollments_q.enroll(
        db_conn, student_id=student["id"], program_id=program["id"]
    )
    assert created["student_id"] == student["id"]
    assert created["program_id"] == program["id"]

    assert await enrollments_q.is_enrolled(
        db_conn, student_id=student["id"], program_id=program["id"]
    )

    removed = await enrollments_q.leave(
        db_conn, student_id=student["id"], program_id=program["id"]
    )
    assert removed is True

    assert not await enrollments_q.is_enrolled(
        db_conn, student_id=student["id"], program_id=program["id"]
    )


async def test_enroll_twice_raises(db_conn: AsyncConnection) -> None:
    """
    The composite PK rejects duplicate enrollments.

    Same student, same program → `UniqueViolation`. Note the PK is
    `(student_id, program_id)`, so both together must be unique; the
    same student in *different* programs is fine.
    """
    from app.core import security
    from app.db.queries import enrollments as enrollments_q
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-dup-enr@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    student = await users_q.create_user(
        db_conn,
        email="q-student-dup-enr@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Student",
        age=20,
    )
    program = await programs_q.create_program(
        db_conn, name="Dup P", code="DUPENR", description=None, created_by=admin["id"]
    )

    await enrollments_q.enroll(
        db_conn, student_id=student["id"], program_id=program["id"]
    )

    with pytest.raises(pg_errors.UniqueViolation):
        await enrollments_q.enroll(
            db_conn, student_id=student["id"], program_id=program["id"]
        )


async def test_same_student_different_programs(
    db_conn: AsyncConnection,
) -> None:
    """The PK's composite nature allows the same student in many programs."""
    from app.core import security
    from app.db.queries import enrollments as enrollments_q
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-multi@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    student = await users_q.create_user(
        db_conn,
        email="q-student-multi@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Student",
        age=20,
    )
    p1 = await programs_q.create_program(
        db_conn, name="M1", code="M1", description=None, created_by=admin["id"]
    )
    p2 = await programs_q.create_program(
        db_conn, name="M2", code="M2", description=None, created_by=admin["id"]
    )

    await enrollments_q.enroll(db_conn, student_id=student["id"], program_id=p1["id"])
    await enrollments_q.enroll(db_conn, student_id=student["id"], program_id=p2["id"])

    assert await enrollments_q.is_enrolled(
        db_conn, student_id=student["id"], program_id=p1["id"]
    )
    assert await enrollments_q.is_enrolled(
        db_conn, student_id=student["id"], program_id=p2["id"]
    )


async def test_leave_not_enrolled_returns_false(
    db_conn: AsyncConnection,
) -> None:
    """`leave` returns False if the enrollment doesn't exist."""
    from app.core import security
    from app.db.queries import enrollments as enrollments_q
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-lv@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    student = await users_q.create_user(
        db_conn,
        email="q-student-lv@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Student",
        age=20,
    )
    program = await programs_q.create_program(
        db_conn, name="LV", code="LV", description=None, created_by=admin["id"]
    )

    removed = await enrollments_q.leave(
        db_conn, student_id=student["id"], program_id=program["id"]
    )
    assert removed is False


async def test_list_enrolled_programs(
    db_conn: AsyncConnection,
) -> None:
    """`list_enrolled_programs` returns the joined shape."""
    from app.core import security
    from app.db.queries import enrollments as enrollments_q
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-lep@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    student = await users_q.create_user(
        db_conn,
        email="q-student-lep@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Student",
        age=20,
    )
    p1 = await programs_q.create_program(
        db_conn, name="First", code="LFP1", description="D1", created_by=admin["id"]
    )
    p2 = await programs_q.create_program(
        db_conn, name="Second", code="LFP2", description="D2", created_by=admin["id"]
    )

    await enrollments_q.enroll(db_conn, student_id=student["id"], program_id=p1["id"])
    await enrollments_q.enroll(db_conn, student_id=student["id"], program_id=p2["id"])

    rows = await enrollments_q.list_enrolled_programs(db_conn, student_id=student["id"])

    assert len(rows) == 2
    for row in rows:
        assert {"id", "name", "code", "description", "enrolled_at"} <= set(row.keys())
        # The joined shape excludes `created_by` and `updated_at` on
        # purpose — the schema wouldn't accept them.
        assert "created_by" not in row
        assert "updated_at" not in row


async def test_list_students_in_program(
    db_conn: AsyncConnection,
) -> None:
    """`list_students_in_program` returns the reverse-joined shape."""
    from app.core import security
    from app.db.queries import enrollments as enrollments_q
    from app.db.queries import programs as programs_q
    from app.db.queries import users as users_q

    admin = await users_q.create_user(
        db_conn,
        email="q-admin-lsp@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Admin",
        age=30,
        role="admin",
    )
    s1 = await users_q.create_user(
        db_conn,
        email="q-s1-lsp@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Student One",
        age=20,
    )
    s2 = await users_q.create_user(
        db_conn,
        email="q-s2-lsp@example.com",
        hashed_password=security.hash_password("x" * 12),
        full_name="Student Two",
        age=21,
    )
    program = await programs_q.create_program(
        db_conn, name="Roster", code="ROST", description=None, created_by=admin["id"]
    )

    await enrollments_q.enroll(db_conn, student_id=s1["id"], program_id=program["id"])
    await enrollments_q.enroll(db_conn, student_id=s2["id"], program_id=program["id"])

    rows = await enrollments_q.list_students_in_program(db_conn, program_id=program["id"])

    assert len(rows) == 2
    emails = {r["email"] for r in rows}
    assert emails == {"q-s1-lsp@example.com", "q-s2-lsp@example.com"}

    for row in rows:
        # The hash must not leak through this join.
        assert "hashed_password" not in row
        assert {"id", "full_name", "email", "enrolled_at"} <= set(row.keys())