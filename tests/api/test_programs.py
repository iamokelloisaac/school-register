"""
tests/api/test_programs.py
==========================

Tests for the student-facing program endpoints:

    GET    /programs                  list active programs
    GET    /programs/{id}             program detail
    POST   /programs/{id}/enroll      enroll current user
    DELETE /programs/{id}/enroll      leave current user

## What's interesting in this file

The enroll/leave flow has more rules than anything else in the app:

  - You can't enroll in a program you're already enrolled in (409).
  - You can't enroll in an archived program (409).
  - You can't enroll in a program that doesn't exist (404).
  - You can't leave a program you're not enrolled in (404).
  - Enroll and leave always operate on *the current user*, never on
    anyone else, even if the caller is an admin.

Each of these gets a test. The identity-boundary rule — enroll/leave
target the caller — gets two, because it's the kind of bug that would
be catastrophic in production (a user enrolling another user, or
leaving on their behalf) and easy to introduce accidentally.

## The pagination tests

`GET /programs` is paginated with the standard envelope. We test the
happy path (returns items + total) and the bounds (limit is clamped,
offset is clamped) at a high level. The exact pagination algorithm is
tested once, here, since every other paginated endpoint reuses the
same `build_page` helper.

## The is_active asymmetry

`GET /programs` filters by `is_active = TRUE` — archived programs are
invisible in the public catalog. `GET /programs/{id}` does *not*
filter — you can still fetch an archived program by id. This
asymmetry is deliberate (see the router docstrings), and one test
pins each half.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from psycopg import AsyncConnection


# ===========================================================================
# GET /programs — catalog listing
# ===========================================================================


async def test_list_programs_empty(
    client: AsyncClient, create_user
) -> None:
    """
    No programs → empty page, total 0, correct envelope.

    The response shape is `{items: [], total: 0, limit, offset}` — not
    a bare `[]`. Pinning the envelope here means any future change to
    the pagination response shape has to be deliberate.
    """
    user = await create_user(email="lister@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get("/api/v1/programs")
    assert response.status_code == 200

    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 20  # default
    assert body["offset"] == 0


async def test_list_programs_returns_active_only(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Archived programs are hidden from the public catalog.

    We create three programs, archive one, and verify only the two
    active ones appear. The archived program is still in the DB — it
    just isn't listed. This is what "soft delete" means for this
    endpoint.
    """
    from app.db.queries import programs as programs_q

    admin = await create_user(email="admin-list@example.com", role="admin")
    p_active = await create_program(name="Active", code="ACT", created_by=admin["id"])
    p_archived = await create_program(name="Archived", code="ARC", created_by=admin["id"])

    await programs_q.deactivate_program(db_conn, program_id=p_archived["id"])

    user = await create_user(email="viewer@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get("/api/v1/programs")
    assert response.status_code == 200

    body = response.json()
    codes = {p["code"] for p in body["items"]}

    assert codes == {"ACT"}
    # `total` reflects the *filtered* count, not the raw table count.
    # A pagination bug that counted all rows would show `2` here.
    assert body["total"] == 1


async def test_list_programs_pagination(
    client: AsyncClient, create_user, create_program
) -> None:
    """
    Pagination returns the right slice and the right total.

    Five programs, page size 2. First page has 2 items, total is 5.
    Second page (offset=2) has the next 2. This verifies both that the
    LIMIT/OFFSET work and that `total` counts the *whole* set, not just
    the page.
    """
    admin = await create_user(email="admin-page@example.com", role="admin")
    for i in range(5):
        await create_program(name=f"Prog {i}", code=f"PG{i}", created_by=admin["id"])

    user = await create_user(email="pager@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    page1 = await client.get("/api/v1/programs?limit=2&offset=0")
    assert page1.status_code == 200
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["total"] == 5
    assert body1["limit"] == 2
    assert body1["offset"] == 0

    page2 = await client.get("/api/v1/programs?limit=2&offset=2")
    body2 = page2.json()
    assert len(body2["items"]) == 2
    assert body2["total"] == 5
    assert body2["offset"] == 2

    # The two pages must not overlap. Since ordering is by id, page 1
    # has the lower ids, page 2 the higher.
    ids_p1 = {p["id"] for p in body1["items"]}
    ids_p2 = {p["id"] for p in body2["items"]}
    assert ids_p1.isdisjoint(ids_p2)


@pytest.mark.parametrize(
    "query,expected_status",
    [
        ("limit=0", 422),      # below min
        ("limit=101", 422),    # above max
        ("offset=-1", 422),    # negative
        ("limit=abc", 422),    # not an int
    ],
)
async def test_list_programs_pagination_bounds(
    client: AsyncClient, create_user, query: str, expected_status: int
) -> None:
    """
    Out-of-bounds or malformed pagination params → 422.

    The `Pagination` schema declares bounds; Pydantic enforces them
    before the handler runs. This is where the bounds live, and these
    tests document them. If someone loosens a bound, the corresponding
    case fails and the change has to be deliberate.
    """
    user = await create_user(email=f"bounds-{query.replace('=', '-')}@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get(f"/api/v1/programs?{query}")
    assert response.status_code == expected_status


async def test_list_programs_requires_authentication(
    client: AsyncClient,
) -> None:
    """No session → 401. The catalog isn't public."""
    response = await client.get("/api/v1/programs")
    assert response.status_code == 401


# ===========================================================================
# GET /programs/{id} — detail
# ===========================================================================


async def test_get_program_success(
    client: AsyncClient, create_user, create_program
) -> None:
    """Fetch a program by id returns its full detail."""
    admin = await create_user(email="admin-detail@example.com", role="admin")
    program = await create_program(
        name="Chemistry",
        code="CHM",
        description="Atoms and stuff.",
        created_by=admin["id"],
    )

    user = await create_user(email="reader@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get(f"/api/v1/programs/{program['id']}")
    assert response.status_code == 200

    body = response.json()
    assert body["id"] == program["id"]
    assert body["name"] == "Chemistry"
    assert body["code"] == "CHM"
    assert body["description"] == "Atoms and stuff."
    assert body["is_active"] is True


async def test_get_program_not_found(client: AsyncClient, create_user) -> None:
    """
    Nonexistent id → 404 with our error shape.

    We don't distinguish "never existed" from "was hard-deleted" — both
    produce the same 404. If we later want to distinguish, we'd add a
    tombstone table, but that's beyond the current scope.
    """
    user = await create_user(email="missing@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get("/api/v1/programs/999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "program_not_found"


async def test_get_program_returns_archived(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    The detail endpoint returns archived programs.

    This is the *other half* of the is_active asymmetry. The catalog
    filters; the detail view doesn't. A student who was enrolled in a
    program that just got archived should still be able to view it,
    and an admin clicking through from a list should see what they
    archived.
    """
    from app.db.queries import programs as programs_q

    admin = await create_user(email="admin-arch@example.com", role="admin")
    program = await create_program(name="Retired", code="RET", created_by=admin["id"])
    await programs_q.deactivate_program(db_conn, program_id=program["id"])

    user = await create_user(email="viewer-arch@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.get(f"/api/v1/programs/{program['id']}")
    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_get_program_requires_authentication(
    client: AsyncClient, create_user, create_program
) -> None:
    """No session → 401."""
    admin = await create_user(email="admin-auth@example.com", role="admin")
    program = await create_program(code="AUTH", created_by=admin["id"])

    response = await client.get(f"/api/v1/programs/{program['id']}")
    assert response.status_code == 401


# ===========================================================================
# POST /programs/{id}/enroll
# ===========================================================================


async def test_enroll_success(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Enrolling in an active program creates an enrollment row.

    We verify both the response (201, correct body) and the DB row's
    existence. The response could be correct even if the insert failed
    silently — checking the DB closes that gap.
    """
    admin = await create_user(email="admin-enroll@example.com", role="admin")
    program = await create_program(code="ENR1", created_by=admin["id"])

    user = await create_user(email="enrollee@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.post(f"/api/v1/programs/{program['id']}/enroll")
    assert response.status_code == 201

    body = response.json()
    assert body["student_id"] == user["id"]
    assert body["program_id"] == program["id"]
    assert "enrolled_at" in body

    # Confirm the row exists in the DB.
    async with db_conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1 FROM enrollments
            WHERE student_id = %(s)s AND program_id = %(p)s
            """,
            {"s": user["id"], "p": program["id"]},
        )
        assert await cur.fetchone() is not None


async def test_enroll_twice_returns_409(
    client: AsyncClient, create_user, create_program
) -> None:
    """
    Enrolling twice → 409 `already_enrolled`.

    The composite PK on `enrollments` rejects the second insert at the
    DB level; the service translates the `UniqueViolation` into a
    `Conflict`. This test verifies the translation runs.
    """
    admin = await create_user(email="admin-dup@example.com", role="admin")
    program = await create_program(code="DUP1", created_by=admin["id"])

    user = await create_user(email="dupuser@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    r1 = await client.post(f"/api/v1/programs/{program['id']}/enroll")
    assert r1.status_code == 201

    r2 = await client.post(f"/api/v1/programs/{program['id']}/enroll")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "already_enrolled"


async def test_enroll_nonexistent_program_returns_404(
    client: AsyncClient, create_user
) -> None:
    """
    Enrolling in a program that doesn't exist → 404.

    The service checks the program first and raises `NotFound`. This
    check exists specifically so that "archived" and "nonexistent"
    produce different errors, which requires distinguishing them
    before the insert.
    """
    user = await create_user(email="ghost@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.post("/api/v1/programs/999999/enroll")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "program_not_found"


async def test_enroll_archived_program_returns_409(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Enrolling in an archived program → 409 `program_inactive`.

    Distinct from "already enrolled" — same status code, different
    error code. A client can tell the difference and respond
    appropriately ("this program isn't accepting students" vs "you're
    already in it").
    """
    from app.db.queries import programs as programs_q

    admin = await create_user(email="admin-arch-enroll@example.com", role="admin")
    program = await create_program(code="ARC1", created_by=admin["id"])
    await programs_q.deactivate_program(db_conn, program_id=program["id"])

    user = await create_user(email="arch-enroller@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.post(f"/api/v1/programs/{program['id']}/enroll")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "program_inactive"


async def test_enroll_targets_current_user(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Enroll always targets the caller, never anyone else.

    This is the identity-boundary test for enroll. There is no path
    parameter for "which student to enroll" — the endpoint has no way
    to enroll someone else. We verify by having Alice enroll and
    confirming the enrollment row is *hers*, not Bob's.

    A bug where enroll accidentally used a stale or global user would
    be caught here.
    """
    admin = await create_user(email="admin-id-enroll@example.com", role="admin")
    program = await create_program(code="IDEN", created_by=admin["id"])

    alice = await create_user(email="alice-enroll@example.com")
    bob = await create_user(email="bob-enroll@example.com")

    await client.post(
        "/api/v1/auth/login",
        json={"email": alice["email"], "password": "test-password"},
    )
    await client.post(f"/api/v1/programs/{program['id']}/enroll")

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT student_id FROM enrollments WHERE program_id = %(p)s",
            {"p": program["id"]},
        )
        rows = await cur.fetchall()

    student_ids = {r["student_id"] for r in rows}
    assert student_ids == {alice["id"]}
    assert bob["id"] not in student_ids


async def test_enroll_requires_authentication(
    client: AsyncClient, create_user, create_program
) -> None:
    """No session → 401."""
    admin = await create_user(email="admin-noauth@example.com", role="admin")
    program = await create_program(code="NOAU", created_by=admin["id"])

    response = await client.post(f"/api/v1/programs/{program['id']}/enroll")
    assert response.status_code == 401


# ===========================================================================
# DELETE /programs/{id}/enroll
# ===========================================================================


async def test_leave_success(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Leaving an enrolled program removes the row.

    We verify both the 204 and the DB row's absence. The 204 alone
    doesn't prove deletion — a buggy endpoint could return 204 without
    deleting.
    """
    admin = await create_user(email="admin-leave@example.com", role="admin")
    program = await create_program(code="LEAV", created_by=admin["id"])

    user = await create_user(email="leaver@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    await client.post(f"/api/v1/programs/{program['id']}/enroll")

    response = await client.delete(f"/api/v1/programs/{program['id']}/enroll")
    assert response.status_code == 204
    assert response.content == b""

    async with db_conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1 FROM enrollments
            WHERE student_id = %(s)s AND program_id = %(p)s
            """,
            {"s": user["id"], "p": program["id"]},
        )
        assert await cur.fetchone() is None


async def test_leave_not_enrolled_returns_404(
    client: AsyncClient, create_user, create_program
) -> None:
    """
    Leaving a program you're not enrolled in → 404 `not_enrolled`.

    Stricter than idempotent delete. The reasoning is in
    `enrollment_service.leave`: if the client sent a delete for
    something they believe exists, and it doesn't, that's a mistake
    worth surfacing.
    """
    admin = await create_user(email="admin-nl@example.com", role="admin")
    program = await create_program(code="NOLV", created_by=admin["id"])

    user = await create_user(email="notleaver@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.delete(f"/api/v1/programs/{program['id']}/enroll")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_enrolled"


async def test_leave_nonexistent_program_returns_404(
    client: AsyncClient, create_user
) -> None:
    """
    Leaving a program that doesn't exist → 404.

    Same error as "not enrolled" — the service can't distinguish, and
    it doesn't try. From the client's perspective, "you're not enrolled
    in that" is honest for both cases.
    """
    user = await create_user(email="leave-ghost@example.com")
    await client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "test-password"},
    )

    response = await client.delete("/api/v1/programs/999999/enroll")
    assert response.status_code == 404
    # Note: the code here is `not_enrolled`, not `program_not_found`.
    # That's because `leave` doesn't check the program exists — see
    # the service docstring.
    assert response.json()["error"]["code"] == "not_enrolled"


async def test_leave_only_affects_current_user(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Leaving removes *your* enrollment, not anyone else's.

    Alice and Bob both enroll in the same program. Alice leaves. Bob's
    enrollment must still exist.

    A bug where leave deleted by program_id alone (without filtering
    by student) would wipe every enrollment in the program — a
    catastrophic bug that this test catches.
    """
    admin = await create_user(email="admin-both@example.com", role="admin")
    program = await create_program(code="BOTH", created_by=admin["id"])

    alice = await create_user(email="alice-leave@example.com")
    bob = await create_user(email="bob-leave@example.com")

    # Directly insert both enrollments to avoid two clients.
    from app.db.queries import enrollments as enrollments_q

    await enrollments_q.enroll(db_conn, student_id=alice["id"], program_id=program["id"])
    await enrollments_q.enroll(db_conn, student_id=bob["id"], program_id=program["id"])

    # Log in as Alice and leave.
    await client.post(
        "/api/v1/auth/login",
        json={"email": alice["email"], "password": "test-password"},
    )
    response = await client.delete(f"/api/v1/programs/{program['id']}/enroll")
    assert response.status_code == 204

    # Bob's enrollment must survive.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT student_id FROM enrollments WHERE program_id = %(p)s",
            {"p": program["id"]},
        )
        rows = await cur.fetchall()

    student_ids = {r["student_id"] for r in rows}
    assert student_ids == {bob["id"]}


async def test_leave_requires_authentication(
    client: AsyncClient, create_user, create_program
) -> None:
    """No session → 401."""
    admin = await create_user(email="admin-leave-auth@example.com", role="admin")
    program = await create_program(code="LAUT", created_by=admin["id"])

    response = await client.delete(f"/api/v1/programs/{program['id']}/enroll")
    assert response.status_code == 401