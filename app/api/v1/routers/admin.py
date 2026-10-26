"""
app/api/v1/routers/admin.py
===========================

Admin-only operations:

    GET    /admin/students                     list all users
    GET    /admin/students/{user_id}           one user's details
    POST   /admin/students                     register a student on their behalf
    POST   /admin/students/{user_id}/password  reset any user's password
    GET    /admin/programs                     list all programs (incl. archived)
    POST   /admin/programs                     create a program
    DELETE /admin/programs/{program_id}        hard-delete a program
    POST   /admin/programs/{program_id}/archive    soft-delete (archive)
    GET    /admin/programs/{program_id}/students   list students in a program

Every route on this router carries `AdminUser` in its signature. That
dependency (`require_admin` from `core/deps.py`) runs the full auth chain
*and* checks the role. A non-admin calling any of these gets a 403 from
the dependency before the handler body runs.

## The namespace boundary

Note how the URL prefix mirrors the permission boundary. Everything under
`/admin/*` requires admin; everything outside it does not. This makes it
impossible to accidentally expose an admin operation under a
student-accessible prefix — the prefix itself is the declaration of
intent. When scanning the codebase, "is this admin-only?" is answered by
"does the path start with `/admin`?"

## Why password reset is admin-only

An admin resetting a user's password is a powerful operation — it's
essentially account takeover. We accept this as a necessary
administrative capability. What we *don't* do is let the reset reveal the
new password back to the admin after the fact, or log it anywhere. The
admin sets it; the user changes it on next login if they want. If we
later want to force a change, we'd add a `must_change_password` flag on
users — not now.

## On the register-on-behalf endpoint

This reuses `auth_service.register` with the same arguments a self-
signup would use. The admin supplies email, password, full name, and
age, and the student gets an account. The student's role is still
hardcoded to `student` — even the admin can't create another admin
through this endpoint. Promoting a user to admin is a separate operation
that we haven't built yet, and that's deliberate: the fewer paths to
"become admin," the better.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.core import security
from app.core.deps import AdminUser, DbConn
from app.core.errors import NotFound
from app.db.queries import enrollments as enrollments_q
from app.db.queries import programs as programs_q
from app.db.queries import sessions as sessions_q
from app.db.queries import users as users_q
from app.schemas.auth import RegisterRequest
from app.schemas.common import ErrorResponse, Page, Pagination
from app.schemas.program import ProgramCreate, ProgramRead
from app.schemas.user import UserRead
from app.services import auth_service, program_service
from app.utils.pagination import build_page

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Inline schema for admin password reset
# ---------------------------------------------------------------------------
#
# Same reasoning as PasswordChangeRequest in me.py: small, single-consumer,
# lives next to its only use. If it grows or gains a second consumer, it
# moves to schemas/user.py.


class AdminPasswordResetRequest(BaseModel):
    """
    Body of `POST /api/v1/admin/students/{user_id}/password`.

    Only a new password. There's no `current_password` field — the whole
    point of an admin reset is that the user forgot theirs.
    """

    new_password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="The new password to set for the target user.",
    )


# ===========================================================================
# Students
# ===========================================================================


@router.get(
    "/students",
    response_model=Page[UserRead],
    summary="List all users",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
    },
)
async def list_students(
    db: DbConn,
    _admin: AdminUser,
    pagination: Annotated[Pagination, Depends()],
) -> Page[UserRead]:
    """
    Return a page of all users.

    Despite the endpoint being called "students," it returns *all* users —
    students and admins alike. That's intentional: the admin looking at
    this list wants to see everyone who has an account, including other
    admins. Filtering to only role='student' would hide the admin's own
    row from their view, which is confusing. If we later want a filter,
    it can be a query parameter (`?role=student`).

    Paginated using the same envelope as every other list. `limit` and
    `offset` are parsed by the `Pagination` dependency, bounded by the
    schema, and echoed back in the response.
    """
    rows, total = await users_q.list_users(
        db,
        limit=pagination.limit,
        offset=pagination.offset,
    )

    return build_page(
        items=[UserRead.model_validate(row) for row in rows],
        total=total,
        pagination=pagination,
    )


@router.get(
    "/students/{user_id}",
    response_model=UserRead,
    summary="Get a user by id",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
        404: {"model": ErrorResponse, "description": "User not found."},
    },
)
async def get_student(
    user_id: int,
    db: DbConn,
    _admin: AdminUser,
) -> UserRead:
    """
    Return a single user by id.

    Just a lookup — no admin-specific logic beyond the `AdminUser`
    dependency. The shape returned is the same `UserRead` used everywhere
    else, so a client's user-parsing code doesn't branch on
    "student-facing vs admin-facing."

    404 for a nonexistent id. We don't distinguish "never existed" from
    "was deleted" — same response either way.
    """
    row = await users_q.get_user_by_id(db, user_id)

    if row is None:
        raise NotFound(
            code="user_not_found",
            message="No such user.",
        )

    return UserRead.model_validate(row)


@router.post(
    "/students",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new student",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
        409: {"model": ErrorResponse, "description": "Email already registered."},
        422: {"model": ErrorResponse, "description": "Validation error."},
    },
)
async def create_student(
    payload: RegisterRequest,
    db: DbConn,
    admin: AdminUser,
) -> UserRead:
    """
    Create a new student account on their behalf.

    The admin supplies the same fields a student would supply during
    self-registration. The account is created with `role='student'` —
    the service hardcodes it (see `auth_service.register`), so even an
    admin can't use this endpoint to create another admin. Promotion is
    a separate operation we haven't built.

    ## Reuse of `auth_service.register`

    Same service function as `POST /auth/register`. The only difference
    is who's calling. This is deliberate: the "create a user account"
    operation is a single concept with a single implementation. If the
    admin path had its own registration logic, the two would drift
    eventually.

    ## The admin variable

    `admin: AdminUser` is declared because the dependency `require_admin`
    returns the user object and we want the auth check to run. We use the
    returned value only for the audit log line below.
    """
    user = await auth_service.register(
        db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        age=payload.age,
    )

    logger.info(
        "admin registered user: admin=%s new_user=%s",
        admin["id"],
        user["id"],
    )

    return UserRead.model_validate(user)


@router.post(
    "/students/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset a user's password",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
        404: {"model": ErrorResponse, "description": "User not found."},
        422: {"model": ErrorResponse, "description": "Validation error."},
    },
)
async def reset_password(
    user_id: int,
    payload: AdminPasswordResetRequest,
    db: DbConn,
    admin: AdminUser,
) -> None:
    """
    Set a user's password to an admin-supplied value.

    ## What this does NOT do

    - It does not reveal the old password (we don't know it — hashes are
      one-way).
    - It does not reveal the new password after setting it. The admin
      already knows it; they typed it.
    - It does not force the user to change on next login. If we want
      that, we add a `must_change_password` flag later.

    ## Session revocation

    We revoke every session for the target user, including any they had
    open. This is important: if the user was actively logged in, they
    should be forced to log in again with the new password. Leaving
    their old sessions alive would mean an admin's password reset
    doesn't actually take effect until those sessions expire naturally
    — a serious security hole.

    Unlike self-service password change (which preserves the current
    session), admin reset kills all sessions. The admin isn't operating
    within one of the target user's sessions, so there's nothing to
    preserve.

    ## Why 204

    Nothing to return. The operation succeeded; the password is set;
    sessions are revoked. A response body would be redundant.
    """
    # Step 1: confirm the target user exists. We don't actually need
    # their row for anything else — get_user_by_id is the cheapest way
    # to check existence, and it lets us 404 before touching anything.
    target = await users_q.get_user_by_id(db, user_id)

    if target is None:
        raise NotFound(
            code="user_not_found",
            message="No such user.",
        )

    # Step 2: hash and store the new password.
    new_hash = security.hash_password(payload.new_password)
    await users_q.update_user_password(
        db,
        user_id=user_id,
        hashed_password=new_hash,
    )

    # Step 3: revoke every session belonging to the target user. See the
    # docstring — this is not optional.
    await sessions_q.delete_all_sessions_for_user(db, user_id=user_id)

    logger.info(
        "admin reset user password: admin=%s target=%s",
        admin["id"],
        user_id,
    )


# ===========================================================================
# Programs
# ===========================================================================


@router.get(
    "/programs",
    response_model=Page[ProgramRead],
    summary="List all programs (including archived)",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
    },
)
async def list_all_programs(
    db: DbConn,
    _admin: AdminUser,
    pagination: Annotated[Pagination, Depends()],
) -> Page[ProgramRead]:
    """
    Return a page of *all* programs, active and archived.

    This is the admin counterpart to `GET /programs`. The public catalog
    filters to `is_active = TRUE`; this one doesn't. Admins need to see
    archived programs so they can reactivate them, audit history, or
    confirm a delete.

    Same pagination envelope as everywhere else.
    """
    rows, total = await programs_q.list_programs(
        db,
        limit=pagination.limit,
        offset=pagination.offset,
        active_only=False,  # the one difference from the public list
    )

    return build_page(
        items=[ProgramRead.model_validate(row) for row in rows],
        total=total,
        pagination=pagination,
    )


@router.post(
    "/programs",
    response_model=ProgramRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a program",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
        409: {"model": ErrorResponse, "description": "Name or code already in use."},
        422: {"model": ErrorResponse, "description": "Validation error."},
    },
)
async def create_program(
    payload: ProgramCreate,
    db: DbConn,
    admin: AdminUser,
) -> ProgramRead:
    """
    Create a new program in the catalog.

    The `created_by` field is set from the authenticated admin's id —
    there's no way for the client to spoof "I'm creating this as someone
    else."

    Unique-constraint errors are translated by the service layer into
    `Conflict` (409) with a code that distinguishes name-collision from
    code-collision. See `program_service.create_program` for the
    constraint-name matching logic.
    """
    program = await program_service.create_program(
        db,
        name=payload.name,
        code=payload.code,
        description=payload.description,
        created_by=admin["id"],
    )

    logger.info(
        "admin created program: admin=%s program=%s",
        admin["id"],
        program["id"],
    )

    return ProgramRead.model_validate(program)


@router.delete(
    "/programs/{program_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete a program",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
        404: {"model": ErrorResponse, "description": "Program not found."},
    },
)
async def delete_program(
    program_id: int,
    db: DbConn,
    admin: AdminUser,
) -> None:
    """
    Permanently delete a program.

    ## Cascade warning

    `enrollments.program_id` has `ON DELETE CASCADE`, so deleting a
    program also deletes every enrollment pointing at it. A student who
    was enrolled in this program loses that enrollment silently.

    This is a real consequence, and it's why we also expose an
    archive/soft-delete endpoint. Admins who want to remove a program
    from the catalog *without* losing enrollment history should archive
    instead.

    ## 404 for missing

    If the program doesn't exist, 404. This is stricter than
    idempotent-delete semantics, and it's a choice: the admin sent a
    delete for a specific id, and it wasn't there. 404 says "you
    targeted something that doesn't exist," which is worth knowing.
    """
    deleted = await programs_q.delete_program(db, program_id=program_id)

    if not deleted:
        raise NotFound(
            code="program_not_found",
            message="No such program.",
        )

    logger.info(
        "admin deleted program: admin=%s program=%s",
        admin["id"],
        program_id,
    )


@router.post(
    "/programs/{program_id}/archive",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a program (soft delete)",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
        404: {"model": ErrorResponse, "description": "Program not found."},
    },
)
async def archive_program(
    program_id: int,
    db: DbConn,
    admin: AdminUser,
) -> None:
    """
    Archive a program by flipping `is_active` to FALSE.

    Enrollments survive — this is the "safe delete" that keeps history
    intact. After archiving:

      - The program no longer appears in `GET /programs` (the public
        catalog).
      - New enrollments are rejected (see `enrollment_service.enroll`).
      - Existing enrollments persist and still appear in students'
        `/me/programs` lists.
      - Admins still see it in `GET /admin/programs`.

    ## Why POST, not DELETE

    Because this isn't a delete — it's a state change on an existing
    resource. `DELETE /programs/{id}` means "remove this"; `POST
    /programs/{id}/archive` means "transition this to archived state."
    The verb matches what actually happens.

    ## Reactivation

    We haven't built unarchive yet. When we do, it's the mirror:
    `POST /programs/{id}/unarchive` (or a PATCH on `is_active`). The
    DB column is boolean, so flipping back is trivial; the endpoint is
    the missing piece.
    """
    archived = await programs_q.deactivate_program(db, program_id=program_id)

    if not archived:
        raise NotFound(
            code="program_not_found",
            message="No such program.",
        )

    logger.info(
        "admin archived program: admin=%s program=%s",
        admin["id"],
        program_id,
    )


@router.get(
    "/programs/{program_id}/students",
    response_model=list[dict],
    summary="List students enrolled in a program",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        403: {"model": ErrorResponse, "description": "Admin required."},
        404: {"model": ErrorResponse, "description": "Program not found."},
    },
)
async def list_program_students(
    program_id: int,
    db: DbConn,
    _admin: AdminUser,
) -> list[dict]:
    """
    Return every student enrolled in a given program.

    ## Response shape is a bare dict list

    We don't have a Pydantic schema for this response yet. The query
    returns `{id, full_name, email, enrolled_at}`, which is a small,
    well-defined shape. A schema would be cleaner for OpenAPI docs, but
    adding one now would be premature — this endpoint is a convenience,
    and if it gets used, we promote it to a proper schema
    (`EnrolledStudent`) in `schemas/enrollment.py`.

    Returning `dict` as the type parameter means FastAPI serializes the
    dicts as-is without validation. That's a tradeoff: less safe, but no
    schema to maintain for a shape we might change.

    ## Not paginated

    A single program's enrollment list is small (bounded by the number
    of students in the school). Same reasoning as `/me/programs`.

    ## 404 if program doesn't exist

    We check the program exists first, because "the program has no
    students" and "the program doesn't exist" are different answers,
    and the admin deserves to know which one they got. An empty list
    from a nonexistent program would be misleading.
    """
    program = await programs_q.get_program_by_id(db, program_id)

    if program is None:
        raise NotFound(
            code="program_not_found",
            message="No such program.",
        )

    return await enrollments_q.list_students_in_program(db, program_id=program_id)