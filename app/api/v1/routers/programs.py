"""
app/api/v1/routers/programs.py
==============================

The student-facing program endpoints:

    GET    /programs                list active programs (paginated)
    GET    /programs/{id}           program detail
    POST   /programs/{id}/enroll    enroll the current user
    DELETE /programs/{id}/enroll    leave the program

Plus one endpoint that surfaces the current user's own enrollments in
the context of the *program catalog*:

    GET    /programs/mine           list programs I'm enrolled in

Wait — `/programs/mine` would collide with `/programs/{id}` because
FastAPI matches routes in declaration order and `mine` would be parsed
as an `id`. That's a classic gotcha. The clean fix is to put "my
programs" under a different prefix. So the fifth endpoint lives under
`/me/programs` instead, in `me.py`. This router stays focused on the
program catalog itself.

## Access control

Everything here is available to any authenticated user. There are no
admin-only operations on this router — admin operations live under
`/admin/programs`, which is a separate router with a separate prefix
and a `require_admin` dependency on every route.

This is a deliberate split:

  - **`/programs/*`** — student-visible catalog. Read is open to any
    authenticated user; enroll/leave are scoped to the *current* user.
    There's no way to enroll someone else.

  - **`/admin/programs/*`** — admin operations. Create, delete, list
    archived, manage students. Same underlying data, different
    permissions, different URL namespace.

The URL namespace mirrors the permission boundary. A client can look at
any URL and know what class of user it's for.

## On the enroll/leave verbs

We use `POST .../enroll` and `DELETE .../enroll` — a verb as a
sub-resource — rather than `POST /enrollments` with a body. Both are
valid REST designs, but this one reads better for a "toggle-like"
operation:

    POST   /programs/5/enroll   → "enroll me in program 5"
    DELETE /programs/5/enroll   → "un-enroll me from program 5"

The alternative — modeling enrollments as their own resource — would be:

    POST   /enrollments           {"program_id": 5}
    DELETE /enrollments/{id}

This is arguably more "RESTful" but it forces clients to know the
enrollment's identity (which is a pair `(student_id, program_id)`, so
there's no single id anyway). The sub-resource form is what most APIs
converge on for this shape of relationship. Consistency matters more
than ideological purity.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.deps import CurrentUser, DbConn
from app.core.errors import NotFound
from app.schemas.common import ErrorResponse, Page, Pagination
from app.schemas.enrollment import EnrollmentRead
from app.schemas.program import ProgramRead
from app.services import enrollment_service
from app.db.queries import programs as programs_q
from app.utils.pagination import build_page

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/programs", tags=["programs"])


# ---------------------------------------------------------------------------
# GET /programs — list the catalog
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=Page[ProgramRead],
    summary="List active programs",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
    },
)
async def list_programs(
    db: DbConn,
    _user: CurrentUser,
    pagination: Annotated[Pagination, Depends()],
) -> Page[ProgramRead]:
    """
    Return a page of active programs.

    Public-catalog semantics: only programs where `is_active = TRUE` are
    listed. Archived programs don't disappear from the database, but
    they do stop appearing here — that's what "archive" means in
    practice.

    Requires authentication, though the endpoint itself is
    read-oriented. Should anonymous users be able to browse the catalog?
    Argument for: yes, it's marketing-ish, it helps prospective students.
    Argument against: no, this is a school register, not a public
    website; only enrolled students should see what's on offer.

    We're going with "authentication required" — the lean scope we
    decided. If we later want a public catalog, the `_user: CurrentUser`
    dependency comes off this one route, and nothing else changes.

    ## Why `_user` with an underscore

    The dependency is declared but the value isn't used in the body. The
    underscore signals "this exists for its side effect" — FastAPI will
    still enforce authentication, but we're not going to reference the
    user object. This is a Python convention and it prevents linters
    from flagging the parameter as unused.
    """
    rows, total = await programs_q.list_programs(
        db,
        limit=pagination.limit,
        offset=pagination.offset,
        active_only=True,
    )

    return build_page(
        items=[ProgramRead.model_validate(row) for row in rows],
        total=total,
        pagination=pagination,
    )


# ---------------------------------------------------------------------------
# GET /programs/{program_id} — catalog detail
# ---------------------------------------------------------------------------


@router.get(
    "/{program_id}",
    response_model=ProgramRead,
    summary="Get a program by id",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        404: {"model": ErrorResponse, "description": "Program not found."},
    },
)
async def get_program(
    program_id: int,
    db: DbConn,
    _user: CurrentUser,
) -> ProgramRead:
    """
    Return a single program by id.

    Archived programs are still returned here — if you know the id, you
    can see the program. That's deliberate: a student who was enrolled
    in a program that just got archived should still be able to view
    its details, and an admin clicking through from a list should see
    what they archived. The *catalog listing* filters by active; the
    detail view does not.

    Returns 404 if no program with that id exists, whether because it
    never existed or because it was hard-deleted. From the client's
    perspective these are the same.

    ## Why not check `is_active` here

    Because it would break the detail view for archived programs, and
    we already decided archived programs are viewable. The rule "only
    active programs are enrollable" lives in the enrollment service,
    where it belongs. Read access and write access have different
    rules.
    """
    row = await programs_q.get_program_by_id(db, program_id)

    if row is None:
        raise NotFound(
            code="program_not_found",
            message="No such program.",
        )

    return ProgramRead.model_validate(row)


# ---------------------------------------------------------------------------
# POST /programs/{program_id}/enroll — join a program
# ---------------------------------------------------------------------------


@router.post(
    "/{program_id}/enroll",
    response_model=EnrollmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Enroll the current user in a program",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        404: {"model": ErrorResponse, "description": "Program not found."},
        409: {
            "model": ErrorResponse,
            "description": "Already enrolled, or program is archived.",
        },
    },
)
async def enroll_in_program(
    program_id: int,
    db: DbConn,
    user: CurrentUser,
) -> EnrollmentRead:
    """
    Enroll the *current* user in the given program.

    Note the user comes from the session — `user["id"]` — not from the
    request body. There is no way to enroll someone else, even for an
    admin. If we later want admins to enroll students, that's a
    distinct admin endpoint (`POST /admin/programs/{id}/students/{sid}`
    or similar) with its own shape and its own authorization check.
    Mixing the two into one endpoint would mean the "who is this
    enrollment for?" question has two answers depending on the caller's
    role, which is the kind of ambiguity that breeds bugs.

    ## Status code: 201 Created

    A new enrollment row was created. That's a 201, not a 200. The
    response body is the created enrollment (student_id, program_id,
    enrolled_at) — small, but enough for the client to update its
    state without a refetch.

    ## Error cases

      - 404 — the program id doesn't exist.
      - 409 — you're already enrolled, or the program is archived.
      - 401 — no valid session.

    All three come from the service layer via domain exceptions. This
    route raises nothing itself.
    """
    enrollment = await enrollment_service.enroll(
        db,
        student_id=user["id"],
        program_id=program_id,
    )

    return EnrollmentRead.model_validate(enrollment)


# ---------------------------------------------------------------------------
# DELETE /programs/{program_id}/enroll — leave a program
# ---------------------------------------------------------------------------


@router.delete(
    "/{program_id}/enroll",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove the current user from a program",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        404: {
            "model": ErrorResponse,
            "description": "Not enrolled in that program.",
        },
    },
)
async def leave_program(
    program_id: int,
    db: DbConn,
    user: CurrentUser,
) -> None:
    """
    Remove the *current* user from the given program.

    Same identity rule as enroll: the student is the authenticated user,
    always. No way to remove someone else.

    ## Status code: 204 No Content

    The enrollment was deleted and there's nothing meaningful to return.
    Returning `{"ok": true}` would be harmless but redundant — the 204
    already says it worked. Some clients prefer a body; if we ever need
    to return metadata (like "you were enrolled for 42 days"), we'd
    switch to a 200 with a body. Until then, 204 is the correct choice.

    ## On the 404

    Leaving a program you're not enrolled in is a 404 (not a silent
    204). This is stricter than fully-idempotent semantics. The
    reasoning is in `enrollment_service.leave`: if the client sent a
    delete for something they believe exists, and it doesn't, that's a
    mistake worth surfacing. If we later decide leave should be
    idempotent, both this docstring and the service docstring need to
    be updated — and it's a one-line change in the service.
    """
    await enrollment_service.leave(
        db,
        student_id=user["id"],
        program_id=program_id,
    )