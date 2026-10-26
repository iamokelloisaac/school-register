"""
tests/api/test_admin.py
=======================

Tests for the admin-only endpoints.

## What's interesting here

Three things distinguish admin tests from the others:

1. **Every route has two dimensions**: the action itself *and* the
   authorization check. A test for "admin can create a program" doesn't
   tell you anything about "student cannot create a program." Both need
   separate tests. We do both, for every route.

2. **The authorization test pattern is uniform.** For each admin route,
   we have one test that an admin succeeds and one that a student gets
   403. A small helper would be tempting, but the tests are more
   readable when written out — you can see at a glance what each one
   does.

3. **Admin is a role on a normal user.** There's no separate admin table
   or login flow. `create_user(role="admin")` produces an admin; the
   same login endpoint works. This means admin tests share all the
   infrastructure of regular user tests — no special setup.

## The identity question for admin operations

For most admin endpoints, the admin operates on *someone else* —
resetting another user's password, viewing another user's profile.
This is the inverse of the `/me/*` endpoints, where the caller is
always the target. The tests here verify that the admin can target
other users, and that the target is exactly who they said.

## What we're not testing

  - Program-level rules (already covered in `test_programs.py`).
  - Password hashing (covered in `test_auth.py`).
  - Query-layer behavior (covered in `tests/db/`).

The focus here is: does the admin route do what it says, and does it
reject non-admins?
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient, AsyncClient as HttpxClient
from psycopg import AsyncConnection


# ---------------------------------------------------------------------------
# Helper — a client logged in as an admin
# ---------------------------------------------------------------------------
#
# The `authenticated_client` fixture from `conftest.py` does exactly
# this, but it operates on the shared `client` fixture (only one
# session per client). For admin tests that also need a student client
# (to verify the 403), we build fresh clients inline. This helper
# reduces that boilerplate.


async def _login_as(
    email: str,
    password: str = "test-password",
) -> HttpxClient:
    """
    Build a fresh client, logged in as the given user.

    Returns the client with its own cookie jar. The caller is
    responsible for closing it (via `async with`), but in tests we let
    the fixture teardown handle it — the client is cheap and the test
    process ends soon after.
    """
    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    client = HttpxClient(transport=transport, base_url="http://test")
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, f"login failed: {response.text}"
    return client


# ===========================================================================
# GET /admin/students
# ===========================================================================


async def test_list_students_as_admin(client: AsyncClient, create_user) -> None:
    """
    Admin sees all users, paginated with the standard envelope.
    """
    await create_user(email="admin-ls@example.com", role="admin")
    await create_user(email="student1@example.com")
    await create_user(email="student2@example.com")

    admin_client = await _login_as("admin-ls@example.com")
    try:
        response = await admin_client.get("/api/v1/admin/students")
    finally:
        await admin_client.aclose()

    assert response.status_code == 200
    body = response.json()
    # At least three (the three we created); other tests may have left
    # rows behind if rollback ever failed, but rollback works — so
    # exactly the ones this test created plus any the fixture made.
    assert body["total"] >= 3
    emails = {u["email"] for u in body["items"]}
    assert "admin-ls@example.com" in emails
    assert "student1@example.com" in emails
    assert "student2@example.com" in emails


async def test_list_students_as_student_forbidden(
    client: AsyncClient, create_user
) -> None:
    """
    A student gets 403 on an admin route.

    This is the central authorization assertion. Note it's 403, not
    401 — the student *is* authenticated, they just lack permission.
    """
    await create_user(email="student-forbidden@example.com")

    student_client = await _login_as("student-forbidden@example.com")
    try:
        response = await student_client.get("/api/v1/admin/students")
    finally:
        await student_client.aclose()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_required"


async def test_list_students_requires_authentication(client: AsyncClient) -> None:
    """No session → 401, not 403."""
    response = await client.get("/api/v1/admin/students")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# ===========================================================================
# GET /admin/students/{id}
# ===========================================================================


async def test_get_student_as_admin(client: AsyncClient, create_user) -> None:
    """Admin fetches another user's profile."""
    await create_user(email="admin-gs@example.com", role="admin")
    target = await create_user(
        email="target@example.com",
        full_name="Target User",
        age=27,
    )

    admin_client = await _login_as("admin-gs@example.com")
    try:
        response = await admin_client.get(f"/api/v1/admin/students/{target['id']}")
    finally:
        await admin_client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == target["id"]
    assert body["email"] == "target@example.com"
    assert body["full_name"] == "Target User"
    # The hash must not leak into admin views either.
    assert "hashed_password" not in response.text


async def test_get_student_not_found(client: AsyncClient, create_user) -> None:
    """Nonexistent id → 404."""
    await create_user(email="admin-gs-404@example.com", role="admin")

    admin_client = await _login_as("admin-gs-404@example.com")
    try:
        response = await admin_client.get("/api/v1/admin/students/999999")
    finally:
        await admin_client.aclose()

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"


async def test_get_student_as_student_forbidden(
    client: AsyncClient, create_user
) -> None:
    """A student can't fetch arbitrary user profiles."""
    await create_user(email="curious@example.com")
    target = await create_user(email="private@example.com")

    student_client = await _login_as("curious@example.com")
    try:
        response = await student_client.get(f"/api/v1/admin/students/{target['id']}")
    finally:
        await student_client.aclose()

    assert response.status_code == 403


# ===========================================================================
# POST /admin/students
# ===========================================================================


async def test_create_student_as_admin(client: AsyncClient, create_user) -> None:
    """
    Admin can register a student on their behalf.

    The new account is a student, even though an admin created it —
    the role hardcoding in `auth_service.register` applies here too.
    """
    await create_user(email="admin-cs@example.com", role="admin")

    admin_client = await _login_as("admin-cs@example.com")
    try:
        response = await admin_client.post(
            "/api/v1/admin/students",
            json={
                "email": "newstudent@example.com",
                "password": "some-password-123",
                "full_name": "New Student",
                "age": 19,
            },
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "newstudent@example.com"
    assert body["role"] == "student"  # not admin, even though admin created
    assert body["is_active"] is True


async def test_create_student_duplicate_email(
    client: AsyncClient, create_user
) -> None:
    """Duplicate email → 409, same as self-registration."""
    await create_user(email="admin-cs-dup@example.com", role="admin")
    await create_user(email="existing-student@example.com")

    admin_client = await _login_as("admin-cs-dup@example.com")
    try:
        response = await admin_client.post(
            "/api/v1/admin/students",
            json={
                "email": "existing-student@example.com",
                "password": "some-password-123",
                "full_name": "Duplicate",
                "age": 20,
            },
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


async def test_create_student_as_student_forbidden(
    client: AsyncClient, create_user
) -> None:
    """A student cannot register other users."""
    await create_user(email="wannabe@example.com")

    student_client = await _login_as("wannabe@example.com")
    try:
        response = await student_client.post(
            "/api/v1/admin/students",
            json={
                "email": "another@example.com",
                "password": "some-password-123",
                "full_name": "Another",
                "age": 20,
            },
        )
    finally:
        await student_client.aclose()

    assert response.status_code == 403


# ===========================================================================
# POST /admin/students/{id}/password
# ===========================================================================


async def test_reset_password_as_admin(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    Admin resets a user's password, and the new password works.

    We verify by:
      - The response is 204.
      - The new password hashes against the stored hash.
      - The old password no longer works.

    The third check is the "reset actually took effect" assertion. A
    buggy implementation might return 204 without updating the hash.
    """
    from app.core import security

    await create_user(email="admin-rp@example.com", role="admin")
    target = await create_user(
        email="reset-target@example.com",
        password="old-password",
    )

    admin_client = await _login_as("admin-rp@example.com")
    try:
        response = await admin_client.post(
            f"/api/v1/admin/students/{target['id']}/password",
            json={"new_password": "brand-new-password"},
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 204

    # Check the hash directly.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT hashed_password FROM users WHERE id = %(id)s",
            {"id": target["id"]},
        )
        row = await cur.fetchone()

    assert security.verify_password("brand-new-password", row["hashed_password"])
    assert not security.verify_password("old-password", row["hashed_password"])


async def test_reset_password_revokes_target_sessions(
    client: AsyncClient, create_user, db_conn: AsyncConnection
) -> None:
    """
    Admin password reset revokes all of the target user's sessions.

    This is critical: without it, the target would remain logged in on
    whatever devices they had open, and the reset wouldn't actually
    take effect until those sessions expired naturally.

    The test:
      1. Creates a target user and logs them in (session A).
      2. Creates a second session for them directly (session B).
      3. Admin resets the password.
      4. Verifies zero sessions remain for the target.
    """
    from app.core import security
    from app.core.config import settings
    from app.db.queries import sessions as sessions_q

    await create_user(email="admin-rps@example.com", role="admin")
    target = await create_user(
        email="reset-target-2@example.com",
        password="old-password",
    )

    # Session A — target logs in.
    target_client = await _login_as(target["email"], "old-password")
    await target_client.aclose()

    # Session B — direct insert.
    other_token = security.generate_token()
    await sessions_q.create_session(
        db_conn,
        user_id=target["id"],
        token_hash=security.hash_token(other_token),
        idle_ttl_seconds=settings.SESSION_IDLE_TTL_SECONDS,
        absolute_ttl_seconds=settings.SESSION_ABSOLUTE_TTL_SECONDS,
    )

    # Sanity: two sessions exist.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": target["id"]},
        )
        assert (await cur.fetchone())["n"] == 2

    # Admin resets the password.
    admin_client = await _login_as("admin-rps@example.com")
    try:
        response = await admin_client.post(
            f"/api/v1/admin/students/{target['id']}/password",
            json={"new_password": "brand-new-password"},
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 204

    # Both sessions must be gone.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": target["id"]},
        )
        assert (await cur.fetchone())["n"] == 0


async def test_reset_password_target_not_found(
    client: AsyncClient, create_user
) -> None:
    """Nonexistent target → 404, no side effects."""
    await create_user(email="admin-rp-404@example.com", role="admin")

    admin_client = await _login_as("admin-rp-404@example.com")
    try:
        response = await admin_client.post(
            "/api/v1/admin/students/999999/password",
            json={"new_password": "some-password-123"},
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"


async def test_reset_password_as_student_forbidden(
    client: AsyncClient, create_user
) -> None:
    """
    A student cannot reset anyone's password.

    This is the highest-stakes 403 in the app. Password reset is
    effectively account takeover; if it were ever exposed to non-admins,
    anyone could take over any account.
    """
    await create_user(email="nobody@example.com")
    target = await create_user(email="victim@example.com")

    student_client = await _login_as("nobody@example.com")
    try:
        response = await student_client.post(
            f"/api/v1/admin/students/{target['id']}/password",
            json={"new_password": "hijack-attempt-123"},
        )
    finally:
        await student_client.aclose()

    assert response.status_code == 403


# ===========================================================================
# GET /admin/programs
# ===========================================================================


async def test_list_all_programs_as_admin_includes_archived(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Admin sees archived programs; students don't.

    This is the *contrast* test for the is_active asymmetry. The public
    `/programs` endpoint hides archived; the admin one shows everything.
    Same underlying data, different filter.
    """
    from app.db.queries import programs as programs_q

    admin = await create_user(email="admin-lap@example.com", role="admin")
    p_active = await create_program(code="AP1", created_by=admin["id"])
    p_archived = await create_program(code="AP2", created_by=admin["id"])
    await programs_q.deactivate_program(db_conn, program_id=p_archived["id"])

    admin_client = await _login_as("admin-lap@example.com")
    try:
        response = await admin_client.get("/api/v1/admin/programs")
    finally:
        await admin_client.aclose()

    assert response.status_code == 200
    body = response.json()
    codes = {p["code"] for p in body["items"]}
    assert "AP1" in codes
    assert "AP2" in codes  # archived, but still listed for admin


async def test_list_all_programs_as_student_forbidden(
    client: AsyncClient, create_user
) -> None:
    """Students can't see archived programs via the admin endpoint."""
    await create_user(email="student-lap@example.com")

    student_client = await _login_as("student-lap@example.com")
    try:
        response = await student_client.get("/api/v1/admin/programs")
    finally:
        await student_client.aclose()

    assert response.status_code == 403


# ===========================================================================
# POST /admin/programs
# ===========================================================================


async def test_create_program_as_admin(
    client: AsyncClient, create_user
) -> None:
    """
    Admin creates a program; the response reflects it.

    `created_by` is set from the admin's id, not from the request body.
    The test verifies this by comparing the response's `created_by` to
    the admin's actual id — a bug that trusted a body-supplied value
    would fail here.
    """
    admin = await create_user(email="admin-cp@example.com", role="admin")

    admin_client = await _login_as("admin-cp@example.com")
    try:
        response = await admin_client.post(
            "/api/v1/admin/programs",
            json={
                "name": "Biology",
                "code": "BIO",
                "description": "The study of life.",
            },
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Biology"
    assert body["code"] == "BIO"
    assert body["created_by"] == admin["id"]
    assert body["is_active"] is True


async def test_create_program_duplicate_code(
    client: AsyncClient, create_user, create_program
) -> None:
    """
    Duplicate code → 409 with a specific error code.

    The service layer distinguishes name-collision from code-collision
    by inspecting the constraint name. This test verifies the code
    path — if the translation broke, we'd get a 500 instead.
    """
    admin = await create_user(email="admin-cpd@example.com", role="admin")
    await create_program(name="Existing", code="EXIST", created_by=admin["id"])

    admin_client = await _login_as("admin-cpd@example.com")
    try:
        response = await admin_client.post(
            "/api/v1/admin/programs",
            json={"name": "Different Name", "code": "EXIST", "description": None},
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "program_code_taken"


async def test_create_program_duplicate_name(
    client: AsyncClient, create_user, create_program
) -> None:
    """Duplicate name → 409 with the name-specific error code."""
    admin = await create_user(email="admin-cpn@example.com", role="admin")
    await create_program(name="Existing Name", code="EXN1", created_by=admin["id"])

    admin_client = await _login_as("admin-cpn@example.com")
    try:
        response = await admin_client.post(
            "/api/v1/admin/programs",
            json={"name": "Existing Name", "code": "NEWCODE", "description": None},
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "program_name_taken"


async def test_create_program_as_student_forbidden(
    client: AsyncClient, create_user
) -> None:
    """Students can't create programs."""
    await create_user(email="student-cp@example.com")

    student_client = await _login_as("student-cp@example.com")
    try:
        response = await student_client.post(
            "/api/v1/admin/programs",
            json={"name": "Sneaky", "code": "SNK", "description": None},
        )
    finally:
        await student_client.aclose()

    assert response.status_code == 403


# ===========================================================================
# DELETE /admin/programs/{id}
# ===========================================================================


async def test_delete_program_as_admin(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Admin hard-deletes a program; the row is gone.

    Verifies the DB row's absence, not just the 204 — a buggy endpoint
    could return 204 without deleting.
    """
    admin = await create_user(email="admin-dp@example.com", role="admin")
    program = await create_program(code="DEL1", created_by=admin["id"])

    admin_client = await _login_as("admin-dp@example.com")
    try:
        response = await admin_client.delete(f"/api/v1/admin/programs/{program['id']}")
    finally:
        await admin_client.aclose()

    assert response.status_code == 204

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM programs WHERE id = %(id)s",
            {"id": program["id"]},
        )
        assert await cur.fetchone() is None


async def test_delete_program_cascades_enrollments(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Hard-deleting a program removes its enrollments.

    This is the FK's `ON DELETE CASCADE` at work. The test proves the
    cascade actually fires — a common assumption that's worth
    verifying, because if we ever changed the FK to `RESTRICT`, this
    test would fail and force a deliberate decision.
    """
    admin = await create_user(email="admin-dpc@example.com", role="admin")
    program = await create_program(code="CASC", created_by=admin["id"])
    student = await create_user(email="student-casc@example.com")

    from app.db.queries import enrollments as enrollments_q
    await enrollments_q.enroll(
        db_conn, student_id=student["id"], program_id=program["id"]
    )

    # Sanity: one enrollment exists.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM enrollments WHERE program_id = %(p)s",
            {"p": program["id"]},
        )
        assert (await cur.fetchone())["n"] == 1

    # Delete the program.
    admin_client = await _login_as("admin-dpc@example.com")
    try:
        response = await admin_client.delete(f"/api/v1/admin/programs/{program['id']}")
    finally:
        await admin_client.aclose()
    assert response.status_code == 204

    # Enrollment is gone too.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM enrollments WHERE program_id = %(p)s",
            {"p": program["id"]},
        )
        assert (await cur.fetchone())["n"] == 0


async def test_delete_program_not_found(
    client: AsyncClient, create_user
) -> None:
    """Nonexistent id → 404."""
    await create_user(email="admin-dp-404@example.com", role="admin")

    admin_client = await _login_as("admin-dp-404@example.com")
    try:
        response = await admin_client.delete("/api/v1/admin/programs/999999")
    finally:
        await admin_client.aclose()

    assert response.status_code == 404


async def test_delete_program_as_student_forbidden(
    client: AsyncClient, create_user, create_program
) -> None:
    """Students can't delete programs."""
    admin = await create_user(email="admin-dpf@example.com", role="admin")
    program = await create_program(code="NOFR", created_by=admin["id"])
    await create_user(email="student-dpf@example.com")

    student_client = await _login_as("student-dpf@example.com")
    try:
        response = await student_client.delete(f"/api/v1/admin/programs/{program['id']}")
    finally:
        await student_client.aclose()

    assert response.status_code == 403


# ===========================================================================
# POST /admin/programs/{id}/archive
# ===========================================================================


async def test_archive_program_as_admin(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Admin archives a program; the row survives with is_active=False.

    This is the soft-delete counterpart to the hard-delete test. After
    archiving, the program is still in the DB, just flagged.
    """
    admin = await create_user(email="admin-arc@example.com", role="admin")
    program = await create_program(code="ARCH", created_by=admin["id"])

    admin_client = await _login_as("admin-arc@example.com")
    try:
        response = await admin_client.post(
            f"/api/v1/admin/programs/{program['id']}/archive"
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 204

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT is_active FROM programs WHERE id = %(id)s",
            {"id": program["id"]},
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["is_active"] is False


async def test_archive_preserves_enrollments(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Archiving a program keeps its enrollments.

    The whole point of soft delete: history preserved. The contrast
    with the cascade-delete test is the lesson — same table, two
    different outcomes depending on which operation the admin chose.
    """
    admin = await create_user(email="admin-arcp@example.com", role="admin")
    program = await create_program(code="ARCP", created_by=admin["id"])
    student = await create_user(email="student-arcp@example.com")

    from app.db.queries import enrollments as enrollments_q
    await enrollments_q.enroll(
        db_conn, student_id=student["id"], program_id=program["id"]
    )

    admin_client = await _login_as("admin-arcp@example.com")
    try:
        response = await admin_client.post(
            f"/api/v1/admin/programs/{program['id']}/archive"
        )
    finally:
        await admin_client.aclose()
    assert response.status_code == 204

    # Enrollment survives.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM enrollments WHERE program_id = %(p)s",
            {"p": program["id"]},
        )
        assert (await cur.fetchone())["n"] == 1


async def test_archive_program_not_found(
    client: AsyncClient, create_user
) -> None:
    """Nonexistent id → 404."""
    await create_user(email="admin-arc-404@example.com", role="admin")

    admin_client = await _login_as("admin-arc-404@example.com")
    try:
        response = await admin_client.post("/api/v1/admin/programs/999999/archive")
    finally:
        await admin_client.aclose()

    assert response.status_code == 404


async def test_archive_program_as_student_forbidden(
    client: AsyncClient, create_user, create_program
) -> None:
    """Students can't archive programs."""
    admin = await create_user(email="admin-arcf@example.com", role="admin")
    program = await create_program(code="ARCF", created_by=admin["id"])
    await create_user(email="student-arcf@example.com")

    student_client = await _login_as("student-arcf@example.com")
    try:
        response = await student_client.post(
            f"/api/v1/admin/programs/{program['id']}/archive"
        )
    finally:
        await student_client.aclose()

    assert response.status_code == 403


# ===========================================================================
# GET /admin/programs/{id}/students
# ===========================================================================


async def test_list_program_students_as_admin(
    client: AsyncClient, create_user, create_program, db_conn: AsyncConnection
) -> None:
    """
    Admin lists the students in a program.

    Two students enrolled, one not. The response should include only
    the two enrolled, with the shape `{id, full_name, email, enrolled_at}`.
    """
    admin = await create_user(email="admin-lps@example.com", role="admin")
    program = await create_program(code="LPS1", created_by=admin["id"])

    alice = await create_user(email="alice-lps@example.com", full_name="Alice")
    bob = await create_user(email="bob-lps@example.com", full_name="Bob")
    await create_user(email="carol-lps@example.com", full_name="Carol")  # not enrolled

    from app.db.queries import enrollments as enrollments_q
    await enrollments_q.enroll(db_conn, student_id=alice["id"], program_id=program["id"])
    await enrollments_q.enroll(db_conn, student_id=bob["id"], program_id=program["id"])

    admin_client = await _login_as("admin-lps@example.com")
    try:
        response = await admin_client.get(
            f"/api/v1/admin/programs/{program['id']}/students"
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2

    emails = {s["email"] for s in body}
    assert emails == {"alice-lps@example.com", "bob-lps@example.com"}
    assert "carol-lps@example.com" not in emails

    # Check the shape of each item.
    for item in body:
        assert {"id", "full_name", "email", "enrolled_at"} <= set(item.keys())
        # The hash must not leak into student listings.
        assert "hashed_password" not in item


async def test_list_program_students_empty(
    client: AsyncClient, create_user, create_program
) -> None:
    """A program with no enrollments returns an empty list, not 404."""
    admin = await create_user(email="admin-lps-empty@example.com", role="admin")
    program = await create_program(code="EMPT", created_by=admin["id"])

    admin_client = await _login_as("admin-lps-empty@example.com")
    try:
        response = await admin_client.get(
            f"/api/v1/admin/programs/{program['id']}/students"
        )
    finally:
        await admin_client.aclose()

    assert response.status_code == 200
    assert response.json() == []


async def test_list_program_students_program_not_found(
    client: AsyncClient, create_user
) -> None:
    """
    Nonexistent program → 404.

    Contrast with the empty case: an empty program returns `[]`, a
    nonexistent one returns 404. The router checks the program exists
    first, precisely so these two cases are distinguishable.
    """
    await create_user(email="admin-lps-404@example.com", role="admin")

    admin_client = await _login_as("admin-lps-404@example.com")
    try:
        response = await admin_client.get("/api/v1/admin/programs/999999/students")
    finally:
        await admin_client.aclose()

    assert response.status_code == 404


async def test_list_program_students_as_student_forbidden(
    client: AsyncClient, create_user, create_program
) -> None:
    """Students can't see the roster of a program they didn't ask about."""
    admin = await create_user(email="admin-lpsf@example.com", role="admin")
    program = await create_program(code="LPSF", created_by=admin["id"])
    await create_user(email="student-lpsf@example.com")

    student_client = await _login_as("student-lpsf@example.com")
    try:
        response = await student_client.get(
            f"/api/v1/admin/programs/{program['id']}/students"
        )
    finally:
        await student_client.aclose()

    assert response.status_code == 403