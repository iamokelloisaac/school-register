"""
app/api/v1/routers/me.py
========================

Endpoints scoped to the currently authenticated user:

    GET    /me                current user's profile
    PATCH  /me                update own profile fields
    POST   /me/password       change own password
    GET    /me/programs       programs the current user is enrolled in

Everything here operates on `user["id"]` — the authenticated user. There
is no path parameter for "which user" because there is no other user these
endpoints can touch. That's the whole point of the `/me/*` namespace: it's
a hard boundary. A bug in authorization can't leak across users because
there's nothing to leak *to* — the target is always the caller.

## Why `/me/programs` lives here, not under `/programs`

Because of the route-collision problem mentioned in `programs.py`: a
`GET /programs/mine` would be shadowed by `GET /programs/{program_id}`.
Putting "my enrollments" under `/me/programs` sidesteps the issue entirely,
and it's semantically cleaner: the resource is "my enrollments," not "a
program."

## Password change requires the current password

A user whose session was stolen (say, from an unlocked laptop) should not
be able to lock the real user out by changing the password. Requiring the
current password means the thief needs the password too, not just the
session token.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.core import security
from app.core.deps import CurrentSession, CurrentUser, DbConn
from app.core.errors import Forbidden, Unauthorized
from app.db.queries import sessions as sessions_q
from app.db.queries import users as users_q
from app.schemas.common import ErrorResponse
from app.schemas.enrollment import EnrolledProgram
from app.schemas.user import UserRead, UserUpdate
from app.services import enrollment_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])


# ---------------------------------------------------------------------------
# Inline schema for password change
# ---------------------------------------------------------------------------
#
# Defined here, next to its only consumer, rather than in `schemas/user.py`.
# It's small and specific to this route. If a second consumer ever appears,
# or if the shape grows, it moves to the schemas package.


class PasswordChangeRequest(BaseModel):
    """
    Body of `POST /api/v1/me/password`.

    Both fields are plain strings. `current_password` is required so that a
    stolen session can't be used to lock the real user out. `new_password`
    has the same length rules as registration — 8..128 — for the same
    reasons (see `RegisterRequest`).

    We don't require `new_password != current_password` at the schema level.
    That's a business rule, not a shape rule, and we're skipping it for now.
    """

    current_password: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The user's current password, for confirmation.",
    )
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="The new password.",
    )


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=UserRead,
    summary="Get the current user's profile",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
    },
)
async def get_me(user: CurrentUser) -> UserRead:
    """
    Return the authenticated user's own profile.

    The user object is already loaded by the `CurrentUser` dependency
    (which runs the full token → session → user chain). This route just
    serializes it.

    No `db: DbConn` parameter — the user dict is complete, and adding an
    unused dependency would just be noise. FastAPI resolves dependencies
    lazily, so the connection isn't fetched for this route.
    """
    return UserRead.model_validate(user)


# ---------------------------------------------------------------------------
# PATCH /me
# ---------------------------------------------------------------------------


@router.patch(
    "",
    response_model=UserRead,
    summary="Update the current user's profile",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
        422: {"model": ErrorResponse, "description": "Validation error."},
    },
)
async def update_me(
    payload: UserUpdate,
    user: CurrentUser,
    db: DbConn,
) -> UserRead:
    """
    Update fields on the authenticated user's own profile.

    ## PATCH semantics

    Only the fields *present* in the request body are updated. A client
    sending `{"full_name": "New Name"}` changes the name and leaves
    everything else untouched. A client sending `{}` changes nothing and
    gets back the current state.

    This is why `UserUpdate` has every field optional — Pydantic's "present
    vs absent" distinction is what makes PATCH work. `exclude_unset=True`
    on the model dump produces only the keys the client actually sent.

    ## Which fields are updatable

    `UserUpdate` currently allows only `full_name`. Everything else a user
    might reasonably want to change has its own endpoint with its own
    verification flow.

    ## What if the body is empty

    We still issue no UPDATE at all. `updated_at` does not move — a PATCH
    that changes nothing shouldn't look like a modification in an audit
    sense.

    ## Why we re-fetch from the DB

    The client sent *some* fields, not all. We can't build a `UserRead`
    from the request payload alone. Instead, we fetch the updated row fresh
    from the DB after the UPDATE. The DB is the source of truth for the
    resulting resource.
    """
    updates = payload.model_dump(exclude_unset=True)

    if updates:
        # The column allowlist is hardcoded here. If `UserUpdate` ever
        # grows a field, this set must grow too — a feature, not a bug:
        # it forces a conscious decision about whether the new field is
        # user-editable.
        allowed = {"full_name"}
        assignments = []
        params: dict[str, object] = {"id": user["id"]}

        for column, value in updates.items():
            if column not in allowed:
                # Should be impossible — UserUpdate only declares allowed
                # fields — but defense in depth.
                raise Forbidden(
                    code="field_not_updatable",
                    message=f"Field '{column}' cannot be updated.",
                )
            assignments.append(f"{column} = %({column})s")
            params[column] = value

        # `assignments` are column names from the allowlist above, not
        # from user input. The user-supplied *values* travel through
        # psycopg's parameterized placeholders. This is why the f-string
        # is safe.
        sql = f"""
            UPDATE users
            SET {", ".join(assignments)},
                updated_at = NOW()
            WHERE id = %(id)s
        """

        async with db.cursor() as cur:
            await cur.execute(sql, params)

    fresh = await users_q.get_user_by_id(db, user["id"])

    if fresh is None:
        # The user was deleted between their request arriving and this
        # point. Narrow race; treat as "unauthenticated."
        raise Unauthorized(
            code="invalid_token",
            message="Invalid or expired session.",
        )

    return UserRead.model_validate(fresh)


# ---------------------------------------------------------------------------
# POST /me/password
# ---------------------------------------------------------------------------


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the current user's password",
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Not authenticated, or current password is wrong.",
        },
        422: {"model": ErrorResponse, "description": "Validation error."},
    },
)
async def change_password(
    payload: PasswordChangeRequest,
    user: CurrentUser,
    session: CurrentSession,
    db: DbConn,
) -> None:
    """
    Change the authenticated user's password.

    Requires the current password, for the reason stated in the module
    docstring: a stolen session shouldn't be enough to lock the real user
    out.

    ## What happens to existing sessions

    On success, we revoke *every session except the current one*. The
    current session survives — logging the user out of the tab they just
    changed their password in would be hostile. Other sessions (other
    devices, other browsers) die, on the assumption that if you changed
    your password, you probably want other logins invalidated.

    ## Why we need both `CurrentUser` and `CurrentSession`

    `CurrentUser` gives us the user id for the UPDATE on the password hash.
    `CurrentSession` gives us the current session's id, which we need to
    *exclude* when revoking others. FastAPI caches dependency results per
    request, so both dependencies resolve to the same objects the auth
    chain already loaded — no extra DB hit for the session.

    ## Error cases

      - 401 — no session, or current password is wrong. Both are "your
        credentials didn't check out."
      - 422 — new password fails length bounds.

    ## Why the current password failure is 401 and not 403

    Semantically this is "your credentials are invalid" — the same class of
    failure as a bad login. A 403 would suggest "you're authenticated but
    not authorized," which is a different problem.
    """
    # Step 1: load the user with their hash. `user` (from CurrentUser)
    # doesn't include the hash — the query deliberately excludes it — so we
    # re-fetch by email, which is the query path that includes it.
    user_with_hash = await users_q.get_user_by_email(db, user["email"])

    if user_with_hash is None:
        # Should be impossible — the user was authenticated a moment ago.
        # If it happens, something raced (user deleted mid-request); treat
        # as unauthenticated.
        raise Unauthorized(
            code="invalid_token",
            message="Invalid or expired session.",
        )

    if not security.verify_password(
        payload.current_password, user_with_hash["hashed_password"]
    ):
        logger.info(
            "password change rejected: bad current password, user=%s",
            user["id"],
        )
        raise Unauthorized(
            code="invalid_credentials",
            message="Current password is incorrect.",
        )

    # Step 2: hash and store the new password.
    new_hash = security.hash_password(payload.new_password)
    await users_q.update_user_password(
        db,
        user_id=user["id"],
        hashed_password=new_hash,
    )

    # Step 3: revoke every session *except* the current one.
    await sessions_q.delete_other_sessions_for_user(
        db,
        user_id=user["id"],
        keep_session_id=session["id"],
    )

    logger.info("password changed: user=%s, other sessions revoked", user["id"])


# ---------------------------------------------------------------------------
# GET /me/programs
# ---------------------------------------------------------------------------


@router.get(
    "/programs",
    response_model=list[EnrolledProgram],
    summary="List programs the current user is enrolled in",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated."},
    },
)
async def list_my_programs(
    user: CurrentUser,
    db: DbConn,
) -> list[EnrolledProgram]:
    """
    Return every program the authenticated user is enrolled in.

    Deliberately not paginated — a student's enrollments are bounded by
    the number of programs the school offers, which is small. See
    `enrollments_q.list_enrolled_programs` for the fuller reasoning.

    Deliberately not filtered by `is_active` — a student's enrollment
    history is theirs. If a program they enrolled in gets archived, it
    stays in their list. The *catalog* endpoint (`GET /programs`) filters;
    this one doesn't. Two lists, two rules, both correct.

    The response is a bare `list[EnrolledProgram]`, not a `Page` — no
    pagination envelope because there's no pagination. That's a deliberate
    inconsistency with the catalog endpoint, justified by the data: this
    list is small and bounded; the catalog is potentially large.
    """
    rows = await enrollment_service.list_my_programs(db, student_id=user["id"])
    return [EnrolledProgram.model_validate(row) for row in rows]